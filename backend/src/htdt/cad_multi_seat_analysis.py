"""Multi-seat measurement analysis authority (#510).

Deterministic derived views over a coherent set of measured frequency
responses: N-way overlay values on a shared explicit grid, a per-frequency
min/max response envelope, an explicitly-labelled central tendency, a
per-frequency spatial spread and member-level outlier identification.
Aggregates are derived evidence only — every member stays individually
addressable by exact dataset identity, and an aggregate is never presented
as a measured trace.
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite, sqrt
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .comparison import FrequencyResponse, _grid, _interpolate


CentralTendency = Literal['none', 'arithmetic_mean_in_db']
MULTI_SEAT_ALGORITHM_VERSION = 'multi-seat-analysis-1'
MULTI_SEAT_SCHEMA_VERSION = 'multi-seat-analysis-1'


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: Any) -> str:
    return sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


MULTI_SEAT_ALGORITHM_IDENTITY: dict[str, Any] = {
    'algorithm_version': MULTI_SEAT_ALGORITHM_VERSION,
    'grid': 'log2_spaced_octave_aligned_grid_over_common_overlap_band',
    'interpolation': 'linear_in_log2_frequency',
    'extrapolation': 'forbidden',
    'envelope': 'per_grid_point_member_min_and_max',
    'spread': 'max_minus_min_db_per_grid_point',
    'central_tendency': ['none', 'arithmetic_mean_in_db'],
    'outlier': 'member_with_largest_rms_deviation_from_mean_over_selected_band',
    'aggregate_never_presented_as_measured': True,
    'mixed_contexts': 'explicit_warnings_never_silent_aggregation',
}
MULTI_SEAT_ALGORITHM_SHA256 = _hash(MULTI_SEAT_ALGORITHM_IDENTITY)


class MultiSeatMember(BaseModel):
    """One exact member dataset with its seat/target binding."""

    model_config = ConfigDict(frozen=True)

    measurement_id: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    dataset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    target_entity_id: str = Field(min_length=1)
    seat_label: str = Field(min_length=1)
    channel_role: str = 'unknown'
    evidence_type: str = 'unknown'
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    is_mlp: bool = False
    level_reference: str = 'unknown'


class MultiSeatAnalysisSet(BaseModel):
    """A coherent, ordered set of measured datasets selected for analysis.

    ``warnings`` lists every detected context incompatibility (mixed channel
    roles, scene revisions, level references or evidence kinds) — detection
    never silently drops members.
    """

    model_config = ConfigDict(frozen=True)

    set_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    members: tuple[MultiSeatMember, ...] = Field(min_length=1)
    warnings: tuple[str, ...] = ()
    created_at: str = Field(min_length=1)
    set_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_set(self) -> 'MultiSeatAnalysisSet':
        dataset_ids = [member.dataset_id for member in self.members]
        if len(dataset_ids) != len(set(dataset_ids)):
            raise ValueError('multi-seat member datasets must be unique')
        if sum(1 for member in self.members if member.is_mlp) > 1:
            raise ValueError('a multi-seat set can mark at most one MLP member')
        if self.set_sha256 != _hash(self.identity_payload()):
            raise ValueError('multi-seat analysis set hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': MULTI_SEAT_SCHEMA_VERSION,
            'document_id': self.document_id,
            'members': [member.model_dump(mode='json') for member in self.members],
            'warnings': list(self.warnings),
        }


class MultiSeatAnalysisResult(BaseModel):
    """Immutable derived analysis over an exact member set.

    Grid/interpolation semantics follow the canonical comparison algorithm
    (``f_k = 1 Hz × 2^(k/96)`` over the common overlap, log2-frequency linear
    interpolation, no extrapolation). ``member_levels_db`` rows follow member
    order; ``mean_db`` is populated only when the declared central tendency
    requires it and its domain is always explicit.
    """

    model_config = ConfigDict(frozen=True)

    result_id: str = Field(min_length=1)
    set_id: str = Field(min_length=1)
    set_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    requested_band_hz: tuple[float, float]
    actual_band_hz: tuple[float, float] | None = None
    grid_hz: tuple[float, ...] = ()
    member_levels_db: tuple[tuple[float, ...], ...] = ()
    min_db: tuple[float, ...] = ()
    max_db: tuple[float, ...] = ()
    spread_db: tuple[float, ...] = ()
    mean_db: tuple[float, ...] = ()
    upper_envelope_member_indices: tuple[int, ...] = ()
    lower_envelope_member_indices: tuple[int, ...] = ()
    central_tendency: CentralTendency = 'none'
    outlier_band_hz: tuple[float, float] | None = None
    outlier_member_index: int | None = None
    warnings: tuple[str, ...] = ()
    algorithm_version: str = MULTI_SEAT_ALGORITHM_VERSION
    algorithm_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    created_at: str = Field(min_length=1)
    analysis_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_result(self) -> 'MultiSeatAnalysisResult':
        if self.requested_band_hz[1] <= self.requested_band_hz[0]:
            raise ValueError('requested band is invalid')
        if self.grid_hz:
            count = len(self.grid_hz)
            arrays = (
                *self.min_db,
                *self.max_db,
                *self.spread_db,
                *self.mean_db,
                *(level for row in self.member_levels_db for level in row),
            )
            if self.actual_band_hz is None:
                raise ValueError('a grid requires the actual band')
            if len(self.min_db) != count or len(self.max_db) != count or len(self.spread_db) != count:
                raise ValueError('envelope arrays must match grid length')
            if len(self.upper_envelope_member_indices) != count or (
                len(self.lower_envelope_member_indices) != count
            ):
                raise ValueError('envelope member arrays must match grid length')
            if self.member_levels_db and any(len(row) != count for row in self.member_levels_db):
                raise ValueError('member level rows must match grid length')
            if self.central_tendency == 'arithmetic_mean_in_db':
                if len(self.mean_db) != count:
                    raise ValueError('mean array must match grid length')
            elif self.mean_db:
                raise ValueError('mean_db requires a declared central tendency')
            if any(not isfinite(float(v)) for v in arrays):
                raise ValueError('analysis values must be finite')
        if self.algorithm_sha256 != MULTI_SEAT_ALGORITHM_SHA256:
            raise ValueError('multi-seat analysis algorithm hash mismatch')
        if self.analysis_sha256 != _hash(self.identity_payload()):
            raise ValueError('multi-seat analysis hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': MULTI_SEAT_SCHEMA_VERSION,
            'set_id': self.set_id,
            'set_sha256': self.set_sha256,
            'requested_band_hz': list(self.requested_band_hz),
            'actual_band_hz': (
                None if self.actual_band_hz is None else list(self.actual_band_hz)
            ),
            'grid_hz': list(self.grid_hz),
            'member_levels_db': [list(row) for row in self.member_levels_db],
            'min_db': list(self.min_db),
            'max_db': list(self.max_db),
            'spread_db': list(self.spread_db),
            'mean_db': list(self.mean_db),
            'upper_envelope_member_indices': list(self.upper_envelope_member_indices),
            'lower_envelope_member_indices': list(self.lower_envelope_member_indices),
            'central_tendency': self.central_tendency,
            'outlier_band_hz': (
                None if self.outlier_band_hz is None else list(self.outlier_band_hz)
            ),
            'outlier_member_index': self.outlier_member_index,
            'warnings': list(self.warnings),
            'algorithm_version': self.algorithm_version,
            'algorithm_sha256': self.algorithm_sha256,
            'created_at': self.created_at,
        }


def build_multi_seat_set(
    members: tuple[MultiSeatMember, ...],
    *,
    document_id: str,
    created_at: str,
) -> MultiSeatAnalysisSet:
    """Assemble a set and record every context incompatibility as a warning."""
    if not members:
        raise ValueError('a multi-seat set requires at least one member')
    warnings: list[str] = []
    for field, label in (
        ('channel_role', 'channel_role'),
        ('scene_revision_id', 'scene_revision_id'),
        ('level_reference', 'level_reference'),
        ('evidence_type', 'evidence_type'),
    ):
        values = {getattr(member, field) for member in members}
        if len(values) > 1:
            warnings.append(
                f'mixed_{label}: ' + ', '.join(sorted(str(v) for v in values))
            )
    payload: dict[str, Any] = {
        'set_id': str(uuid4()),
        'document_id': document_id,
        'members': members,
        'warnings': warnings,
        'created_at': created_at,
    }
    provisional = MultiSeatAnalysisSet.model_construct(
        **payload,
        set_sha256='0' * 64,
    )
    return MultiSeatAnalysisSet(
        **payload,
        set_sha256=_hash(provisional.identity_payload()),
    )


def run_multi_seat_analysis(
    analysis_set: MultiSeatAnalysisSet,
    member_responses: tuple[FrequencyResponse, ...],
    *,
    low_hz: float,
    high_hz: float,
    central_tendency: CentralTendency = 'arithmetic_mean_in_db',
    outlier_band_hz: tuple[float, float] | None = None,
    created_at: str,
) -> MultiSeatAnalysisResult:
    """Run the pinned N-way analysis over the exact member responses.

    ``member_responses`` must be given in member order and correspond to the
    recorded dataset identities — callers resolve them from the authoritative
    repository so the persisted result is replayable.
    """
    if len(member_responses) != len(analysis_set.members):
        raise ValueError('member responses must match the analysis set members')
    if high_hz <= low_hz:
        raise ValueError('requested band is invalid')
    overlap_low = max(low_hz, max(r.frequency_hz[0] for r in member_responses))
    overlap_high = min(high_hz, min(r.frequency_hz[-1] for r in member_responses))
    if overlap_high <= overlap_low:
        raise ValueError('member datasets do not overlap in the requested band')
    grid = _grid(overlap_low, overlap_high)
    rows = tuple(
        tuple(_interpolate(response, f) for f in grid)
        for response in member_responses
    )
    min_db = tuple(min(row[i] for row in rows) for i in range(len(grid)))
    max_db = tuple(max(row[i] for row in rows) for i in range(len(grid)))
    spread_db = tuple(hi - lo for hi, lo in zip(max_db, min_db, strict=True))
    upper = tuple(max(range(len(rows)), key=lambda m, i=i: rows[m][i]) for i in range(len(grid)))
    lower = tuple(min(range(len(rows)), key=lambda m, i=i: rows[m][i]) for i in range(len(grid)))
    mean_db: tuple[float, ...] = ()
    if central_tendency == 'arithmetic_mean_in_db':
        mean_db = tuple(
            sum(row[i] for row in rows) / len(rows) for i in range(len(grid))
        )
    outlier_index = None
    if outlier_band_hz is not None and len(rows) >= 2 and len(grid) >= 2:
        band_indices = [
            i for i, f in enumerate(grid)
            if outlier_band_hz[0] <= f <= outlier_band_hz[1]
        ]
        if band_indices:
            band_mean = [
                sum(row[i] for row in rows) / len(rows) for i in band_indices
            ]
            deviations = [
                sqrt(
                    sum((rows[m][i] - band_mean[k]) ** 2 for k, i in enumerate(band_indices))
                    / len(band_indices)
                )
                for m in range(len(rows))
            ]
            outlier_index = max(range(len(rows)), key=lambda m: deviations[m])
    payload: dict[str, Any] = {
        'result_id': str(uuid4()),
        'set_id': analysis_set.set_id,
        'set_sha256': analysis_set.set_sha256,
        'requested_band_hz': (low_hz, high_hz),
        'actual_band_hz': (overlap_low, overlap_high),
        'grid_hz': grid,
        'member_levels_db': rows,
        'min_db': min_db,
        'max_db': max_db,
        'spread_db': spread_db,
        'mean_db': mean_db,
        'upper_envelope_member_indices': upper,
        'lower_envelope_member_indices': lower,
        'central_tendency': central_tendency,
        'outlier_band_hz': outlier_band_hz,
        'outlier_member_index': outlier_index,
        'warnings': analysis_set.warnings,
        'algorithm_version': MULTI_SEAT_ALGORITHM_VERSION,
        'algorithm_sha256': MULTI_SEAT_ALGORITHM_SHA256,
        'created_at': created_at,
    }
    provisional = MultiSeatAnalysisResult.model_construct(
        **payload,
        analysis_sha256='0' * 64,
    )
    return MultiSeatAnalysisResult(
        **payload,
        analysis_sha256=_hash(provisional.identity_payload()),
    )


def replay_multi_seat_analysis(
    result: MultiSeatAnalysisResult,
    analysis_set: MultiSeatAnalysisSet,
    member_responses: tuple[FrequencyResponse, ...],
) -> MultiSeatAnalysisResult:
    """Rerun the pinned algorithm against the bound set and responses."""
    if result.set_id != analysis_set.set_id or result.set_sha256 != analysis_set.set_sha256:
        raise ValueError('analysis result is bound to a different set')
    replay = run_multi_seat_analysis(
        analysis_set,
        member_responses,
        low_hz=result.requested_band_hz[0],
        high_hz=result.requested_band_hz[1],
        central_tendency=result.central_tendency,
        outlier_band_hz=result.outlier_band_hz,
        created_at=result.created_at,
    )
    comparable = replay.model_dump(
        mode='json', exclude={'result_id', 'analysis_sha256'}
    )
    original = result.model_dump(
        mode='json', exclude={'result_id', 'analysis_sha256'}
    )
    if comparable != original:
        raise ValueError('multi-seat analysis does not replay to persisted values')
    return replay
