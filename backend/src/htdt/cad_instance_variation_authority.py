"""Installed loudspeaker instance-variation authority (#628, REV57-AUD).

A manufacturer/reference/open-dataset measurement describes *a model or
measured sample* — a real theater contains individual units whose
frequency response, impedance, sensitivity, resonance, distortion,
polarity and large-signal behavior vary through production tolerance,
assembly defects, environment, service history and aging. This module
owns the model↔instance evidence layer:

- :class:`CadInstanceAcousticEvidence` — sealed per-instance evidence:
  the evidence level (model reference / golden sample / factory QC /
  lab / in-room / diagnostic / derived adjustment), the observed
  properties with method/level/environment/validity domain, and the
  pinned installed-equipment instance (#569) and model definition.
- :class:`CadModelToInstanceDelta` — an immutable *derived* comparison
  artifact: reference evidence + instance evidence + registration +
  domain + delta summary + uncertainty + algorithm identity. Raw
  measurements are never overwritten and a delta can only correct a
  physically compatible quantity — on-axis deviation cannot manufacture
  3D directivity, and a low-level measurement cannot establish
  large-signal capability (#628 §8).
- :class:`CadMatchedSetDeclaration` — an explicit matched group (L/R,
  L/C/R, surround array, height array, multi-sub).
- :class:`CadMatchedSetQualification` +
  :func:`evaluate_matched_set` — the fail-closed matched-set verdict:
  independent per-metric spreads (level / FR deviation / polarity /
  impedance / delay / nonlinear), never one opaque score, and a fault
  classification on the #628 §10 ladder.
- :func:`classify_instance_delta` — the per-delta classification ladder
  (within declared tolerance / within project tolerance / outlier /
  suspected defect / confirmed fault / environment dependent /
  inconclusive / no population tolerance available).

Honesty rules baked in:

- An unmeasured instance never inherits serial-specific truth: model
  reference evidence stays model-level and a delta cannot correct
  physically incompatible quantities.
- A factory-QC pass is not an anechoic directivity measurement; an
  in-room response is not a free-field quantity — the evidence *domain*
  is a first-class field.
- Tolerances are never invented: without a manufacturer population
  tolerance or project tolerance, the classification is
  ``no_population_tolerance_available``, not a guessed threshold.
- Environmental conditioning binds to #573: a temperature/humidity/
  recent-stress shift is ``environment_dependent``, not a permanent
  defect, until repeat evidence exists.
- Same-model replacement invalidates serial evidence: the evaluator
  takes a ``replaced_instance`` flag so #596 service events stale
  instance baselines even when the model number is unchanged.
- AES-X241 (End of Line Testing for Production Loudspeaker Drivers) is
  an AES *standards-development* project — research status only. HTDT
  never encodes guessed AES-X241 normative tolerances.

Literature basis
----------------
- ANSI/CTA-2034-B (2024) — standardized model/sample measurement; one
  measured sample is not a statistical population guarantee.
- IEC 60268-21:2018 / IEC 60268-22:2020 — instance-level acoustic and
  electrical/mechanical measurement methods.
- KLIPPEL production-QC (dB-Lab documentation) — DUT-vs-golden-reference
  comparison with configured tolerance limits (vendor evidence, not a
  universal tolerance profile).
- AES-X241 — active standards-development project; research-only status.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from statistics import median
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


INSTANCE_VARIATION_SCHEMA_VERSION = 'aud-instance-variation-1'
INSTANCE_VARIATION_EVALUATION_VERSION = 'aud-instance-eval-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _require_finite(value: float, label: str) -> None:
    if not isfinite(float(value)):
        raise ValueError(f'{label} must be finite')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


# ---------------------------------------------------------------------------
# Taxonomies (#628)
# ---------------------------------------------------------------------------

InstanceEvidenceLevel = Literal[
    'model_reference_only',
    'manufacturer_population_tolerance',
    'golden_sample_reference',
    'instance_factory_qc',
    'instance_lab_measurement',
    'instance_in_room_measurement',
    'instance_diagnostic_check',
    'derived_instance_adjustment',
    'unknown',
]
"""#628 §2 — evidence levels stay distinct: a factory QC pass is not an
anechoic measurement; an in-room response is not a free-field quantity;
model reference is never silently serial-specific."""

InstanceObservable = Literal[
    'polarity',
    'dc_impedance',
    'impedance_zf',
    'resonance_features',
    'sensitivity',
    'frequency_response_deviation',
    'phase_group_delay',
    'thd_nonlinear',
    'compression_output',
    'mechanical_anomaly',
    'self_noise',
    'dsp_firmware_state',
]
"""#628 §3 — the instance-observable taxonomy. Each retains method,
level, environment, calibration and validity domain."""

EvidenceSource = Literal['model', 'golden', 'instance']
"""Which physical object the evidence describes. ``model``/
``golden`` evidence is reference truth; only ``instance`` evidence is
serial-specific."""

MeasurementDomain = Literal[
    'free_field',
    'in_room',
    'lab',
    'factory_line',
    'unknown',
]
"""The physical domain the evidence was captured in — an in-room trace is
never a free-field response."""

MeasurementLevelClass = Literal[
    'low_level', 'medium_level', 'large_signal', 'unknown'
]
"""The drive level the evidence was captured at — low-level evidence
never establishes large-signal capability (#628 §8)."""

DeltaQuantity = Literal[
    'on_axis_response',
    'sensitivity_offset',
    'band_deviation',
    'impedance_curve',
    'resonance_shift',
    'thd_offset',
    'directivity_full_3d',
    'large_signal_capability',
]
"""What a model→instance delta may correct. ``directivity_full_3d``
requires true directional evidence on both sides; ``large_signal_capability``
requires large-signal evidence — neither is manufacturable from a
weaker domain."""

DeltaClassification = Literal[
    'within_declared_tolerance',
    'matched_within_project_tolerance',
    'outlier',
    'suspected_defect',
    'confirmed_fault',
    'environment_dependent',
    'measurement_inconclusive',
    'no_population_tolerance_available',
]
"""#628 §10 fault/variation states — never invent a manufacturer
tolerance when only a model reference exists."""

MatchedSetRole = Literal[
    'l_r', 'l_c_r', 'surround_array', 'height_array', 'multi_sub', 'custom'
]
"""#628 §6 matched-set groupings."""

MatchedMetric = Literal[
    'level_spread',
    'fr_deviation',
    'polarity_consistency',
    'impedance_spread',
    'delay_spread',
    'nonlinear_spread',
]
"""Independent matched-set metrics — spread only, no opaque score."""

MatchedMetricState = Literal['within', 'outside', 'unmeasured']

MatchedSetVerdict = Literal[
    'matched_within_declared_tolerance',
    'matched_within_project_tolerance',
    'outlier_detected',
    'suspected_defect',
    'environment_dependent',
    'measurement_inconclusive',
    'no_population_tolerance_available',
    'insufficient_evidence',
]
"""Set-level outcome on the #628 §10 ladder."""


# ---------------------------------------------------------------------------
# Embedded models
# ---------------------------------------------------------------------------


class CadInstanceObservableValue(BaseModel):
    """One measured value for one observable — units are mandatory."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    band_hz: float | None = Field(default=None, gt=0.0)
    value: float
    unit: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'CadInstanceObservableValue':
        _require_finite(self.value, 'observable value')
        if self.band_hz is not None:
            _require_finite(self.band_hz, 'band_hz')
        return self


class CadInstanceObservableRecord(BaseModel):
    """One instance-specific observable with full provenance (#628 §3)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    observable: InstanceObservable
    method: str | None = Field(default=None, min_length=1)
    measurement_level_class: MeasurementLevelClass = 'unknown'
    domain: MeasurementDomain = 'unknown'
    environment_ref: AuthorityRef | None = None
    """#573 operating-state pin — temperature/RH/warmth binds to the
    observation; a temporary environmental shift is not a defect."""
    calibration_ref: AuthorityRef | None = None
    """#611 instrument/calibration pin."""
    validity_domain: str | None = Field(default=None, min_length=1)
    values: tuple[CadInstanceObservableValue, ...] = ()
    summary_value: float | None = None
    summary_unit: str | None = Field(default=None, min_length=1)
    environmental_confound: bool = False
    """The measurement is known to be confounded by environment —
    a confounded observation can never carry a defect claim."""
    note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'CadInstanceObservableRecord':
        for ref, label in (
            (self.environment_ref, 'environment_ref'),
            (self.calibration_ref, 'calibration_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        for value in self.values:
            if not isinstance(value, CadInstanceObservableValue):
                continue
        if self.summary_value is not None:
            _require_finite(self.summary_value, 'summary_value')
            if self.summary_unit is None:
                raise ValueError(
                    'a summary value requires its unit — a bare number '
                    'is not evidence'
                )
        if self.summary_unit is not None and self.summary_value is None:
            raise ValueError('summary_unit requires a summary_value')
        return self


class CadMatchedMetricVerdict(BaseModel):
    """One matched-set metric outcome — spread is the honest primitive."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    metric: MatchedMetric
    state: MatchedMetricState
    spread_value: float | None = None
    spread_unit: str | None = Field(default=None, min_length=1)
    limiting_instance_ids: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'CadMatchedMetricVerdict':
        if self.spread_value is not None:
            _require_finite(self.spread_value, 'spread_value')
            if self.spread_unit is None:
                raise ValueError('a spread value requires its unit')
        return self


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{
            sha_field: digest,
            id_field: _semantic_id(prefix, digest),
        },
    )


class CadInstanceAcousticEvidence(BaseModel):
    """Sealed evidence for one installed loudspeaker instance (#628 §2/§3).

    Pins the exact installed unit (#569 ``instance_id`` + ``semantic_sha256``)
    and optionally the model reference the unit inherits. The evidence level
    and domain are first-class — factory QC, lab measurement, in-room
    measurement and diagnostic checks are distinct tiers.
    """

    model_config = ConfigDict(frozen=True)

    evidence_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    instance_ref: AuthorityRef | None = None
    """Pin to the #569 InstalledEquipmentInstance — ``kind`` must be
    ``installed_instance``; may be omitted only for evidence that is not
    yet bound to a declared unit (e.g. imported factory QC before the
    serial is assigned), which the evaluator then treats as weaker."""
    model_ref: AuthorityRef | None = None
    """Pin to the model-level definition/dataset the instance inherits."""
    evidence_level: InstanceEvidenceLevel = 'unknown'
    evidence_source: EvidenceSource = 'instance'
    observables: tuple[CadInstanceObservableRecord, ...] = ()
    measurement_domain: MeasurementDomain = 'unknown'
    measured_at_utc: str | None = None
    authority_version: str = Field(
        default=INSTANCE_VARIATION_SCHEMA_VERSION, min_length=1
    )
    declared_at_utc: str = Field(min_length=1)
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'instance_ref': (
                self.instance_ref.model_dump(mode='json')
                if self.instance_ref is not None
                else None
            ),
            'model_ref': (
                self.model_ref.model_dump(mode='json')
                if self.model_ref is not None
                else None
            ),
            'evidence_level': self.evidence_level,
            'evidence_source': self.evidence_source,
            'observables': [
                obs.model_dump(mode='json') for obs in self.observables
            ],
            'measurement_domain': self.measurement_domain,
            'measured_at_utc': self.measured_at_utc,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'CadInstanceAcousticEvidence':
        _require_iso8601(self.declared_at_utc, 'evidence declared_at_utc')
        if self.measured_at_utc is not None:
            _require_iso8601(self.measured_at_utc, 'measured_at_utc')
        for ref, label in (
            (self.instance_ref, 'instance_ref'),
            (self.model_ref, 'model_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        if self.instance_ref is not None and (
            self.instance_ref.kind != 'installed_instance'
        ):
            raise ValueError(
                "instance_ref must pin an 'installed_instance' authority"
            )
        observable_names = [obs.observable for obs in self.observables]
        if len(observable_names) != len(set(observable_names)):
            raise ValueError('each observable may appear only once')
        instance_levels = {
            'instance_factory_qc',
            'instance_lab_measurement',
            'instance_in_room_measurement',
            'instance_diagnostic_check',
        }
        if self.evidence_level in instance_levels:
            if self.evidence_source != 'instance':
                raise ValueError(
                    'instance-level evidence must be instance-sourced — '
                    'model/golden data can never wear an instance level'
                )
            if not self.observables:
                raise ValueError(
                    'instance-level evidence requires at least one '
                    'observable — a level claim with no data is not '
                    'evidence'
                )
        if self.evidence_level in {
            'model_reference_only',
            'manufacturer_population_tolerance',
            'golden_sample_reference',
        } and self.evidence_source == 'instance':
            raise ValueError(
                'reference-level evidence cannot be instance-sourced — '
                'population/reference claims stay model-level truth'
            )
        if self.evidence_level == 'instance_in_room_measurement' and (
            self.measurement_domain == 'free_field'
        ):
            raise ValueError(
                'an in-room measurement cannot declare a free-field '
                'domain'
            )
        expected = _hash(self.identity_payload())
        if self.evidence_sha256 != expected:
            raise ValueError('instance evidence hash mismatch')
        if self.evidence_id != _semantic_id('instev', expected):
            raise ValueError('instance evidence id does not match its hash')
        return self


def instance_evidence_binding(
    evidence: CadInstanceAcousticEvidence,
) -> AuthorityRef:
    return AuthorityRef(
        kind='instance_acoustic_evidence',
        ref_id=evidence.evidence_id,
        ref_sha256=evidence.evidence_sha256,
    )


class CadModelToInstanceDelta(BaseModel):
    """Immutable derived model↔instance comparison (#628 §4).

    The delta is a derived artifact pinned to BOTH evidence records —
    the raw reference and instance measurements are never overwritten.
    Compatibility is enforced: a delta cannot correct a quantity the
    underlying evidence cannot carry.
    """

    model_config = ConfigDict(frozen=True)

    delta_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    reference_evidence_ref: AuthorityRef
    """The reference/golden/model evidence pin."""
    instance_evidence_ref: AuthorityRef
    """The instance evidence pin."""
    quantity: DeltaQuantity
    registration: str | None = Field(default=None, min_length=1)
    """How the two measurements were registered/normalized."""
    frequency_min_hz: float | None = Field(default=None, gt=0.0)
    frequency_max_hz: float | None = Field(default=None, gt=0.0)
    delta_summary: tuple[tuple[str, float], ...] = ()
    """Named summary deltas (e.g. ('on_axis_max_deviation_db', 0.4))."""
    max_delta_db: float | None = None
    mean_delta_db: float | None = None
    uncertainty_db: float | None = Field(default=None, ge=0.0)
    algorithm: str = Field(min_length=1)
    algorithm_version: str = Field(min_length=1)
    limitations: tuple[str, ...] = ()
    derived_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=INSTANCE_VARIATION_SCHEMA_VERSION, min_length=1
    )
    delta_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'reference_evidence_ref': (
                self.reference_evidence_ref.model_dump(mode='json')
            ),
            'instance_evidence_ref': (
                self.instance_evidence_ref.model_dump(mode='json')
            ),
            'quantity': self.quantity,
            'registration': self.registration,
            'frequency_min_hz': self.frequency_min_hz,
            'frequency_max_hz': self.frequency_max_hz,
            'delta_summary': [list(row) for row in self.delta_summary],
            'max_delta_db': self.max_delta_db,
            'mean_delta_db': self.mean_delta_db,
            'uncertainty_db': self.uncertainty_db,
            'algorithm': self.algorithm,
            'algorithm_version': self.algorithm_version,
            'limitations': list(self.limitations),
            'derived_at_utc': self.derived_at_utc,
            'authority_version': self.authority_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'CadModelToInstanceDelta':
        _require_iso8601(self.derived_at_utc, 'delta derived_at_utc')
        for ref, label in (
            (self.reference_evidence_ref, 'reference_evidence_ref'),
            (self.instance_evidence_ref, 'instance_evidence_ref'),
        ):
            if ref.kind != 'instance_acoustic_evidence':
                raise ValueError(
                    f'{label} must pin an instance_acoustic_evidence '
                    'authority'
                )
            if ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        for value, label in (
            (self.frequency_min_hz, 'frequency_min_hz'),
            (self.frequency_max_hz, 'frequency_max_hz'),
            (self.max_delta_db, 'max_delta_db'),
            (self.mean_delta_db, 'mean_delta_db'),
            (self.uncertainty_db, 'uncertainty_db'),
        ):
            if value is not None:
                _require_finite(value, label)
        if (
            self.frequency_min_hz is not None
            and self.frequency_max_hz is not None
            and self.frequency_max_hz <= self.frequency_min_hz
        ):
            raise ValueError(
                'frequency domain max must exceed min'
            )
        names = [name for name, _ in self.delta_summary]
        if len(names) != len(set(names)):
            raise ValueError('delta summary names must be unique')
        for _, value in self.delta_summary:
            _require_finite(value, 'delta summary value')
        expected = _hash(self.identity_payload())
        if self.delta_sha256 != expected:
            raise ValueError('model-to-instance delta hash mismatch')
        if self.delta_id != _semantic_id('instdelta', expected):
            raise ValueError('delta id does not match its hash')
        return self


def model_instance_delta_binding(
    delta: CadModelToInstanceDelta,
) -> AuthorityRef:
    return AuthorityRef(
        kind='model_instance_delta',
        ref_id=delta.delta_id,
        ref_sha256=delta.delta_sha256,
    )


def validate_delta_compatibility(
    *,
    delta: CadModelToInstanceDelta,
    reference: CadInstanceAcousticEvidence,
    instance: CadInstanceAcousticEvidence,
) -> None:
    """Enforce the #628 §8 adjustment boundary.

    ``ValueError`` when the delta claims a correction the evidence cannot
    physically carry: on-axis or in-room evidence never manufactures 3D
    directivity, and a low/medium-level measurement never establishes
    large-signal capability.
    """
    if delta.quantity == 'directivity_full_3d':
        if (
            reference.measurement_domain != 'free_field'
            or instance.measurement_domain != 'free_field'
        ):
            raise ValueError(
                'a full-3D directivity delta requires free-field '
                'evidence on both sides — on-axis or in-room deviation '
                'cannot manufacture directional truth'
            )
    if delta.quantity == 'large_signal_capability':
        for evidence, label in (
            (reference, 'reference'), (instance, 'instance')
        ):
            if any(
                obs.measurement_level_class != 'large_signal'
                for obs in evidence.observables
            ) or not evidence.observables:
                raise ValueError(
                    f'{label} evidence is not large-signal — a '
                    'low-level measurement cannot establish '
                    'large-signal capability'
                )
    if (
        delta.quantity == 'impedance_curve'
        and 'impedance_zf' not in [
            obs.observable for obs in instance.observables
        ]
    ):
        raise ValueError(
            'an impedance-curve delta requires Z(f) instance evidence'
        )


class CadMatchedSetDeclaration(BaseModel):
    """Explicit matched-set grouping (#628 §6/§7).

    L/R, L/C/R, surround/height arrays and multi-sub groups are declared
    memberships — each member keeps its own identity, no opaque shared
    score is implied.
    """

    model_config = ConfigDict(frozen=True)

    set_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    role: MatchedSetRole
    member_instance_refs: tuple[AuthorityRef, ...] = Field(min_length=2)
    """Every member's #569 pin — at least two; a "set" of one is not a
    matched set."""
    label: str | None = Field(default=None, min_length=1)
    authority_version: str = Field(
        default=INSTANCE_VARIATION_SCHEMA_VERSION, min_length=1
    )
    declared_at_utc: str = Field(min_length=1)
    set_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'role': self.role,
            'member_instance_refs': [
                ref.model_dump(mode='json')
                for ref in self.member_instance_refs
            ],
            'label': self.label,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'CadMatchedSetDeclaration':
        _require_iso8601(self.declared_at_utc, 'set declared_at_utc')
        ids = [ref.ref_id for ref in self.member_instance_refs]
        if len(ids) != len(set(ids)):
            raise ValueError('matched-set member instances must be unique')
        for ref in self.member_instance_refs:
            if ref.kind != 'installed_instance':
                raise ValueError(
                    "matched-set members must pin 'installed_instance' "
                    'authorities'
                )
            if ref.ref_sha256 is None:
                raise ValueError('member refs must pin their sha256')
        expected = _hash(self.identity_payload())
        if self.set_sha256 != expected:
            raise ValueError('matched set hash mismatch')
        if self.set_id != _semantic_id('matchset', expected):
            raise ValueError('matched set id does not match its hash')
        return self


def matched_set_binding(
    declaration: CadMatchedSetDeclaration,
) -> AuthorityRef:
    return AuthorityRef(
        kind='matched_set',
        ref_id=declaration.set_id,
        ref_sha256=declaration.set_sha256,
    )


class CadMatchedSetQualification(BaseModel):
    """Sealed matched-set verdict (#628 §10)."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    set_ref: AuthorityRef
    evidence_refs: tuple[AuthorityRef, ...] = ()
    metric_verdicts: tuple[CadMatchedMetricVerdict, ...] = ()
    verdict: MatchedSetVerdict
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'set_ref': self.set_ref.model_dump(mode='json'),
            'evidence_refs': [
                ref.model_dump(mode='json')
                for ref in self.evidence_refs
            ],
            'metric_verdicts': [
                verdict.model_dump(mode='json')
                for verdict in self.metric_verdicts
            ],
            'verdict': self.verdict,
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'CadMatchedSetQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        if self.set_ref.kind != 'matched_set':
            raise ValueError("set_ref must pin a 'matched_set' authority")
        if self.set_ref.ref_sha256 is None:
            raise ValueError('set ref must pin its sha256')
        for ref in self.evidence_refs:
            if ref.kind != 'instance_acoustic_evidence':
                raise ValueError(
                    'evidence_refs must pin instance acoustic evidence'
                )
            if ref.ref_sha256 is None:
                raise ValueError('evidence refs must pin their sha256')
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('matched-set qualification hash mismatch')
        if self.qualification_id != _semantic_id('setqual', expected):
            raise ValueError(
                'matched-set qualification id does not match its hash'
            )
        return self


def matched_set_qualification_binding(
    qualification: CadMatchedSetQualification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='matched_set_qualification',
        ref_id=qualification.qualification_id,
        ref_sha256=qualification.qualification_sha256,
    )


# ---------------------------------------------------------------------------
# Evaluation (#628 §10/§15)
# ---------------------------------------------------------------------------

_OBSERVABLE_FOR_METRIC: dict[MatchedMetric, InstanceObservable] = {
    'level_spread': 'sensitivity',
    'fr_deviation': 'frequency_response_deviation',
    'polarity_consistency': 'polarity',
    'impedance_spread': 'impedance_zf',
    'delay_spread': 'phase_group_delay',
    'nonlinear_spread': 'thd_nonlinear',
}


def _observable_summary(
    evidence: CadInstanceAcousticEvidence,
    observable: InstanceObservable,
) -> CadInstanceObservableRecord | None:
    for record in evidence.observables:
        if record.observable == observable:
            return record
    return None


def classify_instance_delta(
    *,
    delta: CadModelToInstanceDelta,
    tolerance_db: float | None = None,
    tolerance_source: Literal[
        'manufacturer_population', 'project', 'none'
    ] = 'none',
    environmental_confound: bool = False,
    suspected_defect: bool = False,
    confirmed_fault: bool = False,
    inconclusive: bool = False,
) -> DeltaClassification:
    """Classify one model↔instance delta (#628 §10).

    Tolerance is only ever bound explicitly — a missing tolerance yields
    ``no_population_tolerance_available``, never a guessed threshold.
    Environmental confounds route to ``environment_dependent`` rather
    than a permanent defect label.
    """
    if confirmed_fault:
        return 'confirmed_fault'
    if environmental_confound:
        return 'environment_dependent'
    if inconclusive or delta.max_delta_db is None:
        return 'measurement_inconclusive'
    if suspected_defect:
        return 'suspected_defect'
    if tolerance_db is None or tolerance_source == 'none':
        return 'no_population_tolerance_available'
    if delta.max_delta_db <= tolerance_db:
        if tolerance_source == 'manufacturer_population':
            return 'within_declared_tolerance'
        return 'matched_within_project_tolerance'
    return 'outlier'


def evaluate_matched_set(
    *,
    document_id: str,
    declaration: CadMatchedSetDeclaration,
    evidence_by_instance: dict[str, CadInstanceAcousticEvidence],
    tolerances: dict[MatchedMetric, tuple[float, str]] | None = None,
    tolerance_source: Literal[
        'manufacturer_population', 'project', 'none'
    ] = 'none',
    evaluated_at_utc: str | None = None,
) -> CadMatchedSetQualification:
    """Fail-closed matched-set verdict (#628 §6/§10/§15).

    ``tolerances`` maps a metric to ``(limit, unit)`` — supplied by the
    manufacturer population tolerance or the project profile, never
    invented here. Members missing instance evidence are unmeasured on
    every metric; a set with no instance evidence at all is
    ``insufficient_evidence``.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    tolerances = dict(tolerances or {})
    member_ids = [ref.ref_id for ref in declaration.member_instance_refs]
    bound_evidence = {
        instance_id: evidence_by_instance.get(instance_id)
        for instance_id in member_ids
    }
    missing = [
        instance_id
        for instance_id, evidence in bound_evidence.items()
        if evidence is None
    ]
    reasons: list[str] = []
    if missing:
        reasons.append(
            'instances without bound evidence: ' + ' / '.join(missing)
        )
    environment_confounded = any(
        evidence is not None
        and any(
            obs.environmental_confound for obs in evidence.observables
        )
        for evidence in bound_evidence.values()
    )

    metric_verdicts: list[CadMatchedMetricVerdict] = []
    any_outlier = False
    any_unmeasured = False
    metrics: tuple[MatchedMetric, ...] = (
        'level_spread',
        'fr_deviation',
        'polarity_consistency',
        'impedance_spread',
        'delay_spread',
        'nonlinear_spread',
    )
    for metric in metrics:
        observable = _OBSERVABLE_FOR_METRIC[metric]
        values: list[tuple[str, float, str]] = []
        for instance_id in member_ids:
            evidence = bound_evidence.get(instance_id)
            if evidence is None:
                continue
            record = _observable_summary(evidence, observable)
            if record is None or record.summary_value is None:
                continue
            values.append(
                (instance_id, record.summary_value, record.summary_unit or '')
            )
        limit = tolerances.get(metric)
        if len(values) < 2:
            metric_verdicts.append(
                CadMatchedMetricVerdict(
                    metric=metric,
                    state='unmeasured',
                )
            )
            any_unmeasured = True
            continue
        spread = max(v for _, v, _ in values) - min(v for _, v, _ in values)
        unit = values[0][2] or None
        center = median(v for _, v, _ in values)
        extreme = max(
            values, key=lambda row: abs(row[1] - center)
        )
        limiting = [extreme[0]] if limit and spread > limit[0] else []
        if limit is None:
            metric_verdicts.append(
                CadMatchedMetricVerdict(
                    metric=metric,
                    state='unmeasured',
                    spread_value=spread,
                    spread_unit=unit,
                    limiting_instance_ids=tuple(limiting),
                )
            )
            any_unmeasured = True
        elif spread <= limit[0]:
            metric_verdicts.append(
                CadMatchedMetricVerdict(
                    metric=metric,
                    state='within',
                    spread_value=spread,
                    spread_unit=unit or limit[1],
                )
            )
        else:
            any_outlier = True
            reasons.append(
                f'{metric}: spread {spread:.3g} exceeds tolerance '
                f'{limit[0]:.3g} (limiting: {", ".join(limiting) or "n/a"})'
            )
            metric_verdicts.append(
                CadMatchedMetricVerdict(
                    metric=metric,
                    state='outside',
                    spread_value=spread,
                    spread_unit=unit or limit[1],
                    limiting_instance_ids=tuple(limiting),
                )
            )

    if environment_confounded:
        verdict: MatchedSetVerdict = 'environment_dependent'
    elif all(evidence is None for evidence in bound_evidence.values()):
        verdict = 'insufficient_evidence'
    elif any_outlier:
        verdict = 'outlier_detected'
    elif any_unmeasured:
        verdict = 'no_population_tolerance_available'
    elif tolerance_source == 'manufacturer_population':
        verdict = 'matched_within_declared_tolerance'
    else:
        verdict = 'matched_within_project_tolerance'

    evidence_refs = [
        instance_evidence_binding(evidence)
        for evidence in bound_evidence.values()
        if evidence is not None
    ]
    return _seal(
        CadMatchedSetQualification,
        {
            'document_id': document_id,
            'set_ref': matched_set_binding(declaration).model_dump(
                mode='json'
            ),
            'evidence_refs': [
                ref.model_dump(mode='json') for ref in evidence_refs
            ],
            'metric_verdicts': [
                verdict.model_dump(mode='json')
                for verdict in metric_verdicts
            ],
            'verdict': verdict,
            'reasons': reasons,
            'evaluation_version': INSTANCE_VARIATION_EVALUATION_VERSION,
            'evaluated_at_utc': evaluated_at_utc,
        },
        'qualification_id',
        'qualification_sha256',
        'setqual',
    )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_instance_evidence(
    *,
    document_id: str,
    instance_ref: AuthorityRef | None = None,
    model_ref: AuthorityRef | None = None,
    evidence_level: InstanceEvidenceLevel = 'unknown',
    evidence_source: EvidenceSource = 'instance',
    observables: Sequence[CadInstanceObservableRecord] = (),
    measurement_domain: MeasurementDomain = 'unknown',
    measured_at_utc: str | None = None,
    declared_at_utc: str | None = None,
) -> CadInstanceAcousticEvidence:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        CadInstanceAcousticEvidence,
        {
            'document_id': document_id,
            'instance_ref': (
                instance_ref.model_dump(mode='json')
                if instance_ref is not None
                else None
            ),
            'model_ref': (
                model_ref.model_dump(mode='json')
                if model_ref is not None
                else None
            ),
            'evidence_level': evidence_level,
            'evidence_source': evidence_source,
            'observables': [
                obs.model_dump(mode='json')
                if isinstance(obs, CadInstanceObservableRecord)
                else obs
                for obs in observables
            ],
            'measurement_domain': measurement_domain,
            'measured_at_utc': measured_at_utc,
            'authority_version': INSTANCE_VARIATION_SCHEMA_VERSION,
            'declared_at_utc': declared_at_utc,
        },
        'evidence_id',
        'evidence_sha256',
        'instev',
    )


def build_model_instance_delta(
    *,
    document_id: str,
    reference_evidence: CadInstanceAcousticEvidence,
    instance_evidence: CadInstanceAcousticEvidence,
    quantity: DeltaQuantity,
    algorithm: str,
    algorithm_version: str,
    registration: str | None = None,
    frequency_min_hz: float | None = None,
    frequency_max_hz: float | None = None,
    delta_summary: Sequence[tuple[str, float]] = (),
    max_delta_db: float | None = None,
    mean_delta_db: float | None = None,
    uncertainty_db: float | None = None,
    limitations: Sequence[str] = (),
    derived_at_utc: str | None = None,
) -> CadModelToInstanceDelta:
    derived_at_utc = derived_at_utc or _utc_now()
    return _seal(
        CadModelToInstanceDelta,
        {
            'document_id': document_id,
            'reference_evidence_ref': (
                instance_evidence_binding(reference_evidence).model_dump(
                    mode='json'
                )
            ),
            'instance_evidence_ref': (
                instance_evidence_binding(instance_evidence).model_dump(
                    mode='json'
                )
            ),
            'quantity': quantity,
            'registration': registration,
            'frequency_min_hz': frequency_min_hz,
            'frequency_max_hz': frequency_max_hz,
            'delta_summary': [list(row) for row in delta_summary],
            'max_delta_db': max_delta_db,
            'mean_delta_db': mean_delta_db,
            'uncertainty_db': uncertainty_db,
            'algorithm': algorithm,
            'algorithm_version': algorithm_version,
            'limitations': list(limitations),
            'derived_at_utc': derived_at_utc,
            'authority_version': INSTANCE_VARIATION_SCHEMA_VERSION,
        },
        'delta_id',
        'delta_sha256',
        'instdelta',
    )


def build_matched_set(
    *,
    document_id: str,
    role: MatchedSetRole,
    member_instance_refs: Sequence[AuthorityRef],
    label: str | None = None,
    declared_at_utc: str | None = None,
) -> CadMatchedSetDeclaration:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        CadMatchedSetDeclaration,
        {
            'document_id': document_id,
            'role': role,
            'member_instance_refs': [
                ref.model_dump(mode='json') for ref in member_instance_refs
            ],
            'label': label,
            'authority_version': INSTANCE_VARIATION_SCHEMA_VERSION,
            'declared_at_utc': declared_at_utc,
        },
        'set_id',
        'set_sha256',
        'matchset',
    )


__all__ = [
    'CadInstanceAcousticEvidence',
    'CadInstanceObservableRecord',
    'CadInstanceObservableValue',
    'CadMatchedMetricVerdict',
    'CadMatchedSetDeclaration',
    'CadMatchedSetQualification',
    'CadModelToInstanceDelta',
    'DeltaClassification',
    'DeltaQuantity',
    'EvidenceSource',
    'INSTANCE_VARIATION_EVALUATION_VERSION',
    'INSTANCE_VARIATION_SCHEMA_VERSION',
    'InstanceEvidenceLevel',
    'InstanceObservable',
    'MatchedMetric',
    'MatchedMetricState',
    'MatchedSetRole',
    'MatchedSetVerdict',
    'MeasurementDomain',
    'MeasurementLevelClass',
    'build_instance_evidence',
    'build_matched_set',
    'build_model_instance_delta',
    'classify_instance_delta',
    'evaluate_matched_set',
    'instance_evidence_binding',
    'matched_set_binding',
    'matched_set_qualification_binding',
    'model_instance_delta_binding',
    'validate_delta_compatibility',
]
