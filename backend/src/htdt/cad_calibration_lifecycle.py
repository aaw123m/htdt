"""Measurement-instrument calibration lifecycle authority (#611,
REV57-METRO).

Storing a correction file is not enough: HTDT must know whether the
physical instrument — and the calibrator used to check it — was
demonstrably fit for use *at the measurement timestamp*, how that
status was established, and whether later evidence puts earlier
results in review. This module provides the sealed lifecycle
authority:

- :class:`CadInstrumentInstance` — the physical instrument instance
  (serial-level, never model-level): category, revisions, sensor/
  capsule identity, accessories, service state.
- :class:`CadCalibrationEvent` — one calibration/periodic-test record:
  provider, certificate, method/standard (with edition state),
  reference standards, correction data, measurement uncertainty and
  declared traceability class.
- :class:`CadCalibrationIntervalPolicy` — the recalibration-interval
  basis (ILAC G24-style programme/policy evidence, never one universal
  hard-coded period).
- :class:`CadInstrumentVerificationCheck` — bounded pre/post-use and
  periodic field checks, kept strictly distinct from laboratory
  calibration.
- :class:`CadInstrumentServiceEvent` — damage/service/modification
  events that invalidate prior calibration applicability before any
  nominal due date.
- :class:`CadInstrumentFitnessAssessment` — sealed fitness verdict at
  an exact measurement timestamp.
- :class:`CadOutOfToleranceReview` — non-destructive impact review
  marking measurements made since the last known-valid event.

Metrology basis
---------------
- ISO/IEC 17025:2017 (Edition 3, reviewed/confirmed 2023): competence
  and metrological-traceability reference profile — used as an
  architecture reference, not an accreditation claim.
- ILAC G24:2022: recalibration intervals are determined/reviewed as
  part of a calibration programme — hence per-instrument policy, never
  a universal ``12-month`` rule.
- IEC 61672-3:2013: periodic tests for class 1/2 sound level meters —
  distinct from simple field checks.
- IEC 60942:2017: current sound-calibrator standard; Edition 5 remains
  under development (research-only, never production method).

Honesty rules baked in:

- Field checks never masquerade as laboratory calibration; periodic
  tests and field checks are distinct kinds.
- ``calibration_overdue_by_policy`` and ``out_of_tolerance`` are
  distinct states — a policy date alone proves no physical failure.
- An unqualified calibrator cannot upgrade traceability — checks that
  cite one degrade rather than strengthen.
- Correction files bind exact instance/orientation/domain; a 90°
  free-field file never silently serves a 0° measurement.
- National traceability claims require certificate/provider evidence;
  ``accredited_traceable`` without them is rejected at the model
  boundary.
- Under-development standard editions record provenance but can never
  ground a conformance-upgrading calibration event.
- Out-of-tolerance findings mark — never rewrite — affected
  historical measurements.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


CALIBRATION_LIFECYCLE_SCHEMA_VERSION = 'metro-cal-1'
FITNESS_EVALUATION_VERSION = 'metro-cal-fit-1'
OOT_REVIEW_VERSION = 'metro-cal-oot-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _iso8601_le(left: str, right: str) -> bool:
    return datetime.fromisoformat(left) <= datetime.fromisoformat(right)


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


# ---------------------------------------------------------------------------
# Taxonomies (#611)
# ---------------------------------------------------------------------------

InstrumentCategory = Literal[
    'measurement_microphone',
    'sound_level_meter',
    'sound_calibrator',
    'audio_interface_adc_dac',
    'electrical_analyzer',
    'accelerometer',
    'colorimeter',
    'spectroradiometer',
    'photometer',
    'luminance_meter',
    'hdmi_video_analyzer',
    'environmental_sensor',
    'position_survey_instrument',
    'other',
]

InstrumentServiceState = Literal[
    'in_service',
    'retired',
    'damaged',
    'storage_only',
    'unknown',
]

CalibrationEventKind = Literal[
    'laboratory_calibration',
    'periodic_test',
    'manufacturer_calibration',
    'field_reference',
    'other',
]

#: Event kinds that can upgrade traceability/fitness.
_LAB_GRADE_EVENTS: frozenset[CalibrationEventKind] = frozenset(
    {'laboratory_calibration', 'periodic_test', 'manufacturer_calibration'}
)

StandardEditionState = Literal[
    'current',
    'confirmed',
    'under_development',
    'withdrawn',
    'unknown',
]

TraceabilityClass = Literal[
    'accredited_traceable',
    'traceable_declared',
    'manufacturer_calibrated',
    'field_referenced',
    'relative_only',
    'unknown',
]

#: Classes requiring certificate/provider evidence — stronger classes
#: without a named provider and certificate are silent upgrades.
_CERTIFICATE_REQUIRED_CLASSES: frozenset[TraceabilityClass] = frozenset(
    {'accredited_traceable', 'traceable_declared'}
)

CalibrationIntervalBasis = Literal[
    'manufacturer_recommended',
    'lab_quality_policy',
    'ilac_g24_derived',
    'project_policy',
    'regulatory_contractual',
    'historical_stability_derived',
    'condition_based',
    'unknown',
]

VerificationCheckKind = Literal[
    'pre_use_field_check',
    'post_use_field_check',
    'periodic_test',
    'interim_check',
    'other',
]

VerificationOutcome = Literal[
    'within_tolerance',
    'deviation_observed',
    'out_of_tolerance',
    'inconclusive',
    'unknown',
]

ServiceEventKind = Literal[
    'dropped_or_impact',
    'capsule_or_probe_replaced',
    'firmware_changed',
    'battery_leak_or_service',
    'repair',
    'connector_or_cable_replaced',
    'sensor_cleaning_or_filter_change',
    'storage_or_environment_excursion',
    'modified',
    'other',
]

InstrumentFitnessState = Literal[
    'fit_for_purpose',
    'fit_with_limitations',
    'calibration_overdue_by_policy',
    'check_required',
    'calibration_review_required',
    'out_of_tolerance',
    'unknown',
]

AffectedDisposition = Literal[
    'review_required',
    'limited',
    'invalid',
    'exempt',
]


# ---------------------------------------------------------------------------
# Instrument instance
# ---------------------------------------------------------------------------


class CadInstrumentInstance(BaseModel):
    """One physical measurement instrument instance.

    ``serial_or_instance_id`` names *this* unit — a model-level
    calibration curve is never instance-level evidence, so the serial
    is mandatory whenever the instance claims calibrated identity.
    """

    model_config = ConfigDict(frozen=True)

    instrument_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    category: InstrumentCategory
    manufacturer: str | None = None
    model: str | None = None
    serial_or_instance_id: str = Field(min_length=1)
    hardware_revision: str | None = None
    firmware_revision: str | None = None
    sensor_capsule_id: str | None = None
    accessories: tuple[str, ...] = ()
    owner_or_lab: str | None = None
    service_state: InstrumentServiceState = 'in_service'
    notes: str | None = None
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    instrument_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_instance(self) -> 'CadInstrumentInstance':
        _require_iso8601(
            self.declared_at_utc, 'instrument declared_at_utc'
        )
        if any(not item for item in self.accessories):
            raise ValueError('accessories must be non-empty strings')
        expected = _hash(self.identity_payload())
        if self.instrument_sha256 != expected:
            raise ValueError('instrument hash mismatch')
        if self.instrument_id != _semantic_id('calinst', expected):
            raise ValueError('instrument id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'category': self.category,
            'manufacturer': self.manufacturer,
            'model': self.model,
            'serial_or_instance_id': self.serial_or_instance_id,
            'hardware_revision': self.hardware_revision,
            'firmware_revision': self.firmware_revision,
            'sensor_capsule_id': self.sensor_capsule_id,
            'accessories': list(self.accessories),
            'owner_or_lab': self.owner_or_lab,
            'service_state': self.service_state,
            'notes': self.notes,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }


def instrument_binding(
    instrument: CadInstrumentInstance,
) -> AuthorityRef:
    return AuthorityRef(
        kind='instrument_instance',
        ref_id=instrument.instrument_id,
        ref_sha256=instrument.instrument_sha256,
    )


# ---------------------------------------------------------------------------
# Calibration events
# ---------------------------------------------------------------------------


class CadCorrectionFileBinding(BaseModel):
    """A correction file bound to an exact instrument/domain.

    Orientation/incidence and frequency domain travel with the file —
    a 90° free-field correction never silently serves a 0° measurement,
    and extrapolation beyond the declared domain is an explicit,
    limited choice.
    """

    model_config = ConfigDict(frozen=True)

    file_sha256: str = Field(pattern=_SHA256_PATTERN)
    provider: str | None = None
    issued_at_utc: str | None = None
    orientation_deg: float | None = None
    frequency_low_hz: float | None = None
    frequency_high_hz: float | None = None
    applies_to_serial: str | None = None
    algorithm_profile: str | None = None
    interpolation_rule: str | None = None
    allows_extrapolation: bool = False

    @model_validator(mode='after')
    def valid_binding(self) -> 'CadCorrectionFileBinding':
        for label, value in (
            ('orientation_deg', self.orientation_deg),
            ('frequency_low_hz', self.frequency_low_hz),
            ('frequency_high_hz', self.frequency_high_hz),
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError(f'correction file {label} must be finite')
        if (
            self.frequency_low_hz is not None
            and self.frequency_high_hz is not None
            and not (0 < self.frequency_low_hz < self.frequency_high_hz)
        ):
            raise ValueError(
                'correction file band must satisfy low < high'
            )
        if self.issued_at_utc is not None:
            _require_iso8601(
                self.issued_at_utc, 'correction file issued_at_utc'
            )
        return self


class CadCalibrationEvent(BaseModel):
    """One calibration / periodic-test / manufacturer event.

    ``traceability_class`` is evidence-classified:
    ``accredited_traceable`` and ``traceable_declared`` require the
    provider and certificate identity the claim stands on;
    ``field_referenced``/``relative_only``/``unknown`` stay honest
    without it.

    ``standard_edition_state`` keeps draft editions research-only: an
    ``under_development`` standard can annotate an event but never
    ground a lab-grade ``event_kind`` conformance claim.
    """

    model_config = ConfigDict(frozen=True)

    calibration_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    instrument_ref: AuthorityRef
    event_kind: CalibrationEventKind
    performed_at_utc: str = Field(min_length=1)
    provider_or_lab: str | None = None
    certificate_id: str | None = None
    certificate_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    method_or_profile: str | None = None
    standard_refs: tuple[str, ...] = ()
    standard_edition_state: StandardEditionState = 'unknown'
    reference_standards: tuple[str, ...] = ()
    correction_files: tuple[CadCorrectionFileBinding, ...] = ()
    measurement_uncertainty_json: str | None = None
    traceability_class: TraceabilityClass = 'unknown'
    traceability_statement: str | None = None
    environmental_conditions: str | None = None
    valid_until_utc: str | None = None
    next_review_utc: str | None = None
    rights_sensitivity: Literal[
        'open', 'protected', 'licensed', 'sensitive', 'unknown'
    ] = 'unknown'
    authority_version: str = Field(min_length=1)
    recorded_at_utc: str = Field(min_length=1)
    calibration_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_event(self) -> 'CadCalibrationEvent':
        _require_iso8601(
            self.performed_at_utc, 'calibration performed_at_utc'
        )
        _require_iso8601(
            self.recorded_at_utc, 'calibration recorded_at_utc'
        )
        if self.valid_until_utc is not None:
            _require_iso8601(
                self.valid_until_utc, 'calibration valid_until_utc'
            )
        if self.next_review_utc is not None:
            _require_iso8601(
                self.next_review_utc, 'calibration next_review_utc'
            )
        if self.instrument_ref.ref_sha256 is None:
            raise ValueError(
                'calibration events must pin the instrument sha256'
            )
        if (
            self.traceability_class in _CERTIFICATE_REQUIRED_CLASSES
            and not (self.provider_or_lab and self.certificate_id)
        ):
            raise ValueError(
                f'{self.traceability_class} requires provider and '
                'certificate evidence — the claim cannot rest on an '
                'unbacked label'
            )
        if (
            self.event_kind in _LAB_GRADE_EVENTS
            and self.standard_edition_state == 'under_development'
        ):
            raise ValueError(
                'an under-development standard edition is research-'
                'only — it cannot ground a conformance-upgrading '
                'calibration event'
            )
        expected = _hash(self.identity_payload())
        if self.calibration_sha256 != expected:
            raise ValueError('calibration event hash mismatch')
        if self.calibration_id != _semantic_id('calevt', expected):
            raise ValueError('calibration id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'instrument_ref': self.instrument_ref.model_dump(mode='json'),
            'event_kind': self.event_kind,
            'performed_at_utc': self.performed_at_utc,
            'provider_or_lab': self.provider_or_lab,
            'certificate_id': self.certificate_id,
            'certificate_sha256': self.certificate_sha256,
            'method_or_profile': self.method_or_profile,
            'standard_refs': list(self.standard_refs),
            'standard_edition_state': self.standard_edition_state,
            'reference_standards': list(self.reference_standards),
            'correction_files': [
                item.model_dump(mode='json')
                for item in self.correction_files
            ],
            'measurement_uncertainty_json': (
                self.measurement_uncertainty_json
            ),
            'traceability_class': self.traceability_class,
            'traceability_statement': self.traceability_statement,
            'environmental_conditions': self.environmental_conditions,
            'valid_until_utc': self.valid_until_utc,
            'next_review_utc': self.next_review_utc,
            'rights_sensitivity': self.rights_sensitivity,
            'authority_version': self.authority_version,
            'recorded_at_utc': self.recorded_at_utc,
        }


def calibration_event_binding(
    event: CadCalibrationEvent,
) -> AuthorityRef:
    return AuthorityRef(
        kind='calibration_event',
        ref_id=event.calibration_id,
        ref_sha256=event.calibration_sha256,
    )


# ---------------------------------------------------------------------------
# Interval policy
# ---------------------------------------------------------------------------


class CadCalibrationIntervalPolicy(BaseModel):
    """The recalibration-interval basis for an instrument or category.

    The interval is programme/policy evidence (ILAC G24-style): it is
    declared with a basis and rationale, never silently inferred from
    certificate age. ``instrument_ref`` scopes to one instance;
    ``instrument_category`` scopes to a class when no instance policy
    exists.
    """

    model_config = ConfigDict(frozen=True)

    policy_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    instrument_ref: AuthorityRef | None = None
    instrument_category: InstrumentCategory | None = None
    basis: CalibrationIntervalBasis
    nominal_interval_days: int | None = Field(default=None, ge=1)
    rationale: str | None = None
    review_at_utc: str | None = None
    change_history_json: str = '[]'
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_policy(self) -> 'CadCalibrationIntervalPolicy':
        _require_iso8601(self.declared_at_utc, 'policy declared_at_utc')
        if self.review_at_utc is not None:
            _require_iso8601(
                self.review_at_utc, 'policy review_at_utc'
            )
        if (self.instrument_ref is None) == (
            self.instrument_category is None
        ):
            raise ValueError(
                'a policy scopes to exactly one instrument instance or '
                'one category'
            )
        if self.instrument_ref is not None and (
            self.instrument_ref.ref_sha256 is None
        ):
            raise ValueError(
                'instrument-scoped policies must pin the instrument '
                'sha256'
            )
        expected = _hash(self.identity_payload())
        if self.policy_sha256 != expected:
            raise ValueError('interval policy hash mismatch')
        if self.policy_id != _semantic_id('calpol', expected):
            raise ValueError('policy id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'instrument_ref': (
                self.instrument_ref.model_dump(mode='json')
                if self.instrument_ref is not None
                else None
            ),
            'instrument_category': self.instrument_category,
            'basis': self.basis,
            'nominal_interval_days': self.nominal_interval_days,
            'rationale': self.rationale,
            'review_at_utc': self.review_at_utc,
            'change_history_json': self.change_history_json,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }


# ---------------------------------------------------------------------------
# Verification checks
# ---------------------------------------------------------------------------


class CadInstrumentVerificationCheck(BaseModel):
    """One bounded field/periodic verification check.

    Kinds stay distinct: ``pre_use_field_check``/``post_use_field_check``
    are operator-level campaign checks; ``periodic_test`` is the formal
    IEC 61672-3-style test set; a field check never satisfies a
    periodic-test requirement. ``calibrator_ref`` pins the exact
    calibrator instance used — its own fitness decides whether the
    check can upgrade anything.
    """

    model_config = ConfigDict(frozen=True)

    check_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    instrument_ref: AuthorityRef
    kind: VerificationCheckKind
    calibrator_ref: AuthorityRef | None = None
    campaign_id: str | None = None
    reference_level_db: float | None = None
    reference_frequency_hz: float | None = None
    observed_value: float | None = None
    observed_deviation_db: float | None = None
    ambient_conditions: str | None = None
    acceptance_profile: str | None = None
    outcome: VerificationOutcome
    performed_at_utc: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    recorded_at_utc: str = Field(min_length=1)
    check_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_check(self) -> 'CadInstrumentVerificationCheck':
        _require_iso8601(
            self.performed_at_utc, 'check performed_at_utc'
        )
        _require_iso8601(
            self.recorded_at_utc, 'check recorded_at_utc'
        )
        for label, value in (
            ('reference_level_db', self.reference_level_db),
            ('reference_frequency_hz', self.reference_frequency_hz),
            ('observed_value', self.observed_value),
            ('observed_deviation_db', self.observed_deviation_db),
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError(f'check {label} must be finite')
        if self.instrument_ref.ref_sha256 is None:
            raise ValueError(
                'verification checks must pin the instrument sha256'
            )
        if self.calibrator_ref is not None and (
            self.calibrator_ref.ref_sha256 is None
        ):
            raise ValueError(
                'calibrator refs must pin the calibrator sha256'
            )
        expected = _hash(self.identity_payload())
        if self.check_sha256 != expected:
            raise ValueError('verification check hash mismatch')
        if self.check_id != _semantic_id('calchk', expected):
            raise ValueError('check id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'instrument_ref': self.instrument_ref.model_dump(mode='json'),
            'kind': self.kind,
            'calibrator_ref': (
                self.calibrator_ref.model_dump(mode='json')
                if self.calibrator_ref is not None
                else None
            ),
            'campaign_id': self.campaign_id,
            'reference_level_db': self.reference_level_db,
            'reference_frequency_hz': self.reference_frequency_hz,
            'observed_value': self.observed_value,
            'observed_deviation_db': self.observed_deviation_db,
            'ambient_conditions': self.ambient_conditions,
            'acceptance_profile': self.acceptance_profile,
            'outcome': self.outcome,
            'performed_at_utc': self.performed_at_utc,
            'authority_version': self.authority_version,
            'recorded_at_utc': self.recorded_at_utc,
        }


def verification_check_binding(
    check: CadInstrumentVerificationCheck,
) -> AuthorityRef:
    return AuthorityRef(
        kind='instrument_verification_check',
        ref_id=check.check_id,
        ref_sha256=check.check_sha256,
    )


# ---------------------------------------------------------------------------
# Service events
# ---------------------------------------------------------------------------


class CadInstrumentServiceEvent(BaseModel):
    """A damage/service/modification event that may invalidate prior
    calibration assumptions — the fitness evaluator treats any material
    event after the last valid calibration as ``review_required``,
    never waiting for a nominal due date.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    instrument_ref: AuthorityRef
    kind: ServiceEventKind
    description: str | None = None
    occurred_at_utc: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    recorded_at_utc: str = Field(min_length=1)
    event_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_event(self) -> 'CadInstrumentServiceEvent':
        _require_iso8601(
            self.occurred_at_utc, 'service event occurred_at_utc'
        )
        _require_iso8601(
            self.recorded_at_utc, 'service event recorded_at_utc'
        )
        if self.instrument_ref.ref_sha256 is None:
            raise ValueError(
                'service events must pin the instrument sha256'
            )
        expected = _hash(self.identity_payload())
        if self.event_sha256 != expected:
            raise ValueError('service event hash mismatch')
        if self.event_id != _semantic_id('calsvc', expected):
            raise ValueError('service event id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'instrument_ref': self.instrument_ref.model_dump(mode='json'),
            'kind': self.kind,
            'description': self.description,
            'occurred_at_utc': self.occurred_at_utc,
            'authority_version': self.authority_version,
            'recorded_at_utc': self.recorded_at_utc,
        }


