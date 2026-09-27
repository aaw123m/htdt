"""Measurement stationarity & temporal drift authority (#1009).

Repeating and averaging measurements presumes the acoustic system stayed
time-invariant over the repeats. Temperature/humidity drift, HVAC, people
moving, door/curtain state changes, source voice-coil warmup and DSP
state drift all break that assumption — and averaging non-stationary
RIRs smears arrivals and biases decay metrics.

This authority is deliberately scoped:

- ``MeasurementStationarityScope`` binds an exact repeat group, time
  interval, observable requirement, band and method — never one global
  ``room_is_stationary`` boolean;
- per-repeat acquisition timestamps (start/end, order) are preserved —
  sharing a target id must not discard timing;
- environment timeline entries carry honest observation states
  (observed / start_end / assumed / unknown) and are never interpolated
  silently;
- clock drift (#642) is a *separate* cause from physical system change —
  a clock-only correction never claims physical drift was removed;
- an aligned comparison (delay alignment, time-stretch) produces derived
  comparison evidence and never rewrites raw IRs;
- the averaging gate decides whether repeats may honestly merge.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash






_SHA256_PATTERN = r'^[0-9a-f]{64}$'


StationarityMethod = Literal[
    'short_time_coherence',
    'pairwise_deviation',
    'repeatability_trend',
    'environment_correlation',
    'producer',
    'unknown',
]

StationarityState = Literal[
    'stationary',
    'drifted',
    'suspect',
    'insufficient_evidence',
    'unknown',
]

EnvironmentEvidenceState = Literal[
    'observed', 'start_end_observation', 'assumed_unchanged', 'unknown'
]

AveragingGate = Literal[
    'average_permitted', 'average_limited', 'average_blocked', 'unknown'
]

AlignmentMethod = Literal[
    'none',
    'timing_reference_correction',
    'global_delay_alignment',
    'time_stretch',
    'producer',
    'unknown',
]


class RepeatTimingRecord(BaseModel):
    """Preserved acquisition timing for one repeat — campaign runners
    must not drop this when repeats share a target id."""

    model_config = ConfigDict(frozen=True)

    measurement_id: str = Field(min_length=1)
    repeat_index: int = Field(ge=0)
    started_at_utc: str | None = None
    ended_at_utc: str | None = None
    sweep_duration_s: float | None = Field(default=None, ge=0.0)
    pause_before_s: float | None = Field(default=None, ge=0.0)

    @model_validator(mode='after')
    def valid_timing(self) -> 'RepeatTimingRecord':
        for value in (self.sweep_duration_s, self.pause_before_s):
            if value is not None and not isfinite(float(value)):
                raise ValueError('timing values must be finite')
        return self


class EnvironmentTimelineEntry(BaseModel):
    """One environment observation inside the assessed interval.

    Only fields actually observed are populated — ``assumed_unchanged``
    is an honest claim state, never silent interpolation.
    """

    model_config = ConfigDict(frozen=True)

    observed_at_utc: str | None = None
    state: EnvironmentEvidenceState = 'unknown'
    temperature_c: float | None = None
    relative_humidity_percent: float | None = None
    pressure_hpa: float | None = None
    hvac_state: str | None = None
    occupancy_state: str | None = None
    room_state_notes: str | None = None

    @model_validator(mode='after')
    def valid_entry(self) -> 'EnvironmentTimelineEntry':
        for value in (
            self.temperature_c,
            self.relative_humidity_percent,
            self.pressure_hpa,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('environment values must be finite')
        if self.state == 'observed' and self.observed_at_utc is None:
            raise ValueError('observed entries require a timestamp')
        return self


class MeasurementStationarityScope(BaseModel):
    """What stationarity is being assessed for, over what span, by which
    method — scope, not a global boolean."""

    model_config = ConfigDict(frozen=True)

    scope_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    measurement_ids: tuple[str, ...] = Field(min_length=2)
    interval_start_utc: str | None = None
    interval_end_utc: str | None = None
    observable: str = Field(min_length=1)
    frequency_band_hz: tuple[float, float] | None = None
    time_region_s: tuple[float, float] | None = None
    source_scenario_ref: str | None = None
    routing_profile_ref: str | None = None
    room_operating_state_ref: str | None = None
    receiver_ref: str | None = None
    method: StationarityMethod = 'unknown'
    method_version: str = Field(min_length=1)
    deviation_threshold_db: float | None = Field(default=None, ge=0.0)
    coherence_threshold: float | None = None
    created_at_utc: str = Field(min_length=1)
    scope_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_scope(self) -> 'MeasurementStationarityScope':
        if len(self.measurement_ids) != len(set(self.measurement_ids)):
            raise ValueError('measurement_ids must be unique')
        if self.method == 'unknown':
            raise ValueError('stationarity scope requires an explicit method')
        if self.frequency_band_hz is not None:
            low, high = self.frequency_band_hz
            if not (isfinite(low) and isfinite(high)) or not (0 < low < high):
                raise ValueError('frequency_band_hz must satisfy 0 < low < high')
        if self.time_region_s is not None:
            low, high = self.time_region_s
            if not (isfinite(low) and isfinite(high)) or not (0.0 <= low < high):
                raise ValueError('time_region_s must satisfy 0 <= low < high')
        for value in (self.deviation_threshold_db, self.coherence_threshold):
            if value is not None and not isfinite(float(value)):
                raise ValueError('thresholds must be finite')
        if self.coherence_threshold is not None and not (
            0.0 <= float(self.coherence_threshold) <= 1.0
        ):
            raise ValueError('coherence_threshold must lie in [0, 1]')
        if self.scope_sha256 != _hash(self.identity_payload()):
            raise ValueError('stationarity scope hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'scope_sha256'})


class RepeatDeviation(BaseModel):
    """Pairwise or per-repeat deviation evidence under the scope method."""

    model_config = ConfigDict(frozen=True)

    measurement_id_a: str = Field(min_length=1)
    measurement_id_b: str = Field(min_length=1)
    max_deviation_db: float | None = None
    mean_deviation_db: float | None = None
    coherence: float | None = None

    @model_validator(mode='after')
    def valid_deviation(self) -> 'RepeatDeviation':
        if self.measurement_id_a == self.measurement_id_b:
            raise ValueError('deviation requires two distinct measurements')
        for label, value in (
            ('max_deviation_db', self.max_deviation_db),
            ('mean_deviation_db', self.mean_deviation_db),
            ('coherence', self.coherence),
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError(f'{label} must be finite')
        if self.coherence is not None and not (0.0 <= self.coherence <= 1.0):
            raise ValueError('coherence must lie in [0, 1]')
        return self


class MeasurementStationarityAssessment(BaseModel):
    """Sealed stationarity verdict for one exact scope."""

    model_config = ConfigDict(frozen=True)

    assessment_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scope_id: str = Field(min_length=1)
    scope_sha256: str = Field(pattern=_SHA256_PATTERN)
    repeat_timings: tuple[RepeatTimingRecord, ...] = ()
    environment_timeline: tuple[EnvironmentTimelineEntry, ...] = ()
    deviations: tuple[RepeatDeviation, ...] = ()
    source_warmup_evidence_json: str | None = None
    clock_drift_separate: bool = True
    alignment_method: AlignmentMethod = 'none'
    alignment_applied_derived_only: bool = True
    stationarity_state: StationarityState = 'unknown'
    averaging_gate: AveragingGate = 'unknown'
    drift_summary: str | None = None
    created_at_utc: str = Field(min_length=1)
    assessment_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_assessment(self) -> 'MeasurementStationarityAssessment':
        timing_ids = [record.measurement_id for record in self.repeat_timings]
        if len(timing_ids) != len(set(timing_ids)):
            raise ValueError('repeat timing records must be unique per measurement')
        if (
            self.alignment_method != 'none'
            and not self.alignment_applied_derived_only
        ):
            raise ValueError(
                'alignment corrections may only produce derived comparison '
                'evidence — raw IRs are never rewritten'
            )
        if self.averaging_gate == 'average_permitted' and (
            self.stationarity_state not in ('stationary',)
        ):
            raise ValueError(
                'averaging is only permitted for a stationary scope'
            )
        if self.averaging_gate == 'average_blocked' and (
            self.stationarity_state == 'stationary'
        ):
            raise ValueError(
                'a stationary scope cannot block averaging'
            )
        if self.assessment_sha256 != _hash(self.identity_payload()):
            raise ValueError('stationarity assessment hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'assessment_sha256'})


def build_stationarity_scope(**kwargs: Any) -> MeasurementStationarityScope:
    """Assemble and seal a :class:`MeasurementStationarityScope`."""
    payload = {'scope_sha256': '0' * 64, **kwargs}
    provisional = MeasurementStationarityScope.model_construct(**payload)
    payload['scope_sha256'] = _hash(provisional.identity_payload())
    return MeasurementStationarityScope(**payload)


def assess_stationarity(
    scope: MeasurementStationarityScope,
    *,
    assessment_id: str,
    repeat_timings: tuple[RepeatTimingRecord, ...],
    deviations: tuple[RepeatDeviation, ...] = (),
    environment_timeline: tuple[EnvironmentTimelineEntry, ...] = (),
    alignment_method: AlignmentMethod = 'none',
    source_warmup_evidence_json: str | None = None,
    created_at_utc: str,
) -> MeasurementStationarityAssessment:
    """Evaluate the scope's declared method over repeat evidence.

    - ``pairwise_deviation``: worst ``max_deviation_db`` is compared to the
      scope's ``deviation_threshold_db``; above → ``drifted`` and
      ``average_blocked``; within → ``stationary`` /
      ``average_permitted``; missing threshold → ``suspect`` /
      ``average_limited``.
    - ``short_time_coherence``: worst coherence vs ``coherence_threshold``.
    - Other methods keep evidence and report ``insufficient_evidence``
      rather than pretending a verdict.
    """
    state: StationarityState
    gate: AveragingGate
    summary: list[str] = []

    known_ids = set(scope.measurement_ids)
    for record in repeat_timings:
        if record.measurement_id not in known_ids:
            raise ValueError(
                'repeat timing outside the scoped measurement set'
            )
    for deviation in deviations:
        if (
            deviation.measurement_id_a not in known_ids
            or deviation.measurement_id_b not in known_ids
        ):
            raise ValueError('deviation outside the scoped measurement set')

    if scope.method == 'pairwise_deviation':
        maxima = [
            float(d.max_deviation_db)
            for d in deviations
            if d.max_deviation_db is not None
        ]
        if not maxima or scope.deviation_threshold_db is None:
            state = 'insufficient_evidence'
            gate = 'average_limited'
            summary.append('no pairwise deviations or no declared threshold')
        else:
            worst = max(maxima)
            if worst <= float(scope.deviation_threshold_db):
                state = 'stationary'
                gate = 'average_permitted'
                summary.append(
                    f'worst deviation {worst:.2f} dB within '
                    f'{float(scope.deviation_threshold_db):g} dB'
                )
            else:
                state = 'drifted'
                gate = 'average_blocked'
                summary.append(
                    f'worst deviation {worst:.2f} dB exceeds '
                    f'{float(scope.deviation_threshold_db):g} dB — repeats do '
                    'not describe one time-invariant state'
                )
    elif scope.method == 'short_time_coherence':
        values = [
            float(d.coherence) for d in deviations if d.coherence is not None
        ]
        if not values or scope.coherence_threshold is None:
            state = 'insufficient_evidence'
            gate = 'average_limited'
            summary.append('no coherence evidence or no declared threshold')
        else:
            worst = min(values)
            if worst >= float(scope.coherence_threshold):
                state = 'stationary'
                gate = 'average_permitted'
                summary.append(
                    f'worst coherence {worst:.3f} at/above '
                    f'{float(scope.coherence_threshold):g}'
                )
            else:
                state = 'suspect'
                gate = 'average_limited'
                summary.append(
                    f'worst coherence {worst:.3f} below '
                    f'{float(scope.coherence_threshold):g} — repeat RIRs are '
                    'not interchangeable'
                )
    else:
        state = 'insufficient_evidence'
        gate = 'unknown'
        summary.append(
            f'method {scope.method} is declared but not evaluated natively'
        )

    payload: dict[str, Any] = {
        'assessment_id': assessment_id,
        'schema_version': scope.schema_version,
        'document_id': scope.document_id,
        'scope_id': scope.scope_id,
        'scope_sha256': scope.scope_sha256,
        'repeat_timings': repeat_timings,
        'environment_timeline': environment_timeline,
        'deviations': deviations,
        'source_warmup_evidence_json': source_warmup_evidence_json,
        'alignment_method': alignment_method,
        'stationarity_state': state,
        'averaging_gate': gate,
        'drift_summary': '; '.join(summary) if summary else None,
        'created_at_utc': created_at_utc,
        'assessment_sha256': '0' * 64,
    }
    provisional = MeasurementStationarityAssessment.model_construct(**payload)
    payload['assessment_sha256'] = _hash(provisional.identity_payload())
    return MeasurementStationarityAssessment(**payload)
