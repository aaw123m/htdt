from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_scene import Direction3, Position3
from .comparison import (
    ComparisonResult,
    FrequencyResponse,
    comparison_algorithm_sha256,
    replay_comparison_result,
)
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash


MeasurementEvidenceType = Literal['measured', 'derived', 'predicted', 'unknown']
MeasurementSourceKind = Literal['rew_api', 'rew_text', 'unknown']
MeasurementPhaseStatus = Literal['valid', 'absent', 'unknown']
RadiationScope = Literal['single', 'bass_managed', 'mixed', 'unknown']
RoutingEvidence = Literal['verified', 'manual', 'inferred', 'unknown']






class CadMeasurementRecord(BaseModel):
    """Immutable evidence binding to one exact native SceneRevision."""

    model_config = ConfigDict(frozen=True)

    measurement_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    measurement_entity_id: str = Field(min_length=1)
    measurement_position: Position3
    measurement_direction: Direction3 | None = None

    evidence_type: MeasurementEvidenceType = 'unknown'
    channel_role: str = 'unknown'
    source_speaker_ids: tuple[str, ...] = ()
    radiation_scope: RadiationScope = 'unknown'
    routing_evidence: RoutingEvidence = 'unknown'

    captured_at: str | None = None
    imported_at: str = Field(min_length=1)
    source_kind: MeasurementSourceKind
    external_source_id: str | None = None

    quality_status: str = 'unknown'
    quality_reasons: tuple[str, ...] = ()
    quality_source: str = 'unknown'
    provenance_json: str = '{}'

    @field_validator('source_speaker_ids')
    @classmethod
    def unique_source_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError('source_speaker_ids must be unique')
        if any(not item for item in value):
            raise ValueError('source_speaker_ids must not contain empty values')
        return value


class CadFrequencyResponseDataset(BaseModel):
    """Immutable frequency-response samples on their original imported grid."""

    model_config = ConfigDict(frozen=True)

    dataset_id: str = Field(min_length=1)
    measurement_id: str = Field(min_length=1)
    frequency_hz: tuple[float, ...]
    level_db: tuple[float, ...]
    phase_deg: tuple[float, ...] | None = None
    phase_status: MeasurementPhaseStatus
    level_reference: str = 'unknown'
    smoothing: str | None = None
    processing_json: str = '{}'
    source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    importer_version: str = Field(min_length=1)

    def identity_payload(self) -> dict[str, Any]:
        """Versioned semantic identity payload covering the whole dataset.

        Covers the sample arrays plus the interpretation metadata
        (``phase_status``, ``level_reference``, ``smoothing``,
        ``processing_json``), the retained raw-asset binding
        (``source_sha256``) and the declared versioned importer
        (``importer_version``), so the persisted ``dataset_sha256``
        identifies exactly one immutable dataset interpretation.
        """
        return self.model_dump(mode='json')

    @property
    def dataset_sha256(self) -> str:
        """Immutable semantic SHA-256 over ``identity_payload``.

        Persisted in ``cad_frequency_responses.dataset_sha256`` at save
        time and re-verified on every authoritative read; downstream
        records (quality reports, comparisons) reference this identity
        directly.
        """
        return _hash(self.identity_payload())

    @model_validator(mode='after')
    def valid_arrays(self) -> 'CadFrequencyResponseDataset':
        count = len(self.frequency_hz)
        if count < 2:
            raise ValueError('frequency response requires at least two samples')
        if len(self.level_db) != count:
            raise ValueError('frequency and level arrays must have equal length')
        if self.phase_deg is not None and len(self.phase_deg) != count:
            raise ValueError('phase array length must match frequency array')
        if self.phase_status == 'absent' and self.phase_deg is not None:
            raise ValueError('phase_status absent requires phase_deg=None')
        if self.phase_status == 'valid' and self.phase_deg is None:
            raise ValueError('phase_status valid requires phase data')
        previous = 0.0
        for index, frequency in enumerate(self.frequency_hz):
            frequency = float(frequency)
            if not isfinite(frequency) or frequency <= 0:
                raise ValueError('frequency values must be finite and positive')
            if index and frequency <= previous:
                raise ValueError('frequency values must be strictly increasing')
            previous = frequency
        if any(not isfinite(float(value)) for value in self.level_db):
            raise ValueError('level values must be finite')
        if self.phase_deg is not None and any(not isfinite(float(value)) for value in self.phase_deg):
            raise ValueError('phase values must be finite')
        return self