# ---------------------------------------------------------------------------
# Fitness assessments
# ---------------------------------------------------------------------------


class CadInstrumentFitnessAssessment(BaseModel):
    """Sealed fitness verdict at one exact measurement timestamp.

    ``at_utc`` is the timestamp the measurement was made — fitness is
    evaluated *as of* that instant from events at/before it; later
    evidence can only downgrade via a review, never silently rewrite.
    ``basis_calibration_ref``/``basis_check_ref`` pin the exact
    evidence the verdict stands on.
    """

    model_config = ConfigDict(frozen=True)

    assessment_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    instrument_ref: AuthorityRef
    at_utc: str = Field(min_length=1)
    state: InstrumentFitnessState
    basis_calibration_ref: AuthorityRef | None = None
    basis_check_ref: AuthorityRef | None = None
    limitations: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    assessment_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_assessment(self) -> 'CadInstrumentFitnessAssessment':
        _require_iso8601(self.at_utc, 'assessment at_utc')
        _require_iso8601(
            self.evaluated_at_utc, 'assessment evaluated_at_utc'
        )
        if self.instrument_ref.ref_sha256 is None:
            raise ValueError(
                'assessments must pin the instrument sha256'
            )
        for label, ref in (
            ('basis_calibration_ref', self.basis_calibration_ref),
            ('basis_check_ref', self.basis_check_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin the record sha256')
        if self.state == 'fit_for_purpose' and (
            self.basis_calibration_ref is None
            and self.basis_check_ref is None
        ):
            raise ValueError(
                'fit_for_purpose requires the evidence it stands on'
            )
        expected = _hash(self.identity_payload())
        if self.assessment_sha256 != expected:
            raise ValueError('fitness assessment hash mismatch')
        if self.assessment_id != _semantic_id('calfit', expected):
            raise ValueError('assessment id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'instrument_ref': self.instrument_ref.model_dump(mode='json'),
            'at_utc': self.at_utc,
            'state': self.state,
            'basis_calibration_ref': (
                self.basis_calibration_ref.model_dump(mode='json')
                if self.basis_calibration_ref is not None
                else None
            ),
            'basis_check_ref': (
                self.basis_check_ref.model_dump(mode='json')
                if self.basis_check_ref is not None
                else None
            ),
            'limitations': list(self.limitations),
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }


# ---------------------------------------------------------------------------
# Out-of-tolerance impact reviews
# ---------------------------------------------------------------------------


class CadOutOfToleranceReview(BaseModel):
    """Non-destructive impact review for an out-of-tolerance finding.

    ``triggering_ref`` pins the calibration/check that found the
    instrument outside tolerance; ``last_known_valid_at_utc`` bounds the
    exposure window; ``dispositions`` *mark* affected measurement
    references — original evidence is preserved untouched.
    """

    model_config = ConfigDict(frozen=True)

    review_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    instrument_ref: AuthorityRef
    triggering_ref: AuthorityRef
    last_known_valid_at_utc: str | None = None
    dispositions: tuple[tuple[str, AffectedDisposition], ...] = ()
    reasons: tuple[str, ...] = ()
    authority_version: str = Field(min_length=1)
    recorded_at_utc: str = Field(min_length=1)
    review_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_review(self) -> 'CadOutOfToleranceReview':
        _require_iso8601(
            self.recorded_at_utc, 'review recorded_at_utc'
        )
        if self.last_known_valid_at_utc is not None:
            _require_iso8601(
                self.last_known_valid_at_utc,
                'review last_known_valid_at_utc',
            )
        for label, ref in (
            ('instrument_ref', self.instrument_ref),
            ('triggering_ref', self.triggering_ref),
        ):
            if ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin the record sha256')
        if any(not ref for ref, _ in self.dispositions):
            raise ValueError('dispositions must name a measurement ref')
        expected = _hash(self.identity_payload())
        if self.review_sha256 != expected:
            raise ValueError('review hash mismatch')
        if self.review_id != _semantic_id('caloot', expected):
            raise ValueError('review id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'instrument_ref': self.instrument_ref.model_dump(mode='json'),
            'triggering_ref': self.triggering_ref.model_dump(mode='json'),
            'last_known_valid_at_utc': self.last_known_valid_at_utc,
            'dispositions': [list(item) for item in self.dispositions],
            'reasons': list(self.reasons),
            'authority_version': self.authority_version,
            'recorded_at_utc': self.recorded_at_utc,
        }


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _seal_model(model, payload: dict[str, Any], id_field: str,
                sha_field: str, prefix: str):
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{id_field: _semantic_id(prefix, digest), sha_field: digest},
    )


def build_instrument_instance(
    *,
    document_id: str,
    category: InstrumentCategory,
    serial_or_instance_id: str,
    manufacturer: str | None = None,
    model: str | None = None,
    hardware_revision: str | None = None,
    firmware_revision: str | None = None,
    sensor_capsule_id: str | None = None,
    accessories: tuple[str, ...] = (),
    owner_or_lab: str | None = None,
    service_state: InstrumentServiceState = 'in_service',
    notes: str | None = None,
    declared_at_utc: str | None = None,
) -> CadInstrumentInstance:
    """Seal one physical instrument instance."""
    payload = dict(
        document_id=document_id,
        category=category,
        manufacturer=manufacturer,
        model=model,
        serial_or_instance_id=serial_or_instance_id,
        hardware_revision=hardware_revision,
        firmware_revision=firmware_revision,
        sensor_capsule_id=sensor_capsule_id,
        accessories=tuple(accessories),
        owner_or_lab=owner_or_lab,
        service_state=service_state,
        notes=notes,
        authority_version=CALIBRATION_LIFECYCLE_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
    )
    return _seal_model(
        CadInstrumentInstance, payload,
        'instrument_id', 'instrument_sha256', 'calinst',
    )


def build_calibration_event(
    *,
    document_id: str,
    instrument_ref: AuthorityRef | CadInstrumentInstance,
    event_kind: CalibrationEventKind,
    performed_at_utc: str,
    provider_or_lab: str | None = None,
    certificate_id: str | None = None,
    certificate_sha256: str | None = None,
    method_or_profile: str | None = None,
    standard_refs: tuple[str, ...] = (),
    standard_edition_state: StandardEditionState = 'unknown',
    reference_standards: tuple[str, ...] = (),
    correction_files: tuple[CadCorrectionFileBinding, ...] = (),
    measurement_uncertainty_json: str | None = None,
    traceability_class: TraceabilityClass = 'unknown',
    traceability_statement: str | None = None,
    environmental_conditions: str | None = None,
    valid_until_utc: str | None = None,
    next_review_utc: str | None = None,
    rights_sensitivity: Literal[
        'open', 'protected', 'licensed', 'sensitive', 'unknown'
    ] = 'unknown',
    recorded_at_utc: str | None = None,
) -> CadCalibrationEvent:
    """Seal one calibration/periodic-test event."""
    if isinstance(instrument_ref, CadInstrumentInstance):
        instrument_ref = instrument_binding(instrument_ref)
    payload = dict(
        document_id=document_id,
        instrument_ref=instrument_ref,
        event_kind=event_kind,
        performed_at_utc=performed_at_utc,
        provider_or_lab=provider_or_lab,
        certificate_id=certificate_id,
        certificate_sha256=certificate_sha256,
        method_or_profile=method_or_profile,
        standard_refs=tuple(standard_refs),
        standard_edition_state=standard_edition_state,
        reference_standards=tuple(reference_standards),
        correction_files=tuple(correction_files),
        measurement_uncertainty_json=measurement_uncertainty_json,
        traceability_class=traceability_class,
        traceability_statement=traceability_statement,
        environmental_conditions=environmental_conditions,
        valid_until_utc=valid_until_utc,
        next_review_utc=next_review_utc,
        rights_sensitivity=rights_sensitivity,
        authority_version=CALIBRATION_LIFECYCLE_SCHEMA_VERSION,
        recorded_at_utc=recorded_at_utc or _utc_now(),
    )
    return _seal_model(
        CadCalibrationEvent, payload,
        'calibration_id', 'calibration_sha256', 'calevt',
    )


def build_interval_policy(
    *,
    document_id: str,
    basis: CalibrationIntervalBasis,
    instrument_ref: AuthorityRef | CadInstrumentInstance | None = None,
    instrument_category: InstrumentCategory | None = None,
    nominal_interval_days: int | None = None,
    rationale: str | None = None,
    review_at_utc: str | None = None,
    change_history_json: str = '[]',
    declared_at_utc: str | None = None,
) -> CadCalibrationIntervalPolicy:
    """Seal a recalibration-interval policy."""
    if isinstance(instrument_ref, CadInstrumentInstance):
        instrument_ref = instrument_binding(instrument_ref)
    payload = dict(
        document_id=document_id,
        instrument_ref=instrument_ref,
        instrument_category=instrument_category,
        basis=basis,
        nominal_interval_days=nominal_interval_days,
        rationale=rationale,
        review_at_utc=review_at_utc,
        change_history_json=change_history_json,
        authority_version=CALIBRATION_LIFECYCLE_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
    )
    return _seal_model(
        CadCalibrationIntervalPolicy, payload,
        'policy_id', 'policy_sha256', 'calpol',
    )


def build_verification_check(
    *,
    document_id: str,
    instrument_ref: AuthorityRef | CadInstrumentInstance,
    kind: VerificationCheckKind,
    outcome: VerificationOutcome,
    performed_at_utc: str,
    calibrator_ref: AuthorityRef | CadInstrumentInstance | None = None,
    campaign_id: str | None = None,
    reference_level_db: float | None = None,
    reference_frequency_hz: float | None = None,
    observed_value: float | None = None,
    observed_deviation_db: float | None = None,
    ambient_conditions: str | None = None,
    acceptance_profile: str | None = None,
    recorded_at_utc: str | None = None,
) -> CadInstrumentVerificationCheck:
    """Seal one field/periodic verification check."""
    if isinstance(instrument_ref, CadInstrumentInstance):
        instrument_ref = instrument_binding(instrument_ref)
    if isinstance(calibrator_ref, CadInstrumentInstance):
        calibrator_ref = instrument_binding(calibrator_ref)
    payload = dict(
        document_id=document_id,
        instrument_ref=instrument_ref,
        kind=kind,
        calibrator_ref=calibrator_ref,
        campaign_id=campaign_id,
        reference_level_db=reference_level_db,
        reference_frequency_hz=reference_frequency_hz,
        observed_value=observed_value,
        observed_deviation_db=observed_deviation_db,
        ambient_conditions=ambient_conditions,
        acceptance_profile=acceptance_profile,
        outcome=outcome,
        performed_at_utc=performed_at_utc,
        authority_version=CALIBRATION_LIFECYCLE_SCHEMA_VERSION,
        recorded_at_utc=recorded_at_utc or _utc_now(),
    )
    return _seal_model(
        CadInstrumentVerificationCheck, payload,
        'check_id', 'check_sha256', 'calchk',
    )


def build_service_event(
    *,
    document_id: str,
    instrument_ref: AuthorityRef | CadInstrumentInstance,
    kind: ServiceEventKind,
    occurred_at_utc: str,
    description: str | None = None,
    recorded_at_utc: str | None = None,
) -> CadInstrumentServiceEvent:
    """Seal one service/damage event."""
    if isinstance(instrument_ref, CadInstrumentInstance):
        instrument_ref = instrument_binding(instrument_ref)
    payload = dict(
        document_id=document_id,
        instrument_ref=instrument_ref,
        kind=kind,
        description=description,
        occurred_at_utc=occurred_at_utc,
        authority_version=CALIBRATION_LIFECYCLE_SCHEMA_VERSION,
        recorded_at_utc=recorded_at_utc or _utc_now(),
    )
    return _seal_model(
        CadInstrumentServiceEvent, payload,
        'event_id', 'event_sha256', 'calsvc',
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _as_of(
    records,
    timestamp: str,
    field: str,
):
    """Records whose timestamp field is at/before ``timestamp``."""
    return [
        record
        for record in records
        if _iso8601_le(getattr(record, field), timestamp)
    ]


def evaluate_instrument_fitness(
    *,
    document_id: str,
    instrument: CadInstrumentInstance,
    calibrations: tuple[CadCalibrationEvent, ...] | list[CadCalibrationEvent] = (),
    checks: tuple[CadInstrumentVerificationCheck, ...] | list[CadInstrumentVerificationCheck] = (),
    service_events: tuple[CadInstrumentServiceEvent, ...] | list[CadInstrumentServiceEvent] = (),
    policies: tuple[CadCalibrationIntervalPolicy, ...] | list[CadCalibrationIntervalPolicy] = (),
    at_utc: str,
    calibrator_fitness: Callable[[str, str], InstrumentFitnessState] | None = None,
    evaluated_at_utc: str | None = None,
) -> CadInstrumentFitnessAssessment:
    """Fail-closed fitness verdict at one exact measurement timestamp.

    ``calibrator_fitness`` optionally resolves another instrument's
    fitness at a timestamp (``(instrument_id, at_utc) -> state``) —
    checks performed with an unqualified calibrator cannot upgrade the
    instrument's standing.

    Precedence (most serious first): service events after the last
    valid evidence → ``calibration_review_required``; a failed check →
    ``out_of_tolerance``; a policy due date passed →
    ``calibration_overdue_by_policy`` (policy evidence, never proof of
    physical failure); valid calibration with limitations →
    ``fit_with_limitations``; nothing on record → ``check_required``
    when only checks exist, else ``unknown``.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(at_utc, 'at_utc')
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    reasons: list[str] = []
    limitations: list[str] = []

    inst_ref = instrument_binding(instrument)
    inst_calibrations = [
        event for event in calibrations
        if event.instrument_ref.ref_id == instrument.instrument_id
    ]
    inst_checks = [
        check for check in checks
        if check.instrument_ref.ref_id == instrument.instrument_id
    ]
    inst_events = [
        event for event in service_events
        if event.instrument_ref.ref_id == instrument.instrument_id
    ]
    inst_policies = [
        policy for policy in policies
        if (
            policy.instrument_ref is not None
            and policy.instrument_ref.ref_id == instrument.instrument_id
        ) or (
            policy.instrument_ref is None
            and policy.instrument_category == instrument.category
        )
    ]

    prior_calibrations = sorted(
        _as_of(inst_calibrations, at_utc, 'performed_at_utc'),
        key=lambda event: event.performed_at_utc,
    )
    prior_checks = sorted(
        _as_of(inst_checks, at_utc, 'performed_at_utc'),
        key=lambda check: check.performed_at_utc,
    )
    prior_events = [
        event for event in inst_events
        if _iso8601_le(event.occurred_at_utc, at_utc)
    ]

    latest_calibration = (
        prior_calibrations[-1] if prior_calibrations else None
    )
    latest_check = prior_checks[-1] if prior_checks else None

    # The "last valid evidence" instant: latest calibration or a passing
    # check, whichever is newer.
    valid_instants: list[str] = []
    if latest_calibration is not None:
        valid_instants.append(latest_calibration.performed_at_utc)
    passing_checks = [
        check for check in prior_checks
        if check.outcome == 'within_tolerance'
    ]
    if passing_checks:
        valid_instants.append(passing_checks[-1].performed_at_utc)
    last_valid_instant = max(valid_instants) if valid_instants else None

    # --- service events after last valid evidence -----------------------
    invalidating = [
        event for event in prior_events
        if last_valid_instant is None
        or event.occurred_at_utc >= last_valid_instant
    ]
    if invalidating:
        latest = max(invalidating, key=lambda event: event.occurred_at_utc)
        reasons.append(
            f'service event {latest.kind} at {latest.occurred_at_utc} '
            'postdates the last known-valid evidence'
        )
        payload = dict(
            document_id=document_id,
            instrument_ref=inst_ref,
            at_utc=at_utc,
            state='calibration_review_required',
            basis_calibration_ref=(
                calibration_event_binding(latest_calibration)
                if latest_calibration is not None
                else None
            ),
            basis_check_ref=(
                verification_check_binding(latest_check)
                if latest_check is not None
                else None
            ),
            limitations=(),
            reasons=tuple(reasons),
            evaluation_version=FITNESS_EVALUATION_VERSION,
            evaluated_at_utc=evaluated_at_utc,
        )
        return _seal_model(
            CadInstrumentFitnessAssessment, payload,
            'assessment_id', 'assessment_sha256', 'calfit',
        )

    # --- failed checks ----------------------------------------------------
    failed_checks = [
        check for check in prior_checks
        if check.outcome == 'out_of_tolerance'
        and (
            latest_calibration is None
            or check.performed_at_utc >= latest_calibration.performed_at_utc
        )
    ]
    if failed_checks:
        reasons.append(
            'latest verification check reported out of tolerance'
        )
        payload = dict(
            document_id=document_id,
            instrument_ref=inst_ref,
            at_utc=at_utc,
            state='out_of_tolerance',
            basis_calibration_ref=(
                calibration_event_binding(latest_calibration)
                if latest_calibration is not None
                else None
            ),
            basis_check_ref=verification_check_binding(failed_checks[-1]),
            limitations=(),
            reasons=tuple(reasons),
            evaluation_version=FITNESS_EVALUATION_VERSION,
            evaluated_at_utc=evaluated_at_utc,
        )
        return _seal_model(
            CadInstrumentFitnessAssessment, payload,
            'assessment_id', 'assessment_sha256', 'calfit',
        )

    # --- policy due date ----------------------------------------------------
    overdue = False
    if inst_policies and latest_calibration is not None:
        for policy in inst_policies:
            if policy.nominal_interval_days is None:
                continue
            performed = datetime.fromisoformat(
                latest_calibration.performed_at_utc
            )
            at = datetime.fromisoformat(at_utc)
            elapsed_days = (at - performed).total_seconds() / 86400.0
            if elapsed_days > policy.nominal_interval_days:
                overdue = True
                reasons.append(
                    f'{policy.basis} interval '
                    f'({policy.nominal_interval_days} d) exceeded at '
                    'measurement time'
                )
    if latest_calibration is not None and (
        latest_calibration.valid_until_utc is not None
        and _iso8601_le(
            latest_calibration.valid_until_utc, at_utc
        )
    ):
        overdue = True
        reasons.append(
            'certificate-declared validity expired at measurement time'
        )
    if overdue:
        payload = dict(
            document_id=document_id,
            instrument_ref=inst_ref,
            at_utc=at_utc,
            state='calibration_overdue_by_policy',
            basis_calibration_ref=calibration_event_binding(
                latest_calibration
            ),
            basis_check_ref=(
                verification_check_binding(latest_check)
                if latest_check is not None
                else None
            ),
            limitations=(),
            reasons=tuple(reasons),
            evaluation_version=FITNESS_EVALUATION_VERSION,
            evaluated_at_utc=evaluated_at_utc,
        )
        return _seal_model(
            CadInstrumentFitnessAssessment, payload,
            'assessment_id', 'assessment_sha256', 'calfit',
        )

    # --- checks through unqualified calibrators ------------------------------
    unqualified_calibrator = False
    for check in prior_checks:
        if check.calibrator_ref is None or calibrator_fitness is None:
            continue
        state = calibrator_fitness(
            check.calibrator_ref.ref_id, check.performed_at_utc
        )
        if state not in ('fit_for_purpose', 'fit_with_limitations'):
            unqualified_calibrator = True
            limitations.append(
                'a field check relied on a calibrator whose own '
                'fitness is not established — it cannot upgrade '
                'traceability'
            )
            break

    # --- final standing -------------------------------------------------------
    if latest_calibration is None and not prior_checks:
        state: InstrumentFitnessState = 'unknown'
        reasons.append('no calibration or check evidence on record')
    elif latest_calibration is None:
        state = 'check_required'
        reasons.append(
            'only field checks on record — a field check is not '
            'laboratory calibration'
        )
    elif latest_calibration.event_kind == 'field_reference':
        state = 'fit_with_limitations'
        limitations.append(
            'field-reference calibration only — relative evidence, '
            'not lab-grade traceability'
        )
    elif unqualified_calibrator:
        state = 'fit_with_limitations'
    else:
        state = 'fit_for_purpose'

    payload = dict(
        document_id=document_id,
        instrument_ref=inst_ref,
        at_utc=at_utc,
        state=state,
        basis_calibration_ref=(
            calibration_event_binding(latest_calibration)
            if latest_calibration is not None
            else None
        ),
        basis_check_ref=(
            verification_check_binding(latest_check)
            if latest_check is not None
            else None
        ),
        limitations=tuple(limitations),
        reasons=tuple(reasons),
        evaluation_version=FITNESS_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadInstrumentFitnessAssessment, payload,
        'assessment_id', 'assessment_sha256', 'calfit',
    )


def review_out_of_tolerance(
    *,
    document_id: str,
    instrument: CadInstrumentInstance,
    triggering_ref: AuthorityRef | CadCalibrationEvent | CadInstrumentVerificationCheck,
    last_known_valid_at_utc: str | None,
    affected_measurement_refs: tuple[str, ...],
    disposition: AffectedDisposition = 'review_required',
    reasons: tuple[str, ...] = (),
    recorded_at_utc: str | None = None,
) -> CadOutOfToleranceReview:
    """Seal a non-destructive out-of-tolerance impact review.

    Every measurement made since the last known-valid event is marked
    with ``disposition`` — the original evidence is never rewritten;
    the review only bounds what may need re-evaluation.
    """
    if isinstance(triggering_ref, CadCalibrationEvent):
        triggering_ref = calibration_event_binding(triggering_ref)
    elif isinstance(triggering_ref, CadInstrumentVerificationCheck):
        triggering_ref = verification_check_binding(triggering_ref)
    payload = dict(
        document_id=document_id,
        instrument_ref=instrument_binding(instrument),
        triggering_ref=triggering_ref,
        last_known_valid_at_utc=last_known_valid_at_utc,
        dispositions=tuple(
            (ref, disposition) for ref in affected_measurement_refs
        ),
        reasons=tuple(reasons),
        authority_version=OOT_REVIEW_VERSION,
        recorded_at_utc=recorded_at_utc or _utc_now(),
    )
    return _seal_model(
        CadOutOfToleranceReview, payload,
        'review_id', 'review_sha256', 'caloot',
    )


__all__ = [
    'AffectedDisposition',
    'CALIBRATION_LIFECYCLE_SCHEMA_VERSION',
    'CadCalibrationEvent',
    'CadCalibrationIntervalPolicy',
    'CadCorrectionFileBinding',
    'CadInstrumentFitnessAssessment',
    'CadInstrumentInstance',
    'CadInstrumentServiceEvent',
    'CadInstrumentVerificationCheck',
    'CadOutOfToleranceReview',
    'CalibrationEventKind',
    'CalibrationIntervalBasis',
    'FITNESS_EVALUATION_VERSION',
    'InstrumentCategory',
    'InstrumentFitnessState',
    'InstrumentServiceState',
    'OOT_REVIEW_VERSION',
    'ServiceEventKind',
    'StandardEditionState',
    'TraceabilityClass',
    'VerificationCheckKind',
    'VerificationOutcome',
    'build_calibration_event',
    'build_instrument_instance',
    'build_interval_policy',
    'build_service_event',
    'build_verification_check',
    'calibration_event_binding',
    'evaluate_instrument_fitness',
    'instrument_binding',
    'review_out_of_tolerance',
    'verification_check_binding',
]
