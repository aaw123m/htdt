"""Playback dynamics / limiter authority (#649, REV58-MEASELEC).

A playback chain that is linear at low level may compress, limit or
derate at programme level — DRC, dialnorm, loudness compensation, peak
limiters, thermal protection all change what a measurement observes.
``cad_program_dynamics`` (#1036) already owns the *capability* profiles
for these mechanisms; this module owns the *measurement state*: which
processing was actually configured/observed during a capture, what a
level-sweep invariance test saw, and whether two states are comparable:

- :class:`CadDynamicsMechanismRecord` — one mechanism's typed state in a
  capture: the taxonomy kind (content-metadata DRC / dialnorm / device
  night mode / auto-volume / volume-dependent loudness / peak or
  lookahead limiter / compressor / protection / thermal derating /
  downmix overload / unknown), its state, the evidence class that backs
  it (configured / runtime readback / empirically observed / topology
  unknown), and any observed gain reduction.
- :class:`CadPlaybackDynamicsState` — the sealed state record bound to
  exact device/renderer/codec/firmware/preset/master-volume/
  output-mode/room-correction state plus declared content metadata.
- :class:`CadLevelSweepObservation` — the sealed transfer-invariance
  diagnostic: same stimulus/routing/state at multiple playback levels,
  with the verdict vocabulary that separates level-dependent gain,
  spectral change, suspected limiting and protection engagement — never
  a proprietary limiter identification from compression alone.
- :class:`CadPlaybackDynamicsQualification` — the fail-closed verdict
  binding a state (+ optional sweep + optional comparison baseline) to a
  measurement purpose; a before/after comparison against a different
  dynamics state returns ``state_mismatch`` and is ineligible for causal
  attribution (#573 consumes this).

Honesty rules baked in:

- Content metadata (dialnorm, DRC profile, loudness tags) and the
  decoder's *applied* behavior are separate fields — bitstream presence
  never proves runtime application.
- A low-level sweep and a reference-level sweep are different system
  states; low-volume transfer is never promoted to reference-level
  behavior without invariance evidence.
- Acoustic flattening is ``inferred`` compression until device
  readback/telemetry confirms a stage — an SPL plateau never names the
  limiter.
- Thermal/derating states are bound to elapsed playback time and stress
  history — a cold short-burst test is not sustained capability.
- Proprietary hidden processing stays ``processing_topology_unknown``
  — never reverse-engineered or invented.

Literature / standards basis
----------------------------
- Dolby metadata/DRC guidance (current 2026 publication): DRC and
  overload behavior differ with output/downmix mode; dialnorm gain
  shift is decoder-applied, not a channel-trim change.
- AES75-2023 / Music-Noise: programme-like output testing requires a
  stimulus the device processes as programme content — one static sweep
  never proves DRC behaviour.
- ITU-R BS.2493-1: dynamic-range-management context.
- ATSC A/85 / EBU dialnorm practice: decoder gain shift is not speaker
  trim — #618 reference calibration is never rewritten to compensate.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


DYN_AUTHORITY_SCHEMA_VERSION = 'playback-dynamics-1'
DYN_EVALUATION_VERSION = 'playback-dynamics-eval-1'

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
# Taxonomies (#649)
# ---------------------------------------------------------------------------

DynamicsProcessingKind = Literal[
    'content_metadata_drc',
    'dialogue_normalization',
    'device_night_mode_drc',
    'automatic_volume_leveling',
    'volume_loudness_compensation',
    'volume_dependent_eq',
    'peak_limiter',
    'lookahead_limiter',
    'compressor',
    'clip_overload_protection',
    'driver_protection',
    'thermal_power_derating',
    'downmix_overload_protection',
    'unknown_dynamic_processing',
]

DynamicsEvidenceClass = Literal[
    'configured_state_known',
    'runtime_readback_known',
    'behavior_empirically_observed',
    'processing_topology_unknown',
]

#: Evidence classes that carry *observed runtime* behavior, not merely
#: a configured state.
_RUNTIME_EVIDENCE: frozenset[DynamicsEvidenceClass] = frozenset(
    {'runtime_readback_known', 'behavior_empirically_observed'}
)

DynamicsMechanismState = Literal[
    'inactive',
    'active',
    'engaged_during_capture',
    'cannot_disable',
    'unknown',
]

#: States in which the mechanism plausibly altered the measurement.
_COMPRESSING_STATES: frozenset[DynamicsMechanismState] = frozenset(
    {'active', 'engaged_during_capture', 'cannot_disable'}
)

OutputModeClass = Literal[
    'native_multichannel',
    'stereo_downmix',
    'mono_render',
    'headphone_render',
    'other_format',
    'unknown',
]

LevelSweepVerdict = Literal[
    'linear_invariant_within_tested_range',
    'level_dependent_gain',
    'level_dependent_spectral_change',
    'limiter_compression_suspected',
    'protection_engaged',
    'unknown_stage',
    'not_performed',
]

NonlinearStageAttribution = Literal[
    'confirmed_stage',
    'supported_hypothesis',
    'unresolved',
    'unknown',
]

DynamicsPurpose = Literal[
    'fr_transfer',
    'absolute_level',
    'max_capability',
    'before_after_comparison',
    'crossover_qualification',
    'reference_level_set',
]

DynamicsQualificationState = Literal[
    'dynamics_state_controlled',
    'controlled_with_limitations',
    'hidden_processing_uncharacterized',
    'state_mismatch',
    'unqualified_insufficient_evidence',
]

PlaybackDynamicsCapability = Literal[
    'fr_transfer_valid',
    'absolute_level_valid',
    'max_output_valid',
    'comparison_eligible',
    'causal_attribution_valid',
    'dynamics_state_pinned',
]

ALL_DYNAMICS_CAPABILITIES: tuple[PlaybackDynamicsCapability, ...] = (
    'fr_transfer_valid',
    'absolute_level_valid',
    'max_output_valid',
    'comparison_eligible',
    'causal_attribution_valid',
    'dynamics_state_pinned',
)

CapabilityState = Literal['valid', 'limited', 'invalid', 'unknown']


# ---------------------------------------------------------------------------
# Embedded evidence blocks
# ---------------------------------------------------------------------------


class CadDynamicsMechanismRecord(BaseModel):
    """One dynamic-processing mechanism's state during a capture.

    ``state`` says what the mechanism did; ``evidence_class`` says how
    that is known — a configured-off with no runtime readback is honest,
    but it is weaker evidence than a readback.
    """

    model_config = ConfigDict(frozen=True)

    kind: DynamicsProcessingKind
    state: DynamicsMechanismState
    evidence_class: DynamicsEvidenceClass
    gain_reduction_db: float | None = None
    threshold_level: float | None = None
    affected_channel: str | None = None
    thermal_state: str | None = None
    observed_at_utc: str | None = None
    detail: str | None = None

    @model_validator(mode='after')
    def valid_record(self) -> 'CadDynamicsMechanismRecord':
        for label, value in (
            ('gain_reduction_db', self.gain_reduction_db),
            ('threshold_level', self.threshold_level),
        ):
            if value is not None:
                _require_finite(value, f'dynamics record {label}')
        if self.gain_reduction_db is not None and (
            self.gain_reduction_db < 0
        ):
            raise ValueError('gain_reduction_db must be non-negative')
        if self.observed_at_utc is not None:
            _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        if self.state == 'engaged_during_capture' and (
            self.evidence_class not in _RUNTIME_EVIDENCE
        ):
            raise ValueError(
                'engaged_during_capture requires runtime readback or '
                'empirical observation — a configured state never proves '
                'runtime engagement'
            )
        return self


class CadContentMetadata(BaseModel):
    """Declared content-side metadata — presence, separate from the
    decoder's applied behavior."""

    model_config = ConfigDict(frozen=True)

    dialnorm_db: float | None = None
    drc_profile: str | None = None
    loudness_metadata_present: bool | None = None
    peak_overload_metadata_present: bool | None = None
    decoder_configured_to_apply: bool | None = None
    applied_gain_shift_db: float | None = None
    codec_format: str | None = None

    @model_validator(mode='after')
    def valid_metadata(self) -> 'CadContentMetadata':
        for label, value in (
            ('dialnorm_db', self.dialnorm_db),
            ('applied_gain_shift_db', self.applied_gain_shift_db),
        ):
            if value is not None:
                _require_finite(value, f'content metadata {label}')
        return self


