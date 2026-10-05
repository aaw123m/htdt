"""Measurement timebase / clock authority (#609, REV57-METRO).

A correct ``t=0`` is not enough when playback and capture run on
different — or drifting — sample clocks. This module makes the timing
basis of a measurement a sealed, honest authority:

- :class:`CadClockDomain` — one declared clock domain: which device /
  interface generated or sampled the samples, nominal vs effective rate,
  observed rate error, and a declared common-clock group. Two domains
  both labeled ``48 kHz`` are never assumed synchronous.
- :class:`CadMeasurementTimebase` — the capture-level authority binding
  playback/capture/per-channel domains, the declared synchronization
  topology (with its evidence basis), the timing reference that
  established ``t=0``, optional drift estimation/compensation records,
  and multi-input relative-timing evidence.
- :class:`CadTimebaseCapabilityAssessment` — the fail-closed verdict:
  which phase / delay / averaging / transfer claims this capture can
  honestly carry, plus derived duration-scaled timing uncertainty.

Honesty rules baked into the models and the evaluator:

- Matching nominal sample rates never imply a shared clock; shared host
  time never upgrades to shared audio sample clock.
- Strong topologies (``common_hardware_clock``, ``digitally_locked``,
  ``network_synced``) require a declared evidence basis — ``assumed``
  and ``unknown`` bases cannot carry them.
- Drift compensation is a derived artifact: the raw capture reference
  is mandatory and the compensated output is never a rewrite of raw
  evidence. The record composes with the #575 transformation DAG via an
  optional transform pin.
- An acoustic timing reference keeps its path semantics (reference
  speaker/path/receiver plus configured offset); it is never silently
  equivalent to an electrical loopback.
- ``no_timing_reference``/``post_hoc_estimated_t0`` can support
  magnitude and relative analysis but never absolute phase/delay.
- Sequential asynchronous captures stay magnitude-valid while vector /
  absolute-phase operations stay invalid or unknown.

Literature basis
----------------
- REW 5.40 timing-reference / clock-rate-adjustment / multi-input
  relative-timing semantics (roomeqwizard.com beta notes and help).
- Bryan, Kolar & Abel, "Impulse Response Measurements in the Presence
  of Clock Drift", AES 129th Convention Paper 8168 (2010): clock drift
  smears/distorts convolution-recovered impulse responses unless
  estimated and compensated.
- "Measuring Audio when Clocks Differ", AES E-Library: differing
  digital clocks create measurement distortion that can be mistaken
  for DUT harmonic/IMD/noise — the artifact must never be reclassified
  as a device defect.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


TIMEBASE_AUTHORITY_SCHEMA_VERSION = 'metro-tba-1'
TIMEBASE_EVALUATION_VERSION = 'metro-tba-eval-1'

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
# Taxonomies (#609)
# ---------------------------------------------------------------------------

ClockDomainKind = Literal[
    'playback_clock',
    'capture_clock',
    'input_channel_clock_domain',
    'external_word_clock',
    'digital_sync',
    'file_generator_timebase',
    'device_internal_clock',
    'unknown',
]

ClockSyncTopology = Literal[
    'common_hardware_clock',
    'digitally_locked',
    'shared_interface_unconfirmed',
    'independent_asynchronous',
    'file_playback_external_device',
    'network_synced',
    'unknown',
]

#: Topologies that can ever carry phase/delay claims; each still needs a
#: declared evidence basis — the label alone is not evidence.
SYNCHRONOUS_TOPOLOGIES: frozenset[ClockSyncTopology] = frozenset(
    {'common_hardware_clock', 'digitally_locked', 'network_synced'}
)

TopologyEvidenceBasis = Literal[
    'device_specification',
    'driver_report',
    'measured',
    'assumed',
    'unknown',
]

#: Evidence bases that can support a synchronous-topology claim.
STRONG_TOPOLOGY_BASES: frozenset[TopologyEvidenceBasis] = frozenset(
    {'device_specification', 'driver_report', 'measured'}
)

DriftEstimationMethod = Literal[
    'reference_channel_correlation',
    'sweep_trajectory_estimation',
    'known_pilot_or_timing_signal',
    'digital_loopback',
    'clock_device_telemetry',
    'cross_capture_alignment',
    'other_validated_method',
]

TimingReferenceKind = Literal[
    'wired_loopback_reference',
    'acoustic_timing_reference',
    'digital_reference',
    'known_trigger_or_pilot',
    'common_clock_only',
    'post_hoc_estimated_t0',
    'no_timing_reference',
    'unknown',
]

#: References whose t=0 is an actual measured edge on a capture.
_MACHINE_TIMING_REFERENCES: frozenset[TimingReferenceKind] = frozenset(
    {
        'wired_loopback_reference',
        'acoustic_timing_reference',
        'digital_reference',
        'known_trigger_or_pilot',
    }
)

TimebaseCapability = Literal[
    'magnitude_valid',
    'relative_phase_valid_within_capture',
    'absolute_phase_valid',
    'inter_channel_phase_valid',
    'absolute_delay_valid',
    'relative_delay_valid',
    'vector_averaging_eligible',
    'complex_transfer_eligible',
]

ALL_TIMEBASE_CAPABILITIES: tuple[TimebaseCapability, ...] = (
    'magnitude_valid',
    'relative_phase_valid_within_capture',
    'absolute_phase_valid',
    'inter_channel_phase_valid',
    'absolute_delay_valid',
    'relative_delay_valid',
    'vector_averaging_eligible',
    'complex_transfer_eligible',
)

CapabilityState = Literal['valid', 'limited', 'invalid', 'unknown']

SequentialTimingAnchor = Literal[
    'common_reference',
    'stable_loopback',
    'acoustic_reference',
    'post_hoc_alignment',
    'magnitude_only',
    'not_applicable',
    'unknown',
]


# ---------------------------------------------------------------------------
# Clock domains
# ---------------------------------------------------------------------------


class CadClockDomain(BaseModel):
    """One declared clock domain a measurement depends on.

    ``domain_kind`` names the role the clock plays (playback generator,
    capture ADC, a per-channel input domain, an external word-clock /
    digital sync feed, a file/generator timebase, or unknown).

    ``nominal_sample_rate_hz`` and ``effective_sample_rate_hz`` stay
    distinct: the nominal setting is what the device was asked for, the
    effective rate is what was observed — a drift estimate or telemetry
    figure, never silently re-derived.

    ``common_clock_group`` is a *declared* shared-clock identity: two
    domains carrying the same group string claim the same hardware
    clock. It is honest metadata about what the operator/driver
    reported — the topology's evidence basis still decides whether that
    claim can carry synchronous-timing conclusions.
    """

    model_config = ConfigDict(frozen=True)

    clock_domain_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    domain_kind: ClockDomainKind
    device_identity: str | None = None
    driver_backend: str | None = None
    nominal_sample_rate_hz: float | None = None
    effective_sample_rate_hz: float | None = None
    rate_error_ppm: float | None = None
    common_clock_group: str | None = None
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    clock_domain_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_domain(self) -> 'CadClockDomain':
        _require_iso8601(self.declared_at_utc, 'clock domain declared_at_utc')
        for label, value in (
            ('nominal_sample_rate_hz', self.nominal_sample_rate_hz),
            ('effective_sample_rate_hz', self.effective_sample_rate_hz),
        ):
            if value is not None:
                _require_finite(value, f'clock domain {label}')
                if value <= 0:
                    raise ValueError(f'clock domain {label} must be positive')
        if self.rate_error_ppm is not None:
            _require_finite(
                self.rate_error_ppm, 'clock domain rate_error_ppm'
            )
        expected = _hash(self.identity_payload())
        if self.clock_domain_sha256 != expected:
            raise ValueError('clock domain hash mismatch')
        if self.clock_domain_id != _semantic_id('clkdom', expected):
            raise ValueError('clock domain id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'domain_kind': self.domain_kind,
            'device_identity': self.device_identity,
            'driver_backend': self.driver_backend,
            'nominal_sample_rate_hz': self.nominal_sample_rate_hz,
            'effective_sample_rate_hz': self.effective_sample_rate_hz,
            'rate_error_ppm': self.rate_error_ppm,
            'common_clock_group': self.common_clock_group,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
            'document_id': self.document_id,
        }


def clock_domain_binding(domain: CadClockDomain) -> AuthorityRef:
    return AuthorityRef(
        kind='clock_domain',
        ref_id=domain.clock_domain_id,
        ref_sha256=domain.clock_domain_sha256,
    )


def clock_domain_rate_ratio(domain: CadClockDomain) -> float | None:
    """Observed effective/nominal ratio when both are declared."""
    if (
        domain.nominal_sample_rate_hz is None
        or domain.effective_sample_rate_hz is None
    ):
        return None
    return (
        domain.effective_sample_rate_hz / domain.nominal_sample_rate_hz
    )


# ---------------------------------------------------------------------------
# Drift estimation / compensation (embedded blocks)
# ---------------------------------------------------------------------------


class CadClockDriftEstimate(BaseModel):
    """A declared clock-drift estimation record.

    ``method`` must be one of the explicit estimation methods — a drift
    figure without a method is a guess, so the field is required and has
    no ``unknown`` member. ``rate_ratio`` is effective/nominal (1.0 =
    no drift); ``drift_ppm``/``uncertainty_ppm`` and the residual are
    kept as declared so the estimate's own confidence travels with it.
    """

    model_config = ConfigDict(frozen=True)

    method: DriftEstimationMethod
    algorithm_identity: str = Field(min_length=1)
    analysis_window_s: float | None = None
    rate_ratio: float | None = None
    drift_ppm: float | None = None
    uncertainty_ppm: float | None = None
    residual_timing_error_s: float | None = None
    confidence: Literal['high', 'medium', 'low', 'unknown'] = 'unknown'

    @model_validator(mode='after')
    def valid_estimate(self) -> 'CadClockDriftEstimate':
        for label, value in (
            ('analysis_window_s', self.analysis_window_s),
            ('rate_ratio', self.rate_ratio),
            ('drift_ppm', self.drift_ppm),
            ('uncertainty_ppm', self.uncertainty_ppm),
            ('residual_timing_error_s', self.residual_timing_error_s),
        ):
            if value is not None:
                _require_finite(value, f'drift estimate {label}')
        if self.rate_ratio is not None and self.rate_ratio <= 0:
            raise ValueError('drift estimate rate_ratio must be positive')
        if self.analysis_window_s is not None and self.analysis_window_s <= 0:
            raise ValueError(
                'drift estimate analysis_window_s must be positive'
            )
        if (
            self.uncertainty_ppm is not None
            and self.uncertainty_ppm < 0
        ):
            raise ValueError(
                'drift estimate uncertainty_ppm must be non-negative'
            )
        if (
            self.residual_timing_error_s is not None
            and self.residual_timing_error_s < 0
        ):
            raise ValueError(
                'drift estimate residual must be non-negative'
            )
        if self.rate_ratio is None and self.drift_ppm is None:
            raise ValueError(
                'a drift estimate requires rate_ratio or drift_ppm — '
                'a bare method label is not an estimate'
            )
        return self

    def effective_rate_ratio(self) -> float:
        if self.rate_ratio is not None:
            return float(self.rate_ratio)
        assert self.drift_ppm is not None
        return 1.0 + float(self.drift_ppm) / 1.0e6


class CadDriftCompensation(BaseModel):
    """A declared drift-compensation / resampling record.

    ``raw_capture_ref`` binds the immutable source artifact (measurement
    dataset, IR or managed capture asset); the corrected capture is a
    derived artifact whose output digest is recorded in
    ``output_artifact_sha256``. ``transform_ref`` optionally pins the
    #575 transformation-DAG node that produced it — the corrected
    capture never silently replaces raw evidence.
    """

    model_config = ConfigDict(frozen=True)

    raw_capture_ref: AuthorityRef
    estimated_clock_ratio: float
    resampler_identity: str = Field(min_length=1)
    resampler_version: str | None = None
    output_sample_rate_hz: float | None = None
    filter_parameters_json: str | None = None
    output_artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    transform_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def valid_compensation(self) -> 'CadDriftCompensation':
        _require_finite(
            self.estimated_clock_ratio,
            'compensation estimated_clock_ratio',
        )
        if self.estimated_clock_ratio <= 0:
            raise ValueError('compensation clock ratio must be positive')
        if self.output_sample_rate_hz is not None:
            _require_finite(
                self.output_sample_rate_hz,
                'compensation output_sample_rate_hz',
            )
            if self.output_sample_rate_hz <= 0:
                raise ValueError(
                    'compensation output rate must be positive'
                )
        if self.transform_ref is not None and (
            self.transform_ref.ref_sha256 is None
        ):
            raise ValueError(
                'a #575 transform pin requires the transform sha256'
            )
        return self


class CadTimingReferenceEvidence(BaseModel):
    """The timing reference that established ``t=0`` for a capture.

    ``kind`` keeps wired-loopback, acoustic, digital and post-hoc
    references distinct — they have different semantics:

    - ``acoustic_timing_reference`` passed through source → speaker →
      room → microphone → capture chain, so ``reference_path`` (which
      speaker/path/receiver) and ``configured_offset_s`` are mandatory.
    - ``post_hoc_estimated_t0`` is an estimate, never a measured edge —
      it can support relative work but never absolute claims.
    - ``common_clock_only`` preserves relative sample timing without
      establishing acoustic propagation ``t=0``.
    - ``no_timing_reference``/``unknown`` are honest absences.
    """

    model_config = ConfigDict(frozen=True)

    kind: TimingReferenceKind
    reference_channel: str | None = None
    expected_event: str | None = None
    detection_method: str | None = None
    threshold: str | None = None
    trim_offset_s: float | None = None
    configured_offset_s: float | None = None
    reference_path: str | None = None
    result: Literal['valid', 'failed', 'unknown'] = 'unknown'

    @model_validator(mode='after')
    def valid_reference(self) -> 'CadTimingReferenceEvidence':
        for label, value in (
            ('trim_offset_s', self.trim_offset_s),
            ('configured_offset_s', self.configured_offset_s),
        ):
            if value is not None:
                _require_finite(value, f'timing reference {label}')
        if self.kind == 'acoustic_timing_reference':
            if self.reference_path is None:
                raise ValueError(
                    'an acoustic timing reference must retain its '
                    'reference speaker/path/receiver identity'
                )
            if self.configured_offset_s is None:
                raise ValueError(
                    'an acoustic timing reference must carry its '
                    'configured path offset — it is never equivalent '
                    'to an electrical loopback'
                )
        if self.kind == 'wired_loopback_reference' and (
            self.reference_channel is None
        ):
            raise ValueError(
                'a wired loopback reference must name its reference '
                'channel'
            )
        return self


class CadChannelSyncSpec(BaseModel):
    """Multi-input relative-timing evidence for one capture.

    ``same_adc_clock`` is a tri-state: ``yes`` only when evidence shows
    the channels shared one ADC clock — a multi-channel interface label
    alone does not prove it without the declared basis. ``preserved``
    records whether relative timing across the inputs was explicitly
    kept (REW-style "preserve relative timing" semantics).
    """

    model_config = ConfigDict(frozen=True)

    channel_count: int = Field(ge=1)
    same_adc_clock: Literal['yes', 'no', 'unknown'] = 'unknown'
    per_channel_latency_s: tuple[float, ...] = ()
    max_skew_s: float | None = None
    alignment_method: str | None = None
    relative_timing_preserved: bool = False

    @model_validator(mode='after')
    def valid_sync(self) -> 'CadChannelSyncSpec':
        for value in self.per_channel_latency_s:
            _require_finite(value, 'per-channel latency')
        if self.max_skew_s is not None:
            _require_finite(self.max_skew_s, 'max_skew_s')
            if self.max_skew_s < 0:
                raise ValueError('max_skew_s must be non-negative')
        if self.per_channel_latency_s and (
            len(self.per_channel_latency_s) != self.channel_count
        ):
            raise ValueError(
                'per-channel latency count must equal channel_count'
            )
        return self


# ---------------------------------------------------------------------------
# Measurement timebase
# ---------------------------------------------------------------------------


class CadMeasurementTimebase(BaseModel):
    """The sealed timebase authority for one measurement capture.

    Binds the playback/capture/per-channel clock domains, the declared
    synchronization topology *and its evidence basis*, the timing
    reference that fixed ``t=0``, optional drift estimation and
    compensation records, multi-input sync evidence, and — for
    sequential workflows — what each capture anchored to.

    ``topology`` says what the relationship is; ``topology_evidence``
    says why we believe it. A strong topology on an ``assumed`` or
    ``unknown`` basis is rejected — shared nominal rates or a shared
    host clock are not evidence of a shared sample clock.
    """

    model_config = ConfigDict(frozen=True)

    timebase_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    playback_domain: AuthorityRef | None = None
    capture_domain: AuthorityRef | None = None
    channel_domains: tuple[AuthorityRef, ...] = ()
    topology: ClockSyncTopology
    topology_evidence: TopologyEvidenceBasis = 'unknown'
    stimulus_ref: AuthorityRef | None = None
    capture_duration_s: float | None = None
    drift_estimate: CadClockDriftEstimate | None = None
    drift_compensation: CadDriftCompensation | None = None
    timing_reference: CadTimingReferenceEvidence | None = None
    channel_sync: CadChannelSyncSpec | None = None
    sequential_anchor: SequentialTimingAnchor = 'unknown'
    requested_timing_resolution_s: float | None = None
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    timebase_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_timebase(self) -> 'CadMeasurementTimebase':
        _require_iso8601(
            self.declared_at_utc, 'timebase declared_at_utc'
        )
        if self.capture_duration_s is not None:
            _require_finite(
                self.capture_duration_s, 'timebase capture_duration_s'
            )
            if self.capture_duration_s <= 0:
                raise ValueError('capture_duration_s must be positive')
        if self.requested_timing_resolution_s is not None:
            _require_finite(
                self.requested_timing_resolution_s,
                'requested_timing_resolution_s',
            )
            if self.requested_timing_resolution_s <= 0:
                raise ValueError(
                    'requested_timing_resolution_s must be positive'
                )
        # A strong topology on a weak basis is a silent upgrade — refuse
        # it at the model boundary rather than at every consumer.
        if (
            self.topology in SYNCHRONOUS_TOPOLOGIES
            and self.topology_evidence not in STRONG_TOPOLOGY_BASES
        ):
            raise ValueError(
                f'topology {self.topology} requires a declared evidence '
                'basis (device_specification / driver_report / '
                'measured) — assumed or unknown bases cannot carry a '
                'synchronous-clock claim'
            )
        # Every bound domain ref must pin its sha — id-only refs can
        # silently degrade to "probably that one".
        for label, ref in (
            ('playback_domain', self.playback_domain),
            ('capture_domain', self.capture_domain),
            ('stimulus_ref', self.stimulus_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin the domain sha256')
        for ref in self.channel_domains:
            if ref.ref_sha256 is None:
                raise ValueError(
                    'channel domain refs must pin the domain sha256'
                )
        if self.drift_compensation is not None and (
            self.drift_estimate is None
            and self.drift_compensation.transform_ref is None
        ):
            # Compensation without an estimate is allowed only when it
            # pins the exact #575 transform that produced the output —
            # an unexplained ratio is an invisible fix.
            raise ValueError(
                'drift compensation requires the drift estimate or a '
                '#575 transform pin — an unexplained clock ratio is an '
                'invisible fix'
            )
        if self.topology == 'file_playback_external_device' and (
            self.stimulus_ref is None
        ):
            raise ValueError(
                'file-playback topology must pin the exact stimulus '
                'file — the stimulus and capture timebases jointly '
                'define deconvolution identity'
            )
        if self.channel_sync is not None and not self.channel_domains:
            raise ValueError(
                'channel_sync evidence requires bound channel_domains'
            )
        expected = _hash(self.identity_payload())
        if self.timebase_sha256 != expected:
            raise ValueError('timebase hash mismatch')
        if self.timebase_id != _semantic_id('mtbase', expected):
            raise ValueError('timebase id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'playback_domain': (
                self.playback_domain.model_dump(mode='json')
                if self.playback_domain is not None
                else None
            ),
            'capture_domain': (
                self.capture_domain.model_dump(mode='json')
                if self.capture_domain is not None
                else None
            ),
            'channel_domains': [
                ref.model_dump(mode='json') for ref in self.channel_domains
            ],
            'topology': self.topology,
            'topology_evidence': self.topology_evidence,
            'stimulus_ref': (
                self.stimulus_ref.model_dump(mode='json')
                if self.stimulus_ref is not None
                else None
            ),
            'capture_duration_s': self.capture_duration_s,
            'drift_estimate': (
                self.drift_estimate.model_dump(mode='json')
                if self.drift_estimate is not None
                else None
            ),
            'drift_compensation': (
                self.drift_compensation.model_dump(mode='json')
                if self.drift_compensation is not None
                else None
            ),
            'timing_reference': (
                self.timing_reference.model_dump(mode='json')
                if self.timing_reference is not None
                else None
            ),
            'channel_sync': (
                self.channel_sync.model_dump(mode='json')
                if self.channel_sync is not None
                else None
            ),
            'sequential_anchor': self.sequential_anchor,
            'requested_timing_resolution_s': (
                self.requested_timing_resolution_s
            ),
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def timebase_binding(timebase: CadMeasurementTimebase) -> AuthorityRef:
    return AuthorityRef(
        kind='measurement_timebase',
        ref_id=timebase.timebase_id,
        ref_sha256=timebase.timebase_sha256,
    )


# ---------------------------------------------------------------------------
# Capability assessment
# ---------------------------------------------------------------------------


class CadTimebaseCapabilityAssessment(BaseModel):
    """Sealed verdict: which timing-sensitive claims this capture
    supports.

    Every assessment reports all eight capabilities — an absent
    capability would silently read as "fine", so the state matrix is
    always complete. ``timing_uncertainty_s`` carries the derived
    duration-scaled uncertainty when it can be computed;
    ``drift_material`` records whether that uncertainty reaches the
    requested timing resolution — the assessment never hard-codes a
    universal ppm threshold independent of sweep length.
    """

    model_config = ConfigDict(frozen=True)

    assessment_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    timebase_ref: AuthorityRef
    capabilities: tuple[tuple[TimebaseCapability, CapabilityState], ...]
    timing_uncertainty_s: float | None = None
    drift_material: bool | None = None
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    assessment_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_assessment(self) -> 'CadTimebaseCapabilityAssessment':
        _require_iso8601(
            self.evaluated_at_utc, 'assessment evaluated_at_utc'
        )
        covered = {capability for capability, _ in self.capabilities}
        if covered != set(ALL_TIMEBASE_CAPABILITIES):
            raise ValueError(
                'a timebase assessment must report every capability — '
                'an omitted capability would silently read as valid'
            )
        if len(self.capabilities) != len(set(ALL_TIMEBASE_CAPABILITIES)):
            raise ValueError('duplicate capability entries')
        if self.timebase_ref.ref_sha256 is None:
            raise ValueError(
                'assessments must pin the timebase sha256'
            )
        if self.timing_uncertainty_s is not None:
            _require_finite(
                self.timing_uncertainty_s, 'timing_uncertainty_s'
            )
            if self.timing_uncertainty_s < 0:
                raise ValueError(
                    'timing_uncertainty_s must be non-negative'
                )
        expected = _hash(self.identity_payload())
        if self.assessment_sha256 != expected:
            raise ValueError('assessment hash mismatch')
        if self.assessment_id != _semantic_id('tbcap', expected):
            raise ValueError('assessment id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'timebase_ref': self.timebase_ref.model_dump(mode='json'),
            'capabilities': [list(item) for item in self.capabilities],
            'timing_uncertainty_s': self.timing_uncertainty_s,
            'drift_material': self.drift_material,
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    def capability_state(
        self, capability: TimebaseCapability
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


def build_clock_domain(
    *,
    document_id: str,
    domain_kind: ClockDomainKind,
    device_identity: str | None = None,
    driver_backend: str | None = None,
    nominal_sample_rate_hz: float | None = None,
    effective_sample_rate_hz: float | None = None,
    rate_error_ppm: float | None = None,
    common_clock_group: str | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadClockDomain:
    """Seal one declared clock domain."""
    payload = dict(
        document_id=document_id,
        domain_kind=domain_kind,
        device_identity=device_identity,
        driver_backend=driver_backend,
        nominal_sample_rate_hz=nominal_sample_rate_hz,
        effective_sample_rate_hz=effective_sample_rate_hz,
        rate_error_ppm=rate_error_ppm,
        common_clock_group=common_clock_group,
        authority_version=TIMEBASE_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadClockDomain, payload,
        'clock_domain_id', 'clock_domain_sha256', 'clkdom',
    )


def build_measurement_timebase(
    *,
    document_id: str,
    topology: ClockSyncTopology,
    topology_evidence: TopologyEvidenceBasis = 'unknown',
    playback_domain: CadClockDomain | AuthorityRef | None = None,
    capture_domain: CadClockDomain | AuthorityRef | None = None,
    channel_domains: tuple[CadClockDomain | AuthorityRef, ...] = (),
    stimulus_ref: AuthorityRef | None = None,
    capture_duration_s: float | None = None,
    drift_estimate: CadClockDriftEstimate | None = None,
    drift_compensation: CadDriftCompensation | None = None,
    timing_reference: CadTimingReferenceEvidence | None = None,
    channel_sync: CadChannelSyncSpec | None = None,
    sequential_anchor: SequentialTimingAnchor = 'unknown',
    requested_timing_resolution_s: float | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadMeasurementTimebase:
    """Seal the timebase authority for one capture."""

    def _ref(value):
        if value is None:
            return None
        if isinstance(value, CadClockDomain):
            return clock_domain_binding(value)
        return value

    payload = dict(
        document_id=document_id,
        playback_domain=_ref(playback_domain),
        capture_domain=_ref(capture_domain),
        channel_domains=tuple(_ref(v) for v in channel_domains),
        topology=topology,
        topology_evidence=topology_evidence,
        stimulus_ref=stimulus_ref,
        capture_duration_s=capture_duration_s,
        drift_estimate=drift_estimate,
        drift_compensation=drift_compensation,
        timing_reference=timing_reference,
        channel_sync=channel_sync,
        sequential_anchor=sequential_anchor,
        requested_timing_resolution_s=requested_timing_resolution_s,
        authority_version=TIMEBASE_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadMeasurementTimebase, payload,
        'timebase_id', 'timebase_sha256', 'mtbase',
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _derive_timing_uncertainty(
    timebase: CadMeasurementTimebase,
) -> float | None:
    """Duration-scaled timing uncertainty in seconds when derivable.

    Clock-rate error accumulates with capture duration: the recoverable
    timing error is ``|rate_ratio − 1| × duration`` plus any declared
    residual after compensation. No duration and no rate evidence means
    no honest number — never zero.
    """
    estimate = timebase.drift_estimate
    if (
        estimate is None
        or timebase.capture_duration_s is None
    ):
        if estimate is not None and (
            estimate.residual_timing_error_s is not None
        ):
            return float(estimate.residual_timing_error_s)
        return None
    accumulated = (
        abs(estimate.effective_rate_ratio() - 1.0)
        * timebase.capture_duration_s
    )
    if timebase.drift_compensation is not None:
        # A compensated capture keeps only the declared residual —
        # compensation corrects the systematic drift; what remains is
        # the estimator's own error.
        if estimate.residual_timing_error_s is not None:
            return float(estimate.residual_timing_error_s)
        return accumulated
    if estimate.residual_timing_error_s is not None:
        return accumulated + float(estimate.residual_timing_error_s)
    return accumulated


def evaluate_timebase_capability(
    *,
    document_id: str,
    timebase: CadMeasurementTimebase,
    capture_domain: CadClockDomain | None = None,
    evaluated_at_utc: str | None = None,
) -> CadTimebaseCapabilityAssessment:
    """Fail-closed timing-capability verdict for one capture.

    Capability derivation never upgrades on absence of evidence:
    ``unknown`` topology or reference states leave the corresponding
    capabilities ``unknown``; declared asynchronous topology leaves
    phase/vector capabilities ``invalid`` while magnitude stays
    ``valid`` — sequential asynchronous measurements remain useful for
    magnitude/statistical analysis.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    reasons: list[str] = []
    caps: dict[TimebaseCapability, CapabilityState] = {}

    topology = timebase.topology
    synchronous = topology in SYNCHRONOUS_TOPOLOGIES

    # --- magnitude ------------------------------------------------------
    # Magnitude/statistical use survives asynchronous clocks as long as
    # some capture clock evidence exists; with no capture-domain binding
    # at all there is nothing to grade — honest unknown.
    if timebase.capture_domain is not None or capture_domain is not None:
        caps['magnitude_valid'] = 'valid'
    else:
        caps['magnitude_valid'] = 'unknown'
        reasons.append(
            'no capture clock domain bound — magnitude claims unverifiable'
        )

    # --- within-capture relative phase ----------------------------------
    if timebase.channel_sync is not None:
        sync = timebase.channel_sync
        if sync.same_adc_clock == 'yes':
            caps['relative_phase_valid_within_capture'] = 'valid'
            caps['inter_channel_phase_valid'] = (
                'valid' if sync.relative_timing_preserved or (
                    sync.per_channel_latency_s or sync.alignment_method
                ) else 'limited'
            )
        elif sync.same_adc_clock == 'no':
            caps['relative_phase_valid_within_capture'] = 'invalid'
            caps['inter_channel_phase_valid'] = 'invalid'
            reasons.append(
                'channels on independent clocks — relative phase unknown'
            )
        else:
            caps['relative_phase_valid_within_capture'] = 'unknown'
            caps['inter_channel_phase_valid'] = 'unknown'
    elif timebase.channel_domains:
        caps['relative_phase_valid_within_capture'] = (
            'valid' if synchronous else 'unknown'
        )
        caps['inter_channel_phase_valid'] = (
            'valid' if synchronous else 'unknown'
        )
        if not synchronous:
            reasons.append(
                'multi-input capture without confirmed common ADC clock'
            )
    else:
        caps['relative_phase_valid_within_capture'] = (
            'valid' if synchronous else 'unknown'
        )
        caps['inter_channel_phase_valid'] = 'unknown'

    # --- t=0 / delay ----------------------------------------------------
    reference = timebase.timing_reference
    reference_kind = (
        reference.kind if reference is not None else 'no_timing_reference'
    )
    reference_valid = (
        reference is not None and reference.result == 'valid'
    )
    machine_t0 = (
        reference_valid and reference_kind in _MACHINE_TIMING_REFERENCES
    )

    if not synchronous:
        if topology in ('shared_interface_unconfirmed', 'unknown'):
            caps['absolute_phase_valid'] = 'unknown'
            caps['absolute_delay_valid'] = 'unknown'
            caps['relative_delay_valid'] = 'unknown'
        else:
            caps['absolute_phase_valid'] = 'invalid'
            caps['absolute_delay_valid'] = 'invalid'
            caps['relative_delay_valid'] = 'invalid'
            reasons.append(
                f'topology {topology} cannot carry phase/delay claims'
            )
    else:
        if machine_t0:
            caps['absolute_phase_valid'] = 'valid'
            caps['absolute_delay_valid'] = (
                'valid'
                if reference_kind
                in {
                    'wired_loopback_reference',
                    'digital_reference',
                    'known_trigger_or_pilot',
                }
                else 'limited'  # acoustic path adds its own propagation
            )
            caps['relative_delay_valid'] = 'valid'
        elif reference_kind == 'acoustic_timing_reference' and (
            reference is not None and reference.result != 'failed'
        ):
            # An acoustic reference that was not confirmed valid still
            # bounds relative delay via its declared path, never absolute.
            caps['absolute_phase_valid'] = 'unknown'
            caps['absolute_delay_valid'] = 'unknown'
            caps['relative_delay_valid'] = 'limited'
        elif reference_kind in ('common_clock_only',):
            caps['absolute_phase_valid'] = 'invalid'
            caps['absolute_delay_valid'] = 'invalid'
            caps['relative_delay_valid'] = 'valid'
            reasons.append(
                'common clock preserves relative sample timing without '
                'establishing acoustic propagation t=0'
            )
        elif reference_kind == 'post_hoc_estimated_t0':
            caps['absolute_phase_valid'] = 'invalid'
            caps['absolute_delay_valid'] = 'invalid'
            caps['relative_delay_valid'] = 'limited'
            reasons.append(
                'post-hoc estimated t=0 is an estimate, not a measured '
                'edge — no absolute timing claims'
            )
        else:
            caps['absolute_phase_valid'] = 'invalid'
            caps['absolute_delay_valid'] = 'invalid'
            caps['relative_delay_valid'] = 'limited' if synchronous else 'unknown'
            if reference_kind in ('no_timing_reference', 'unknown'):
                reasons.append(
                    'no timing reference — relative sample timing only'
                )
            else:
                reasons.append(
                    'timing reference not confirmed valid'
                )

    # --- drift materiality ----------------------------------------------
    uncertainty_s = _derive_timing_uncertainty(timebase)
    drift_material: bool | None = None
    if (
        uncertainty_s is not None
        and timebase.requested_timing_resolution_s is not None
    ):
        drift_material = (
            uncertainty_s >= timebase.requested_timing_resolution_s
        )
        if drift_material and timebase.drift_compensation is None:
            reasons.append(
                'accumulated clock drift reaches the requested timing '
                'resolution — uncompensated capture'
            )
            for capability in (
                'absolute_phase_valid',
                'absolute_delay_valid',
                'inter_channel_phase_valid',
            ):
                if caps[capability] == 'valid':
                    caps[capability] = 'limited'
    elif uncertainty_s is not None:
        drift_material = False

    # --- vector averaging / complex transfer ----------------------------
    inter_channel = caps['inter_channel_phase_valid']
    sync_spec = timebase.channel_sync
    if not synchronous:
        caps['vector_averaging_eligible'] = 'unknown' if topology in (
            'shared_interface_unconfirmed',
            'unknown',
        ) else 'invalid'
    elif sync_spec is not None and sync_spec.same_adc_clock == 'no':
        caps['vector_averaging_eligible'] = 'invalid'
    elif timebase.channel_domains or sync_spec is not None:
        # Multi-input capture: vector work across inputs requires
        # confirmed inter-channel phase.
        if inter_channel == 'valid':
            caps['vector_averaging_eligible'] = 'valid'
        elif inter_channel == 'unknown':
            caps['vector_averaging_eligible'] = 'unknown'
        else:
            caps['vector_averaging_eligible'] = 'invalid'
    else:
        # Single-input synchronous capture preserves complex values for
        # averaging and transfer work.
        caps['vector_averaging_eligible'] = 'valid'

    if synchronous and machine_t0 and not drift_material:
        caps['complex_transfer_eligible'] = 'valid'
    elif synchronous and machine_t0 and drift_material:
        caps['complex_transfer_eligible'] = 'limited'
    elif synchronous:
        caps['complex_transfer_eligible'] = 'limited' if (
            reference_kind not in ('no_timing_reference', 'unknown')
        ) else 'invalid'
    else:
        caps['complex_transfer_eligible'] = 'unknown' if topology in (
            'shared_interface_unconfirmed',
            'unknown',
        ) else 'invalid'

    # --- sequential anchoring -------------------------------------------
    if timebase.sequential_anchor == 'magnitude_only':
        for capability in (
            'absolute_phase_valid',
            'absolute_delay_valid',
            'vector_averaging_eligible',
            'complex_transfer_eligible',
        ):
            caps[capability] = 'invalid'
        reasons.append(
            'sequential magnitude-only workflow — phase/vector '
            'operations not eligible'
        )
    elif timebase.sequential_anchor == 'post_hoc_alignment':
        for capability in ('absolute_phase_valid', 'absolute_delay_valid'):
            caps[capability] = 'invalid'

    payload = dict(
        document_id=document_id,
        timebase_ref=timebase_binding(timebase),
        capabilities=tuple(
            (capability, caps[capability])
            for capability in ALL_TIMEBASE_CAPABILITIES
        ),
        timing_uncertainty_s=uncertainty_s,
        drift_material=drift_material,
        reasons=tuple(reasons),
        evaluation_version=TIMEBASE_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadTimebaseCapabilityAssessment, payload,
        'assessment_id', 'assessment_sha256', 'tbcap',
    )


__all__ = [
    'ALL_TIMEBASE_CAPABILITIES',
    'CadChannelSyncSpec',
    'CadClockDomain',
    'CadClockDriftEstimate',
    'CadDriftCompensation',
    'CadMeasurementTimebase',
    'CadTimebaseCapabilityAssessment',
    'CadTimingReferenceEvidence',
    'CapabilityState',
    'ClockDomainKind',
    'ClockSyncTopology',
    'DriftEstimationMethod',
    'SequentialTimingAnchor',
    'STRONG_TOPOLOGY_BASES',
    'SYNCHRONOUS_TOPOLOGIES',
    'TIMEBASE_AUTHORITY_SCHEMA_VERSION',
    'TIMEBASE_EVALUATION_VERSION',
    'TimebaseCapability',
    'TimingReferenceKind',
    'TopologyEvidenceBasis',
    'build_clock_domain',
    'build_measurement_timebase',
    'clock_domain_binding',
    'clock_domain_rate_ratio',
    'evaluate_timebase_capability',
    'timebase_binding',
]
