from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_repository import SceneRepository
from .capture_ingestion_transaction import CaptureIngestionRepository
from .semantic_geometry import (
    RawMeshDiagnosticFinding,
    SemanticAcousticGeometry,
    SemanticCoordinateTransform,
    SemanticGeometryConversionProfile,
    SurfaceSemanticAssignment,
    convert_raw_visual_mesh_to_semantic_geometry,
    diagnose_raw_visual_mesh,
    make_semantic_geometry_conversion_request,
    raw_triangle_ids,
)


PROMOTION_DOMAIN = 'htdt.capture.semantic-promotion.v1'
WORLD_ALIGNMENT_DOMAIN = 'htdt.capture.world-to-scene-authority.v1'


class CaptureSemanticPromotionError(ValueError):
    pass


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _semantic_hash(value: object) -> str:
    return sha256(_canonical_json(value).encode('utf-8')).hexdigest()


class CaptureWorldToSceneAuthority(BaseModel):
    """Explicit mapping from one Capture coordinate space into HTDT scene axes."""

    model_config = ConfigDict(frozen=True)

    authority_id: str = Field(pattern=r'^capture-world-to-scene:[0-9a-f]{64}$')
    coordinate_space_id: str = Field(
        pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
    )
    transform: SemanticCoordinateTransform

    @model_validator(mode='after')
    def validate_identity(self) -> 'CaptureWorldToSceneAuthority':
        payload = {
            'coordinate_space_id': self.coordinate_space_id,
            'transform': self.transform.model_dump(mode='json'),
        }
        expected = f'capture-world-to-scene:{_semantic_hash({"domain": WORLD_ALIGNMENT_DOMAIN, **payload})}'
        if self.authority_id != expected:
            raise ValueError('capture world-to-scene authority_id mismatch')
        return self


def make_capture_world_to_scene_authority(
    *,
    coordinate_space_id: str,
    transform: SemanticCoordinateTransform,
) -> CaptureWorldToSceneAuthority:
    payload = {
        'coordinate_space_id': coordinate_space_id,
        'transform': transform.model_dump(mode='json'),
    }
    authority_id = (
        'capture-world-to-scene:'
        + _semantic_hash({'domain': WORLD_ALIGNMENT_DOMAIN, **payload})
    )
    return CaptureWorldToSceneAuthority(
        authority_id=authority_id,
        coordinate_space_id=coordinate_space_id,
        transform=transform,
    )


PromotionReadinessPolicy = Literal[
    'allow_blocked_semantic_authority',
    'require_r120_compiler_contract_ready',
]


class CaptureSemanticPromotionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    promotion_id: str = Field(pattern=r'^capture-semantic-promotion:[0-9a-f]{64}$')
    ingestion_lineage_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    raw_mesh_binding_id: str = Field(
        pattern=r'^capture-raw-mesh-binding:[0-9a-f]{64}$'
    )
    target_document_id: str = Field(min_length=1)
    source_scene_revision_id: str = Field(min_length=1)
    world_to_scene_authority: CaptureWorldToSceneAuthority
    profile: SemanticGeometryConversionProfile = Field(
        default_factory=SemanticGeometryConversionProfile
    )
    surface_assignments: tuple[SurfaceSemanticAssignment, ...] = ()
    readiness_policy: PromotionReadinessPolicy
    reason: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_identity(self) -> 'CaptureSemanticPromotionRequest':
        payload = self.model_dump(
            mode='json',
            exclude={'promotion_id'},
        )
        expected = (
            'capture-semantic-promotion:'
            + _semantic_hash({'domain': PROMOTION_DOMAIN, **payload})
        )
        if self.promotion_id != expected:
            raise ValueError('capture semantic promotion_id mismatch')
        return self


def make_capture_semantic_promotion_request(
    *,
    ingestion_lineage_digest: str,
    raw_mesh_binding_id: str,
    target_document_id: str,
    source_scene_revision_id: str,
    world_to_scene_authority: CaptureWorldToSceneAuthority,
    readiness_policy: PromotionReadinessPolicy,
    reason: str,
    profile: SemanticGeometryConversionProfile | None = None,
    surface_assignments: tuple[SurfaceSemanticAssignment, ...] = (),
) -> CaptureSemanticPromotionRequest:
    core = {
        'ingestion_lineage_digest': ingestion_lineage_digest,
        'raw_mesh_binding_id': raw_mesh_binding_id,
        'target_document_id': target_document_id,
        'source_scene_revision_id': source_scene_revision_id,
        'world_to_scene_authority': world_to_scene_authority,
        'profile': profile or SemanticGeometryConversionProfile(),
        'surface_assignments': surface_assignments,
        'readiness_policy': readiness_policy,
        'reason': reason,
    }
    payload = {
        key: (
            value.model_dump(mode='json')
            if isinstance(value, BaseModel)
            else [item.model_dump(mode='json') for item in value]
            if key == 'surface_assignments'
            else value
        )
        for key, value in core.items()
    }
    promotion_id = (
        'capture-semantic-promotion:'
        + _semantic_hash({'domain': PROMOTION_DOMAIN, **payload})
    )
    return CaptureSemanticPromotionRequest(
        promotion_id=promotion_id,
        **core,
    )


