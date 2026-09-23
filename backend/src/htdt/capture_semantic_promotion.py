from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import isfinite, sqrt
from pathlib import Path
import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_repository import SceneRepository
from .capture_ingestion_transaction import CaptureIngestionRepository
from .raw_mesh import meshbin_face_classification_label
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
WORLD_ALIGNMENT_DOMAIN = 'htdt.capture.world-to-scene-authority.v2'

CAPTURE_ALIGNMENT_ORTHONORMAL_TOLERANCE = 1.0e-4
CAPTURE_ALIGNMENT_UNIFORM_SCALE_REL_TOLERANCE = 1.0e-4
# Unit-conversion range (mm/cm/in/ft -> m and back): a uniform scale outside
# these bounds is never a legitimate capture alignment.
CAPTURE_ALIGNMENT_MIN_UNIFORM_SCALE = 1.0e-3
CAPTURE_ALIGNMENT_MAX_UNIFORM_SCALE = 1.0e3


class CaptureSemanticPromotionError(ValueError):
    pass


class CaptureAlignmentError(ValueError):
    pass


AlignmentTransformClass = Literal['rigid', 'similarity', 'affine']
CaptureAlignmentMethod = Literal[
    'identity',
    'rigid_registration',
    'uniform_scale_alignment',
    'explicit_affine_override',
]

_ALIGNMENT_METHODS_BY_CLASS: dict[AlignmentTransformClass, tuple[CaptureAlignmentMethod, ...]] = {
    'rigid': ('identity', 'rigid_registration', 'explicit_affine_override'),
    'similarity': ('uniform_scale_alignment', 'explicit_affine_override'),
    'affine': ('explicit_affine_override',),
}


def _det3(rows: tuple[tuple[float, float, float, float], ...]) -> float:
    a, b, c = rows[0][:3]
    d, e, f = rows[1][:3]
    g, h, i = rows[2][:3]
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def _classify_alignment_matrix(
    rows: tuple[tuple[float, float, float, float], ...],
) -> tuple[AlignmentTransformClass, float, float | None]:
    """Return (transform_class, determinant, uniform_scale) for an affine 4x4."""
    columns = [
        (float(rows[0][j]), float(rows[1][j]), float(rows[2][j]))
        for j in range(3)
    ]
    norms = [sqrt(sum(v * v for v in col)) for col in columns]
    if min(norms) <= 0.0:
        raise CaptureAlignmentError('capture alignment transform has a degenerate basis')
    determinant = _det3(rows)
    # Handedness policy: capture alignment must preserve orientation.
    # Reflections (det < 0) are never a valid world-to-scene alignment.
    if determinant <= 0.0:
        raise CaptureAlignmentError(
            'capture alignment transform must preserve handedness; '
            'reflections (det <= 0) are not valid alignments'
        )
    dots = []
    for i in range(3):
        for j in range(i + 1, 3):
            dots.append(
                abs(sum(columns[i][k] * columns[j][k] for k in range(3)))
                / (norms[i] * norms[j])
            )
    if max(dots) > CAPTURE_ALIGNMENT_ORTHONORMAL_TOLERANCE:
        return 'affine', determinant, None
    scale = sum(norms) / 3.0
    spread = max(abs(n - scale) for n in norms) / scale
    if spread > CAPTURE_ALIGNMENT_UNIFORM_SCALE_REL_TOLERANCE:
        return 'affine', determinant, None
    if abs(scale - 1.0) <= CAPTURE_ALIGNMENT_ORTHONORMAL_TOLERANCE:
        return 'rigid', determinant, None
    return 'similarity', determinant, scale


def _is_identity_matrix(
    rows: tuple[tuple[float, float, float, float], ...],
) -> bool:
    for i in range(4):
        for j in range(4):
            expected = 1.0 if i == j else 0.0
            if abs(float(rows[i][j]) - expected) > CAPTURE_ALIGNMENT_ORTHONORMAL_TOLERANCE:
                return False
    return True


