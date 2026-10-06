"""Active multi-way loudspeaker crossover calibration authority (#665,
REV58-MEASELEC).

A crossover between woofer/midrange/tweeter ways inside *one* loudspeaker
system is a different engineering problem from the #574
main-speaker↔subwoofer bass-management splice: it needs its own routing,
filter, protection, acoustic alignment and recombination evidence before
any room correction (#568) may run. Trinnov's Altitude workflow treats
this as a dedicated pre-calibration stage for the same reason — a
multi-way speaker that is not internally coherent is not one acoustic
source, and a global optimizer asked to repair driver-level
routing/phase/time defects is fragile and can be unsafe.

- :class:`CadMultiwaySpeakerDefinition` — sealed identity of the
  loudspeaker as a multi-way source: the ways, their roles, the exact
  physical drivers, per-way amplifier channel and DSP output, polarity
  convention, and acoustic-origin evidence (#654).
- :class:`CadActiveCrossoverPlan` — sealed intended/deployed crossover:
  per-way HPF/LPF *topology* (family/order/frequency/Q/latency — never
  just ``crossover_hz``), mandatory protection filters, requested level /
  delay / DSP polarity per way, and the manufacturer operating envelope.
- :class:`CadDriverAlignmentMeasurement` — sealed per-way measured
  evidence binding #608 stimulus, #609 timebase, #611 calibration, mic
  position, level and measured delay/polarity.
- :class:`CadActiveCrossoverQualification` — the fail-closed verdict:
  routing proven, protection intact, per-way measured, adjacent-way
  splices evaluated from complex summation, recombined loudspeaker
  coherent — and only then ``room_correction_eligible``.

Honesty rules baked in:

- Routing is safety-critical: unproven DSP-output→amplifier→driver
  routing fails closed *before* any stimulus — full-range energy into a
  tweeter destroys hardware.
- A mandatory protection filter can never be removed by an optimizer
  proposal; the plan rejects overrides of protection-marked filters.
- Physical polarity, DSP inversion, crossover phase response and
  acoustic relative phase are four distinct quantities — a DSP
  inversion that compensates a wiring error never rewrites the wiring
  state as correct.
- Magnitude-only recombination is not complex summation: without
  relative phase/delay evidence a splice is ``inconclusive`` at best.
- A low-level alignment never supports max-output claims; an on-axis
  splice pass never implies off-axis coherence.
- Room correction is gated: ``room_correction_eligible`` is ``valid``
  only when the speaker is coherent as one source.

Literature / standards basis
----------------------------
- Trinnov Altitude/Optimizer documentation — Active Xover Calibration
  stage (kb.trinnov.com setup-calibration / active-crossover-calibration,
  current Altitude32 manual): per-way measurement, level/delay/polarity
  alignment up to four ways, recombined-response inspection, required
  before global calibration. HTDT stays provider-neutral: filter
  families remain generic (Bessel/LR/Butterworth/FIR), no vendor
  behavior is hard-coded.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


AXO_AUTHORITY_SCHEMA_VERSION = 'active-xover-1'
AXO_EVALUATION_VERSION = 'active-xover-eval-1'

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


def _require_positive(value: float, label: str) -> None:
    _require_finite(value, label)
    if value <= 0:
        raise ValueError(f'{label} must be positive')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


# ---------------------------------------------------------------------------
# Taxonomies (#665)
# ---------------------------------------------------------------------------

DriverWayRole = Literal[
    'sub_bass',
    'woofer',
    'low_mid',
    'mid',
    'high_mid',
    'tweeter',
    'super_tweeter',
    'custom',
]

PolarityConvention = Literal['normal', 'inverted', 'unknown']

FilterKind = Literal['hpf', 'lpf', 'band_pass', 'allpass', 'protection']

FilterFamily = Literal[
    'linkwitz_riley',
    'butterworth',
    'bessel',
    'chebyshev',
    'fir_linear_phase',
    'fir_other',
    'other',
    'unknown',
]

FilterTopology = Literal['iir', 'fir', 'analog', 'unknown']

FilterDomainTarget = Literal['electrical', 'acoustic', 'unknown']

RoutingProofMethod = Literal[
    'per_way_stimulus_identification',
    'device_readback',
    'documented_patchbay',
    'visual_physical_trace',
    'unknown',
]

SpliceVerdict = Literal[
    'coherent_sum',
    'cancellation_observed',
    'partial_overlap',
    'inconclusive',
    'unevaluated',
]

CrossoverQualificationState = Literal[
    'crossover_qualified',
    'qualified_with_limitations',
    'routing_unproven',
    'splice_incoherent',
    'protection_compromised',
    'deployed_state_mismatch',
    'unqualified_insufficient_evidence',
]

CrossoverCapability = Literal[
    'routing_verified',
    'protection_intact',
    'per_way_measured',
    'splice_coherent',
    'recombined_response_valid',
    'high_level_valid',
    'off_axis_valid',
    'room_correction_eligible',
]

ALL_CROSSOVER_CAPABILITIES: tuple[CrossoverCapability, ...] = (
    'routing_verified',
    'protection_intact',
    'per_way_measured',
    'splice_coherent',
    'recombined_response_valid',
    'high_level_valid',
    'off_axis_valid',
    'room_correction_eligible',
)

CapabilityState = Literal['valid', 'limited', 'invalid', 'unknown']


# ---------------------------------------------------------------------------
# Embedded evidence blocks
# ---------------------------------------------------------------------------


class CadDriverWay(BaseModel):
    """One driver way of a multi-way loudspeaker — label, role, the exact
    physical driver and the chain feeding it."""

    model_config = ConfigDict(frozen=True)

    way_label: str = Field(min_length=1)
    role: DriverWayRole
    driver_identity: str | None = None
    amplifier_channel: str | None = None
    dsp_output: str | None = None
    physical_polarity: PolarityConvention = 'unknown'
    dsp_polarity_inversion: bool | None = None

    @model_validator(mode='after')
    def valid_way(self) -> 'CadDriverWay':
        if self.driver_identity is None:
            raise ValueError(
                'each way must name its physical driver — role labels '
                'never replace driver evidence'
            )
        return self


class CadWayRoutingProof(BaseModel):
    """Evidence that DSP output → amplifier → driver way is proven.

    Routing is safety-critical: an unproven way fails closed before any
    stimulus is sent.
    """

    model_config = ConfigDict(frozen=True)

    way_label: str = Field(min_length=1)
    method: RoutingProofMethod
    verified: bool
    channel_identity_checked: bool = False
    polarity_checked: bool = False
    cross_route_absent: bool = False
    observed_at_utc: str | None = None
    detail: str | None = None

    @model_validator(mode='after')
    def valid_proof(self) -> 'CadWayRoutingProof':
        if self.observed_at_utc is not None:
            _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        if self.verified and not (
            self.channel_identity_checked
            and self.polarity_checked
            and self.cross_route_absent
        ):
            raise ValueError(
                'a verified routing proof requires channel identity, '
                'polarity and cross-route checks — partial proofs stay '
                'unverified'
            )
        if self.method == 'unknown' and self.verified:
            raise ValueError(
                'a routing proof with method unknown cannot be verified'
            )
        return self


class CadWayFilterSpec(BaseModel):
    """One filter in one way — full topology, never just ``Hz``."""

    model_config = ConfigDict(frozen=True)

    way_label: str = Field(min_length=1)
    filter_kind: FilterKind
    family: FilterFamily = 'unknown'
    order: int | None = None
    frequency_hz: float | None = None
    q: float | None = None
    domain_target: FilterDomainTarget = 'unknown'
    topology: FilterTopology = 'unknown'
    sample_rate_hz: float | None = None
    implementation_latency_ms: float | None = None
    phase_characteristic: str | None = None
    mandatory_protection: bool = False
    source: str | None = None

    @model_validator(mode='after')
    def valid_filter(self) -> 'CadWayFilterSpec':
        if self.frequency_hz is not None:
            _require_positive(self.frequency_hz, 'filter frequency_hz')
        if self.q is not None:
            _require_positive(self.q, 'filter q')
        if self.order is not None and self.order <= 0:
            raise ValueError('filter order must be positive')
        for label, value in (
            ('sample_rate_hz', self.sample_rate_hz),
            ('implementation_latency_ms', self.implementation_latency_ms),
        ):
            if value is not None:
                _require_positive(value, f'filter {label}')
        if self.filter_kind == 'protection' and not (
            self.mandatory_protection
        ):
            raise ValueError(
                'a protection-kind filter is mandatory by definition — '
                'set mandatory_protection=True'
            )
        if self.mandatory_protection and self.filter_kind not in (
            'hpf', 'protection',
        ):
            raise ValueError(
                'mandatory protection filters are HPF/protection kind — '
                'an LP cannot be the protective edge'
            )
        return self


class CadManufacturerEnvelope(BaseModel):
    """Manufacturer/designer operating envelope for one way — declared,
    never inferred from T/S parameters or cabinet size."""

    model_config = ConfigDict(frozen=True)

    way_label: str = Field(min_length=1)
    safe_low_hz: float | None = None
    safe_high_hz: float | None = None
    minimum_hpf_hz: float | None = None
    recommended_crossover_low_hz: float | None = None
    recommended_crossover_high_hz: float | None = None
    max_input: str | None = None
    thermal_excursion_limits: str | None = None
    source: str | None = None

    @model_validator(mode='after')
    def valid_envelope(self) -> 'CadManufacturerEnvelope':
        for label, value in (
            ('safe_low_hz', self.safe_low_hz),
            ('safe_high_hz', self.safe_high_hz),
            ('minimum_hpf_hz', self.minimum_hpf_hz),
            ('recommended_crossover_low_hz',
             self.recommended_crossover_low_hz),
            ('recommended_crossover_high_hz',
             self.recommended_crossover_high_hz),
        ):
            if value is not None:
                _require_positive(value, f'envelope {label}')
        if (
            self.safe_low_hz is not None
            and self.safe_high_hz is not None
            and self.safe_high_hz <= self.safe_low_hz
        ):
            raise ValueError('envelope safe band must be ascending')
        return self


class CadWayAlignment(BaseModel):
    """Requested/deployed per-way alignment: level, delay and DSP
    polarity — each kept distinct from physical wiring polarity."""

    model_config = ConfigDict(frozen=True)

    way_label: str = Field(min_length=1)
    requested_gain_db: float | None = None
    deployed_gain_db: float | None = None
    requested_delay_ms: float | None = None
    deployed_delay_ms: float | None = None
    dsp_polarity_inversion: bool = False
    alignment_reference_band: str | None = None

    @model_validator(mode='after')
    def valid_alignment(self) -> 'CadWayAlignment':
        for label, value in (
            ('requested_gain_db', self.requested_gain_db),
            ('deployed_gain_db', self.deployed_gain_db),
            ('requested_delay_ms', self.requested_delay_ms),
            ('deployed_delay_ms', self.deployed_delay_ms),
        ):
            if value is not None:
                _require_finite(value, f'way alignment {label}')
        for label, value in (
            ('requested_delay_ms', self.requested_delay_ms),
            ('deployed_delay_ms', self.deployed_delay_ms),
        ):
            if value is not None and value < 0:
                raise ValueError(f'way alignment {label} must be >= 0')
        return self


class CadSpliceAssessment(BaseModel):
    """Adjacent-way splice evaluation through one crossover region —
    from measured/modelled complex summation, never arithmetic dB
    addition alone."""

    model_config = ConfigDict(frozen=True)

    lower_way: str = Field(min_length=1)
    upper_way: str = Field(min_length=1)
    crossover_hz: float | None = None
    verdict: SpliceVerdict
    relative_delay_ms: float | None = None
    relative_phase_deg: float | None = None
    magnitude_only: bool = False
    on_axis_only: bool = True
    measurement_refs: tuple[AuthorityRef, ...] = ()
    detail: str | None = None

    @model_validator(mode='after')
    def valid_splice(self) -> 'CadSpliceAssessment':
        if self.crossover_hz is not None:
            _require_positive(self.crossover_hz, 'splice crossover_hz')
        for label, value in (
            ('relative_delay_ms', self.relative_delay_ms),
            ('relative_phase_deg', self.relative_phase_deg),
        ):
            if value is not None:
                _require_finite(value, f'splice {label}')
        for ref in self.measurement_refs:
            if ref.ref_sha256 is None:
                raise ValueError(
                    'splice measurement pins must carry sha256'
                )
        if self.verdict == 'coherent_sum' and (
            self.magnitude_only or not self.measurement_refs
        ):
            raise ValueError(
                'a coherent_sum verdict requires complex/phase evidence '
                'pins — arithmetic dB addition is not summation'
            )
        if self.verdict == 'unevaluated':
            raise ValueError(
                'an unevaluated splice must not be constructed — omit '
                'the assessment instead'
            )
        return self


# ---------------------------------------------------------------------------
# Sealed authorities
# ---------------------------------------------------------------------------


class CadMultiwaySpeakerDefinition(BaseModel):
    """Sealed identity of one loudspeaker as a multi-way active source.

    A multi-way speaker is never represented as several unrelated
    full-range loudspeakers — the ways are one acoustic source whose
    internal crossover is qualified before room correction.
    """

    model_config = ConfigDict(frozen=True)

    definition_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    speaker_instance: str = Field(min_length=1)
    ways: tuple[CadDriverWay, ...]
    acoustic_origin_ref: AuthorityRef | None = None
    manufacturer_source: str | None = None
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    provenance_json: str = '{}'
    definition_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_definition(self) -> 'CadMultiwaySpeakerDefinition':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        if len(self.ways) < 2:
            raise ValueError(
                'a multi-way definition requires at least two ways — a '
                'single-way speaker is a conventional passive loudspeaker'
            )
        labels = [w.way_label for w in self.ways]
        if len(labels) != len(set(labels)):
            raise ValueError('way labels must be unique')
        if self.acoustic_origin_ref is not None and (
            self.acoustic_origin_ref.ref_sha256 is None
        ):
            raise ValueError(
                'the #654 acoustic-origin pin must carry its sha256'
            )
        expected = _hash(self.identity_payload())
        if self.definition_sha256 != expected:
            raise ValueError('speaker definition hash mismatch')
        if self.definition_id != _semantic_id('axospk', expected):
            raise ValueError('definition id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'speaker_instance': self.speaker_instance,
            'ways': [w.model_dump(mode='json') for w in self.ways],
            'acoustic_origin_ref': (
                self.acoustic_origin_ref.model_dump(mode='json')
                if self.acoustic_origin_ref is not None
                else None
            ),
            'manufacturer_source': self.manufacturer_source,
            'declared_at_utc': self.declared_at_utc,
            'authority_version': self.authority_version,
            'provenance_json': self.provenance_json,
        }

    def way_labels(self) -> tuple[str, ...]:
        return tuple(w.way_label for w in self.ways)


def speaker_definition_binding(
    definition: CadMultiwaySpeakerDefinition,
) -> AuthorityRef:
    return AuthorityRef(
        kind='multiway_speaker_definition',
        ref_id=definition.definition_id,
        ref_sha256=definition.definition_sha256,
    )


class CadActiveCrossoverPlan(BaseModel):
    """Sealed crossover plan for one multi-way speaker: per-way filter
    topology, mandatory protection, and requested alignment.

    The plan is the *intended/deployed* filter set — a design worksheet
    never proves applied state; deployed-state mismatch is caught by the
    qualification when readback evidence exists (#592).
    """

    model_config = ConfigDict(frozen=True)

    plan_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    speaker_ref: AuthorityRef
    filter_specs: tuple[CadWayFilterSpec, ...] = ()
    alignments: tuple[CadWayAlignment, ...] = ()
    manufacturer_envelopes: tuple[CadManufacturerEnvelope, ...] = ()
    routing_proofs: tuple[CadWayRoutingProof, ...] = ()
    dsp_device: str | None = None
    dsp_firmware: str | None = None
    sample_rate_hz: float | None = None
    preset: str | None = None
    readback_ref: AuthorityRef | None = None
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    provenance_json: str = '{}'
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_plan(self) -> 'CadActiveCrossoverPlan':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        if self.speaker_ref.ref_sha256 is None:
            raise ValueError(
                'the speaker-definition pin must carry its sha256'
            )
        if self.sample_rate_hz is not None:
            _require_positive(self.sample_rate_hz, 'sample_rate_hz')
        if self.readback_ref is not None and (
            self.readback_ref.ref_sha256 is None
        ):
            raise ValueError(
                'the deployed-state readback pin must carry its sha256'
            )
        expected = _hash(self.identity_payload())
        if self.plan_sha256 != expected:
            raise ValueError('crossover plan hash mismatch')
        if self.plan_id != _semantic_id('axoplan', expected):
            raise ValueError('plan id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'speaker_ref': self.speaker_ref.model_dump(mode='json'),
            'filter_specs': [
                f.model_dump(mode='json') for f in self.filter_specs
            ],
            'alignments': [
                a.model_dump(mode='json') for a in self.alignments
            ],
            'manufacturer_envelopes': [
                e.model_dump(mode='json')
                for e in self.manufacturer_envelopes
            ],
            'routing_proofs': [
                p.model_dump(mode='json') for p in self.routing_proofs
            ],
            'dsp_device': self.dsp_device,
            'dsp_firmware': self.dsp_firmware,
            'sample_rate_hz': self.sample_rate_hz,
            'preset': self.preset,
            'readback_ref': (
                self.readback_ref.model_dump(mode='json')
                if self.readback_ref is not None
                else None
            ),
            'declared_at_utc': self.declared_at_utc,
            'authority_version': self.authority_version,
            'provenance_json': self.provenance_json,
        }

    def filters_for(self, way_label: str) -> tuple[CadWayFilterSpec, ...]:
        return tuple(
            f for f in self.filter_specs if f.way_label == way_label
        )

    def protection_for(
        self, way_label: str
    ) -> tuple[CadWayFilterSpec, ...]:
        return tuple(
            f
            for f in self.filter_specs
            if f.way_label == way_label and f.mandatory_protection
        )


def crossover_plan_binding(
    plan: CadActiveCrossoverPlan,
) -> AuthorityRef:
    return AuthorityRef(
        kind='active_crossover_plan',
        ref_id=plan.plan_id,
        ref_sha256=plan.plan_sha256,
    )


class CadDriverAlignmentMeasurement(BaseModel):
    """Sealed per-way measured evidence — each way independently, each
    bound to its exact stimulus/timebase/calibration/state."""

    model_config = ConfigDict(frozen=True)

    measurement_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    speaker_ref: AuthorityRef
    way_label: str = Field(min_length=1)
    stimulus_ref: AuthorityRef
    timebase_ref: AuthorityRef | None = None
    calibration_ref: AuthorityRef | None = None
    measchain_ref: AuthorityRef | None = None
    state_ref: AuthorityRef | None = None
    mic_position: str | None = None
    measured_level_db: float | None = None
    measured_delay_ms: float | None = None
    acoustic_polarity: PolarityConvention = 'unknown'
    response_artifact_ref: AuthorityRef | None = None
    level_band: str | None = None
    observed_at_utc: str | None = None
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    provenance_json: str = '{}'
    measurement_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_measurement(self) -> 'CadDriverAlignmentMeasurement':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        if self.observed_at_utc is not None:
            _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        if self.stimulus_ref.ref_sha256 is None:
            raise ValueError(
                'the #608 stimulus pin must carry its sha256'
            )
        for label, ref in (
            ('#609 timebase', self.timebase_ref),
            ('#611 calibration', self.calibration_ref),
            ('#695 measchain', self.measchain_ref),
            ('#573 state', self.state_ref),
            ('response artifact', self.response_artifact_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'the {label} pin must carry its sha256')
        for label, value in (
            ('measured_level_db', self.measured_level_db),
            ('measured_delay_ms', self.measured_delay_ms),
        ):
            if value is not None:
                _require_finite(value, f'alignment measurement {label}')
        expected = _hash(self.identity_payload())
        if self.measurement_sha256 != expected:
            raise ValueError('alignment measurement hash mismatch')
        if self.measurement_id != _semantic_id('axomeas', expected):
            raise ValueError('measurement id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'speaker_ref': self.speaker_ref.model_dump(mode='json'),
            'way_label': self.way_label,
            'stimulus_ref': self.stimulus_ref.model_dump(mode='json'),
            'timebase_ref': (
                self.timebase_ref.model_dump(mode='json')
                if self.timebase_ref is not None
                else None
            ),
            'calibration_ref': (
                self.calibration_ref.model_dump(mode='json')
                if self.calibration_ref is not None
                else None
            ),
            'measchain_ref': (
                self.measchain_ref.model_dump(mode='json')
                if self.measchain_ref is not None
                else None
            ),
            'state_ref': (
                self.state_ref.model_dump(mode='json')
                if self.state_ref is not None
                else None
            ),
            'mic_position': self.mic_position,
            'measured_level_db': self.measured_level_db,
            'measured_delay_ms': self.measured_delay_ms,
            'acoustic_polarity': self.acoustic_polarity,
            'response_artifact_ref': (
                self.response_artifact_ref.model_dump(mode='json')
                if self.response_artifact_ref is not None
                else None
            ),
            'level_band': self.level_band,
            'observed_at_utc': self.observed_at_utc,
            'declared_at_utc': self.declared_at_utc,
            'authority_version': self.authority_version,
            'provenance_json': self.provenance_json,
        }


def alignment_measurement_binding(
    measurement: CadDriverAlignmentMeasurement,
) -> AuthorityRef:
    return AuthorityRef(
        kind='driver_alignment_measurement',
        ref_id=measurement.measurement_id,
        ref_sha256=measurement.measurement_sha256,
    )


class CadActiveCrossoverQualification(BaseModel):
    """Sealed fail-closed verdict: is the internal crossover coherent
    enough for room correction to run against it?"""

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    speaker_ref: AuthorityRef
    plan_ref: AuthorityRef
    measurement_refs: tuple[AuthorityRef, ...] = ()
    splices: tuple[CadSpliceAssessment, ...] = ()
    recombined_measurement_ref: AuthorityRef | None = None
    high_level_ref: AuthorityRef | None = None
    off_axis_ref: AuthorityRef | None = None
    state: CrossoverQualificationState
    capabilities: tuple[tuple[CrossoverCapability, CapabilityState], ...]
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_qualification(self) -> 'CadActiveCrossoverQualification':
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        if self.speaker_ref.ref_sha256 is None or (
            self.plan_ref.ref_sha256 is None
        ):
            raise ValueError(
                'qualification pins must carry sha256'
            )
        for ref in self.measurement_refs:
            if ref.ref_sha256 is None:
                raise ValueError('measurement pins must carry sha256')
        for label, ref in (
            ('recombined measurement', self.recombined_measurement_ref),
            ('high-level evidence', self.high_level_ref),
            ('off-axis evidence', self.off_axis_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'the {label} pin must carry its sha256')
        covered = {capability for capability, _ in self.capabilities}
        if covered != set(ALL_CROSSOVER_CAPABILITIES):
            raise ValueError(
                'a crossover qualification must report every capability'
            )
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('qualification hash mismatch')
        if self.qualification_id != _semantic_id('axoqual', expected):
            raise ValueError('qualification id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'speaker_ref': self.speaker_ref.model_dump(mode='json'),
            'plan_ref': self.plan_ref.model_dump(mode='json'),
            'measurement_refs': [
                r.model_dump(mode='json') for r in self.measurement_refs
            ],
            'splices': [s.model_dump(mode='json') for s in self.splices],
            'recombined_measurement_ref': (
                self.recombined_measurement_ref.model_dump(mode='json')
                if self.recombined_measurement_ref is not None
                else None
            ),
            'high_level_ref': (
                self.high_level_ref.model_dump(mode='json')
                if self.high_level_ref is not None
                else None
            ),
            'off_axis_ref': (
                self.off_axis_ref.model_dump(mode='json')
                if self.off_axis_ref is not None
                else None
            ),
            'state': self.state,
            'capabilities': [list(item) for item in self.capabilities],
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    def capability_state(
        self, capability: CrossoverCapability
    ) -> CapabilityState:
        return dict(self.capabilities)[capability]


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


def build_multiway_speaker(
    *,
    document_id: str,
    speaker_instance: str,
    ways: tuple[CadDriverWay, ...] | list[CadDriverWay],
    acoustic_origin_ref: AuthorityRef | None = None,
    manufacturer_source: str | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadMultiwaySpeakerDefinition:
    """Seal a multi-way speaker definition (#665 §2)."""
    payload = dict(
        document_id=document_id,
        speaker_instance=speaker_instance,
        ways=tuple(ways),
        acoustic_origin_ref=acoustic_origin_ref,
        manufacturer_source=manufacturer_source,
        declared_at_utc=declared_at_utc or _utc_now(),
        authority_version=AXO_AUTHORITY_SCHEMA_VERSION,
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadMultiwaySpeakerDefinition, payload,
        'definition_id', 'definition_sha256', 'axospk',
    )


def build_crossover_plan(
    *,
    document_id: str,
    speaker_ref: AuthorityRef | CadMultiwaySpeakerDefinition,
    filter_specs: tuple[CadWayFilterSpec, ...]
    | list[CadWayFilterSpec] = (),
    alignments: tuple[CadWayAlignment, ...] | list[CadWayAlignment] = (),
    manufacturer_envelopes: tuple[CadManufacturerEnvelope, ...]
    | list[CadManufacturerEnvelope] = (),
    routing_proofs: tuple[CadWayRoutingProof, ...]
    | list[CadWayRoutingProof] = (),
    dsp_device: str | None = None,
    dsp_firmware: str | None = None,
    sample_rate_hz: float | None = None,
    preset: str | None = None,
    readback_ref: AuthorityRef | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadActiveCrossoverPlan:
    """Seal an active-crossover plan (#665 §5/§6/§8/§9/§10)."""
    if isinstance(speaker_ref, CadMultiwaySpeakerDefinition):
        speaker_ref = speaker_definition_binding(speaker_ref)
    payload = dict(
        document_id=document_id,
        speaker_ref=speaker_ref,
        filter_specs=tuple(filter_specs),
        alignments=tuple(alignments),
        manufacturer_envelopes=tuple(manufacturer_envelopes),
        routing_proofs=tuple(routing_proofs),
        dsp_device=dsp_device,
        dsp_firmware=dsp_firmware,
        sample_rate_hz=sample_rate_hz,
        preset=preset,
        readback_ref=readback_ref,
        declared_at_utc=declared_at_utc or _utc_now(),
        authority_version=AXO_AUTHORITY_SCHEMA_VERSION,
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadActiveCrossoverPlan, payload,
        'plan_id', 'plan_sha256', 'axoplan',
    )


def build_driver_alignment_measurement(
    *,
    document_id: str,
    speaker_ref: AuthorityRef | CadMultiwaySpeakerDefinition,
    way_label: str,
    stimulus_ref: AuthorityRef,
    timebase_ref: AuthorityRef | None = None,
    calibration_ref: AuthorityRef | None = None,
    measchain_ref: AuthorityRef | None = None,
    state_ref: AuthorityRef | None = None,
    mic_position: str | None = None,
    measured_level_db: float | None = None,
    measured_delay_ms: float | None = None,
    acoustic_polarity: PolarityConvention = 'unknown',
    response_artifact_ref: AuthorityRef | None = None,
    level_band: str | None = None,
    observed_at_utc: str | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadDriverAlignmentMeasurement:
    """Seal a per-way alignment measurement (#665 §7)."""
    if isinstance(speaker_ref, CadMultiwaySpeakerDefinition):
        speaker_ref = speaker_definition_binding(speaker_ref)
    payload = dict(
        document_id=document_id,
        speaker_ref=speaker_ref,
        way_label=way_label,
        stimulus_ref=stimulus_ref,
        timebase_ref=timebase_ref,
        calibration_ref=calibration_ref,
        measchain_ref=measchain_ref,
        state_ref=state_ref,
        mic_position=mic_position,
        measured_level_db=measured_level_db,
        measured_delay_ms=measured_delay_ms,
        acoustic_polarity=acoustic_polarity,
        response_artifact_ref=response_artifact_ref,
        level_band=level_band,
        observed_at_utc=observed_at_utc,
        declared_at_utc=declared_at_utc or _utc_now(),
        authority_version=AXO_AUTHORITY_SCHEMA_VERSION,
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadDriverAlignmentMeasurement, payload,
        'measurement_id', 'measurement_sha256', 'axomeas',
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_active_crossover(
    *,
    document_id: str,
    definition: CadMultiwaySpeakerDefinition,
    plan: CadActiveCrossoverPlan,
    measurements: tuple[CadDriverAlignmentMeasurement, ...]
    | list[CadDriverAlignmentMeasurement] = (),
    splices: tuple[CadSpliceAssessment, ...]
    | list[CadSpliceAssessment] = (),
    recombined_measurement_ref: AuthorityRef | None = None,
    high_level_ref: AuthorityRef | None = None,
    off_axis_ref: AuthorityRef | None = None,
    deployed_state_differs: bool = False,
    evaluated_at_utc: str | None = None,
) -> CadActiveCrossoverQualification:
    """Fail-closed crossover verdict — the room-correction gate.

    Ordering matters: unproven routing or compromised protection fails
    *before* splice coherence is even considered — energy into the wrong
    driver is a safety failure, not a measurement limitation.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    reasons: list[str] = []
    caps: dict[CrossoverCapability, CapabilityState] = {
        capability: 'unknown'
        for capability in ALL_CROSSOVER_CAPABILITIES
    }

    measurements = tuple(measurements)
    splices = tuple(splices)
    way_labels = definition.way_labels()
    measured_ways = {m.way_label for m in measurements}
    proofs = {p.way_label: p for p in plan.routing_proofs}
    unproven = [
        label
        for label in way_labels
        if label not in proofs or not proofs[label].verified
    ]
    unprotected = [
        label
        for label in way_labels
        if not plan.protection_for(label)
        and any(
            e.way_label == label and e.minimum_hpf_hz is not None
            for e in plan.manufacturer_envelopes
        )
    ]
    # Envelope HPF violations: a mandatory minimum HPF must be present
    # and at/above the declared minimum.
    envelope_violations: list[str] = []
    for env in plan.manufacturer_envelopes:
        if env.minimum_hpf_hz is None:
            continue
        hpfs = [
            f
            for f in plan.filters_for(env.way_label)
            if f.filter_kind in ('hpf', 'protection')
        ]
        if not hpfs:
            envelope_violations.append(env.way_label)
        elif all(
            (f.frequency_hz or 0.0) < env.minimum_hpf_hz for f in hpfs
        ):
            envelope_violations.append(env.way_label)

    # --- state ---------------------------------------------------------------
    if unproven:
        state: CrossoverQualificationState = 'routing_unproven'
        reasons.append(
            'unproven routing on ways: ' + ', '.join(unproven)
            + ' — full-range energy into the wrong driver is a safety '
            'failure, not a measurement limitation'
        )
    elif envelope_violations or unprotected:
        state = 'protection_compromised'
        if envelope_violations:
            reasons.append(
                'mandatory HPF below the manufacturer minimum on: '
                + ', '.join(sorted(set(envelope_violations)))
            )
        if unprotected:
            reasons.append(
                'declared manufacturer envelope requires protection on: '
                + ', '.join(sorted(unprotected))
            )
    elif deployed_state_differs:
        state = 'deployed_state_mismatch'
        reasons.append(
            'deployed DSP state differs from the plan — a design '
            'worksheet is not proof of applied state'
        )
    elif any(s.verdict == 'cancellation_observed' for s in splices):
        state = 'splice_incoherent'
        bad = [
            f'{s.lower_way}↔{s.upper_way}'
            for s in splices
            if s.verdict == 'cancellation_observed'
        ]
        reasons.append(
            'destructive summation at crossover pair(s): '
            + ', '.join(bad)
        )
    elif set(way_labels) - measured_ways:
        state = 'unqualified_insufficient_evidence'
        reasons.append(
            'unmeasured ways: '
            + ', '.join(sorted(set(way_labels) - measured_ways))
        )
    elif not splices:
        state = 'unqualified_insufficient_evidence'
        reasons.append('no adjacent-way splice evaluations bound')
    else:
        inconclusive = [
            s for s in splices if s.verdict == 'inconclusive'
        ]
        state = (
            'qualified_with_limitations'
            if inconclusive or recombined_measurement_ref is None
            else 'crossover_qualified'
        )
        if inconclusive:
            reasons.append('inconclusive splices remain')
        if recombined_measurement_ref is None:
            reasons.append(
                'no physically measured recombined-speaker response — '
                'modelled recombination is not the measurement'
            )

    # --- capability flags -------------------------------------------------------
    caps['routing_verified'] = 'invalid' if unproven else 'valid'
    caps['protection_intact'] = (
        'invalid' if (unprotected or envelope_violations) else 'valid'
    )
    caps['per_way_measured'] = (
        'valid' if set(way_labels) <= measured_ways else 'invalid'
    )
    if not splices:
        caps['splice_coherent'] = 'invalid'
    elif any(s.verdict == 'cancellation_observed' for s in splices):
        caps['splice_coherent'] = 'invalid'
    elif any(
        s.verdict in ('inconclusive', 'partial_overlap')
        or s.magnitude_only
        for s in splices
    ):
        caps['splice_coherent'] = 'limited'
    else:
        caps['splice_coherent'] = 'valid'
    caps['recombined_response_valid'] = (
        'valid'
        if recombined_measurement_ref is not None
        and state in ('crossover_qualified', 'qualified_with_limitations')
        else 'invalid'
    )
    caps['high_level_valid'] = (
        'valid'
        if high_level_ref is not None
        else ('invalid' if state == 'crossover_qualified' else 'limited')
    )
    if caps['high_level_valid'] != 'valid':
        reasons.append(
            'low-level alignment never supports max-output claims '
            'without high-level evidence'
        )
    caps['off_axis_valid'] = (
        'valid'
        if off_axis_ref is not None
        else (
            'limited'
            if splices and all(s.on_axis_only for s in splices)
            else 'unknown'
        )
    )
    if caps['off_axis_valid'] == 'limited':
        reasons.append(
            'splice evidence is on-axis only — off-axis/listening-area '
            'coherence is not implied'
        )
    caps['room_correction_eligible'] = (
        'valid'
        if state in ('crossover_qualified', 'qualified_with_limitations')
        and caps['routing_verified'] == 'valid'
        and caps['protection_intact'] == 'valid'
        else 'invalid'
    )
    if caps['room_correction_eligible'] != 'valid':
        reasons.append(
            'room correction (#568) is gated on a coherent loudspeaker '
            '— an optimizer never repairs an incoherent crossover'
        )

    payload = dict(
        document_id=document_id,
        speaker_ref=speaker_definition_binding(definition),
        plan_ref=crossover_plan_binding(plan),
        measurement_refs=tuple(
            alignment_measurement_binding(m) for m in measurements
        ),
        splices=splices,
        recombined_measurement_ref=recombined_measurement_ref,
        high_level_ref=high_level_ref,
        off_axis_ref=off_axis_ref,
        state=state,
        capabilities=tuple(
            (capability, caps[capability])
            for capability in ALL_CROSSOVER_CAPABILITIES
        ),
        reasons=tuple(reasons),
        evaluation_version=AXO_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadActiveCrossoverQualification, payload,
        'qualification_id', 'qualification_sha256', 'axoqual',
    )


__all__ = [
    'ALL_CROSSOVER_CAPABILITIES',
    'AXO_AUTHORITY_SCHEMA_VERSION',
    'AXO_EVALUATION_VERSION',
    'CadActiveCrossoverPlan',
    'CadActiveCrossoverQualification',
    'CadDriverAlignmentMeasurement',
    'CadDriverWay',
    'CadManufacturerEnvelope',
    'CadMultiwaySpeakerDefinition',
    'CadSpliceAssessment',
    'CadWayAlignment',
    'CadWayFilterSpec',
    'CadWayRoutingProof',
    'CapabilityState',
    'CrossoverCapability',
    'CrossoverQualificationState',
    'DriverWayRole',
    'FilterDomainTarget',
    'FilterFamily',
    'FilterKind',
    'FilterTopology',
    'PolarityConvention',
    'RoutingProofMethod',
    'SpliceVerdict',
    'alignment_measurement_binding',
    'build_crossover_plan',
    'build_driver_alignment_measurement',
    'build_multiway_speaker',
    'crossover_plan_binding',
    'evaluate_active_crossover',
    'speaker_definition_binding',
]
