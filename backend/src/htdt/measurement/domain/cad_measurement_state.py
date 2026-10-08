"""Measurement-state snapshot & stability gate (#573, REV56-MEASEV).

A measurement is only comparable evidence when the room, the device and
the noise regime it was captured under are themselves declared — and
stable. This module records an immutable, versioned snapshot of the
measurement-time state and evaluates state comparability and
in-capture stationarity, fail-closed: unstable or unevidenced state is
never silently treated as comparable.

Literature basis
----------------
- Room impulse responses are time-varying in practice: Prawda, Schlecht
  & Välimäki (2024) and the earlier periodicity literature show
  reproducibility degrades exponentially with reflection order — air
  movement and thermal drift act on late-time and phase first.
- Sound speed in air follows c ≈ 331.3 + 0.606·T (Cramer 1993), so a
  few °C drift already moves timing/phase observables measurably while
  leaving magnitude almost untouched — the gate therefore invalidates
  phase/time comparability before magnitude.
- Environmental drift (temperature/humidity) and time-varying DSP
  (loudness, dynamic EQ, AGC, compressors/limiters, adaptive room
  correction — e.g. Dirac-style adaptive processing) silently contaminate
  measurement chains; each dynamic feature must be declared or honestly
  UNKNOWN, never assumed off.
- Repeat-capture stationarity replaces the myth of a universal
  stability threshold: per-observable tolerances are declared on the
  versioned policy and evaluated against bounded repeat sequences.

Design
------
- ``MeasurementStateSnapshot`` binds the measurement to the observed
  state: scene pins (revision + content hash), environment values
  (optionally backed by an ``AcousticEnvironmentProfile`` ref), openings,
  occupancy, dynamic DSP state, noise regime and thermal/level history.
  Absent fields are *unrecorded*, not "off".
- ``StateControlPolicy`` is a versioned campaign/calibration policy:
  allowed deltas, occupancy/opening requirements, required dynamic
  states, noise rule, warmup/repeat requirements and per-observable
  tolerances.
- ``evaluate_state_comparability`` produces the sealed
  ``StateComparabilityVerdict`` with per-domain comparability flags —
  environment change invalidates phase/time before magnitude — and
  ``evaluate_sequence_stationarity`` detects non-stationary captures
  from bounded repeat sequences.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...canonical_json import (
    canonical_sha256 as _hash,
    canonicalize_payload,
)
from .cad_measurement_models import CadFrequencyResponseDataset
from ...r120_geometry_compiler import ExactExternalAuthorityRef


MEASUREMENT_STATE_SCHEMA_VERSION = 'measev-mss-1'
STATE_GATE_VERSION = 'measev-mss-gate-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


OccupancyState = Literal[
    'unoccupied',
    'occupied_as_designed',
    'partial_occupancy',
    'temporary_obstacle_present',
    'unknown',
]

DynamicFeatureState = Literal['enabled', 'disabled', 'engaged', 'unknown']
"""``enabled`` = armed but not necessarily acting; ``engaged`` = actively
modifying the signal right now; ``unknown`` = honestly not recorded —
never assumed off."""

NoiseSourceState = Literal['on', 'off', 'unknown']

ThermalState = Literal['cold', 'warmed', 'hot', 'unknown']

OpeningObservationState = Literal[
    'open', 'closed', 'partially_open', 'unknown'
]


class EnvironmentObservation(BaseModel):
    """Observed environment values at capture time.

    ``source_ref`` may point at a sealed ``AcousticEnvironmentProfile``;
    the observed values are embedded regardless so the snapshot stands
    alone as evidence.
    """

    model_config = ConfigDict(frozen=True)

    temperature_c: float | None = None
    relative_humidity_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    air_pressure_pa: float | None = Field(default=None, gt=0.0)
    sound_speed_m_s: float | None = Field(default=None, gt=0.0)
    source: Literal['measured', 'estimated', 'declared', 'unknown'] = 'unknown'
    authority_ref: ExactExternalAuthorityRef | None = None

    @model_validator(mode='after')
    def finite(self) -> 'EnvironmentObservation':
        for value in (
            self.temperature_c,
            self.relative_humidity_percent,
            self.air_pressure_pa,
            self.sound_speed_m_s,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('environment values must be finite')
        return self


class OpeningObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    opening_id: str = Field(min_length=1)
    state: OpeningObservationState = 'unknown'


class DeviceDynamicState(BaseModel):
    """Time-varying playback/processing state at capture time.

    Every feature is explicit or ``unknown`` — never assumed off. This is
    the contamination class that silently decorates measurements
    (loudness/dynamic EQ/AGC/compressors/limiters/adaptive correction).
    """

    model_config = ConfigDict(frozen=True)

    playback_profile_ref: str | None = None
    playback_master_volume_db: float | None = None
    loudness: DynamicFeatureState = 'unknown'
    dynamic_eq: DynamicFeatureState = 'unknown'
    dynamic_volume_agc: DynamicFeatureState = 'unknown'
    compressor_limiter: DynamicFeatureState = 'unknown'
    adaptive_room_correction: DynamicFeatureState = 'unknown'
    dynamic_bass_management: DynamicFeatureState = 'unknown'
    protection_limiting: DynamicFeatureState = 'unknown'
    auto_source_level: DynamicFeatureState = 'unknown'
    limiter_engagement_observed: bool | None = None
    thermal_state: ThermalState = 'unknown'
    stimulus_level_db_spl: float | None = None
    stimulus_duration_s: float | None = None
    recent_stress_json: str = '{}'
    notes: str | None = None

    def feature_states(self) -> dict[str, DynamicFeatureState]:
        return {
            'loudness': self.loudness,
            'dynamic_eq': self.dynamic_eq,
            'dynamic_volume_agc': self.dynamic_volume_agc,
            'compressor_limiter': self.compressor_limiter,
            'adaptive_room_correction': self.adaptive_room_correction,
            'dynamic_bass_management': self.dynamic_bass_management,
            'protection_limiting': self.protection_limiting,
            'auto_source_level': self.auto_source_level,
        }

    @model_validator(mode='after')
    def finite(self) -> 'DeviceDynamicState':
        for value in (
            self.playback_master_volume_db,
            self.stimulus_level_db_spl,
            self.stimulus_duration_s,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('dynamic state values must be finite')
        return self


class NoiseRegime(BaseModel):
    """Acoustic noise regime during capture."""

    model_config = ConfigDict(frozen=True)

    hvac: NoiseSourceState = 'unknown'
    projector_fans: NoiseSourceState = 'unknown'
    traffic: NoiseSourceState = 'unknown'
    appliances: NoiseSourceState = 'unknown'
    occupants: NoiseSourceState = 'unknown'
    noise_floor_db_spl: float | None = None
    notes: str | None = None

    def source_states(self) -> dict[str, NoiseSourceState]:
        return {
            'hvac': self.hvac,
            'projector_fans': self.projector_fans,
            'traffic': self.traffic,
            'appliances': self.appliances,
            'occupants': self.occupants,
        }

    @model_validator(mode='after')
    def finite(self) -> 'NoiseRegime':
        if self.noise_floor_db_spl is not None and not isfinite(
            float(self.noise_floor_db_spl)
        ):
            raise ValueError('noise_floor_db_spl must be finite')
        return self


class MeasurementStateSnapshot(BaseModel):
    """Sealed record of the state under which one measurement was
    captured — static configuration pins (scene revision, openings,
    occupancy) alongside the dynamic state (environment, DSP, noise)."""

    model_config = ConfigDict(frozen=True)

    snapshot_id: str = Field(min_length=1)
    schema_version: str = MEASUREMENT_STATE_SCHEMA_VERSION
    document_id: str = Field(min_length=1)
    measurement_id: str = Field(min_length=1)
    measurement_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    sequence_index: int | None = Field(default=None, ge=0)
    observed_at_utc: str = Field(min_length=1)
    scene_revision_id: str | None = None
    scene_content_hash: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    environment: EnvironmentObservation | None = None
    operating_state_ref: ExactExternalAuthorityRef | None = None
    openings: tuple[OpeningObservation, ...] = ()
    occupancy: OccupancyState = 'unknown'
    occupancy_ref: ExactExternalAuthorityRef | None = None
    dynamic_state: DeviceDynamicState | None = None
    noise_regime: NoiseRegime | None = None
    notes: str = ''
    provenance_json: str = '{}'
    semantic_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'snapshot_id', 'semantic_sha256'}
        )

    @model_validator(mode='after')
    def consistent(self) -> 'MeasurementStateSnapshot':
        opening_ids = [o.opening_id for o in self.openings]
        if len(set(opening_ids)) != len(opening_ids):
            raise ValueError('opening observations must be unique per opening')
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('measurement state snapshot hash mismatch')
        expected_id = f'mss:{self.semantic_sha256}'
        if self.snapshot_id != expected_id:
            raise ValueError('snapshot_id must be mss:<semantic sha256>')
        return self


class OpeningRequirement(BaseModel):
    model_config = ConfigDict(frozen=True)

    opening_id: str = Field(min_length=1)
    required_state: Literal['open', 'closed']


class DynamicRequirement(BaseModel):
    """A feature the policy insists on — 'disabled' for qualification
    evidence (no dynamic DSP may decorate the measurement) or
    'enabled'/'engaged' when the campaign explicitly measures it."""

    model_config = ConfigDict(frozen=True)

    feature: str = Field(min_length=1)
    required_state: DynamicFeatureState


class StateControlPolicy(BaseModel):
    """Versioned campaign policy: what must be true and how much drift is
    tolerable per observable — declared, never a universal threshold."""

    model_config = ConfigDict(frozen=True)

    policy_id: str = Field(min_length=1)
    schema_version: str = MEASUREMENT_STATE_SCHEMA_VERSION
    document_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version_label: str | None = None
    allowed_temperature_delta_c: float | None = Field(default=None, gt=0.0)
    allowed_humidity_delta_percent: float | None = Field(default=None, gt=0.0)
    allowed_pressure_delta_pa: float | None = Field(default=None, gt=0.0)
    allowed_sound_speed_relative_delta: float | None = Field(
        default=None, gt=0.0
    )
    occupancy_policy: Literal[
        'must_match', 'must_be_unoccupied', 'declared_only', 'unrestricted'
    ] = 'declared_only'
    required_openings: tuple[OpeningRequirement, ...] = ()
    required_dynamic_states: tuple[DynamicRequirement, ...] = ()
    noise_rule: Literal[
        'must_match', 'floor_below_db', 'unrestricted'
    ] = 'unrestricted'
    noise_floor_max_db_spl: float | None = None
    warmup_min_s: float | None = Field(default=None, ge=0.0)
    required_repeat_captures: int | None = Field(default=None, ge=2)
    max_elapsed_s: float | None = Field(default=None, gt=0.0)
    magnitude_db_tolerance: float | None = Field(default=None, gt=0.0)
    phase_deg_tolerance: float | None = Field(default=None, gt=0.0)
    arrival_time_s_tolerance: float | None = Field(default=None, gt=0.0)
    level_drift_db_tolerance: float | None = Field(default=None, gt=0.0)
    created_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'policy_id', 'semantic_sha256'}
        )

    @model_validator(mode='after')
    def consistent(self) -> 'StateControlPolicy':
        for name, value in (
            ('allowed_temperature_delta_c', self.allowed_temperature_delta_c),
            (
                'allowed_humidity_delta_percent',
                self.allowed_humidity_delta_percent,
            ),
            ('allowed_pressure_delta_pa', self.allowed_pressure_delta_pa),
            (
                'allowed_sound_speed_relative_delta',
                self.allowed_sound_speed_relative_delta,
            ),
            ('noise_floor_max_db_spl', self.noise_floor_max_db_spl),
            ('warmup_min_s', self.warmup_min_s),
            ('max_elapsed_s', self.max_elapsed_s),
            ('magnitude_db_tolerance', self.magnitude_db_tolerance),
            ('phase_deg_tolerance', self.phase_deg_tolerance),
            ('arrival_time_s_tolerance', self.arrival_time_s_tolerance),
            ('level_drift_db_tolerance', self.level_drift_db_tolerance),
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError(f'{name} must be finite')
        if self.noise_rule == 'floor_below_db' and (
            self.noise_floor_max_db_spl is None
        ):
            raise ValueError(
                'floor_below_db noise rule requires noise_floor_max_db_spl'
            )
        opening_ids = [o.opening_id for o in self.required_openings]
        if len(set(opening_ids)) != len(opening_ids):
            raise ValueError('required openings must be unique')
        features = [d.feature for d in self.required_dynamic_states]
        if len(set(features)) != len(features):
            raise ValueError('required dynamic states must be unique')
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('state control policy hash mismatch')
        expected_id = f'spol:{self.semantic_sha256}'
        if self.policy_id != expected_id:
            raise ValueError('policy_id must be spol:<semantic sha256>')
        return self


StateComparabilityState = Literal[
    'comparable_stable_state',
    'comparable_with_declared_drift',
    'state_changed',
    'non_stationary_during_capture',
    'insufficient_state_evidence',
]

StateReasonCode = Literal[
    'environment_delta_exceeds',
    'occupancy_changed',
    'opening_state_changed',
    'opening_requirement_unmet',
    'dynamic_dsp_state_changed',
    'limiter_detected',
    'noise_regime_changed',
    'noise_floor_exceeds',
    'repeat_non_stationarity',
    'elapsed_interval_exceeds',
    'thermal_state_changed',
    'scene_content_changed',
    'geometry_or_position_changed',
    'unknown_device_state',
    'unknown_occupancy',
    'unknown_environment',
    'snapshot_missing',
]


class StateComparabilityVerdict(BaseModel):
    """Sealed verdict for a pair of snapshots (or a bounded repeat
    sequence) under one policy — fail-closed for comparison and
    calibration evidence."""

    model_config = ConfigDict(frozen=True)

    verdict_id: str = Field(min_length=1)
    schema_version: str = MEASUREMENT_STATE_SCHEMA_VERSION
    document_id: str = Field(min_length=1)
    subject_kind: Literal[
        'pair_comparability', 'sequence_stationarity', 'calibration_gate'
    ]
    baseline_snapshot_id: str | None = None
    baseline_snapshot_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    subject_snapshot_id: str | None = None
    subject_snapshot_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    subject_measurement_ids: tuple[str, ...] = ()
    policy_id: str | None = None
    policy_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    evaluator_version: str = STATE_GATE_VERSION
    state: StateComparabilityState
    reason_codes: tuple[StateReasonCode, ...] = ()
    magnitude_comparable: bool = True
    phase_comparable: bool = True
    timing_comparable: bool = True
    level_comparable: bool = True
    decay_comparable: bool = True
    distortion_comparable: bool = True
    details_json: str = '{}'
    created_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'verdict_id', 'semantic_sha256'}
        )

    @model_validator(mode='after')
    def consistent(self) -> 'StateComparabilityVerdict':
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('state comparability verdict hash mismatch')
        expected_id = f'msv:{self.semantic_sha256}'
        if self.verdict_id != expected_id:
            raise ValueError('verdict_id must be msv:<semantic sha256>')
        return self


def build_state_snapshot(**kwargs: Any) -> MeasurementStateSnapshot:
    payload = {'snapshot_id': '0' * 64, 'semantic_sha256': '0' * 64, **kwargs}
    provisional = MeasurementStateSnapshot.model_construct(
        **canonicalize_payload(MeasurementStateSnapshot, dict(**payload))
    )
    payload['semantic_sha256'] = _hash(provisional.identity_payload())
    payload['snapshot_id'] = f'mss:{payload["semantic_sha256"]}'
    return MeasurementStateSnapshot(**payload)


def build_state_control_policy(**kwargs: Any) -> StateControlPolicy:
    payload = {'policy_id': '0' * 64, 'semantic_sha256': '0' * 64, **kwargs}
    provisional = StateControlPolicy.model_construct(
        **canonicalize_payload(StateControlPolicy, dict(**payload))
    )
    payload['semantic_sha256'] = _hash(provisional.identity_payload())
    payload['policy_id'] = f'spol:{payload["semantic_sha256"]}'
    return StateControlPolicy(**payload)


def _verdict(
    *,
    document_id: str,
    subject_kind: str,
    state: StateComparabilityState,
    reason_codes: tuple[StateReasonCode, ...],
    baseline: MeasurementStateSnapshot | None,
    subject: MeasurementStateSnapshot | None,
    policy: StateControlPolicy | None,
    flags: dict[str, bool],
    details: dict[str, Any],
    subject_measurement_ids: tuple[str, ...] = (),
    created_at_utc: str,
) -> StateComparabilityVerdict:
    import json as _json

    payload: dict[str, Any] = {
        'verdict_id': '0' * 64,
        'schema_version': MEASUREMENT_STATE_SCHEMA_VERSION,
        'document_id': document_id,
        'subject_kind': subject_kind,
        'baseline_snapshot_id': None if baseline is None else baseline.snapshot_id,
        'baseline_snapshot_sha256': (
            None if baseline is None else baseline.semantic_sha256
        ),
        'subject_snapshot_id': None if subject is None else subject.snapshot_id,
        'subject_snapshot_sha256': (
            None if subject is None else subject.semantic_sha256
        ),
        'subject_measurement_ids': tuple(subject_measurement_ids),
        'policy_id': None if policy is None else policy.policy_id,
        'policy_sha256': None if policy is None else policy.semantic_sha256,
        'evaluator_version': STATE_GATE_VERSION,
        'state': state,
        'reason_codes': tuple(reason_codes),
        'magnitude_comparable': flags.get('magnitude_comparable', True),
        'phase_comparable': flags.get('phase_comparable', True),
        'timing_comparable': flags.get('timing_comparable', True),
        'level_comparable': flags.get('level_comparable', True),
        'decay_comparable': flags.get('decay_comparable', True),
        'distortion_comparable': flags.get('distortion_comparable', True),
        'details_json': _json.dumps(
            details, sort_keys=True, separators=(',', ':')
        ),
        'created_at_utc': created_at_utc,
        'semantic_sha256': '0' * 64,
    }
    provisional = StateComparabilityVerdict.model_construct(
        **canonicalize_payload(StateComparabilityVerdict, dict(**payload))
    )
    payload['semantic_sha256'] = _hash(provisional.identity_payload())
    payload['verdict_id'] = f'msv:{payload["semantic_sha256"]}'
    return StateComparabilityVerdict(**payload)


def evaluate_state_comparability(
    baseline: MeasurementStateSnapshot | None,
    subject: MeasurementStateSnapshot | None,
    policy: StateControlPolicy | None,
    *,
    document_id: str,
    created_at_utc: str,
    elapsed_s: float | None = None,
) -> StateComparabilityVerdict:
    """Compare the state under which two measurements were captured.

    Fail-closed: missing snapshots or policy-required unknown state yield
    ``insufficient_state_evidence``; changed dynamic DSP, limiter
    engagement, occupancy, openings or scene content yield
    ``state_changed``; environment deltas invalidate phase/timing before
    magnitude (sound-speed drift acts on time observables first).
    """
    if baseline is None or subject is None:
        return _verdict(
            document_id=document_id,
            subject_kind='pair_comparability',
            state='insufficient_state_evidence',
            reason_codes=('snapshot_missing',),
            baseline=baseline,
            subject=subject,
            policy=policy,
            flags={
                'magnitude_comparable': False,
                'phase_comparable': False,
                'timing_comparable': False,
                'level_comparable': False,
                'decay_comparable': False,
                'distortion_comparable': False,
            },
            details={},
            created_at_utc=created_at_utc,
        )

    flags = {
        'magnitude_comparable': True,
        'phase_comparable': True,
        'timing_comparable': True,
        'level_comparable': True,
        'decay_comparable': True,
        'distortion_comparable': True,
    }
    hard_reasons: list[StateReasonCode] = []
    drift_reasons: list[StateReasonCode] = []
    unknown_reasons: list[StateReasonCode] = []
    details: dict[str, Any] = {}

    # static geometry / scene pins
    if (
        baseline.scene_content_hash is not None
        and subject.scene_content_hash is not None
        and baseline.scene_content_hash != subject.scene_content_hash
    ):
        hard_reasons.append('scene_content_changed')
        flags['magnitude_comparable'] = False
        flags['phase_comparable'] = False
        flags['timing_comparable'] = False
        flags['decay_comparable'] = False

    # environment — sound speed hits timing/phase before magnitude
    env_a, env_b = baseline.environment, subject.environment
    if env_a is not None and env_b is not None:
        env_detail: dict[str, float] = {}
        if (
            policy is not None
            and policy.allowed_temperature_delta_c is not None
            and env_a.temperature_c is not None
            and env_b.temperature_c is not None
        ):
            delta = abs(env_b.temperature_c - env_a.temperature_c)
            env_detail['temperature_delta_c'] = delta
            if delta > float(policy.allowed_temperature_delta_c):
                hard_reasons.append('environment_delta_exceeds')
                flags['phase_comparable'] = False
                flags['timing_comparable'] = False
                flags['decay_comparable'] = False
        if (
            policy is not None
            and policy.allowed_humidity_delta_percent is not None
            and env_a.relative_humidity_percent is not None
            and env_b.relative_humidity_percent is not None
        ):
            delta = abs(
                env_b.relative_humidity_percent
                - env_a.relative_humidity_percent
            )
            env_detail['humidity_delta_percent'] = delta
            if delta > float(policy.allowed_humidity_delta_percent):
                hard_reasons.append('environment_delta_exceeds')
                flags['phase_comparable'] = False
                flags['timing_comparable'] = False
                flags['decay_comparable'] = False
        if (
            policy is not None
            and policy.allowed_pressure_delta_pa is not None
            and env_a.air_pressure_pa is not None
            and env_b.air_pressure_pa is not None
        ):
            delta = abs(env_b.air_pressure_pa - env_a.air_pressure_pa)
            env_detail['pressure_delta_pa'] = delta
            if delta > float(policy.allowed_pressure_delta_pa):
                drift_reasons.append('environment_delta_exceeds')
                flags['timing_comparable'] = False
        if (
            policy is not None
            and policy.allowed_sound_speed_relative_delta is not None
            and env_a.sound_speed_m_s is not None
            and env_b.sound_speed_m_s is not None
            and env_a.sound_speed_m_s > 0
        ):
            rel = abs(
                env_b.sound_speed_m_s - env_a.sound_speed_m_s
            ) / env_a.sound_speed_m_s
            env_detail['sound_speed_relative_delta'] = rel
            if rel > float(policy.allowed_sound_speed_relative_delta):
                drift_reasons.append('environment_delta_exceeds')
                flags['phase_comparable'] = False
                flags['timing_comparable'] = False
        details['environment'] = env_detail
    elif policy is not None and any(
        limit is not None
        for limit in (
            policy.allowed_temperature_delta_c,
            policy.allowed_humidity_delta_percent,
            policy.allowed_pressure_delta_pa,
            policy.allowed_sound_speed_relative_delta,
        )
    ):
        unknown_reasons.append('unknown_environment')
        flags['phase_comparable'] = False
        flags['timing_comparable'] = False

    # openings
    if policy is not None:
        subject_openings = {o.opening_id: o.state for o in subject.openings}
        for requirement in policy.required_openings:
            observed = subject_openings.get(requirement.opening_id)
            if observed is None or observed == 'unknown':
                unknown_reasons.append('opening_requirement_unmet')
            elif observed != requirement.required_state:
                hard_reasons.append('opening_requirement_unmet')
                flags['decay_comparable'] = False
                flags['magnitude_comparable'] = False
    openings_a = {o.opening_id: o.state for o in baseline.openings}
    openings_b = {o.opening_id: o.state for o in subject.openings}
    shared_openings = openings_a.keys() & openings_b.keys()
    if any(
        openings_a[o] != openings_b[o]
        and openings_a[o] != 'unknown'
        and openings_b[o] != 'unknown'
        for o in shared_openings
    ):
        hard_reasons.append('opening_state_changed')
        flags['decay_comparable'] = False
        flags['magnitude_comparable'] = False

    # occupancy
    if policy is not None and policy.occupancy_policy != 'unrestricted':
        if baseline.occupancy == 'unknown' or subject.occupancy == 'unknown':
            if policy.occupancy_policy in (
                'must_match',
                'must_be_unoccupied',
            ):
                unknown_reasons.append('unknown_occupancy')
        elif policy.occupancy_policy == 'must_be_unoccupied' and (
            subject.occupancy != 'unoccupied'
            or baseline.occupancy != 'unoccupied'
        ):
            hard_reasons.append('occupancy_changed')
            flags['magnitude_comparable'] = False
            flags['decay_comparable'] = False
        elif (
            policy.occupancy_policy == 'must_match'
            and baseline.occupancy != subject.occupancy
        ):
            hard_reasons.append('occupancy_changed')
            flags['magnitude_comparable'] = False
            flags['decay_comparable'] = False

    # dynamic DSP state — contamination class
    dyn_a, dyn_b = baseline.dynamic_state, subject.dynamic_state
    required_dynamic = (
        {d.feature: d.required_state for d in policy.required_dynamic_states}
        if policy is not None
        else {}
    )
    if dyn_a is not None or dyn_b is not None:
        states_a = dyn_a.feature_states() if dyn_a is not None else {}
        states_b = dyn_b.feature_states() if dyn_b is not None else {}
        for feature in set(states_a) | set(states_b):
            a = states_a.get(feature, 'unknown')
            b = states_b.get(feature, 'unknown')
            if required_dynamic.get(feature) is not None:
                required = required_dynamic[feature]
                if b == 'unknown':
                    unknown_reasons.append('unknown_device_state')
                elif b != required:
                    hard_reasons.append('dynamic_dsp_state_changed')
                    flags['magnitude_comparable'] = False
                    flags['level_comparable'] = False
                    flags['phase_comparable'] = False
                    flags['distortion_comparable'] = False
            if 'engaged' in (a, b):
                hard_reasons.append('dynamic_dsp_state_changed')
                flags['magnitude_comparable'] = False
                flags['level_comparable'] = False
                flags['phase_comparable'] = False
                flags['distortion_comparable'] = False
            elif (
                a != b
                and a != 'unknown'
                and b != 'unknown'
                and feature not in required_dynamic
            ):
                drift_reasons.append('dynamic_dsp_state_changed')
                flags['level_comparable'] = False
                flags['magnitude_comparable'] = False
        for dyn in (dyn_a, dyn_b):
            if dyn is not None and dyn.limiter_engagement_observed:
                hard_reasons.append('limiter_detected')
                flags['level_comparable'] = False
                flags['magnitude_comparable'] = False
                flags['distortion_comparable'] = False
        thermal_states = {
            d.thermal_state
            for d in (dyn_a, dyn_b)
            if d is not None and d.thermal_state != 'unknown'
        }
        if len(thermal_states) > 1:
            drift_reasons.append('thermal_state_changed')
            flags['distortion_comparable'] = False
    elif required_dynamic:
        unknown_reasons.append('unknown_device_state')
        flags['level_comparable'] = False

    # noise regime
    noise_a, noise_b = baseline.noise_regime, subject.noise_regime
    if noise_a is not None and noise_b is not None:
        src_a, src_b = noise_a.source_states(), noise_b.source_states()
        changed = [
            name
            for name in src_a
            if src_a[name] != src_b[name]
            and src_a[name] != 'unknown'
            and src_b[name] != 'unknown'
        ]
        if changed:
            if policy is not None and policy.noise_rule == 'must_match':
                hard_reasons.append('noise_regime_changed')
                flags['decay_comparable'] = False
                flags['distortion_comparable'] = False
            else:
                drift_reasons.append('noise_regime_changed')
                flags['decay_comparable'] = False
                flags['distortion_comparable'] = False
            details['noise_changed_sources'] = changed
    if (
        policy is not None
        and policy.noise_rule == 'floor_below_db'
        and noise_b is not None
        and noise_b.noise_floor_db_spl is not None
        and noise_b.noise_floor_db_spl > float(policy.noise_floor_max_db_spl)
    ):
        hard_reasons.append('noise_floor_exceeds')
        flags['decay_comparable'] = False

    # elapsed interval
    if (
        policy is not None
        and policy.max_elapsed_s is not None
        and elapsed_s is not None
        and elapsed_s > float(policy.max_elapsed_s)
    ):
        drift_reasons.append('elapsed_interval_exceeds')
        flags['timing_comparable'] = False
        flags['phase_comparable'] = False

    if hard_reasons:
        state: StateComparabilityState = 'state_changed'
        reasons = tuple(dict.fromkeys(hard_reasons + unknown_reasons + drift_reasons))
    elif unknown_reasons:
        state = 'insufficient_state_evidence'
        reasons = tuple(dict.fromkeys(unknown_reasons + drift_reasons))
    elif drift_reasons:
        state = 'comparable_with_declared_drift'
        reasons = tuple(dict.fromkeys(drift_reasons))
    else:
        state = 'comparable_stable_state'
        reasons = ()

    return _verdict(
        document_id=document_id,
        subject_kind='pair_comparability',
        state=state,
        reason_codes=reasons,
        baseline=baseline,
        subject=subject,
        policy=policy,
        flags=flags,
        details=details,
        created_at_utc=created_at_utc,
    )


def evaluate_sequence_stationarity(
    snapshots: tuple[MeasurementStateSnapshot, ...],
    datasets: tuple[CadFrequencyResponseDataset, ...],
    policy: StateControlPolicy | None,
    *,
    document_id: str,
    created_at_utc: str,
) -> StateComparabilityVerdict:
    """Detect non-stationarity inside a bounded repeat sequence.

    Magnitude drift is judged against the policy's declared
    ``magnitude_db_tolerance`` — per-observable, never a universal
    threshold. Repeat datasets must share one frequency grid; mixed
    grids are insufficient evidence, not silently interpolated.
    """
    measurement_ids = tuple(s.measurement_id for s in snapshots)
    flags = {
        'magnitude_comparable': True,
        'phase_comparable': True,
        'timing_comparable': True,
        'level_comparable': True,
        'decay_comparable': True,
        'distortion_comparable': True,
    }
    if len(datasets) < 2 or len(snapshots) < 2:
        return _verdict(
            document_id=document_id,
            subject_kind='sequence_stationarity',
            state='insufficient_state_evidence',
            reason_codes=('snapshot_missing',),
            baseline=snapshots[0] if snapshots else None,
            subject=snapshots[-1] if snapshots else None,
            policy=policy,
            flags=flags,
            details={},
            subject_measurement_ids=measurement_ids,
            created_at_utc=created_at_utc,
        )
    grids = {d.frequency_hz for d in datasets}
    details: dict[str, Any] = {}
    reasons: list[StateReasonCode] = []
    if len(grids) != 1:
        reasons.append('snapshot_missing')
        details['reason'] = (
            'repeat captures declared different frequency grids — '
            'stationarity refused rather than silently interpolated'
        )
        state: StateComparabilityState = 'insufficient_state_evidence'
        flags['magnitude_comparable'] = False
        flags['phase_comparable'] = False
        flags['timing_comparable'] = False
    else:
        levels = np.asarray([d.level_db for d in datasets], dtype=float)
        max_delta = float(np.max(np.abs(np.diff(levels, axis=0))))
        details['max_consecutive_magnitude_delta_db'] = max_delta
        if (
            policy is not None
            and policy.magnitude_db_tolerance is not None
            and max_delta > float(policy.magnitude_db_tolerance)
        ):
            reasons.append('repeat_non_stationarity')
            flags['magnitude_comparable'] = False
            flags['decay_comparable'] = False
            flags['distortion_comparable'] = False
        if (
            policy is not None
            and policy.level_drift_db_tolerance is not None
        ):
            mean_levels = levels.mean(axis=1)
            drift = float(np.max(np.abs(np.diff(mean_levels))))
            details['max_consecutive_level_drift_db'] = drift
            if drift > float(policy.level_drift_db_tolerance):
                reasons.append('repeat_non_stationarity')
                flags['level_comparable'] = False
        state = (
            'non_stationary_during_capture'
            if reasons
            else 'comparable_stable_state'
        )
    return _verdict(
        document_id=document_id,
        subject_kind='sequence_stationarity',
        state=state,
        reason_codes=tuple(dict.fromkeys(reasons)),
        baseline=snapshots[0],
        subject=snapshots[-1],
        policy=policy,
        flags=flags,
        details=details,
        subject_measurement_ids=measurement_ids,
        created_at_utc=created_at_utc,
    )