class CadLevelSweepPoint(BaseModel):
    """One playback level of a transfer-invariance sweep."""

    model_config = ConfigDict(frozen=True)

    level_dbfs: float | None = None
    level_spl_db: float | None = None
    normalized_residual_db: float | None = None
    gain_shift_db: float | None = None
    coherence: float | None = None
    distortion_percent: float | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def valid_point(self) -> 'CadLevelSweepPoint':
        for label, value in (
            ('level_dbfs', self.level_dbfs),
            ('level_spl_db', self.level_spl_db),
            ('normalized_residual_db', self.normalized_residual_db),
            ('gain_shift_db', self.gain_shift_db),
            ('coherence', self.coherence),
            ('distortion_percent', self.distortion_percent),
        ):
            if value is not None:
                _require_finite(value, f'level sweep {label}')
        if self.coherence is not None and not (0 <= self.coherence <= 1):
            raise ValueError('coherence must be within [0, 1]')
        if self.distortion_percent is not None and (
            self.distortion_percent < 0
        ):
            raise ValueError('distortion_percent must be non-negative')
        return self


# ---------------------------------------------------------------------------
# Sealed authorities
# ---------------------------------------------------------------------------


class CadPlaybackDynamicsState(BaseModel):
    """Sealed measurement-time dynamics state.

    Bound to the exact renderer/codec/firmware/preset/master-volume/
    output-mode/room-correction state plus declared content metadata —
    two captures under different mechanism states are *different*
    measurement states, and #573 must not silently compare them.
    """

    model_config = ConfigDict(frozen=True)

    state_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    device: str | None = None
    renderer_decoder: str | None = None
    codec_format: str | None = None
    metadata_profile: str | None = None
    firmware: str | None = None
    preset: str | None = None
    master_volume: str | None = None
    reference_volume_relation: str | None = None
    output_mode: OutputModeClass = 'unknown'
    channel_downmix_mode: str | None = None
    room_correction_state: str | None = None
    bass_management_state: str | None = None
    program_dynamics_profile_ref: AuthorityRef | None = None
    content_metadata: CadContentMetadata | None = None
    mechanisms: tuple[CadDynamicsMechanismRecord, ...] = ()
    elapsed_playback_seconds: float | None = None
    prior_stress: str | None = None
    recorded_at_utc: str | None = None
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    provenance_json: str = '{}'
    state_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_state(self) -> 'CadPlaybackDynamicsState':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        if self.recorded_at_utc is not None:
            _require_iso8601(self.recorded_at_utc, 'recorded_at_utc')
        if self.elapsed_playback_seconds is not None:
            _require_finite(
                self.elapsed_playback_seconds,
                'elapsed_playback_seconds',
            )
            if self.elapsed_playback_seconds < 0:
                raise ValueError(
                    'elapsed_playback_seconds must be non-negative'
                )
        if self.program_dynamics_profile_ref is not None and (
            self.program_dynamics_profile_ref.ref_sha256 is None
        ):
            raise ValueError(
                'the #1036 program-dynamics profile pin must carry sha256'
            )
        if self.content_metadata is not None and (
            self.content_metadata.applied_gain_shift_db is not None
            and self.content_metadata.decoder_configured_to_apply is not True
        ):
            raise ValueError(
                'an applied gain shift requires '
                'decoder_configured_to_apply=True — bitstream metadata '
                'alone never proves runtime application'
            )
        expected = _hash(self.identity_payload())
        if self.state_sha256 != expected:
            raise ValueError('dynamics state hash mismatch')
        if self.state_id != _semantic_id('dynstate', expected):
            raise ValueError('state id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'device': self.device,
            'renderer_decoder': self.renderer_decoder,
            'codec_format': self.codec_format,
            'metadata_profile': self.metadata_profile,
            'firmware': self.firmware,
            'preset': self.preset,
            'master_volume': self.master_volume,
            'reference_volume_relation': self.reference_volume_relation,
            'output_mode': self.output_mode,
            'channel_downmix_mode': self.channel_downmix_mode,
            'room_correction_state': self.room_correction_state,
            'bass_management_state': self.bass_management_state,
            'program_dynamics_profile_ref': (
                self.program_dynamics_profile_ref.model_dump(mode='json')
                if self.program_dynamics_profile_ref is not None
                else None
            ),
            'content_metadata': (
                self.content_metadata.model_dump(mode='json')
                if self.content_metadata is not None
                else None
            ),
            'mechanisms': [
                m.model_dump(mode='json') for m in self.mechanisms
            ],
            'elapsed_playback_seconds': self.elapsed_playback_seconds,
            'prior_stress': self.prior_stress,
            'recorded_at_utc': self.recorded_at_utc,
            'declared_at_utc': self.declared_at_utc,
            'authority_version': self.authority_version,
            'provenance_json': self.provenance_json,
        }

    def active_mechanisms(self) -> tuple[CadDynamicsMechanismRecord, ...]:
        """Mechanisms that plausibly altered the capture."""
        return tuple(
            m for m in self.mechanisms if m.state in _COMPRESSING_STATES
        )

    def state_signature(self) -> tuple[tuple[str, str, str], ...]:
        """Comparable (kind, state, evidence) signature for two states —
        used by the comparison gate."""
        return tuple(
            sorted(
                (m.kind, m.state, m.evidence_class)
                for m in self.mechanisms
            )
        )