class CaptureAlignmentScalePolicy(BaseModel):
    """Operator-declared tolerance bounds for a uniform-scale (similarity) alignment."""

    model_config = ConfigDict(frozen=True)

    min_uniform_scale: float = CAPTURE_ALIGNMENT_MIN_UNIFORM_SCALE
    max_uniform_scale: float = CAPTURE_ALIGNMENT_MAX_UNIFORM_SCALE

    @model_validator(mode='after')
    def validate_bounds(self) -> 'CaptureAlignmentScalePolicy':
        if not isfinite(self.min_uniform_scale) or self.min_uniform_scale <= 0.0:
            raise ValueError('min_uniform_scale must be finite and positive')
        if not isfinite(self.max_uniform_scale):
            raise ValueError('max_uniform_scale must be finite')
        if self.min_uniform_scale > self.max_uniform_scale:
            raise ValueError('min_uniform_scale must not exceed max_uniform_scale')
        return self

    def allows(self, uniform_scale: float) -> bool:
        return self.min_uniform_scale <= uniform_scale <= self.max_uniform_scale


class CaptureAlignmentInspection(BaseModel):
    """Classification of a candidate alignment — the 'guided' surface operators see."""

    model_config = ConfigDict(frozen=True)

    transform_class: AlignmentTransformClass
    determinant: float
    uniform_scale_m_per_capture_m: float | None = None
    allowed_methods: tuple[CaptureAlignmentMethod, ...]
    requires_explicit_scale_provenance: bool
    requires_affine_override: bool


def inspect_capture_alignment(
    transform: SemanticCoordinateTransform,
) -> CaptureAlignmentInspection:
    """Classify a candidate world-to-scene transform so an operator can choose a method."""
    transform_class, determinant, scale = _classify_alignment_matrix(
        transform.matrix_source_to_scene_m
    )
    return CaptureAlignmentInspection(
        transform_class=transform_class,
        determinant=determinant,
        uniform_scale_m_per_capture_m=scale,
        allowed_methods=_ALIGNMENT_METHODS_BY_CLASS[transform_class],
        requires_explicit_scale_provenance=transform_class == 'similarity',
        requires_affine_override=transform_class == 'affine',
    )


def validate_capture_alignment(
    transform: SemanticCoordinateTransform,
    *,
    alignment_method: CaptureAlignmentMethod = 'rigid_registration',
    scale_policy: CaptureAlignmentScalePolicy | None = None,
) -> CaptureAlignmentInspection:
    """Enforce the alignment-method contract on a candidate transform.

    Shared by world-to-scene authorities (#347) and cross-revision
    registration records (#589/#395): the same class/method rules apply —
    rigid by default, uniform scale only via ``uniform_scale_alignment``
    inside declared bounds, shear/non-uniform scale only via the explicit
    affine override, reflections never.
    """
    inspection = inspect_capture_alignment(transform)
    if alignment_method not in inspection.allowed_methods:
        raise CaptureAlignmentError(
            f'capture alignment method {alignment_method} is not permitted '
            f'for a {inspection.transform_class} transform; allowed: '
            + ', '.join(inspection.allowed_methods)
        )
    if alignment_method == 'identity' and not _is_identity_matrix(
        transform.matrix_source_to_scene_m
    ):
        raise CaptureAlignmentError(
            "capture alignment method 'identity' requires an identity matrix"
        )
    if inspection.transform_class == 'similarity':
        policy = scale_policy or CaptureAlignmentScalePolicy()
        scale = inspection.uniform_scale_m_per_capture_m
        assert scale is not None
        if not policy.allows(scale):
            raise CaptureAlignmentError(
                f'uniform scale {scale!r} is outside the declared policy '
                f'[{policy.min_uniform_scale!r}, {policy.max_uniform_scale!r}]'
            )
    return inspection


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
    """Explicit mapping from one Capture coordinate space into HTDT scene axes.

    The authority pins the transform *class* and the operator-declared
    *method* into its identity: metric-preserving rigid alignment is the
    default, uniform scale is only admitted via the ``uniform_scale_alignment``
    method (which records the measured scale as provenance), and general
    affine content (shear, non-uniform scale) only via the deliberate
    ``explicit_affine_override`` method. Reflections are always rejected.
    """

    model_config = ConfigDict(frozen=True)

    authority_id: str = Field(pattern=r'^capture-world-to-scene:[0-9a-f]{64}$')
    coordinate_space_id: str = Field(
        pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
    )
    transform: SemanticCoordinateTransform
    transform_class: AlignmentTransformClass
    alignment_method: CaptureAlignmentMethod
    uniform_scale_m_per_capture_m: float | None = None

    @model_validator(mode='after')
    def validate_alignment_contract(self) -> 'CaptureWorldToSceneAuthority':
        transform_class, _determinant, scale = _classify_alignment_matrix(
            self.transform.matrix_source_to_scene_m
        )
        if transform_class != self.transform_class:
            raise ValueError(
                'capture world-to-scene transform_class mismatch: declared '
                f'{self.transform_class}, matrix is {transform_class}'
            )
        if self.alignment_method not in _ALIGNMENT_METHODS_BY_CLASS[transform_class]:
            raise ValueError(
                f'capture alignment method {self.alignment_method} is not '
                f'permitted for a {transform_class} transform'
            )
        if transform_class == 'similarity':
            if self.uniform_scale_m_per_capture_m is None:
                raise ValueError('similarity alignment must record its uniform scale')
            if abs(self.uniform_scale_m_per_capture_m - float(scale)) > 1.0e-9:
                raise ValueError('recorded uniform scale does not match the transform matrix')
        elif self.uniform_scale_m_per_capture_m is not None:
            raise ValueError(
                'uniform_scale_m_per_capture_m is only recorded for similarity alignments'
            )
        if self.alignment_method == 'identity' and not _is_identity_matrix(
            self.transform.matrix_source_to_scene_m
        ):
            raise ValueError("alignment method 'identity' requires an identity matrix")
        return self

    @model_validator(mode='after')
    def validate_identity(self) -> 'CaptureWorldToSceneAuthority':
        payload = {
            'coordinate_space_id': self.coordinate_space_id,
            'transform': self.transform.model_dump(mode='json'),
            'transform_class': self.transform_class,
            'alignment_method': self.alignment_method,
            'uniform_scale_m_per_capture_m': self.uniform_scale_m_per_capture_m,
        }
        expected = f'capture-world-to-scene:{_semantic_hash({"domain": WORLD_ALIGNMENT_DOMAIN, **payload})}'
        if self.authority_id != expected:
            raise ValueError('capture world-to-scene authority_id mismatch')
        return self