class CaptureSemanticMeshInspection(BaseModel):
    model_config = ConfigDict(frozen=True)

    ingestion_lineage_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    raw_mesh_binding_id: str = Field(
        pattern=r'^capture-raw-mesh-binding:[0-9a-f]{64}$'
    )
    capture_coordinate_space_id: str
    raw_mesh_id: str
    raw_mesh_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    original_asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    triangle_ids: tuple[str, ...]
    diagnostic_id: str
    diagnostic_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    findings: tuple[RawMeshDiagnosticFinding, ...]


@dataclass(frozen=True)
class CaptureSemanticPromotionResult:
    promotion_id: str
    scene_revision_id: str
    semantic_geometry_id: str
    geometry_compiler_readiness: str
    promotion_created: bool
    scene_revision_created: bool


def _capture_matrix_rows(
    values: tuple[float, ...],
) -> tuple[
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
]:
    if len(values) != 16 or any(not isfinite(float(value)) for value in values):
        raise CaptureSemanticPromotionError('capture anchor transform is invalid')
    rows = tuple(
        tuple(float(values[column * 4 + row]) for column in range(4))
        for row in range(4)
    )
    return rows  # type: ignore[return-value]


def _matmul4(
    left: tuple[tuple[float, float, float, float], ...],
    right: tuple[tuple[float, float, float, float], ...],
) -> tuple[
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
]:
    result = tuple(
        tuple(
            sum(float(left[row][k]) * float(right[k][column]) for k in range(4))
            for column in range(4)
        )
        for row in range(4)
    )
    return result  # type: ignore[return-value]


