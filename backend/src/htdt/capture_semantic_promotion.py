from __future__ import annotations

from base64 import b64encode
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
from .cad_schema import require_native_tables
from .capture_ingestion_transaction import (
    CaptureCoordinateAuthority,
    CaptureIngestionRepository,
    CaptureQualityGateError,
)
from .capture_mesh_ingestion import CaptureRawVisualMeshBinding
from .content_blobs import store_content_blob
from .raw_mesh import (
    RawMeshImportProvenance,
    RawMeshTriangle,
    RawMeshVertex,
    RawVisualMesh,
    meshbin_face_classification_label,
)
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
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _semantic_hash


PROMOTION_DOMAIN = 'htdt.capture.semantic-promotion.v1'
WORLD_ALIGNMENT_DOMAIN = 'htdt.capture.world-to-scene-authority.v2'

CAPTURE_ALIGNMENT_ORTHONORMAL_TOLERANCE = 1.0e-4
CAPTURE_ALIGNMENT_UNIFORM_SCALE_REL_TOLERANCE = 1.0e-4
# Unit-conversion range (mm/cm/in/ft -> m and back): a uniform scale outside
# these bounds is never a legitimate capture alignment.
CAPTURE_ALIGNMENT_MIN_UNIFORM_SCALE = 1.0e-3
CAPTURE_ALIGNMENT_MAX_UNIFORM_SCALE = 1.0e3
COMPOSITION_DOMAIN = 'htdt.capture.mesh-composition.v1'
COMPOSITION_SCHEMA_VERSION = '1.0.0'
# The only overlap policy this contract supports: every selected triangle is
# retained verbatim in capture-world coordinates, namespaced by its source
# binding so provenance stays exact. Overlaps are not welded, dropped, or
# re-ordered; diagnostics report them on the complete selection.
COMPOSITION_OVERLAP_POLICY = 'retain_all_verbatim'


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


class CapturePromotionReplayError(CaptureSemanticPromotionError):
    """A persisted promotion failed replay verification (#368).

    ``diagnostic`` distinguishes durable policy failures (an unsupported
    converter version) from integrity failures (a derivation mismatch).
    """

    def __init__(
        self,
        message: str,
        *,
        promotion_id: str,
        diagnostic: str,
    ) -> None:
        super().__init__(message)
        self.promotion_id = promotion_id
        self.diagnostic = diagnostic






def _authority_identity_payload(
    *,
    coordinate_space_id: str,
    coordinate_authority_id: str | None,
    transform: SemanticCoordinateTransform,
    transform_class: AlignmentTransformClass,
    alignment_method: CaptureAlignmentMethod,
    uniform_scale_m_per_capture_m: float | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        'coordinate_space_id': coordinate_space_id,
        'transform': transform.model_dump(mode='json'),
        'transform_class': transform_class,
        'alignment_method': alignment_method,
        'uniform_scale_m_per_capture_m': uniform_scale_m_per_capture_m,
    }
    if coordinate_authority_id is not None:
        payload['coordinate_authority_id'] = coordinate_authority_id
    return payload