class CadMeasurementComparison(BaseModel):
    """Saved A/B result identity-bound to exact datasets and scene revisions.

    ``dataset_*_sha256`` pin the semantic dataset rows the comparison was
    computed from, ``spec_sha256`` pins the declared comparison spec, and
    ``comparison_sha256`` seals the whole record so a persisted row cannot be
    reinterpreted against different evidence.
    """

    model_config = ConfigDict(frozen=True)

    comparison_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    dataset_a_id: str = Field(min_length=1)
    dataset_b_id: str = Field(min_length=1)
    dataset_a_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    dataset_b_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    scene_revision_a_id: str = Field(min_length=1)
    scene_revision_b_id: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    requested_band_hz: tuple[float, float]
    reference_band_hz: tuple[float, float] | None = None
    excluded_bands: tuple[tuple[float, float], ...] = ()
    actual_band_hz: tuple[float, float]
    grid_hz: tuple[float, ...]
    a_db: tuple[float, ...]
    b_db: tuple[float, ...]
    difference_db: tuple[float, ...]
    mean_difference_db: float | None = None
    rms_difference_db: float | None = None
    level_offset_db: float | None = None
    shape_rms_db: float | None = None
    valid_points: int = Field(ge=0)
    total_grid_points: int = Field(ge=0)
    algorithm_version: str = Field(min_length=1)
    algorithm_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    comparison_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    # Semantic provenance (#852): the frozen side contexts, advisory
    # mismatches and metric-eligibility decisions derived at comparison
    # time — history reopens with the same interpretation regardless of
    # how the project has evolved since. ``None`` only on legacy rows.
    semantics_json: str | None = None
    label_a: str | None = None
    label_b: str | None = None
    level_compatibility: str | None = None

    @model_validator(mode='after')
    def valid_result(self) -> 'CadMeasurementComparison':
        if self.dataset_a_id == self.dataset_b_id:
            raise ValueError('comparison requires two different datasets')
        if self.requested_band_hz[1] <= self.requested_band_hz[0]:
            raise ValueError('requested comparison band is invalid')
        if self.actual_band_hz[1] <= self.actual_band_hz[0]:
            raise ValueError('actual comparison band is invalid')
        if self.reference_band_hz is not None:
            low, high = self.reference_band_hz
            if not (isfinite(low) and isfinite(high)) or low <= 0 or high <= low:
                raise ValueError('reference comparison band is invalid')
        for low, high in self.excluded_bands:
            if not (isfinite(low) and isfinite(high)) or low <= 0 or high <= low:
                raise ValueError('excluded comparison band is invalid')
        count = len(self.grid_hz)
        if len(self.a_db) != count or len(self.b_db) != count or len(self.difference_db) != count:
            raise ValueError('comparison arrays must have equal length')
        if self.valid_points != count:
            raise ValueError('valid_points must match comparison array length')
        arrays = (
            *self.requested_band_hz,
            *self.actual_band_hz,
            *self.grid_hz,
            *self.a_db,
            *self.b_db,
            *self.difference_db,
        )
        if any(not isfinite(float(value)) for value in arrays):
            raise ValueError('comparison values must be finite')
        for value in (
            self.mean_difference_db,
            self.rms_difference_db,
            self.level_offset_db,
            self.shape_rms_db,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('comparison metrics must be finite')
        return self

    @model_validator(mode='after')
    def valid_identity(self) -> 'CadMeasurementComparison':
        if self.spec_sha256 != _hash(self.spec_identity_payload()):
            raise ValueError('measurement comparison spec hash mismatch')
        if self.comparison_sha256 != _hash(self.identity_payload()):
            raise ValueError('measurement comparison hash mismatch')
        return self

    def spec_identity_payload(self) -> dict[str, Any]:
        """The exact comparison spec this record claims was executed."""
        return {
            'algorithm_version': self.algorithm_version,
            'algorithm_sha256': self.algorithm_sha256,
            'requested_band_hz': list(self.requested_band_hz),
            'reference_band_hz': (
                None
                if self.reference_band_hz is None
                else list(self.reference_band_hz)
            ),
            'excluded_bands': [list(band) for band in self.excluded_bands],
        }

    def comparison_result(self) -> ComparisonResult:
        """The replayable algorithm result carried by this record."""
        return ComparisonResult(
            requested_band_hz=self.requested_band_hz,
            actual_band_hz=self.actual_band_hz,
            grid_hz=self.grid_hz,
            a_db=self.a_db,
            b_db=self.b_db,
            difference_db=self.difference_db,
            mean_difference_db=self.mean_difference_db,
            rms_difference_db=self.rms_difference_db,
            level_offset_db=self.level_offset_db,
            shape_rms_db=self.shape_rms_db,
            valid_points=self.valid_points,
            total_grid_points=self.total_grid_points,
            algorithm_version=self.algorithm_version,
            reference_band_hz=self.reference_band_hz,
            excluded_bands=self.excluded_bands,
        )

    def identity_payload(self) -> dict[str, Any]:
        payload = {
            'comparison_id': self.comparison_id,
            'document_id': self.document_id,
            'dataset_a_id': self.dataset_a_id,
            'dataset_b_id': self.dataset_b_id,
            'dataset_a_sha256': self.dataset_a_sha256,
            'dataset_b_sha256': self.dataset_b_sha256,
            'scene_revision_a_id': self.scene_revision_a_id,
            'scene_revision_b_id': self.scene_revision_b_id,
            'created_at': self.created_at,
            'requested_band_hz': list(self.requested_band_hz),
            'reference_band_hz': (
                None
                if self.reference_band_hz is None
                else list(self.reference_band_hz)
            ),
            'excluded_bands': [list(band) for band in self.excluded_bands],
            'actual_band_hz': list(self.actual_band_hz),
            'grid_hz': list(self.grid_hz),
            'a_db': list(self.a_db),
            'b_db': list(self.b_db),
            'difference_db': list(self.difference_db),
            'mean_difference_db': self.mean_difference_db,
            'rms_difference_db': self.rms_difference_db,
            'level_offset_db': self.level_offset_db,
            'shape_rms_db': self.shape_rms_db,
            'valid_points': self.valid_points,
            'total_grid_points': self.total_grid_points,
            'algorithm_version': self.algorithm_version,
            'algorithm_sha256': self.algorithm_sha256,
            'spec_sha256': self.spec_sha256,
        }
        # Semantics seal into the identity only when present — legacy rows
        # keep replaying under their original hash.
        if self.semantics_json is not None:
            payload['semantics_json'] = self.semantics_json
            payload['label_a'] = self.label_a
            payload['label_b'] = self.label_b
            payload['level_compatibility'] = self.level_compatibility
        return payload


def build_measurement_comparison(
    *,
    comparison_id: str,
    document_id: str,
    dataset_a_id: str,
    dataset_b_id: str,
    dataset_a_sha256: str,
    dataset_b_sha256: str,
    scene_revision_a_id: str,
    scene_revision_b_id: str,
    created_at: str,
    result: ComparisonResult,
    semantics_json: str | None = None,
    label_a: str | None = None,
    label_b: str | None = None,
    level_compatibility: str | None = None,
) -> CadMeasurementComparison:
    """Assemble an identity-bound record for a canonical comparison result.

    The dataset semantic hashes must be computed over the exact persisted
    dataset rows the result was replayed against; the spec/algorithm identity
    and the comparison seal are derived here so a persisted row self-verifies
    on every parse.
    """
    payload: dict[str, Any] = {
        'comparison_id': comparison_id,
        'document_id': document_id,
        'dataset_a_id': dataset_a_id,
        'dataset_b_id': dataset_b_id,
        'dataset_a_sha256': dataset_a_sha256,
        'dataset_b_sha256': dataset_b_sha256,
        'scene_revision_a_id': scene_revision_a_id,
        'scene_revision_b_id': scene_revision_b_id,
        'created_at': created_at,
        **asdict(result),
        'algorithm_sha256': comparison_algorithm_sha256(result.algorithm_version),
        'spec_sha256': '0' * 64,
        'comparison_sha256': '0' * 64,
        'semantics_json': semantics_json,
        'label_a': label_a,
        'label_b': label_b,
        'level_compatibility': level_compatibility,
    }
    provisional = CadMeasurementComparison.model_construct(**payload)
    payload['spec_sha256'] = _hash(provisional.spec_identity_payload())
    provisional = CadMeasurementComparison.model_construct(**payload)
    payload['comparison_sha256'] = _hash(provisional.identity_payload())
    return CadMeasurementComparison(**payload)


def replay_measurement_comparison(
    comparison: CadMeasurementComparison,
    *,
    dataset_a: CadFrequencyResponseDataset,
    dataset_b: CadFrequencyResponseDataset,
) -> ComparisonResult:
    """Rerun the comparison's pinned algorithm against the bound datasets.

    Authoritative persistence and reads replay the canonical algorithm
    registered for the comparison's (algorithm_version, algorithm_sha256)
    identity so persisted results are never trusted on payload alone.
    """
    if comparison.dataset_a_id != dataset_a.dataset_id:
        raise ValueError('comparison dataset A binding mismatch')
    if comparison.dataset_b_id != dataset_b.dataset_id:
        raise ValueError('comparison dataset B binding mismatch')
    if comparison.algorithm_sha256 != comparison_algorithm_sha256(comparison.algorithm_version):
        raise ValueError('comparison algorithm hash mismatch')
    return replay_comparison_result(
        comparison.comparison_result(),
        FrequencyResponse(dataset_a.frequency_hz, dataset_a.level_db),
        FrequencyResponse(dataset_b.frequency_hz, dataset_b.level_db),
    )


class CadMeasurementAttachment(BaseModel):
    """One raw source artifact linked to a measurement.

    Bytes live in the content-addressed managed-asset store (which the native
    backup sweeps wholesale); this row is the auditable association —
    ``sha256`` pins the exact attached bytes.
    """

    model_config = ConfigDict(frozen=True)

    attachment_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    measurement_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    size_bytes: int = Field(ge=0)
    note: str | None = None
    created_at_utc: str = Field(min_length=1)


MEASUREMENT_ATTACHMENT_KINDS = ('mdat', 'calibration', 'notes', 'other')


def measurement_is_synthetic(record: CadMeasurementRecord) -> bool:
    try:
        provenance = json.loads(record.provenance_json)
    except (TypeError, json.JSONDecodeError):
        provenance = {}
    return bool(
        provenance.get('validation_scope') == 'synthetic_fixture'
        or provenance.get('synthetic_fixture') is True
        or record.quality_status == 'synthetic_fixture'
    )


def measurement_evidence_label(record: CadMeasurementRecord) -> str:
    if measurement_is_synthetic(record):
        return '合成'
    return {
        'measured': '実測',
        'derived': '派生',
        'predicted': '予測',
        'unknown': '不明',
    }.get(record.evidence_type, record.evidence_type)