class CaptureSemanticPromotionRepository:
    """Explicit Capture RawVisualMesh -> SemanticAcousticGeometry promotion."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        capture_repository: CaptureIngestionRepository,
    ) -> None:
        if Path(scene_repository.path).resolve() != Path(capture_repository.path).resolve():
            raise ValueError('capture and scene repositories must share one native database')
        self.scene_repository = scene_repository
        self.capture_repository = capture_repository
        self.path = Path(scene_repository.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS capture_semantic_promotions (
                    promotion_id TEXT PRIMARY KEY,
                    ingestion_lineage_digest TEXT NOT NULL,
                    raw_mesh_binding_id TEXT NOT NULL,
                    source_scene_revision_id TEXT NOT NULL,
                    scene_revision_id TEXT NOT NULL,
                    semantic_geometry_id TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    FOREIGN KEY(ingestion_lineage_digest)
                        REFERENCES capture_ingestion_runs(lineage_digest),
                    FOREIGN KEY(raw_mesh_binding_id)
                        REFERENCES capture_raw_visual_mesh_bindings(binding_id),
                    FOREIGN KEY(source_scene_revision_id)
                        REFERENCES scene_revisions(revision_id),
                    FOREIGN KEY(scene_revision_id)
                        REFERENCES scene_revisions(revision_id)
                )
                '''
            )
            connection.execute(
                '''
                CREATE INDEX IF NOT EXISTS idx_capture_semantic_promotion_ingestion
                ON capture_semantic_promotions(
                    ingestion_lineage_digest,
                    raw_mesh_binding_id
                )
                '''
            )

    def inspect_capture_mesh(
        self,
        *,
        ingestion_lineage_digest: str,
        raw_mesh_binding_id: str,
        profile: SemanticGeometryConversionProfile | None = None,
    ) -> CaptureSemanticMeshInspection:
        binding = self._resolve_binding(
            ingestion_lineage_digest,
            raw_mesh_binding_id,
        )
        profile = profile or SemanticGeometryConversionProfile()
        diagnostic = diagnose_raw_visual_mesh(
            binding.raw_mesh,
            profile=profile.diagnostic_profile,
        )
        return CaptureSemanticMeshInspection(
            ingestion_lineage_digest=ingestion_lineage_digest,
            raw_mesh_binding_id=raw_mesh_binding_id,
            capture_coordinate_space_id=binding.handoff.coordinate_space_id,
            raw_mesh_id=binding.raw_mesh.mesh_id,
            raw_mesh_semantic_hash=binding.raw_mesh.semantic_hash(),
            original_asset_sha256=binding.raw_mesh.provenance.original_asset_sha256,
            triangle_ids=raw_triangle_ids(binding.raw_mesh),
            diagnostic_id=diagnostic.diagnostic_id,
            diagnostic_semantic_hash=diagnostic.semantic_hash(),
            findings=diagnostic.findings,
        )

    def promote(
        self,
        request: CaptureSemanticPromotionRequest,
    ) -> CaptureSemanticPromotionResult:
        binding = self._resolve_binding(
            request.ingestion_lineage_digest,
            request.raw_mesh_binding_id,
        )
        if (
            request.world_to_scene_authority.coordinate_space_id
            != binding.handoff.coordinate_space_id
        ):
            raise CaptureSemanticPromotionError(
                'capture world-to-scene authority coordinate space mismatch'
            )

        source_revision = self.scene_repository.get(
            request.source_scene_revision_id
        )
        if source_revision is None:
            raise CaptureSemanticPromotionError('source SceneRevision not found')
        if source_revision.document_id != request.target_document_id:
            raise CaptureSemanticPromotionError(
                'source SceneRevision belongs to a different document'
            )

        capture_anchor = _capture_matrix_rows(
            binding.handoff.world_from_mesh_anchor.values
        )
        scene_from_capture_world = (
            request.world_to_scene_authority.transform.matrix_source_to_scene_m
        )
        scene_from_anchor = _matmul4(
            scene_from_capture_world,
            capture_anchor,
        )
        source_to_scene = SemanticCoordinateTransform(
            matrix_source_to_scene_m=scene_from_anchor,
            provenance=request.world_to_scene_authority.transform.provenance,
            reason=(
                request.world_to_scene_authority.transform.reason
                + '; composed with exact Capture T_world_from_mesh_anchor '
                + binding.handoff.raw_visual_mesh_handoff_id
            ),
        )
        conversion_request = make_semantic_geometry_conversion_request(
            binding.raw_mesh,
            source_scene_revision_id=request.source_scene_revision_id,
            source_to_scene_transform=source_to_scene,
            profile=request.profile,
            surface_assignments=request.surface_assignments,
        )
        geometry = convert_raw_visual_mesh_to_semantic_geometry(
            binding.raw_mesh,
            conversion_request,
        )
        if (
            request.readiness_policy
            == 'require_r120_compiler_contract_ready'
            and geometry.geometry_compiler_readiness
            != 'ready_for_r120_geometry_compiler_contract'
        ):
            raise CaptureSemanticPromotionError(
                'semantic geometry does not satisfy required R120 compiler-contract readiness'
            )

        updated_document = source_revision.document.model_copy(
            update={
                'schema_version': max(4, source_revision.document.schema_version),
                'r120_semantic_geometry': geometry,
            }
        )
        request_json = _canonical_json(request.model_dump(mode='json'))

        with closing(self._connect()) as connection:
            try:
                connection.execute('BEGIN IMMEDIATE')
                existing = connection.execute(
                    '''
                    SELECT *
                    FROM capture_semantic_promotions
                    WHERE promotion_id=?
                    ''',
                    (request.promotion_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing['request_json'] != request_json
                        or existing['semantic_geometry_id'] != geometry.geometry_id
                    ):
                        raise CaptureSemanticPromotionError(
                            'promotion identity already exists with different semantics'
                        )
                    connection.rollback()
                    return CaptureSemanticPromotionResult(
                        promotion_id=request.promotion_id,
                        scene_revision_id=existing['scene_revision_id'],
                        semantic_geometry_id=existing['semantic_geometry_id'],
                        geometry_compiler_readiness=geometry.geometry_compiler_readiness,
                        promotion_created=False,
                        scene_revision_created=False,
                    )

                linked = connection.execute(
                    '''
                    SELECT 1
                    FROM capture_ingestion_mesh_links
                    WHERE lineage_digest=? AND binding_id=?
                    ''',
                    (
                        request.ingestion_lineage_digest,
                        request.raw_mesh_binding_id,
                    ),
                ).fetchone()
                if linked is None:
                    raise CaptureSemanticPromotionError(
                        'raw mesh binding is not linked to the requested ingestion'
                    )

                save = self.scene_repository._save_in_transaction(
                    connection,
                    updated_document,
                    parent_revision_id=request.source_scene_revision_id,
                )
                connection.execute(
                    '''
                    INSERT INTO capture_semantic_promotions(
                        promotion_id,
                        ingestion_lineage_digest,
                        raw_mesh_binding_id,
                        source_scene_revision_id,
                        scene_revision_id,
                        semantic_geometry_id,
                        request_json,
                        created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (
                        request.promotion_id,
                        request.ingestion_lineage_digest,
                        request.raw_mesh_binding_id,
                        request.source_scene_revision_id,
                        save.revision.revision_id,
                        geometry.geometry_id,
                        request_json,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return CaptureSemanticPromotionResult(
            promotion_id=request.promotion_id,
            scene_revision_id=save.revision.revision_id,
            semantic_geometry_id=geometry.geometry_id,
            geometry_compiler_readiness=geometry.geometry_compiler_readiness,
            promotion_created=True,
            scene_revision_created=save.created,
        )

    def _resolve_binding(
        self,
        ingestion_lineage_digest: str,
        raw_mesh_binding_id: str,
    ):
        if self.capture_repository.get_ingestion(ingestion_lineage_digest) is None:
            raise CaptureSemanticPromotionError('capture ingestion not found')
        binding_ids = self.capture_repository.mesh_binding_ids_for_ingestion(
            ingestion_lineage_digest
        )
        if raw_mesh_binding_id not in binding_ids:
            raise CaptureSemanticPromotionError(
                'raw mesh binding is not linked to the requested ingestion'
            )
        binding = self.capture_repository.get_mesh_binding(raw_mesh_binding_id)
        if binding is None:
            raise CaptureSemanticPromotionError('raw mesh binding not found')
        return binding
