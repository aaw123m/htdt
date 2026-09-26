"""#1068 — FLAIR geometry-acoustics benchmark bridge.

Admits the FLAIR dataset (Zenodo 17037517, CC BY 4.0): one room captured
as a millimetre-accurate laser point cloud plus 270 measured RIRs — the
only public corpus that binds geometry and acoustics at room scale, which
is what end-to-end room-reconstruction and solver validation need.

The bridge keeps three things mechanically separate:

- **staged geometry authority** — the published point cloud is OBSERVED
  evidence, never assumed geometry. A reconstruction derived from it is
  a RECONSTRUCTION CANDIDATE until a person or a verified pipeline marks
  it ``solver_ready``; solver ingestion may only ever see
  ``solver_ready`` artifacts. ``advance_geometry_stage`` enforces the
  ladder one rung at a time;
- **preregistered split** — the 270 RIRs are partitioned deterministically
  (hash of the measurement id, fixed seed) into ``calibration`` /
  ``holdout`` roles from #773 BEFORE any tuning: what fits geometry or
  materials against calibration data is then scored only on holdout
  data. ``flair_split_role`` is pure — same id in, same role out;
- **UNKNOWN materials** — the dataset publishes geometry + RIRs, not
  surface absorption; every surface binding starts UNKNOWN and may only
  be upgraded by measured evidence, never from the point cloud's
  appearance.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_external_admission import (
    ExternalAssetAdmission,
    build_external_asset_admission,
    external_asset_file,
)
from .cad_validation_corpus import CorpusSplitRole


FLAIR_AUTHORITY_VERSION = 'flair-bridge-1'


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: dict) -> str:
    return hashlib.sha256(
        _canonical(payload).encode('utf-8')
    ).hexdigest()


def _provenance(ref: str) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='measured',
        source_name='zenodo-record-verified',
        source_version='1',
        source_reference=ref,
        source_sha256='0' * 64,
    )


# ---------------------------------------------------------------------------
# Admission record
# ---------------------------------------------------------------------------

FLAIR_ADMISSION = build_external_asset_admission(
    admission_id='ledger/flair-geometry-rir',
    dataset_name='flair-room',
    dataset_title=(
        'FLAIR: Room Impulse Response Dataset with Laser-Calibrated '
        'Room Geometry'
    ),
    publisher='FLAIR dataset authors (Zenodo)',
    source_kind='zenodo_record',
    admission_state='download_on_demand_candidate',
    concept_doi='10.5281/zenodo.17037516',
    version_doi='10.5281/zenodo.17037517',
    version_record_id='17037517',
    record_uri='https://zenodo.org/records/17037517',
    license_id='cc-by-4.0',
    license_family='cc_by',
    license_uri='https://creativecommons.org/licenses/by/4.0/',
    files=(
        external_asset_file(
            file_name='data_FLAIR.mat',
            uri='https://zenodo.org/records/17037517/files/data_FLAIR.mat',
            size_bytes=115821339,
            md5='41e06a449ff39d271e32b3b82ab29341',
            role='primary_payload',
        ),
    ),
    dataset_notes=(
        'Single MATLAB payload: laser-scanned room geometry (mm-scale '
        'point cloud), measured source/receiver positions, and 270 RIRs '
        'pairing them. Version record 17037517, concept 17037516.'
    ),
)


# ---------------------------------------------------------------------------
# Staged geometry authority ladder
# ---------------------------------------------------------------------------

FlairGeometryStage = Literal[
    'observed_point_cloud',
    'reconstruction_candidate',
    'solver_ready',
]
"""Authority ladder — solver ingestion may only see ``solver_ready``."""

_STAGE_ORDER: dict[FlairGeometryStage, int] = {
    'observed_point_cloud': 0,
    'reconstruction_candidate': 1,
    'solver_ready': 2,
}


class FlairGeometryArtifact(BaseModel):
    """One geometry artifact in the authority ladder.

    ``artifact_sha256`` pins the artifact bytes: the point cloud, the
    reconstruction mesh, or the solver-ready surface model.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['flair-bridge-1'] = FLAIR_AUTHORITY_VERSION
    artifact_id: str = Field(min_length=1)
    stage: FlairGeometryStage
    artifact_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    derived_from_artifact_id: str | None = None
    derived_from_stage: FlairGeometryStage | None = None
    producer_id: str | None = None
    material_bindings: dict[str, str] = Field(default_factory=dict)
    provenance: EquipmentDataProvenance
    note: str = ''
    semantic_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'FlairGeometryArtifact':
        if self.stage == 'observed_point_cloud':
            if self.derived_from_artifact_id is not None:
                raise ValueError(
                    'observed point cloud has no derivation parent'
                )
        else:
            if (
                self.derived_from_artifact_id is None
                or self.derived_from_stage is None
            ):
                raise ValueError(
                    'derived stages must name their parent artifact'
                )
            if _STAGE_ORDER[self.derived_from_stage] != (
                _STAGE_ORDER[self.stage] - 1
            ):
                raise ValueError(
                    'derived stage must be exactly one rung below'
                )
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'flair geometry artifact semantic hash mismatch'
            )
        return self