def make_capture_world_to_scene_authority(
    *,
    coordinate_space_id: str,
    transform: SemanticCoordinateTransform,
    alignment_method: CaptureAlignmentMethod = 'rigid_registration',
    scale_policy: CaptureAlignmentScalePolicy | None = None,
) -> CaptureWorldToSceneAuthority:
    """Bind a Capture coordinate space to scene axes under an explicit method.

    Fails closed: shear/non-uniform scale is rejected for every method except
    the deliberate ``explicit_affine_override``; uniform scale is rejected
    unless ``uniform_scale_alignment`` is chosen and the measured factor is
    inside the declared scale policy bounds.
    """
    inspection = validate_capture_alignment(
        transform,
        alignment_method=alignment_method,
        scale_policy=scale_policy,
    )
    transform_class = inspection.transform_class
    uniform_scale = inspection.uniform_scale_m_per_capture_m
    payload = {
        'coordinate_space_id': coordinate_space_id,
        'transform': transform.model_dump(mode='json'),
        'transform_class': transform_class,
        'alignment_method': alignment_method,
        'uniform_scale_m_per_capture_m': uniform_scale,
    }
    authority_id = (
        'capture-world-to-scene:'
        + _semantic_hash({'domain': WORLD_ALIGNMENT_DOMAIN, **payload})
    )
    return CaptureWorldToSceneAuthority(
        authority_id=authority_id,
        coordinate_space_id=coordinate_space_id,
        transform=transform,
        transform_class=transform_class,
        alignment_method=alignment_method,
        uniform_scale_m_per_capture_m=uniform_scale,
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


class CaptureMeshFaceClassificationAdvisory(BaseModel):
    """One retained HTDTMSH1 face-classification byte with its advisory label.

    This is provenance for inspection and operator suggestions only. It is
    never semantic or acoustic authority: promoting a mesh still requires
    explicit ``SurfaceSemanticAssignment`` records.
    """

    model_config = ConfigDict(frozen=True)

    source_primitive: str = Field(min_length=1)
    classification_value: int = Field(ge=0, le=255)
    classification_label: str = Field(min_length=1)


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
    source_normals_present: bool = False
    face_classification_advisories: tuple[
        CaptureMeshFaceClassificationAdvisory, ...
    ] = ()


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


_CAPTURE_SEMANTIC_PROMOTIONS_DDL = '''
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
                    FOREIGN KEY(ingestion_lineage_digest, raw_mesh_binding_id)
                        REFERENCES capture_ingestion_mesh_links(
                            lineage_digest,
                            binding_id
                        )
                        ON DELETE RESTRICT,
                    FOREIGN KEY(source_scene_revision_id)
                        REFERENCES scene_revisions(revision_id),
                    FOREIGN KEY(scene_revision_id)
                        REFERENCES scene_revisions(revision_id)
                )
                '''


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
            self._migrate_promotion_link_fk(connection)
            connection.execute(_CAPTURE_SEMANTIC_PROMOTIONS_DDL)
            connection.execute(
                '''
                CREATE INDEX IF NOT EXISTS idx_capture_semantic_promotion_ingestion
                ON capture_semantic_promotions(
                    ingestion_lineage_digest,
                    raw_mesh_binding_id
                )
                '''
            )

    @staticmethod
    def _migrate_promotion_link_fk(connection: sqlite3.Connection) -> None:
        """Rebuild a pre-normalization promotions table with the exact link FK.

        ``capture_semantic_promotions`` originally enforced the ingestion run
        and the mesh binding through two independent foreign keys, so the
        database could persist a promotion whose (lineage, binding) pair was
        never linked — or lose the link row afterwards while every individual
        FK stayed valid. The durable provenance edge is the pair itself:
        rebuild the table so it references
        ``capture_ingestion_mesh_links(lineage_digest, binding_id)`` and fail
        closed — naming the offending promotions — when a persisted row is
        already provenance-orphaned, rather than silently adopting it.
        """

        tables = {
            str(row['name'])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if 'capture_semantic_promotions' not in tables:
            return
        foreign_keys = connection.execute(
            'PRAGMA foreign_key_list(capture_semantic_promotions)'
        ).fetchall()
        if any(
            str(row['table']) == 'capture_ingestion_mesh_links'
            for row in foreign_keys
        ):
            return
        if 'capture_ingestion_mesh_links' not in tables:
            raise CaptureSemanticPromotionError(
                'cannot normalize capture semantic promotions: '
                'capture_ingestion_mesh_links is missing'
            )
        orphans = connection.execute(
            '''
            SELECT p.promotion_id
            FROM capture_semantic_promotions AS p
            LEFT JOIN capture_ingestion_mesh_links AS l
              ON l.lineage_digest = p.ingestion_lineage_digest
             AND l.binding_id = p.raw_mesh_binding_id
            WHERE l.lineage_digest IS NULL
            ORDER BY p.promotion_id
            '''
        ).fetchall()
        if orphans:
            ids = ', '.join(str(row['promotion_id']) for row in orphans)
            raise CaptureSemanticPromotionError(
                'persisted capture semantic promotions are not backed by an '
                'ingestion-mesh link; refusing to normalize provenance '
                f'foreign keys: {ids}'
            )

        # RENAME/CREATE/DROP are autocommitted when no transaction is open,
        # so the rebuild runs inside an explicit transaction: either the
        # whole normalized table replaces the legacy one or nothing changes
        # and a later open retries the migration deterministically.
        if connection.in_transaction:
            connection.commit()
        connection.execute('BEGIN IMMEDIATE')
        try:
            connection.execute(
                '''
                ALTER TABLE capture_semantic_promotions
                RENAME TO capture_semantic_promotions_legacy
                '''
            )
            connection.execute(_CAPTURE_SEMANTIC_PROMOTIONS_DDL)
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
                )
                SELECT
                    promotion_id,
                    ingestion_lineage_digest,
                    raw_mesh_binding_id,
                    source_scene_revision_id,
                    scene_revision_id,
                    semantic_geometry_id,
                    request_json,
                    created_at_utc
                FROM capture_semantic_promotions_legacy
                '''
            )
            connection.execute('DROP TABLE capture_semantic_promotions_legacy')
            connection.commit()
        except Exception:
            connection.rollback()
            raise

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
            source_normals_present=bool(binding.raw_mesh.source_normals),
            face_classification_advisories=tuple(
                CaptureMeshFaceClassificationAdvisory(
                    source_primitive=triangle.source_primitive,
                    classification_value=value,
                    classification_label=(
                        meshbin_face_classification_label(value)
                    ),
                )
                for triangle, value in zip(
                    binding.raw_mesh.triangles,
                    binding.raw_mesh.source_face_classifications,
                )
            ),
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