def dynamics_state_binding(
    state: CadPlaybackDynamicsState,
) -> AuthorityRef:
    return AuthorityRef(
        kind='playback_dynamics_state',
        ref_id=state.state_id,
        ref_sha256=state.state_sha256,
    )


class CadLevelSweepObservation(BaseModel):
    """Sealed level-sweep / transfer-invariance diagnostic.

    Same stimulus family, same routing/device state, multiple playback
    levels — the verdict vocabulary keeps 'level-dependent' distinct from
    'limiter suspected' and 'protection engaged', and a stage attribution
    keeps 'confirmed' separate from 'hypothesis'.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    stimulus_ref: AuthorityRef
    dynamics_state_ref: AuthorityRef | None = None
    points: tuple[CadLevelSweepPoint, ...]
    verdict: LevelSweepVerdict
    stage_attribution: NonlinearStageAttribution = 'unknown'
    attributed_stage: str | None = None
    observed_at_utc: str | None = None
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    provenance_json: str = '{}'
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_observation(self) -> 'CadLevelSweepObservation':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        if self.observed_at_utc is not None:
            _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        if self.stimulus_ref.ref_sha256 is None:
            raise ValueError(
                'the #608 stimulus pin must carry its sha256'
            )
        if self.dynamics_state_ref is not None and (
            self.dynamics_state_ref.ref_sha256 is None
        ):
            raise ValueError(
                'the dynamics-state pin must carry its sha256'
            )
        if len(self.points) < 2:
            raise ValueError(
                'a level-sweep diagnostic requires at least two levels'
            )
        if self.verdict == 'not_performed':
            raise ValueError(
                'a level sweep with verdict not_performed must not be '
                'constructed — omit the observation instead'
            )
        if self.stage_attribution == 'confirmed_stage' and (
            self.attributed_stage is None
        ):
            raise ValueError(
                'a confirmed stage attribution must name the stage — '
                'SPL flattening alone is a hypothesis, never a stage'
            )
        expected = _hash(self.identity_payload())
        if self.observation_sha256 != expected:
            raise ValueError('level sweep hash mismatch')
        if self.observation_id != _semantic_id('dynobs', expected):
            raise ValueError('observation id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'stimulus_ref': self.stimulus_ref.model_dump(mode='json'),
            'dynamics_state_ref': (
                self.dynamics_state_ref.model_dump(mode='json')
                if self.dynamics_state_ref is not None
                else None
            ),
            'points': [p.model_dump(mode='json') for p in self.points],
            'verdict': self.verdict,
            'stage_attribution': self.stage_attribution,
            'attributed_stage': self.attributed_stage,
            'observed_at_utc': self.observed_at_utc,
            'declared_at_utc': self.declared_at_utc,
            'authority_version': self.authority_version,
            'provenance_json': self.provenance_json,
        }


def level_sweep_binding(
    observation: CadLevelSweepObservation,
) -> AuthorityRef:
    return AuthorityRef(
        kind='level_sweep_observation',
        ref_id=observation.observation_id,
        ref_sha256=observation.observation_sha256,
    )


class CadPlaybackDynamicsQualification(BaseModel):
    """Sealed fail-closed verdict for one measurement purpose under one
    dynamics state."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    dynamics_state_ref: AuthorityRef | None = None
    level_sweep_ref: AuthorityRef | None = None
    baseline_state_ref: AuthorityRef | None = None
    purpose: DynamicsPurpose
    state: DynamicsQualificationState
    capabilities: tuple[
        tuple[PlaybackDynamicsCapability, CapabilityState], ...
    ]
    state_mismatches: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_qualification(self) -> 'CadPlaybackDynamicsQualification':
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        for label, ref in (
            ('dynamics state', self.dynamics_state_ref),
            ('level sweep', self.level_sweep_ref),
            ('baseline state', self.baseline_state_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'the {label} pin must carry its sha256')
        covered = {capability for capability, _ in self.capabilities}
        if covered != set(ALL_DYNAMICS_CAPABILITIES):
            raise ValueError(
                'a dynamics qualification must report every capability'
            )
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('qualification hash mismatch')
        if self.qualification_id != _semantic_id('dynqual', expected):
            raise ValueError('qualification id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'dynamics_state_ref': (
                self.dynamics_state_ref.model_dump(mode='json')
                if self.dynamics_state_ref is not None
                else None
            ),
            'level_sweep_ref': (
                self.level_sweep_ref.model_dump(mode='json')
                if self.level_sweep_ref is not None
                else None
            ),
            'baseline_state_ref': (
                self.baseline_state_ref.model_dump(mode='json')
                if self.baseline_state_ref is not None
                else None
            ),
            'purpose': self.purpose,
            'state': self.state,
            'capabilities': [list(item) for item in self.capabilities],
            'state_mismatches': list(self.state_mismatches),
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    def capability_state(
        self, capability: PlaybackDynamicsCapability
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


def build_dynamics_state(
    *,
    document_id: str,
    device: str | None = None,
    renderer_decoder: str | None = None,
    codec_format: str | None = None,
    metadata_profile: str | None = None,
    firmware: str | None = None,
    preset: str | None = None,
    master_volume: str | None = None,
    reference_volume_relation: str | None = None,
    output_mode: OutputModeClass = 'unknown',
    channel_downmix_mode: str | None = None,
    room_correction_state: str | None = None,
    bass_management_state: str | None = None,
    program_dynamics_profile_ref: AuthorityRef | None = None,
    content_metadata: CadContentMetadata | None = None,
    mechanisms: tuple[CadDynamicsMechanismRecord, ...]
    | list[CadDynamicsMechanismRecord] = (),
    elapsed_playback_seconds: float | None = None,
    prior_stress: str | None = None,
    recorded_at_utc: str | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadPlaybackDynamicsState:
    """Seal a measurement-time dynamics state (#649 §2/§11)."""
    payload = dict(
        document_id=document_id,
        device=device,
        renderer_decoder=renderer_decoder,
        codec_format=codec_format,
        metadata_profile=metadata_profile,
        firmware=firmware,
        preset=preset,
        master_volume=master_volume,
        reference_volume_relation=reference_volume_relation,
        output_mode=output_mode,
        channel_downmix_mode=channel_downmix_mode,
        room_correction_state=room_correction_state,
        bass_management_state=bass_management_state,
        program_dynamics_profile_ref=program_dynamics_profile_ref,
        content_metadata=content_metadata,
        mechanisms=tuple(mechanisms),
        elapsed_playback_seconds=elapsed_playback_seconds,
        prior_stress=prior_stress,
        recorded_at_utc=recorded_at_utc,
        declared_at_utc=declared_at_utc or _utc_now(),
        authority_version=DYN_AUTHORITY_SCHEMA_VERSION,
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadPlaybackDynamicsState, payload,
        'state_id', 'state_sha256', 'dynstate',
    )


def build_level_sweep_observation(
    *,
    document_id: str,
    stimulus_ref: AuthorityRef,
    points: tuple[CadLevelSweepPoint, ...] | list[CadLevelSweepPoint],
    verdict: LevelSweepVerdict,
    dynamics_state_ref: AuthorityRef | None = None,
    stage_attribution: NonlinearStageAttribution = 'unknown',
    attributed_stage: str | None = None,
    observed_at_utc: str | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadLevelSweepObservation:
    """Seal a level-sweep transfer-invariance diagnostic (#649 §5)."""
    payload = dict(
        document_id=document_id,
        stimulus_ref=stimulus_ref,
        dynamics_state_ref=dynamics_state_ref,
        points=tuple(points),
        verdict=verdict,
        stage_attribution=stage_attribution,
        attributed_stage=attributed_stage,
        observed_at_utc=observed_at_utc,
        declared_at_utc=declared_at_utc or _utc_now(),
        authority_version=DYN_AUTHORITY_SCHEMA_VERSION,
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadLevelSweepObservation, payload,
        'observation_id', 'observation_sha256', 'dynobs',
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def dynamics_state_mismatches(
    state: CadPlaybackDynamicsState,
    baseline: CadPlaybackDynamicsState,
) -> tuple[str, ...]:
    """Mechanism-level differences between two dynamics states.

    A before/after comparison is only causal when every mechanism's
    state — and the state-carrying identity fields — agree.
    """
    diffs: list[str] = []
    mine = {m.kind: m for m in state.mechanisms}
    theirs = {m.kind: m for m in baseline.mechanisms}
    for kind in sorted(set(mine) | set(theirs)):
        a = mine.get(kind)
        b = theirs.get(kind)
        if a is None:
            diffs.append(f'{kind}:absent_in_current')
        elif b is None:
            diffs.append(f'{kind}:absent_in_baseline')
        elif a.state != b.state:
            diffs.append(f'{kind}:{b.state}->{a.state}')
    for field in (
        'device', 'renderer_decoder', 'codec_format', 'firmware',
        'preset', 'master_volume', 'output_mode',
        'channel_downmix_mode', 'room_correction_state',
        'bass_management_state',
    ):
        if getattr(state, field) != getattr(baseline, field):
            diffs.append(f'{field}:changed')
    return tuple(diffs)


def evaluate_playback_dynamics(
    *,
    document_id: str,
    dynamics_state: CadPlaybackDynamicsState | None,
    purpose: DynamicsPurpose,
    level_sweep: CadLevelSweepObservation | None = None,
    baseline_state: CadPlaybackDynamicsState | None = None,
    evaluated_at_utc: str | None = None,
) -> CadPlaybackDynamicsQualification:
    """Fail-closed dynamics verdict for one measurement purpose.

    A hidden/uncharacterized mechanism limits level-sensitive claims; a
    mismatched baseline makes before/after comparisons ineligible — #573
    consumes this verdict to classify state-mismatched campaigns.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    reasons: list[str] = []
    caps: dict[PlaybackDynamicsCapability, CapabilityState] = {
        capability: 'unknown'
        for capability in ALL_DYNAMICS_CAPABILITIES
    }
    mismatches: tuple[str, ...] = ()

    if dynamics_state is None:
        state: DynamicsQualificationState = (
            'unqualified_insufficient_evidence'
        )
        reasons.append('no dynamics state bound — hidden DSP never defaults to off')
        caps.update({c: 'invalid' for c in caps})
        caps['dynamics_state_pinned'] = 'invalid'
    else:
        active = dynamics_state.active_mechanisms()
        unknown_topology = any(
            m.evidence_class == 'processing_topology_unknown'
            for m in dynamics_state.mechanisms
        )
        unknown_states = [
            m for m in dynamics_state.mechanisms if m.state == 'unknown'
        ]
        sweep_verdict = (
            level_sweep.verdict if level_sweep is not None else None
        )
        level_dependent = sweep_verdict in (
            'level_dependent_gain',
            'level_dependent_spectral_change',
            'limiter_compression_suspected',
            'protection_engaged',
            'unknown_stage',
        )

        if purpose == 'before_after_comparison':
            if baseline_state is None:
                mismatches = ('baseline_state_missing',)
                reasons.append(
                    'a comparison requires the baseline dynamics state'
                )
            else:
                mismatches = dynamics_state_mismatches(
                    dynamics_state, baseline_state
                )
                if mismatches:
                    reasons.append(
                        'dynamics state differs between baseline and '
                        'current capture: ' + ', '.join(mismatches)
                    )

        caps['dynamics_state_pinned'] = (
            'valid' if dynamics_state.mechanisms else 'limited'
        )
        caps['fr_transfer_valid'] = (
            'invalid'
            if level_dependent
            and sweep_verdict in (
                'limiter_compression_suspected',
                'protection_engaged',
            )
            else (
                'limited' if (active or level_dependent) else 'valid'
            )
        )
        caps['absolute_level_valid'] = (
            'limited'
            if active or level_dependent or unknown_topology
            else 'valid'
        )
        caps['max_output_valid'] = (
            'invalid'
            if sweep_verdict in ('protection_engaged', 'unknown_stage')
            or any(m.state == 'cannot_disable' for m in active)
            else (
                'limited'
                if active or level_dependent or unknown_states
                else 'valid'
            )
        )
        if purpose == 'before_after_comparison':
            caps['comparison_eligible'] = (
                'invalid' if mismatches else 'valid'
            )
        else:
            caps['comparison_eligible'] = 'unknown'
        caps['causal_attribution_valid'] = (
            'valid'
            if (
                level_sweep is not None
                and level_sweep.stage_attribution == 'confirmed_stage'
                and not level_dependent
            )
            else (
                'invalid'
                if level_dependent and sweep_verdict != 'unknown_stage'
                or mismatches
                else 'limited'
            )
        )
        if level_sweep is not None and (
            level_sweep.stage_attribution == 'supported_hypothesis'
        ):
            caps['causal_attribution_valid'] = 'limited'
            reasons.append(
                'nonlinear stage attribution is a supported hypothesis, '
                'not a confirmed stage — an SPL plateau never names the '
                'limiter'
            )

        if mismatches:
            state = 'state_mismatch'
        elif unknown_topology and purpose in (
            'max_capability', 'absolute_level',
        ):
            state = 'hidden_processing_uncharacterized'
            reasons.append(
                'processing topology is unknown while a level-sensitive '
                'claim is requested'
            )
        elif (
            purpose == 'max_capability'
            and caps['max_output_valid'] == 'invalid'
        ):
            state = 'hidden_processing_uncharacterized'
        elif active or unknown_states or level_dependent or (
            caps['absolute_level_valid'] == 'limited'
        ):
            state = 'controlled_with_limitations'
        else:
            state = 'dynamics_state_controlled'

        if active:
            reasons.append(
                'active dynamics during capture: '
                + ', '.join(sorted({m.kind for m in active}))
            )
        if unknown_states:
            reasons.append(
                'mechanism states left unknown — assumed-off is not '
                'evidence: '
                + ', '.join(sorted({m.kind for m in unknown_states}))
            )

    payload = dict(
        document_id=document_id,
        dynamics_state_ref=(
            dynamics_state_binding(dynamics_state)
            if dynamics_state is not None
            else None
        ),
        level_sweep_ref=(
            level_sweep_binding(level_sweep)
            if level_sweep is not None
            else None
        ),
        baseline_state_ref=(
            dynamics_state_binding(baseline_state)
            if baseline_state is not None
            else None
        ),
        purpose=purpose,
        state=state,
        capabilities=tuple(
            (capability, caps[capability])
            for capability in ALL_DYNAMICS_CAPABILITIES
        ),
        state_mismatches=mismatches,
        reasons=tuple(reasons),
        evaluation_version=DYN_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadPlaybackDynamicsQualification, payload,
        'qualification_id', 'qualification_sha256', 'dynqual',
    )


__all__ = [
    'ALL_DYNAMICS_CAPABILITIES',
    'CadContentMetadata',
    'CadDynamicsMechanismRecord',
    'CadLevelSweepObservation',
    'CadLevelSweepPoint',
    'CadPlaybackDynamicsQualification',
    'CadPlaybackDynamicsState',
    'CapabilityState',
    'DYN_AUTHORITY_SCHEMA_VERSION',
    'DYN_EVALUATION_VERSION',
    'DynamicsEvidenceClass',
    'DynamicsMechanismState',
    'DynamicsProcessingKind',
    'DynamicsPurpose',
    'DynamicsQualificationState',
    'LevelSweepVerdict',
    'NonlinearStageAttribution',
    'OutputModeClass',
    'PlaybackDynamicsCapability',
    'build_dynamics_state',
    'build_level_sweep_observation',
    'dynamics_state_binding',
    'dynamics_state_mismatches',
    'evaluate_playback_dynamics',
    'level_sweep_binding',
]