def build_flair_geometry_artifact(
    *,
    artifact_id: str,
    stage: FlairGeometryStage,
    artifact_sha256: str,
    provenance: EquipmentDataProvenance,
    derived_from_artifact_id: str | None = None,
    derived_from_stage: FlairGeometryStage | None = None,
    producer_id: str | None = None,
    material_bindings: dict[str, str] | None = None,
    note: str = '',
) -> FlairGeometryArtifact:
    probe = FlairGeometryArtifact.model_construct(
        schema_version=1,
        authority_version=FLAIR_AUTHORITY_VERSION,
        artifact_id=artifact_id,
        stage=stage,
        artifact_sha256=artifact_sha256,
        derived_from_artifact_id=derived_from_artifact_id,
        derived_from_stage=derived_from_stage,
        producer_id=producer_id,
        material_bindings=dict(material_bindings or {}),
        provenance=provenance,
        note=note,
        semantic_sha256='',
    )
    return FlairGeometryArtifact(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


def advance_geometry_stage(
    artifact: FlairGeometryArtifact,
    *,
    new_stage: FlairGeometryStage,
) -> FlairGeometryArtifact:
    """One rung at a time; solver ingestion only ever sees solver_ready."""
    if _STAGE_ORDER[new_stage] != _STAGE_ORDER[artifact.stage] + 1:
        raise ValueError(
            f'cannot advance {artifact.stage} → {new_stage}: '
            'authority ladder moves one stage at a time'
        )
    return build_flair_geometry_artifact(
        artifact_id=artifact.artifact_id,
        stage=new_stage,
        artifact_sha256=artifact.artifact_sha256,
        provenance=artifact.provenance,
        derived_from_artifact_id=artifact.artifact_id,
        derived_from_stage=artifact.stage,
        producer_id=artifact.producer_id,
        material_bindings=dict(artifact.material_bindings),
        note=artifact.note,
    )


def solver_may_consume(artifact: FlairGeometryArtifact) -> bool:
    """The only ingestion gate: solver input requires solver_ready."""
    return artifact.stage == 'solver_ready'


# ---------------------------------------------------------------------------
# Preregistered calibration/holdout split (270 RIRs)
# ---------------------------------------------------------------------------

FLAIR_MEASUREMENT_COUNT = 270
FLAIR_SPLIT_SEED = 'flair-split-v1'
_HOLDOUT_FRACTION = 0.30


def flair_measurement_id(index: int) -> str:
    if not 0 <= index < FLAIR_MEASUREMENT_COUNT:
        raise ValueError(
            f'FLAIR measurement index {index} out of range '
            f'0..{FLAIR_MEASUREMENT_COUNT - 1}'
        )
    return f'flair/rir-{index:03d}'


def flair_split_role(measurement_id: str) -> CorpusSplitRole:
    """Deterministic preregistered role — declared before any tuning."""
    if not measurement_id.startswith('flair/rir-'):
        raise ValueError(
            f'not a FLAIR measurement id: {measurement_id!r}'
        )
    digest = hashlib.sha256(
        f'{FLAIR_SPLIT_SEED}:{measurement_id}'.encode('utf-8')
    ).hexdigest()
    bucket = int(digest[:8], 16) / 0xFFFFFFFF
    return 'holdout' if bucket < _HOLDOUT_FRACTION else 'calibration'


def flair_split_counts() -> dict[str, int]:
    counts: dict[str, int] = {'calibration': 0, 'holdout': 0}
    for i in range(FLAIR_MEASUREMENT_COUNT):
        counts[flair_split_role(flair_measurement_id(i))] += 1
    return counts


# ---------------------------------------------------------------------------
# Case descriptor bound to the admission
# ---------------------------------------------------------------------------


class FlairBridgeCase(BaseModel):
    """Admission + declared contract for FLAIR-driven validation."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['flair-bridge-1'] = FLAIR_AUTHORITY_VERSION
    case_id: str = Field(min_length=1)
    admission: ExternalAssetAdmission
    measurement_count: int = FLAIR_MEASUREMENT_COUNT
    split_seed: str = FLAIR_SPLIT_SEED
    holdout_fraction: float = _HOLDOUT_FRACTION
    material_policy: Literal['unknown_unless_measured'] = (
        'unknown_unless_measured'
    )
    geometry_authority_ladder: tuple[FlairGeometryStage, ...] = (
        'observed_point_cloud',
        'reconstruction_candidate',
        'solver_ready',
    )
    provenance: EquipmentDataProvenance
    semantic_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'FlairBridgeCase':
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('flair bridge case semantic hash mismatch')
        return self


def build_flair_bridge_case() -> FlairBridgeCase:
    probe = FlairBridgeCase.model_construct(
        schema_version=1,
        authority_version=FLAIR_AUTHORITY_VERSION,
        case_id='flair/end-to-end-room',
        admission=FLAIR_ADMISSION,
        measurement_count=FLAIR_MEASUREMENT_COUNT,
        split_seed=FLAIR_SPLIT_SEED,
        holdout_fraction=_HOLDOUT_FRACTION,
        material_policy='unknown_unless_measured',
        geometry_authority_ladder=(
            'observed_point_cloud',
            'reconstruction_candidate',
            'solver_ready',
        ),
        provenance=_provenance(FLAIR_ADMISSION.record_uri),
        semantic_sha256='',
    )
    return FlairBridgeCase(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


FLAIR_BRIDGE_CASE = build_flair_bridge_case()


__all__ = [
    'FLAIR_ADMISSION',
    'FLAIR_AUTHORITY_VERSION',
    'FLAIR_BRIDGE_CASE',
    'FLAIR_MEASUREMENT_COUNT',
    'FLAIR_SPLIT_SEED',
    'FlairBridgeCase',
    'FlairGeometryArtifact',
    'FlairGeometryStage',
    'advance_geometry_stage',
    'build_flair_bridge_case',
    'build_flair_geometry_artifact',
    'flair_measurement_id',
    'flair_split_counts',
    'flair_split_role',
    'solver_may_consume',
]