class CaptureWorldToSceneAuthority(BaseModel):
    """Explicit mapping from one Capture coordinate space into HTDT scene axes.

    The authority pins the transform *class* and the operator-declared
    *method* into its identity: metric-preserving rigid alignment is the
    default, uniform scale is only admitted via the ``uniform_scale_alignment``
    method (which records the measured scale as provenance), and general
    affine content (shear, non-uniform scale) only via the deliberate
    ``explicit_affine_override`` method. Reflections are always rejected.

    ``coordinate_authority_id`` binds the alignment to the registry entry
    for (bundle_digest, coordinate_space_id) — the scoped authority of the
    exact immutable capture identity (#365). It stays Optional only so
    pre-registry persisted requests and bare coordinate-space alignments
    still parse; registry-scoped requests minted through
    ``make_capture_world_to_scene_authority`` carry it, and ``promote``
    fails closed when it is absent.
    """

    model_config = ConfigDict(frozen=True)

    authority_id: str = Field(pattern=r'^capture-world-to-scene:[0-9a-f]{64}$')
    coordinate_space_id: str = Field(
        pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
    )
    coordinate_authority_id: str | None = Field(
        default=None,
        pattern=r'^capture-coordinate-authority:[0-9a-f]{64}$',
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
        payload = _authority_identity_payload(
            coordinate_space_id=self.coordinate_space_id,
            coordinate_authority_id=self.coordinate_authority_id,
            transform=self.transform,
            transform_class=self.transform_class,
            alignment_method=self.alignment_method,
            uniform_scale_m_per_capture_m=self.uniform_scale_m_per_capture_m,
        )
        expected = f'capture-world-to-scene:{_semantic_hash({"domain": WORLD_ALIGNMENT_DOMAIN, **payload})}'
        if self.authority_id != expected:
            raise ValueError('capture world-to-scene authority_id mismatch')
        return self


def make_capture_world_to_scene_authority(
    *,
    coordinate_space_id: str | None = None,
    coordinate_authority: CaptureCoordinateAuthority | None = None,
    transform: SemanticCoordinateTransform,
    alignment_method: CaptureAlignmentMethod = 'rigid_registration',
    scale_policy: CaptureAlignmentScalePolicy | None = None,
) -> CaptureWorldToSceneAuthority:
    """Bind a Capture coordinate space to scene axes under an explicit method.

    Fails closed: shear/non-uniform scale is rejected for every method except
    the deliberate ``explicit_affine_override``; uniform scale is rejected
    unless ``uniform_scale_alignment`` is chosen and the measured factor is
    inside the declared scale policy bounds.

    Pass ``coordinate_authority`` (the registry entry for
    (bundle_digest, space)) to scope the authority to the exact immutable
    capture identity (#365); a bare ``coordinate_space_id`` produces an
    operator-declared alignment that ``promote`` will refuse.
    """
    if (coordinate_space_id is None) == (coordinate_authority is None):
        raise CaptureAlignmentError(
            'exactly one of coordinate_space_id or coordinate_authority '
            'is required'
        )
    inspection = validate_capture_alignment(
        transform,
        alignment_method=alignment_method,
        scale_policy=scale_policy,
    )
    transform_class = inspection.transform_class
    uniform_scale = inspection.uniform_scale_m_per_capture_m
    payload = _authority_identity_payload(
        coordinate_space_id=(
            coordinate_authority.coordinate_space_id
            if coordinate_authority is not None
            else str(coordinate_space_id)
        ),
        coordinate_authority_id=(
            coordinate_authority.coordinate_authority_id
            if coordinate_authority is not None
            else None
        ),
        transform=transform,
        transform_class=transform_class,
        alignment_method=alignment_method,
        uniform_scale_m_per_capture_m=uniform_scale,
    )
    authority_id = (
        'capture-world-to-scene:'
        + _semantic_hash({'domain': WORLD_ALIGNMENT_DOMAIN, **payload})
    )
    return CaptureWorldToSceneAuthority(
        authority_id=authority_id,
        coordinate_space_id=payload['coordinate_space_id'],
        coordinate_authority_id=(
            coordinate_authority.coordinate_authority_id
            if coordinate_authority is not None
            else None
        ),
        transform=transform,
        transform_class=transform_class,
        alignment_method=alignment_method,
        uniform_scale_m_per_capture_m=uniform_scale,
    )


PromotionReadinessPolicy = Literal[
    'allow_blocked_semantic_authority',
    'require_r120_compiler_contract_ready',
]

# The one supported conflict policy for promoting onto a SceneRevision that
# already carries semantic geometry (#359): the caller declares it intends
# an exact replacement bound to the expected prior geometry identity and
# semantic hash, recorded as supersession lineage on the promotion row.
GeometryConflictPolicy = Literal['replace_exact']


class CaptureSemanticPromotionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    promotion_id: str = Field(pattern=r'^capture-semantic-promotion:[0-9a-f]{64}$')
    ingestion_run_id: str = Field(
        pattern=r'^capture-ingestion-run:[0-9a-f]{64}$'
    )
    raw_mesh_binding_id: str | None = Field(
        default=None,
        pattern=r'^capture-raw-mesh-binding:[0-9a-f]{64}$',
    )
    mesh_composition_id: str | None = Field(
        default=None,
        pattern=r'^capture-mesh-composition:[0-9a-f]{64}$',
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
    geometry_conflict_policy: GeometryConflictPolicy | None = None
    expected_prior_geometry_id: str | None = Field(
        default=None,
        pattern=r'^semantic-acoustic-geometry:[0-9a-f]{64}$',
    )
    expected_prior_geometry_semantic_hash: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )

    @model_validator(mode='after')
    def validate_identity(self) -> 'CaptureSemanticPromotionRequest':
        if (self.raw_mesh_binding_id is None) == (
            self.mesh_composition_id is None
        ):
            raise ValueError(
                'exactly one of raw_mesh_binding_id or '
                'mesh_composition_id is required'
            )
        if self.geometry_conflict_policy is None:
            if (
                self.expected_prior_geometry_id is not None
                or self.expected_prior_geometry_semantic_hash is not None
            ):
                raise ValueError(
                    'expected prior geometry fields require an explicit '
                    'geometry_conflict_policy'
                )
        elif (
            self.expected_prior_geometry_id is None
            or self.expected_prior_geometry_semantic_hash is None
        ):
            raise ValueError(
                'geometry_conflict_policy requires the expected prior '
                'geometry id and semantic hash'
            )
        payload = self.model_dump(
            mode='json',
            exclude={'promotion_id'},
            exclude_none=True,
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
    ingestion_run_id: str,
    target_document_id: str,
    source_scene_revision_id: str,
    world_to_scene_authority: CaptureWorldToSceneAuthority,
    readiness_policy: PromotionReadinessPolicy,
    reason: str,
    raw_mesh_binding_id: str | None = None,
    mesh_composition_id: str | None = None,
    profile: SemanticGeometryConversionProfile | None = None,
    surface_assignments: tuple[SurfaceSemanticAssignment, ...] = (),
    geometry_conflict_policy: GeometryConflictPolicy | None = None,
    expected_prior_geometry_id: str | None = None,
    expected_prior_geometry_semantic_hash: str | None = None,
) -> CaptureSemanticPromotionRequest:
    core = {
        'ingestion_run_id': ingestion_run_id,
        'raw_mesh_binding_id': raw_mesh_binding_id,
        'mesh_composition_id': mesh_composition_id,
        'target_document_id': target_document_id,
        'source_scene_revision_id': source_scene_revision_id,
        'world_to_scene_authority': world_to_scene_authority,
        'profile': profile or SemanticGeometryConversionProfile(),
        'surface_assignments': surface_assignments,
        'readiness_policy': readiness_policy,
        'reason': reason,
        'geometry_conflict_policy': geometry_conflict_policy,
        'expected_prior_geometry_id': expected_prior_geometry_id,
        'expected_prior_geometry_semantic_hash': (
            expected_prior_geometry_semantic_hash
        ),
    }
    payload = {
        key: (
            value.model_dump(mode='json', exclude_none=True)
            if isinstance(value, BaseModel)
            else [item.model_dump(mode='json') for item in value]
            if key == 'surface_assignments'
            else value
        )
        for key, value in core.items()
        if value is not None
    }
    promotion_id = (
        'capture-semantic-promotion:'
        + _semantic_hash({'domain': PROMOTION_DOMAIN, **payload})
    )
    return CaptureSemanticPromotionRequest(
        promotion_id=promotion_id,
        **core,
    )


class CaptureMeshCompositionRequest(BaseModel):
    """Deterministic request composing several mesh bindings of one run.

    ``raw_mesh_binding_ids`` is the explicit selected set: it must be
    sorted and unique, and every binding must belong to the same ingestion
    run and share one coordinate space — changing the selection, the
    declared scoped coordinate authority, or the overlap policy changes the
    composition identity.
    """

    model_config = ConfigDict(frozen=True)

    composition_id: str = Field(
        pattern=r'^capture-mesh-composition:[0-9a-f]{64}$'
    )
    ingestion_run_id: str = Field(
        pattern=r'^capture-ingestion-run:[0-9a-f]{64}$'
    )
    coordinate_authority_id: str = Field(
        pattern=r'^capture-coordinate-authority:[0-9a-f]{64}$'
    )
    raw_mesh_binding_ids: tuple[
        str, ...
    ] = Field(min_length=2)
    overlap_policy: Literal['retain_all_verbatim'] = (
        COMPOSITION_OVERLAP_POLICY
    )
    composition_version: Literal['1.0.0'] = COMPOSITION_SCHEMA_VERSION

    @model_validator(mode='after')
    def validate_identity(self) -> 'CaptureMeshCompositionRequest':
        binding_ids = list(self.raw_mesh_binding_ids)
        if binding_ids != sorted(set(binding_ids)):
            raise ValueError(
                'raw_mesh_binding_ids must be sorted and unique'
            )
        payload = self.model_dump(
            mode='json', exclude={'composition_id'}
        )
        expected = (
            'capture-mesh-composition:'
            + _semantic_hash({'domain': COMPOSITION_DOMAIN, **payload})
        )
        if self.composition_id != expected:
            raise ValueError('capture mesh composition_id mismatch')
        return self


def make_capture_mesh_composition_request(
    *,
    ingestion_run_id: str,
    coordinate_authority_id: str,
    raw_mesh_binding_ids: tuple[str, ...],
    overlap_policy: Literal['retain_all_verbatim'] = (
        COMPOSITION_OVERLAP_POLICY
    ),
) -> CaptureMeshCompositionRequest:
    core = {
        'ingestion_run_id': ingestion_run_id,
        'coordinate_authority_id': coordinate_authority_id,
        'raw_mesh_binding_ids': raw_mesh_binding_ids,
        'overlap_policy': overlap_policy,
        'composition_version': COMPOSITION_SCHEMA_VERSION,
    }
    payload = {
        key: (list(value) if isinstance(value, tuple) else value)
        for key, value in core.items()
    }
    composition_id = (
        'capture-mesh-composition:'
        + _semantic_hash({'domain': COMPOSITION_DOMAIN, **payload})
    )
    return CaptureMeshCompositionRequest(
        composition_id=composition_id,
        **core,
    )


def _binding_coordinate_space_id(payload_json: str) -> str:
    """Extract the handoff coordinate space from either binding storage form."""

    data = json.loads(payload_json)
    if not isinstance(data, dict):
        raise ValueError('binding payload is not an object')
    handoff = data.get('handoff')
    if not isinstance(handoff, dict):
        handoff = data
    space = handoff.get('coordinate_space_id')
    if not isinstance(space, str) or not space:
        raise ValueError('binding payload has no coordinate_space_id')
    return space


def _drop_none_values(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _drop_none_values(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, list):
        return [_drop_none_values(item) for item in value]
    return value


def _translate_legacy_request_json(
    request_json: str,
    ingestion_run_id: str,
    coordinate_authority_id: str,
) -> tuple[str, str]:
    """Re-express a lineage-scoped persisted request in the current contract.

    The legacy payload keyed the request by ``ingestion_lineage_digest`` and
    its world-to-scene authority carried only a bare coordinate-space UUID.
    Re-binding both to the exact run and the registered
    (bundle, space) coordinate authority produces the identical request the
    current contract would have persisted — so identity, deduplication, and
    replay all work on migrated rows unchanged. Returns the canonical
    ``request_json`` and the recomputed ``promotion_id``.
    """

    data = json.loads(request_json)
    data.pop('promotion_id', None)
    data.pop('ingestion_lineage_digest', None)
    data['ingestion_run_id'] = ingestion_run_id
    authority = dict(data.get('world_to_scene_authority') or {})
    authority.pop('authority_id', None)
    authority['coordinate_authority_id'] = coordinate_authority_id
    # Legacy (v1) persisted authorities predate the transform-class contract:
    # classify the retained matrix and record the alignment under the
    # permissive default methods so the rebound request validates.
    transform = SemanticCoordinateTransform.model_validate(
        authority['transform']
    )
    if 'transform_class' not in authority:
        transform_class, _determinant, scale = _classify_alignment_matrix(
            transform.matrix_source_to_scene_m
        )
        authority['transform_class'] = transform_class
        authority['alignment_method'] = (
            'explicit_affine_override'
            if transform_class == 'affine'
            else 'uniform_scale_alignment'
            if transform_class == 'similarity'
            else 'rigid_registration'
        )
        authority['uniform_scale_m_per_capture_m'] = scale
    authority['authority_id'] = (
        'capture-world-to-scene:'
        + _semantic_hash(
            {
                'domain': WORLD_ALIGNMENT_DOMAIN,
                **_authority_identity_payload(
                    coordinate_space_id=str(authority['coordinate_space_id']),
                    coordinate_authority_id=coordinate_authority_id,
                    transform=transform,
                    transform_class=authority['transform_class'],
                    alignment_method=authority['alignment_method'],
                    uniform_scale_m_per_capture_m=authority[
                        'uniform_scale_m_per_capture_m'
                    ],
                ),
            }
        )
    )
    data['world_to_scene_authority'] = authority
    # The request's own identity validator hashes model_dump(exclude_none);
    # the rebound payload must drop None values recursively so the
    # recomputed promotion_id is self-consistent.
    payload = _drop_none_values(data)
    promotion_id = (
        'capture-semantic-promotion:'
        + _semantic_hash({'domain': PROMOTION_DOMAIN, **payload})
    )
    try:
        request = CaptureSemanticPromotionRequest.model_validate(
            {**payload, 'promotion_id': promotion_id}
        )
    except ValueError as exc:
        raise ValueError(f'legacy promotion request cannot be rebound: {exc}')
    return (
        _canonical_json(request.model_dump(mode='json')),
        promotion_id,
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

    ingestion_run_id: str = Field(
        pattern=r'^capture-ingestion-run:[0-9a-f]{64}$'
    )
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


class CaptureCompositionInspection(BaseModel):
    """Diagnostics over the complete composed selection (#351).

    ``findings`` run on the composed mesh as a whole so overlap evidence
    spans anchors; ``binding_ids`` keeps the exact selection visible.
    """

    model_config = ConfigDict(frozen=True)

    mesh_composition_id: str
    ingestion_run_id: str
    coordinate_authority_id: str
    raw_mesh_binding_ids: tuple[str, ...]
    raw_mesh_id: str
    raw_mesh_semantic_hash: str
    triangle_ids: tuple[str, ...]
    diagnostic_id: str
    diagnostic_semantic_hash: str
    findings: tuple[RawMeshDiagnosticFinding, ...]


@dataclass(frozen=True)
class CaptureSemanticPromotionResult:
    promotion_id: str
    scene_revision_id: str
    semantic_geometry_id: str
    geometry_compiler_readiness: str
    promotion_created: bool
    scene_revision_created: bool


@dataclass(frozen=True)
class CaptureMeshCompositionResult:
    composition_id: str
    ingestion_run_id: str
    coordinate_authority_id: str
    raw_mesh_binding_ids: tuple[str, ...]
    composed_mesh_id: str
    composed_semantic_hash: str
    vertex_count: int
    triangle_count: int
    created: bool


@dataclass(frozen=True)
class PersistedCaptureMeshComposition:
    """The persisted composition record plus its rebuilt composed mesh."""

    composition_id: str
    ingestion_run_id: str
    coordinate_authority_id: str
    coordinate_space_id: str
    overlap_policy: str
    raw_mesh_binding_ids: tuple[str, ...]
    composed_mesh: RawVisualMesh
    manifest_sha256: str


@dataclass(frozen=True)
class CaptureSemanticPromotionRecord:
    promotion_id: str
    ingestion_run_id: str
    raw_mesh_binding_id: str | None
    mesh_composition_id: str | None
    source_scene_revision_id: str
    scene_revision_id: str
    semantic_geometry_id: str
    prior_semantic_geometry_id: str | None
    created_at_utc: str


@dataclass(frozen=True)
class CapturePromotionReplayResult:
    promotion_id: str
    ingestion_run_id: str
    scene_revision_id: str
    semantic_geometry_id: str
    verified: bool


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


def _transform_point(
    matrix: tuple[tuple[float, float, float, float], ...],
    vertex: RawMeshVertex,
) -> RawMeshVertex:
    x, y, z = float(vertex.x), float(vertex.y), float(vertex.z)
    w = (
        matrix[3][0] * x
        + matrix[3][1] * y
        + matrix[3][2] * z
        + matrix[3][3]
    )
    if w != 1.0:
        raise CaptureSemanticPromotionError(
            'capture anchor transform is not affine-rigid'
        )
    return RawMeshVertex(
        x=matrix[0][0] * x + matrix[0][1] * y + matrix[0][2] * z + matrix[0][3],
        y=matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] * z + matrix[1][3],
        z=matrix[2][0] * x + matrix[2][1] * y + matrix[2][2] * z + matrix[2][3],
    )


def compose_capture_mesh_bindings(
    request: CaptureMeshCompositionRequest,
    bindings: tuple[CaptureRawVisualMeshBinding, ...],
) -> tuple[RawVisualMesh, bytes, str]:
    """Compose selected bindings into one capture-world mesh (#351).

    Every selected vertex is transformed by its anchor's exact
    ``T_world_from_mesh_anchor`` into the shared capture-world frame, so a
    single world-to-scene transform afterwards is exact for the whole
    selection. ``source_primitive`` is namespaced as
    ``<binding_id>:<original primitive>`` which namespaces the derived
    ``raw-triangle:`` ids per binding for exact provenance.

    Returns (composed_mesh, canonical_manifest_bytes, manifest_sha256).
    The manifest is the composed mesh's content-addressed "asset": it
    deterministically records the run, the selected binding set in order,
    each anchor transform, and each source mesh identity, so the composed
    geometry is reproducible and the composition identity is bound to
    exactly what was selected.
    """

    ordered = []
    by_id = {binding.binding_id: binding for binding in bindings}
    for binding_id in request.raw_mesh_binding_ids:
        binding = by_id.get(binding_id)
        if binding is None:
            raise CaptureSemanticPromotionError(
                'composition binding is not in the resolved set: '
                f'{binding_id}'
            )
        ordered.append(binding)

    manifest = {
        'schema': 'htdt.capture.mesh-composition',
        'schema_version': COMPOSITION_SCHEMA_VERSION,
        'composition_id': request.composition_id,
        'ingestion_run_id': request.ingestion_run_id,
        'coordinate_authority_id': request.coordinate_authority_id,
        'overlap_policy': request.overlap_policy,
        'bindings': [
            {
                'binding_id': binding.binding_id,
                'handoff_id': binding.handoff.raw_visual_mesh_handoff_id,
                'capture_session_id': binding.handoff.capture_session_id,
                'coordinate_space_id': binding.handoff.coordinate_space_id,
                'T_world_from_mesh_anchor': list(
                    binding.handoff.world_from_mesh_anchor.values
                ),
                'source_mesh_id': binding.raw_mesh.mesh_id,
                'source_mesh_semantic_hash': binding.raw_mesh.semantic_hash(),
            }
            for binding in ordered
        ],
    }
    manifest_json = _canonical_json(manifest)
    manifest_bytes = manifest_json.encode('utf-8')
    manifest_sha256 = sha256(manifest_bytes).hexdigest()

    vertices: list[RawMeshVertex] = []
    triangles: list[RawMeshTriangle] = []
    for binding in ordered:
        anchor = _capture_matrix_rows(
            binding.handoff.world_from_mesh_anchor.values
        )
        offset = len(vertices)
        for vertex in binding.raw_mesh.vertices:
            vertices.append(_transform_point(anchor, vertex))
        for triangle in binding.raw_mesh.triangles:
            triangles.append(
                RawMeshTriangle(
                    a=triangle.a + offset,
                    b=triangle.b + offset,
                    c=triangle.c + offset,
                    source_primitive=(
                        f'{binding.binding_id}:{triangle.source_primitive}'
                    ),
                )
            )

    mesh = RawVisualMesh(
        mesh_id=f'raw-mesh:{manifest_sha256}',
        provenance=RawMeshImportProvenance(
            source_name=(
                'htdt.capture.mesh-composition/'
                + request.composition_id
            ),
            asset_format='capture_mesh_composition_v1',
            original_asset_sha256=manifest_sha256,
            original_size_bytes=len(manifest_bytes),
            importer_version='3',
            coordinate_authority='capture_world_coordinates',
        ),
        original_asset_base64=b64encode(manifest_bytes).decode('ascii'),
        vertices=tuple(vertices),
        triangles=tuple(triangles),
    )
    return mesh, manifest_bytes, manifest_sha256


_CAPTURE_MESH_COMPOSITIONS_DDL = '''
                CREATE TABLE IF NOT EXISTS capture_mesh_compositions (
                    composition_id TEXT PRIMARY KEY,
                    ingestion_run_id TEXT NOT NULL
                        REFERENCES capture_ingestion_runs(ingestion_run_id),
                    coordinate_authority_id TEXT NOT NULL
                        REFERENCES capture_coordinate_authorities(
                            coordinate_authority_id
                        ),
                    overlap_policy TEXT NOT NULL,
                    manifest_sha256 TEXT NOT NULL,
                    composed_mesh_id TEXT NOT NULL,
                    composed_semantic_hash TEXT NOT NULL,
                    vertex_count INTEGER NOT NULL,
                    triangle_count INTEGER NOT NULL,
                    binding_ids_json TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                )
                '''

_CAPTURE_SEMANTIC_PROMOTIONS_DDL = '''
                CREATE TABLE IF NOT EXISTS capture_semantic_promotions (
                    promotion_id TEXT PRIMARY KEY,
                    ingestion_run_id TEXT NOT NULL,
                    raw_mesh_binding_id TEXT,
                    mesh_composition_id TEXT,
                    source_scene_revision_id TEXT NOT NULL,
                    scene_revision_id TEXT NOT NULL,
                    semantic_geometry_id TEXT NOT NULL,
                    prior_semantic_geometry_id TEXT,
                    request_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    FOREIGN KEY(ingestion_run_id)
                        REFERENCES capture_ingestion_runs(ingestion_run_id),
                    FOREIGN KEY(raw_mesh_binding_id)
                        REFERENCES capture_raw_visual_mesh_bindings(binding_id),
                    FOREIGN KEY(ingestion_run_id, raw_mesh_binding_id)
                        REFERENCES capture_ingestion_mesh_links(
                            ingestion_run_id,
                            binding_id
                        )
                        ON DELETE RESTRICT,
                    FOREIGN KEY(mesh_composition_id)
                        REFERENCES capture_mesh_compositions(composition_id),
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
            require_native_tables(
                connection,
                'capture_mesh_compositions',
                'capture_semantic_promotions',
            )

    def _converge_schema(self, connection: sqlite3.Connection) -> None:
        """Legacy-shape tail of the schema-authority migration (#302).

        Plain ``CREATE TABLE`` lives in ``cad_schema_ddl`` and runs inside
        the versioned migration; this sequence converges databases whose
        persisted shapes predate the canonical contract (the promotion
        run-scope rebind parses persisted payloads) and installs the
        semantic-promotion tables so every supported open path converges
        the same way.
        """
        connection.execute(_CAPTURE_MESH_COMPOSITIONS_DDL)
        self._migrate_promotion_run_scope(connection)
        connection.execute(_CAPTURE_SEMANTIC_PROMOTIONS_DDL)
        connection.execute(
            '''
            CREATE INDEX IF NOT EXISTS idx_capture_semantic_promotion_ingestion
            ON capture_semantic_promotions(
                ingestion_run_id,
                raw_mesh_binding_id
            )
            '''
        )
        connection.execute(
            '''
            CREATE INDEX IF NOT EXISTS idx_capture_mesh_composition_run
            ON capture_mesh_compositions(ingestion_run_id)
            '''
        )

    @staticmethod
    def _migrate_promotion_run_scope(connection: sqlite3.Connection) -> None:
        """Rebind persisted promotions to the exact ingestion run (#413).

        Legacy rows carried ``ingestion_lineage_digest`` keyed to the old
        runs primary key. The run-identity migration already mapped every
        legacy run row to a deterministic run id, so each legacy promotion
        joins to its unique run; a lineage that somehow maps to zero or
        several runs fails closed rather than guessing a scope. Rows are
        also checked against their ingestion-mesh link: a promotion whose
        (run, binding) pair was never linked is named and refused.
        ``prior_semantic_geometry_id`` and ``mesh_composition_id`` are new
        columns — legacy rows keep NULL — and each ``request_json`` is
        re-expressed in the run-scoped contract: the lineage key is replaced
        by the exact run and the bare-UUID world-to-scene authority is
        rebound to the registered (bundle, space) coordinate authority,
        recomputing ``promotion_id`` so identity and deduplication survive
        the migration unchanged.
        """

        tables = {
            str(row['name'])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if 'capture_semantic_promotions' not in tables:
            return
        columns = {
            str(row['name'])
            for row in connection.execute(
                'PRAGMA table_info(capture_semantic_promotions)'
            )
        }
        if 'ingestion_run_id' in columns:
            return
        if 'capture_ingestion_runs' not in tables:
            raise CaptureSemanticPromotionError(
                'cannot rebind capture semantic promotions: '
                'capture_ingestion_runs is missing'
            )
        if 'capture_ingestion_mesh_links' not in tables:
            raise CaptureSemanticPromotionError(
                'cannot rebind capture semantic promotions: '
                'capture_ingestion_mesh_links is missing'
            )
        if 'capture_mesh_compositions' not in tables:
            connection.execute(_CAPTURE_MESH_COMPOSITIONS_DDL)

        legacy = connection.execute(
            '''
            SELECT promotion_id, ingestion_lineage_digest,
                   raw_mesh_binding_id, source_scene_revision_id,
                   scene_revision_id, semantic_geometry_id,
                   request_json, created_at_utc
            FROM capture_semantic_promotions
            ORDER BY promotion_id
            '''
        ).fetchall()
        errors: list[str] = []
        mapped: list[tuple[object, ...]] = []
        for row in legacy:
            runs = connection.execute(
                '''
                SELECT ingestion_run_id
                FROM capture_ingestion_runs
                WHERE lineage_digest=?
                ORDER BY ingestion_run_id
                ''',
                (row['ingestion_lineage_digest'],),
            ).fetchall()
            if len(runs) != 1:
                errors.append(
                    f"{row['promotion_id']}: lineage "
                    f"{row['ingestion_lineage_digest']} maps to "
                    f'{len(runs)} runs'
                )
                continue
            run_id = str(runs[0]['ingestion_run_id'])
            linked = connection.execute(
                '''
                SELECT 1
                FROM capture_ingestion_mesh_links
                WHERE ingestion_run_id=? AND binding_id=?
                ''',
                (run_id, row['raw_mesh_binding_id']),
            ).fetchone()
            if linked is None:
                errors.append(
                    f"{row['promotion_id']}: not backed by an "
                    'ingestion-mesh link'
                )
                continue
            binding_row = connection.execute(
                '''
                SELECT payload_json
                FROM capture_raw_visual_mesh_bindings
                WHERE binding_id=?
                ''',
                (row['raw_mesh_binding_id'],),
            ).fetchone()
            run_row = connection.execute(
                '''
                SELECT bundle_digest
                FROM capture_ingestion_runs
                WHERE ingestion_run_id=?
                ''',
                (run_id,),
            ).fetchone()
            authority_row = None
            if binding_row is not None and run_row is not None:
                try:
                    space_id = _binding_coordinate_space_id(
                        binding_row['payload_json']
                    )
                except ValueError:
                    space_id = None
                if space_id is not None:
                    authority_row = connection.execute(
                        '''
                        SELECT coordinate_authority_id
                        FROM capture_coordinate_authorities
                        WHERE bundle_digest=? AND coordinate_space_id=?
                        ''',
                        (str(run_row['bundle_digest']), space_id),
                    ).fetchone()
            if authority_row is None:
                errors.append(
                    f"{row['promotion_id']}: no registered coordinate "
                    'authority for its bundle and space'
                )
                continue
            try:
                request_json, promotion_id = _translate_legacy_request_json(
                    row['request_json'],
                    run_id,
                    str(authority_row['coordinate_authority_id']),
                )
            except ValueError as exc:
                errors.append(f"{row['promotion_id']}: {exc}")
                continue
            mapped.append(
                (
                    promotion_id,
                    run_id,
                    row['raw_mesh_binding_id'],
                    None,
                    row['source_scene_revision_id'],
                    row['scene_revision_id'],
                    row['semantic_geometry_id'],
                    None,
                    request_json,
                    row['created_at_utc'],
                )
            )
        if errors:
            raise CaptureSemanticPromotionError(
                'persisted capture semantic promotions cannot be rebound '
                'to an exact ingestion run; refusing to migrate: '
                + '; '.join(errors)
            )

        # RENAME/CREATE/DROP are autocommitted when no transaction is open,
        # so the rebuild runs inside an explicit transaction: either the
        # whole run-scoped table replaces the legacy one or nothing changes
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
            connection.execute(
                _CAPTURE_SEMANTIC_PROMOTIONS_DDL.replace(
                    'IF NOT EXISTS ', ''
                )
            )
            connection.executemany(
                '''
                INSERT INTO capture_semantic_promotions(
                    promotion_id,
                    ingestion_run_id,
                    raw_mesh_binding_id,
                    mesh_composition_id,
                    source_scene_revision_id,
                    scene_revision_id,
                    semantic_geometry_id,
                    prior_semantic_geometry_id,
                    request_json,
                    created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                mapped,
            )
            connection.execute('DROP TABLE capture_semantic_promotions_legacy')
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    # ---- composition (#351) ----------------------------------------------

    def compose(
        self,
        request: CaptureMeshCompositionRequest,
    ) -> CaptureMeshCompositionResult:
        """Persist one multi-binding composition for an ingestion run.

        Composing is deterministic and idempotent: the same request recomputes
        the same composition identity and verifies the persisted record rather
        than writing a second copy. Bindings from different coordinate spaces
        — or a scoped coordinate authority that does not match the run's
        registry — fail closed.
        """

        run = self.capture_repository.get_ingestion_run(
            request.ingestion_run_id
        )
        if run is None:
            raise CaptureSemanticPromotionError(
                'capture ingestion run not found'
            )
        linked = set(
            self.capture_repository.mesh_binding_ids_for_run(
                run.ingestion_run_id
            )
        )
        missing = [
            binding_id
            for binding_id in request.raw_mesh_binding_ids
            if binding_id not in linked
        ]
        if missing:
            raise CaptureSemanticPromotionError(
                'raw mesh bindings are not linked to the requested '
                f'ingestion run: {sorted(missing)}'
            )
        bindings = tuple(
            binding
            for binding_id in request.raw_mesh_binding_ids
            for binding in [
                self.capture_repository.get_mesh_binding(binding_id)
            ]
        )
        if any(binding is None for binding in bindings):
            raise CaptureSemanticPromotionError(
                'composition references a mesh binding that is not persisted'
            )
        spaces = {
            binding.handoff.coordinate_space_id for binding in bindings
        }
        if len(spaces) != 1:
            raise CaptureSemanticPromotionError(
                'capture mesh composition requires all bindings to share '
                'one coordinate space; incompatible spaces fail closed '
                'without an explicit alignment authority'
            )
        coordinate_space_id = spaces.pop()
        authority = self.capture_repository.coordinate_authority_for(
            run.bundle_digest, coordinate_space_id
        )
        if (
            authority is None
            or authority.coordinate_authority_id
            != request.coordinate_authority_id
        ):
            raise CaptureSemanticPromotionError(
                'composition coordinate authority is not the registered '
                'scope for this bundle and coordinate space'
            )
        if authority.capture_revision_id != run.capture_revision_id:
            raise CaptureSemanticPromotionError(
                'composition coordinate authority does not match the '
                'run capture revision'
            )

        mesh, manifest_bytes, manifest_sha256 = (
            compose_capture_mesh_bindings(request, bindings)
        )
        request_json = _canonical_json(request.model_dump(mode='json'))

        with closing(self._connect()) as connection:
            try:
                connection.execute('BEGIN IMMEDIATE')
                existing = connection.execute(
                    '''
                    SELECT *
                    FROM capture_mesh_compositions
                    WHERE composition_id=?
                    ''',
                    (request.composition_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing['request_json'] != request_json
                        or existing['composed_mesh_id'] != mesh.mesh_id
                        or existing['manifest_sha256'] != manifest_sha256
                    ):
                        raise CaptureSemanticPromotionError(
                            'composition identity already exists with '
                            'different semantics'
                        )
                    connection.rollback()
                    return CaptureMeshCompositionResult(
                        composition_id=request.composition_id,
                        ingestion_run_id=request.ingestion_run_id,
                        coordinate_authority_id=(
                            request.coordinate_authority_id
                        ),
                        raw_mesh_binding_ids=request.raw_mesh_binding_ids,
                        composed_mesh_id=mesh.mesh_id,
                        composed_semantic_hash=mesh.semantic_hash(),
                        vertex_count=len(mesh.vertices),
                        triangle_count=len(mesh.triangles),
                        created=False,
                    )

                self._store_composition_manifest(
                    connection, manifest_sha256, manifest_bytes
                )
                connection.execute(
                    '''
                    INSERT INTO capture_mesh_compositions(
                        composition_id,
                        ingestion_run_id,
                        coordinate_authority_id,
                        overlap_policy,
                        manifest_sha256,
                        composed_mesh_id,
                        composed_semantic_hash,
                        vertex_count,
                        triangle_count,
                        binding_ids_json,
                        request_json,
                        created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (
                        request.composition_id,
                        request.ingestion_run_id,
                        request.coordinate_authority_id,
                        request.overlap_policy,
                        manifest_sha256,
                        mesh.mesh_id,
                        mesh.semantic_hash(),
                        len(mesh.vertices),
                        len(mesh.triangles),
                        _canonical_json(list(request.raw_mesh_binding_ids)),
                        request_json,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return CaptureMeshCompositionResult(
            composition_id=request.composition_id,
            ingestion_run_id=request.ingestion_run_id,
            coordinate_authority_id=request.coordinate_authority_id,
            raw_mesh_binding_ids=request.raw_mesh_binding_ids,
            composed_mesh_id=mesh.mesh_id,
            composed_semantic_hash=mesh.semantic_hash(),
            vertex_count=len(mesh.vertices),
            triangle_count=len(mesh.triangles),
            created=True,
        )

    @staticmethod
    def _store_composition_manifest(
        connection: sqlite3.Connection,
        manifest_sha256: str,
        manifest_bytes: bytes,
    ) -> None:
        """Persist the canonical composition manifest as a content blob.

        The manifest is the composed mesh's content-addressed asset — the
        same deduplicated store that holds canonical source payloads — so
        composition provenance bytes survive even where the composed mesh
        itself is intentionally only derived.
        """

        store_content_blob(
            connection, manifest_bytes, expected_sha256=manifest_sha256
        )

    def get_composition(
        self, composition_id: str
    ) -> PersistedCaptureMeshComposition | None:
        """Load a composition record and deterministically rebuild its mesh.

        The composed mesh is derived state: it is rebuilt from the persisted
        request plus the exact linked bindings, and the rebuild must
        reproduce the recorded mesh id and semantic hash or the read fails
        closed.
        """

        with closing(self._connect()) as connection:
            row = connection.execute(
                '''
                SELECT *
                FROM capture_mesh_compositions
                WHERE composition_id=?
                ''',
                (composition_id,),
            ).fetchone()
            if row is None:
                return None
            return self._composition_from_row(connection, row)

    def list_compositions(
        self, *, ingestion_run_id: str | None = None
    ) -> tuple[PersistedCaptureMeshComposition, ...]:
        query = 'SELECT * FROM capture_mesh_compositions'
        params: tuple[str, ...] = ()
        if ingestion_run_id is not None:
            query += ' WHERE ingestion_run_id=?'
            params = (ingestion_run_id,)
        query += ' ORDER BY composition_id ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
            return tuple(
                self._composition_from_row(connection, row) for row in rows
            )

    def _composition_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> PersistedCaptureMeshComposition:
        try:
            request = CaptureMeshCompositionRequest.model_validate_json(
                row['request_json']
            )
        except ValueError as exc:
            raise CaptureSemanticPromotionError(
                f'persisted composition request is invalid: '
                f'{row["composition_id"]}: {exc}'
            ) from exc
        bindings = []
        for binding_id in request.raw_mesh_binding_ids:
            binding = self._binding_from_row_or_name(
                connection, binding_id
            )
            bindings.append(binding)
        mesh, _, manifest_sha256 = compose_capture_mesh_bindings(
            request, tuple(bindings)
        )
        if (
            manifest_sha256 != row['manifest_sha256']
            or mesh.mesh_id != row['composed_mesh_id']
            or mesh.semantic_hash() != row['composed_semantic_hash']
        ):
            raise CaptureSemanticPromotionError(
                'persisted composition does not reproduce its recorded '
                f'identity: {row["composition_id"]}'
            )
        authority = self.capture_repository.get_coordinate_authority(
            request.coordinate_authority_id
        )
        if authority is None:
            raise CaptureSemanticPromotionError(
                'composition coordinate authority is not registered: '
                f'{request.coordinate_authority_id}'
            )
        return PersistedCaptureMeshComposition(
            composition_id=request.composition_id,
            ingestion_run_id=request.ingestion_run_id,
            coordinate_authority_id=request.coordinate_authority_id,
            coordinate_space_id=authority.coordinate_space_id,
            overlap_policy=request.overlap_policy,
            raw_mesh_binding_ids=request.raw_mesh_binding_ids,
            composed_mesh=mesh,
            manifest_sha256=manifest_sha256,
        )

    def _binding_from_row_or_name(
        self, connection: sqlite3.Connection, binding_id: str
    ):
        del connection  # reads are immutable; the capture repo resolves
        binding = self.capture_repository.get_mesh_binding(binding_id)
        if binding is None:
            raise CaptureSemanticPromotionError(
                f'composition binding is not persisted: {binding_id}'
            )
        return binding

    def inspect_capture_composition(
        self,
        *,
        mesh_composition_id: str,
        profile: SemanticGeometryConversionProfile | None = None,
    ) -> CaptureCompositionInspection:
        composition = self.get_composition(mesh_composition_id)
        if composition is None:
            raise CaptureSemanticPromotionError(
                'capture mesh composition not found'
            )
        profile = profile or SemanticGeometryConversionProfile()
        diagnostic = diagnose_raw_visual_mesh(
            composition.composed_mesh,
            profile=profile.diagnostic_profile,
        )
        return CaptureCompositionInspection(
            mesh_composition_id=composition.composition_id,
            ingestion_run_id=composition.ingestion_run_id,
            coordinate_authority_id=composition.coordinate_authority_id,
            raw_mesh_binding_ids=composition.raw_mesh_binding_ids,
            raw_mesh_id=composition.composed_mesh.mesh_id,
            raw_mesh_semantic_hash=composition.composed_mesh.semantic_hash(),
            triangle_ids=raw_triangle_ids(composition.composed_mesh),
            diagnostic_id=diagnostic.diagnostic_id,
            diagnostic_semantic_hash=diagnostic.semantic_hash(),
            findings=diagnostic.findings,
        )

    def inspect_capture_mesh(
        self,
        *,
        ingestion_run_id: str,
        raw_mesh_binding_id: str,
        profile: SemanticGeometryConversionProfile | None = None,
    ) -> CaptureSemanticMeshInspection:
        _, binding = self._resolve_binding(
            ingestion_run_id,
            raw_mesh_binding_id,
        )
        profile = profile or SemanticGeometryConversionProfile()
        diagnostic = diagnose_raw_visual_mesh(
            binding.raw_mesh,
            profile=profile.diagnostic_profile,
        )
        return CaptureSemanticMeshInspection(
            ingestion_run_id=ingestion_run_id,
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

    # ---- promotion (#351/#359/#365) ---------------------------------------

    def _resolve_world_authority_scope(
        self,
        request: CaptureSemanticPromotionRequest,
        *,
        run_bundle_digest: str,
        coordinate_space_id: str,
    ) -> None:
        """Fail closed unless the world-to-scene authority is scoped right.

        The request's authority must name the registered
        (bundle_digest, coordinate_space_id) authority of the exact run it
        promotes from (#365): a bare UUID cannot stand in for it, so the
        same UUID minted by a different bundle yields a different authority
        id and a different world-to-scene authority.
        """

        authority = request.world_to_scene_authority
        if authority.coordinate_space_id != coordinate_space_id:
            raise CaptureSemanticPromotionError(
                'capture world-to-scene authority coordinate space mismatch'
            )
        if authority.coordinate_authority_id is None:
            raise CaptureSemanticPromotionError(
                'capture world-to-scene authority is not scoped to a '
                'registered coordinate authority'
            )
        registered = self.capture_repository.get_coordinate_authority(
            authority.coordinate_authority_id
        )
        if (
            registered is None
            or registered.bundle_digest != run_bundle_digest
            or registered.coordinate_space_id != coordinate_space_id
        ):
            raise CaptureSemanticPromotionError(
                'capture world-to-scene authority scope mismatch: the '
                'registered coordinate authority does not bind this '
                'bundle and space'
            )

    def _resolve_binding(
        self,
        ingestion_run_id: str,
        raw_mesh_binding_id: str,
    ):
        run = self.capture_repository.get_ingestion_run(ingestion_run_id)
        if run is None:
            raise CaptureSemanticPromotionError('capture ingestion run not found')
        binding_ids = self.capture_repository.mesh_binding_ids_for_run(
            ingestion_run_id
        )
        if raw_mesh_binding_id not in binding_ids:
            raise CaptureSemanticPromotionError(
                'raw mesh binding is not linked to the requested ingestion run'
            )
        binding = self.capture_repository.get_mesh_binding(raw_mesh_binding_id)
        if binding is None:
            raise CaptureSemanticPromotionError('raw mesh binding not found')
        return run, binding

    def _enforce_geometry_conflict_policy(
        self,
        request: CaptureSemanticPromotionRequest,
        existing: SemanticAcousticGeometry | None,
    ) -> str | None:
        """Gate replacing persisted semantic geometry on explicit policy (#359).

        Returns the superseded geometry id for the promotion record, or
        None when no prior geometry existed. Fails closed on every
        combination where the caller's declared expectation does not match
        what is actually persisted.
        """

        if existing is None:
            if (
                request.geometry_conflict_policy is not None
                or request.expected_prior_geometry_id is not None
                or request.expected_prior_geometry_semantic_hash is not None
            ):
                raise CaptureSemanticPromotionError(
                    'request declares a geometry conflict policy but the '
                    'source SceneRevision has no semantic geometry to '
                    'replace'
                )
            return None
        if request.geometry_conflict_policy != 'replace_exact':
            raise CaptureSemanticPromotionError(
                'source SceneRevision already has semantic geometry; '
                'promotion requires an explicit geometry_conflict_policy '
                'bound to the expected prior geometry identity'
            )
        if (
            request.expected_prior_geometry_id != existing.geometry_id
            or request.expected_prior_geometry_semantic_hash
            != existing.semantic_hash_sha256
        ):
            raise CaptureSemanticPromotionError(
                'expected prior semantic geometry identity/hash does not '
                'match the persisted geometry; refusing silent replacement'
            )
        return existing.geometry_id

    def promote(
        self,
        request: CaptureSemanticPromotionRequest,
    ) -> CaptureSemanticPromotionResult:
        composition: PersistedCaptureMeshComposition | None = None
        if request.mesh_composition_id is not None:
            composition = self.get_composition(request.mesh_composition_id)
            if composition is None:
                raise CaptureSemanticPromotionError(
                    'capture mesh composition not found'
                )
            if (
                composition.ingestion_run_id != request.ingestion_run_id
            ):
                raise CaptureSemanticPromotionError(
                    'mesh composition is not linked to the requested '
                    'ingestion run'
                )
            if (
                composition.coordinate_authority_id
                != request.world_to_scene_authority.coordinate_authority_id
            ):
                raise CaptureSemanticPromotionError(
                    'capture world-to-scene authority does not match the '
                    'composition coordinate authority scope'
                )
            run = self.capture_repository.get_ingestion_run(
                request.ingestion_run_id
            )
            if run is None:
                raise CaptureSemanticPromotionError(
                    'capture ingestion run not found'
                )
            mesh = composition.composed_mesh
            expected_space = composition.coordinate_space_id
        else:
            composition = None
            run, binding = self._resolve_binding(
                request.ingestion_run_id,
                request.raw_mesh_binding_id or '',
            )
            mesh = binding.raw_mesh
            expected_space = binding.handoff.coordinate_space_id

        # Fail closed unless the ingestion carried a validated quality
        # authority — pre-gate or unresolved-quality runs cannot promote.
        try:
            self.capture_repository.require_quality_state(
                run.lineage_digest
            )
        except CaptureQualityGateError as exc:
            raise CaptureSemanticPromotionError(str(exc)) from exc

        self._resolve_world_authority_scope(
            request,
            run_bundle_digest=run.bundle_digest,
            coordinate_space_id=expected_space,
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
        request_json = _canonical_json(request.model_dump(mode='json'))
        with closing(self._connect()) as connection:
            existing = connection.execute(
                '''
                SELECT *
                FROM capture_semantic_promotions
                WHERE promotion_id=?
                ''',
                (request.promotion_id,),
            ).fetchone()
        if existing is not None:
            if existing['request_json'] != request_json:
                raise CaptureSemanticPromotionError(
                    'promotion identity already exists with different semantics'
                )
            # Replaying an already-persisted request is idempotent: the
            # conflict policy must not re-gate it against the head it
            # created, and the conversion need not re-run — the persisted
            # scene revision is the source of truth for the result.
            promoted_revision = self.scene_repository.get(
                str(existing['scene_revision_id'])
            )
            persisted_geometry = (
                promoted_revision.document.r120_semantic_geometry
                if promoted_revision is not None
                else None
            )
            if (
                persisted_geometry is None
                or persisted_geometry.geometry_id
                != str(existing['semantic_geometry_id'])
            ):
                raise CaptureSemanticPromotionError(
                    'persisted promotion result is missing its linked '
                    'semantic geometry'
                )
            return CaptureSemanticPromotionResult(
                promotion_id=str(existing['promotion_id']),
                scene_revision_id=str(existing['scene_revision_id']),
                semantic_geometry_id=str(existing['semantic_geometry_id']),
                geometry_compiler_readiness=(
                    persisted_geometry.geometry_compiler_readiness
                ),
                promotion_created=False,
                scene_revision_created=False,
            )

        prior_geometry_id = self._enforce_geometry_conflict_policy(
            request,
            source_revision.document.r120_semantic_geometry,
        )

        scene_from_capture_world = (
            request.world_to_scene_authority.transform.matrix_source_to_scene_m
        )
        if composition is None:
            capture_anchor = _capture_matrix_rows(
                binding.handoff.world_from_mesh_anchor.values
            )
            scene_from_anchor = _matmul4(
                scene_from_capture_world,
                capture_anchor,
            )
            transform_reason = (
                request.world_to_scene_authority.transform.reason
                + '; composed with exact Capture T_world_from_mesh_anchor '
                + binding.handoff.raw_visual_mesh_handoff_id
            )
            source_to_scene = SemanticCoordinateTransform(
                matrix_source_to_scene_m=scene_from_anchor,
                provenance=request.world_to_scene_authority.transform.provenance,
                reason=transform_reason,
            )
        else:
            # The composed mesh is already in capture-world coordinates:
            # anchor transforms were baked in at composition time, so the
            # world-to-scene authority applies verbatim.
            source_to_scene = SemanticCoordinateTransform(
                matrix_source_to_scene_m=scene_from_capture_world,
                provenance=request.world_to_scene_authority.transform.provenance,
                reason=(
                    request.world_to_scene_authority.transform.reason
                    + '; applied to capture-world composed mesh '
                    + composition.composition_id
                ),
            )
        conversion_request = make_semantic_geometry_conversion_request(
            mesh,
            source_scene_revision_id=request.source_scene_revision_id,
            source_to_scene_transform=source_to_scene,
            profile=request.profile,
            surface_assignments=request.surface_assignments,
        )
        geometry = convert_raw_visual_mesh_to_semantic_geometry(
            mesh,
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

                if composition is None:
                    linked = connection.execute(
                        '''
                        SELECT 1
                        FROM capture_ingestion_mesh_links
                        WHERE ingestion_run_id=? AND binding_id=?
                        ''',
                        (
                            request.ingestion_run_id,
                            request.raw_mesh_binding_id,
                        ),
                    ).fetchone()
                    if linked is None:
                        raise CaptureSemanticPromotionError(
                            'raw mesh binding is not linked to the requested ingestion run'
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
                        ingestion_run_id,
                        raw_mesh_binding_id,
                        mesh_composition_id,
                        source_scene_revision_id,
                        scene_revision_id,
                        semantic_geometry_id,
                        prior_semantic_geometry_id,
                        request_json,
                        created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (
                        request.promotion_id,
                        request.ingestion_run_id,
                        request.raw_mesh_binding_id,
                        request.mesh_composition_id,
                        request.source_scene_revision_id,
                        save.revision.revision_id,
                        geometry.geometry_id,
                        prior_geometry_id,
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

    # ---- listing + replay (#353/#368) -------------------------------------

    def get_promotion(
        self, promotion_id: str
    ) -> CaptureSemanticPromotionRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                '''
                SELECT *
                FROM capture_semantic_promotions
                WHERE promotion_id=?
                ''',
                (promotion_id,),
            ).fetchone()
        if row is None:
            return None
        return self._promotion_from_row(row)

    def list_promotions(
        self,
        *,
        ingestion_run_id: str | None = None,
        raw_mesh_binding_id: str | None = None,
        mesh_composition_id: str | None = None,
    ) -> tuple[CaptureSemanticPromotionRecord, ...]:
        clauses: list[str] = []
        params: list[str] = []
        if ingestion_run_id is not None:
            clauses.append('ingestion_run_id=?')
            params.append(ingestion_run_id)
        if raw_mesh_binding_id is not None:
            clauses.append('raw_mesh_binding_id=?')
            params.append(raw_mesh_binding_id)
        if mesh_composition_id is not None:
            clauses.append('mesh_composition_id=?')
            params.append(mesh_composition_id)
        query = 'SELECT * FROM capture_semantic_promotions'
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        query += ' ORDER BY promotion_id ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(self._promotion_from_row(row) for row in rows)

    @staticmethod
    def _promotion_from_row(
        row: sqlite3.Row,
    ) -> CaptureSemanticPromotionRecord:
        # Row/payload invariant (#313): the persisted request is canonical;
        # duplicated id columns must agree with the request keys they carry.
        try:
            request = json.loads(row['request_json'])
        except (TypeError, ValueError) as exc:
            raise CapturePromotionReplayError(
                'persisted promotion request_json is not valid JSON',
                promotion_id=str(row['promotion_id']),
                diagnostic='persisted_request_invalid',
            ) from exc
        if isinstance(request, dict):
            # Legacy requests predate run-scoped identity; their rebound
            # promotion_id legitimately differs from the row identity, so
            # binding checks apply only to requests written in the current
            # scope shape (same gate as _request_from_row).
            authority = request.get('world_to_scene_authority')
            legacy_scope = 'ingestion_run_id' not in request or (
                isinstance(authority, dict)
                and 'coordinate_authority_id' not in authority
            )
            if not legacy_scope:
                for column in (
                    'promotion_id',
                    'ingestion_run_id',
                    'raw_mesh_binding_id',
                    'mesh_composition_id',
                    'source_scene_revision_id',
                ):
                    if row[column] != request.get(column):
                        raise CapturePromotionReplayError(
                            'persisted promotion row disagrees with its '
                            f'request: {column}',
                            promotion_id=str(row['promotion_id']),
                            diagnostic='persisted_row_payload_mismatch',
                        )
        return CaptureSemanticPromotionRecord(
            promotion_id=str(row['promotion_id']),
            ingestion_run_id=str(row['ingestion_run_id']),
            raw_mesh_binding_id=(
                None
                if row['raw_mesh_binding_id'] is None
                else str(row['raw_mesh_binding_id'])
            ),
            mesh_composition_id=(
                None
                if row['mesh_composition_id'] is None
                else str(row['mesh_composition_id'])
            ),
            source_scene_revision_id=str(row['source_scene_revision_id']),
            scene_revision_id=str(row['scene_revision_id']),
            semantic_geometry_id=str(row['semantic_geometry_id']),
            prior_semantic_geometry_id=(
                None
                if row['prior_semantic_geometry_id'] is None
                else str(row['prior_semantic_geometry_id'])
            ),
            created_at_utc=str(row['created_at_utc']),
        )

    def _request_from_row(
        self,
        row: sqlite3.Row,
    ) -> tuple[CaptureSemanticPromotionRequest, bool]:
        """Parse a persisted promotion request, tolerating legacy scope keys.

        Returns (request, legacy_scope): ``legacy_scope`` marks rows whose
        request predates run-scoped/scoped-authority identity — those rows
        are rebound to the promotion row's persisted run and registered
        coordinate authority rather than discarded. ``promotion_id`` then
        differs from the stored row id (the persisted row remains the
        identity), which is expected for migrated requests.
        """

        try:
            data = json.loads(row['request_json'])
        except (TypeError, ValueError) as exc:
            raise CapturePromotionReplayError(
                'persisted promotion request_json is not valid JSON',
                promotion_id=str(row['promotion_id']),
                diagnostic='persisted_request_invalid',
            ) from exc
        if not isinstance(data, dict):
            raise CapturePromotionReplayError(
                'persisted promotion request is not an object',
                promotion_id=str(row['promotion_id']),
                diagnostic='persisted_request_invalid',
            )
        authority = data.get('world_to_scene_authority')
        legacy_scope = 'ingestion_run_id' not in data or (
            isinstance(authority, dict)
            and 'coordinate_authority_id' not in authority
        )
        if legacy_scope:
            # Rebind to the promotion row's exact run and the registered
            # (bundle, space) coordinate authority — the same translation
            # the schema migration performs on persisted rows.
            coordinate_authority_id = (
                self._expected_coordinate_authority_for_row(row, data)
            )
            try:
                request_json, _pid = _translate_legacy_request_json(
                    row['request_json'],
                    str(row['ingestion_run_id']),
                    coordinate_authority_id,
                )
            except ValueError as exc:
                raise CapturePromotionReplayError(
                    f'persisted promotion request cannot be rebound: {exc}',
                    promotion_id=str(row['promotion_id']),
                    diagnostic='persisted_request_invalid',
                ) from exc
            data = json.loads(request_json)
        try:
            request = CaptureSemanticPromotionRequest.model_validate(data)
        except ValueError as exc:
            diagnostic = (
                'unsupported_converter_version'
                if 'algorithm_version' in str(exc)
                else 'persisted_request_invalid'
            )
            raise CapturePromotionReplayError(
                f'persisted promotion request is invalid: {exc}',
                promotion_id=str(row['promotion_id']),
                diagnostic=diagnostic,
            ) from exc
        return request, legacy_scope

    def _expected_coordinate_authority_for_row(
        self,
        row: sqlite3.Row,
        data: dict,
    ) -> str:
        """Resolve the scoped coordinate authority a legacy request used."""

        run = self.capture_repository.get_ingestion_run(
            str(row['ingestion_run_id'])
        )
        if run is None:
            raise CapturePromotionReplayError(
                'promotion references an ingestion run that is not persisted',
                promotion_id=str(row['promotion_id']),
                diagnostic='source_authority_missing',
            )
        binding_id = data.get('raw_mesh_binding_id')
        space: str | None = None
        if binding_id:
            binding = self.capture_repository.get_mesh_binding(
                str(binding_id)
            )
            if binding is not None:
                space = binding.handoff.coordinate_space_id
        else:
            space = self._composition_space(
                str(row['ingestion_run_id']),
                data.get('mesh_composition_id'),
            )
        if space is None:
            raise CapturePromotionReplayError(
                'cannot resolve coordinate authority scope for the '
                'persisted request',
                promotion_id=str(row['promotion_id']),
                diagnostic='source_authority_missing',
            )
        authority = self.capture_repository.coordinate_authority_for(
            run.bundle_digest, space
        )
        if authority is None:
            raise CapturePromotionReplayError(
                'no registered coordinate authority for the persisted '
                'request scope',
                promotion_id=str(row['promotion_id']),
                diagnostic='coordinate_authority_missing',
            )
        return authority.coordinate_authority_id

    def _composition_space(
        self, ingestion_run_id: str, composition_id: str | None
    ) -> str | None:
        if composition_id is None:
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                '''
                SELECT coordinate_authority_id
                FROM capture_mesh_compositions
                WHERE composition_id=? AND ingestion_run_id=?
                ''',
                (composition_id, ingestion_run_id),
            ).fetchone()
        if row is None:
            return None
        authority = self.capture_repository.get_coordinate_authority(
            str(row['coordinate_authority_id'])
        )
        return None if authority is None else authority.coordinate_space_id

    def verify_persisted_promotion(
        self, promotion_id: str
    ) -> CapturePromotionReplayResult:
        """Replay one persisted promotion's canonical derivation (#368).

        Reloads the persisted request and the exact persisted source
        authorities (run-scoped binding or persisted composition plus its
        ordered bindings), reruns the canonical semantic-geometry
        conversion, and requires exact equality with the geometry linked
        from the referenced SceneRevision. A persisted request whose
        converter/profile version the current contract cannot re-derive is
        reported explicitly rather than silently skipped.
        """

        with closing(self._connect()) as connection:
            row = connection.execute(
                '''
                SELECT *
                FROM capture_semantic_promotions
                WHERE promotion_id=?
                ''',
                (promotion_id,),
            ).fetchone()
        if row is None:
            raise CapturePromotionReplayError(
                'promotion is not persisted',
                promotion_id=promotion_id,
                diagnostic='promotion_not_found',
            )
        request, _legacy = self._request_from_row(row)
        with closing(self._connect()) as connection:
            if str(request.ingestion_run_id) != str(row['ingestion_run_id']):
                raise CapturePromotionReplayError(
                    'persisted request run does not match the promotion row',
                    promotion_id=str(row['promotion_id']),
                    diagnostic='promotion_derivation_mismatch',
                )
            run = self.capture_repository.get_ingestion_run(
                request.ingestion_run_id
            )
            if run is None:
                raise CapturePromotionReplayError(
                    'promotion references an ingestion run that is not '
                    'persisted',
                    promotion_id=str(row['promotion_id']),
                    diagnostic='source_authority_missing',
                )

            if row['mesh_composition_id'] is not None:
                composition_row = connection.execute(
                    '''
                    SELECT * FROM capture_mesh_compositions
                    WHERE composition_id=?
                    ''',
                    (row['mesh_composition_id'],),
                ).fetchone()
                if composition_row is None:
                    raise CapturePromotionReplayError(
                        'persisted composition record is missing',
                        promotion_id=str(row['promotion_id']),
                        diagnostic='source_authority_missing',
                    )
                composition = self._composition_from_row(
                    connection, composition_row
                )
                if composition.ingestion_run_id != request.ingestion_run_id:
                    raise CapturePromotionReplayError(
                        'persisted composition is not linked to the '
                        'promotion run',
                        promotion_id=str(row['promotion_id']),
                        diagnostic='promotion_derivation_mismatch',
                    )
                mesh = composition.composed_mesh
                expected_space = composition.coordinate_space_id
                source_to_scene = SemanticCoordinateTransform(
                    matrix_source_to_scene_m=(
                        request.world_to_scene_authority.transform
                        .matrix_source_to_scene_m
                    ),
                    provenance=(
                        request.world_to_scene_authority.transform
                        .provenance
                    ),
                    reason=(
                        request.world_to_scene_authority.transform.reason
                        + '; applied to capture-world composed mesh '
                        + composition.composition_id
                    ),
                )
            else:
                binding = self._binding_from_row_or_name(
                    connection, str(row['raw_mesh_binding_id'])
                )
                linked = connection.execute(
                    '''
                    SELECT 1 FROM capture_ingestion_mesh_links
                    WHERE ingestion_run_id=? AND binding_id=?
                    ''',
                    (request.ingestion_run_id, row['raw_mesh_binding_id']),
                ).fetchone()
                if linked is None:
                    raise CapturePromotionReplayError(
                        'promotion binding is not linked to its run',
                        promotion_id=str(row['promotion_id']),
                        diagnostic='promotion_derivation_mismatch',
                    )
                mesh = binding.raw_mesh
                expected_space = binding.handoff.coordinate_space_id
                capture_anchor = _capture_matrix_rows(
                    binding.handoff.world_from_mesh_anchor.values
                )
                scene_from_anchor = _matmul4(
                    request.world_to_scene_authority.transform
                    .matrix_source_to_scene_m,
                    capture_anchor,
                )
                source_to_scene = SemanticCoordinateTransform(
                    matrix_source_to_scene_m=scene_from_anchor,
                    provenance=(
                        request.world_to_scene_authority.transform
                        .provenance
                    ),
                    reason=(
                        request.world_to_scene_authority.transform.reason
                        + '; composed with exact Capture '
                        'T_world_from_mesh_anchor '
                        + binding.handoff.raw_visual_mesh_handoff_id
                    ),
                )

            if (
                request.world_to_scene_authority.coordinate_space_id
                != expected_space
                or request.world_to_scene_authority.coordinate_authority_id
                is None
            ):
                raise CapturePromotionReplayError(
                    'persisted world-to-scene authority scope mismatch',
                    promotion_id=str(row['promotion_id']),
                    diagnostic='coordinate_authority_mismatch',
                )
            registered = self.capture_repository.get_coordinate_authority(
                request.world_to_scene_authority.coordinate_authority_id
            )
            if (
                registered is None
                or registered.bundle_digest != run.bundle_digest
                or registered.coordinate_space_id != expected_space
            ):
                raise CapturePromotionReplayError(
                    'persisted world-to-scene authority is not the '
                    'registered coordinate authority scope',
                    promotion_id=str(row['promotion_id']),
                    diagnostic='coordinate_authority_mismatch',
                )

            conversion_request = make_semantic_geometry_conversion_request(
                mesh,
                source_scene_revision_id=request.source_scene_revision_id,
                source_to_scene_transform=source_to_scene,
                profile=request.profile,
                surface_assignments=request.surface_assignments,
            )
            geometry = convert_raw_visual_mesh_to_semantic_geometry(
                mesh,
                conversion_request,
            )
            scene_revision = self.scene_repository.get(
                str(row['scene_revision_id'])
            )
            if scene_revision is None:
                raise CapturePromotionReplayError(
                    'promoted SceneRevision is not persisted',
                    promotion_id=str(row['promotion_id']),
                    diagnostic='source_authority_missing',
                )
            persisted_geometry = (
                scene_revision.document.r120_semantic_geometry
            )
            if (
                persisted_geometry is None
                or persisted_geometry.geometry_id
                != str(row['semantic_geometry_id'])
                or persisted_geometry.geometry_id != geometry.geometry_id
                or persisted_geometry.semantic_hash_sha256
                != geometry.semantic_hash_sha256
                or persisted_geometry != geometry
            ):
                raise CapturePromotionReplayError(
                    're-derived semantic geometry does not match the '
                    'persisted promotion result',
                    promotion_id=str(row['promotion_id']),
                    diagnostic='promotion_derivation_mismatch',
                )
        return CapturePromotionReplayResult(
            promotion_id=str(row['promotion_id']),
            ingestion_run_id=str(row['ingestion_run_id']),
            scene_revision_id=str(row['scene_revision_id']),
            semantic_geometry_id=str(row['semantic_geometry_id']),
            verified=True,
        )

    def verify_all_promotions(
        self,
    ) -> tuple[CapturePromotionReplayResult, ...]:
        """Replay-verify every persisted promotion in order."""

        with closing(self._connect()) as connection:
            ids = [
                str(row['promotion_id'])
                for row in connection.execute(
                    'SELECT promotion_id FROM capture_semantic_promotions '
                    'ORDER BY promotion_id ASC'
                ).fetchall()
            ]
        return tuple(
            self.verify_persisted_promotion(promotion_id)
            for promotion_id in ids
        )


def run_semantic_promotion_schema_convergence(
    connection: sqlite3.Connection,
) -> None:
    """Legacy-shape tail of the schema-authority migration (#302).

    ``ensure_native_schema`` invokes this while converging databases whose
    semantic promotions predate the run-scope contract; it runs the same
    sequence ``CaptureSemanticPromotionRepository._initialize`` applies,
    without constructing a repository instance.
    """

    repository = CaptureSemanticPromotionRepository.__new__(
        CaptureSemanticPromotionRepository
    )
    repository._converge_schema(connection)
