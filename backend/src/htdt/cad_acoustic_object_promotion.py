"""Explicit acoustic object promotion (#965 / #101).

#464 deliberately separated ``EntityBodyGeometry`` (display/collision
authority) from acoustic solver geometry so a visible body can never become
physical acoustic truth implicitly. This module is the explicit opt-in
bridge that boundary requires: a versioned :class:`AcousticObjectPromotion`
binds one scene entity's *exact* body geometry to a bounded acoustic
participation state and derives an immutable set of ``object_surface``
triangles in scene world coordinates.

Bounded participation states:

- ``occlusion_only`` — the body enters geometric occlusion checks only;
  no boundary physics or material claims.
- ``rigid_boundary`` — the body is a rigid reflective/occluding boundary
  (no material authority bound).
- ``material_bound_boundary`` — boundary participation with an exact
  acoustic material authority bound; material is never inferred from
  visual color or object name.
- ``unsupported_complex_object`` — the entity is recorded as acoustically
  relevant but its geometry cannot be represented by this compiler
  (e.g. imported mesh bodies); produces no surfaces.

Honesty constraints:

- GA participation is occlusion-only in this slice: promoted surfaces are
  consumed as occluders by the R150 deterministic GA adapter; no specular
  reflection or scattering claim is made.
- Wave-solver participation is ``none``: occupancy/voxelization needs its
  own supported representation which does not exist yet.
- An empty-seat body is only the seat's own geometry — occupant acoustic
  effects are governed by occupancy authorities (#1038), never implied.
- Riser internals (cavities, vents, porous fill) are #1024's authority; a
  promoted riser is its external surface shell only.
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import cos, isfinite, pi, sin
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_repository import SceneRevision
from .cad_scene import (
    SceneEntity,
    quaternion_to_matrix3,
)
from .r120_geometry_compiler import ExactExternalAuthorityRef


ACOUSTIC_OBJECT_PROMOTION_SCHEMA_VERSION = 1
ACOUSTIC_OBJECT_PROMOTION_AUTHORITY_VERSION = 'acoustic-object-promotion-1'
ACOUSTIC_OBJECT_COMPILER_ID = 'htdt.object_promotion.surface_compiler'
ACOUSTIC_OBJECT_COMPILER_VERSION = '1'

_CYLINDER_PRISM_SIDES = 24
# Bodies with an occupied volume beyond this threshold are acoustically
# relevant enough that leaving them solver-inert should warn in preflight.
_LARGE_INERT_VOLUME_M3 = 0.5

ObjectAcousticParticipation = Literal[
    'occlusion_only',
    'rigid_boundary',
    'material_bound_boundary',
    'unsupported_complex_object',
]

AcousticObjectRepresentation = Literal[
    'none', 'occlusion_body', 'boundary_shell'
]


def _canonical(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(payload: object) -> str:
    return sha256(_canonical(payload).encode('utf-8')).hexdigest()


def _rounded(value: float) -> float:
    if not isfinite(value):
        raise ValueError('promoted object vertex is not finite')
    return round(float(value), 9)


def _entity_body_sha256(entity: SceneEntity) -> str:
    """Exact hash of everything that defines the promoted body.

    Any later edit to size, pose or refined body geometry changes this
    digest, which stales the derived acoustic representation.
    """

    payload = entity.model_dump(mode='json', include={
        'position',
        'orientation',
        'size_m',
        'body_geometry',
    })
    return _digest(payload)


def _world_vertex(
    rotation: tuple[tuple[float, float, float], ...],
    position: tuple[float, float, float],
    local: tuple[float, float, float],
) -> tuple[float, float, float]:
    x, y, z = local
    return (
        _rounded(rotation[0][0] * x + rotation[0][1] * y + rotation[0][2] * z
                 + position[0]),
        _rounded(rotation[1][0] * x + rotation[1][1] * y + rotation[1][2] * z
                 + position[1]),
        _rounded(rotation[2][0] * x + rotation[2][1] * y + rotation[2][2] * z
                 + position[2]),
    )


class PromotedObjectSurface(BaseModel):
    """One ``object_surface`` boundary surface of a promoted entity."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    surface_key: str = Field(min_length=1)
    semantic_class: Literal['object_surface'] = 'object_surface'
    triangle_indices: tuple[tuple[int, int, int], ...] = Field(min_length=1)


class PromotedObjectGeometry(BaseModel):
    """Derived surface shell in scene world coordinates."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    vertices: tuple[tuple[float, float, float], ...] = Field(min_length=4)
    surfaces: tuple[PromotedObjectSurface, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def validate_membership(self) -> 'PromotedObjectGeometry':
        count = len(self.vertices)
        seen: set[int] = set()
        for surface in self.surfaces:
            for triangle in surface.triangle_indices:
                if len(set(triangle)) != 3 or any(
                    index < 0 or index >= count for index in triangle
                ):
                    raise ValueError(
                        'promoted object triangle indices are invalid'
                    )
                seen.update(triangle)
        if seen != set(range(count)):
            raise ValueError(
                'every promoted vertex must belong to a surface triangle'
            )
        return self


class AcousticObjectPromotion(BaseModel):
    """Versioned authority binding an entity body to acoustic participation."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = ACOUSTIC_OBJECT_PROMOTION_SCHEMA_VERSION
    authority_version: Literal[
        'acoustic-object-promotion-1'
    ] = ACOUSTIC_OBJECT_PROMOTION_AUTHORITY_VERSION
    promotion_id: str = Field(
        pattern=r'^acoustic-object-promotion:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    scene_revision_id: str = Field(min_length=1)
    scene_revision_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    entity_kind: str = Field(min_length=1)
    body_geometry_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    participation: ObjectAcousticParticipation
    acoustic_representation: AcousticObjectRepresentation
    simplification_declared: tuple[str, ...] = ()
    material_ref: ExactExternalAuthorityRef | None = None

    ga_participation: Literal['occlusion', 'none']
    wave_participation: Literal['boundary_voxels', 'none'] = 'none'

    geometry: PromotedObjectGeometry | None = None

    compiler_id: Literal[
        'htdt.object_promotion.surface_compiler'
    ] = ACOUSTIC_OBJECT_COMPILER_ID
    compiler_version: Literal['1'] = ACOUSTIC_OBJECT_COMPILER_VERSION

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            mode='json',
            exclude={'promotion_id', 'semantic_sha256'},
        )
        if self.material_ref is None:
            payload.pop('material_ref', None)
        if self.geometry is None:
            payload.pop('geometry', None)
        return payload

    @model_validator(mode='after')
    def validate_identity(self) -> 'AcousticObjectPromotion':
        if self.participation == 'unsupported_complex_object':
            if self.geometry is not None:
                raise ValueError(
                    'unsupported complex objects carry no promoted geometry'
                )
        else:
            if self.geometry is None:
                raise ValueError(
                    'promoted objects require derived surface geometry'
                )
        if (
            self.participation == 'material_bound_boundary'
            and self.material_ref is None
        ):
            raise ValueError(
                'material_bound_boundary participation requires an exact '
                'acoustic material authority'
            )
        if self.ga_participation == 'none' and self.geometry is not None:
            raise ValueError(
                'geometry-bearing promotion must participate in GA occlusion'
            )
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('acoustic object promotion hash mismatch')
        if self.promotion_id != f'acoustic-object-promotion:{expected}':
            raise ValueError('acoustic object promotion id mismatch')
        return self

    def authority_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.promotion_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


def _box_shell(half: tuple[float, float, float]) -> tuple[
    list[tuple[float, float, float]],
    list[tuple[str, list[tuple[int, int, int]]]],
]:
    hx, hy, hz = half
    v = [
        (-hx, -hy, -hz), (hx, -hy, -hz), (hx, hy, -hz), (-hx, hy, -hz),
        (-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz),
    ]
    faces = [
        ('floor', [0, 1, 2, 3]),
        ('ceiling', [7, 6, 5, 4]),
        ('front', [0, 4, 5, 1]),
        ('right', [1, 5, 6, 2]),
        ('rear', [2, 6, 7, 3]),
        ('left', [3, 7, 4, 0]),
    ]
    surfaces: list[tuple[str, list[tuple[int, int, int]]]] = []
    for key, quad in faces:
        surfaces.append((
            key,
            [
                (quad[0], quad[1], quad[2]),
                (quad[0], quad[2], quad[3]),
            ],
        ))
    return v, surfaces


def _extruded_shell(
    footprint: tuple[tuple[float, float], ...],
    half_z: float,
) -> tuple[
    list[tuple[float, float, float]],
    list[tuple[str, list[tuple[int, int, int]]]],
]:
    count = len(footprint)
    v = [(x, y, -half_z) for x, y in footprint]
    v += [(x, y, half_z) for x, y in footprint]
    surfaces: list[tuple[str, list[tuple[int, int, int]]]] = [
        ('bottom', [(0, i + 1, i + 2) for i in range(count - 2)]),
        ('top', [
            (count, count + i + 2, count + i + 1)
            for i in range(count - 2)
        ]),
    ]
    for i in range(count):
        j = (i + 1) % count
        surfaces.append((
            f'side-{i}',
            [(i, j, count + j), (i, count + j, count + i)],
        ))
    return v, surfaces


def _derived_shell(
    entity: SceneEntity,
) -> tuple[
    tuple[float, float, float],
    list[tuple[float, float, float]],
    list[tuple[str, list[tuple[int, int, int]]]],
    tuple[str, ...],
]:
    if entity.size_m is None:
        raise ValueError('promoted entities require a physical size_m')
    half = (
        float(entity.size_m.x_m) * 0.5,
        float(entity.size_m.y_m) * 0.5,
        float(entity.size_m.z_m) * 0.5,
    )
    body = entity.body_geometry
    if body is None or body.kind == 'box':
        v, surfaces = _box_shell(half)
        return half, v, surfaces, ()
    if body.kind == 'extruded_polygon':
        footprint = tuple(
            (float(vertex.x_m), float(vertex.y_m))
            for vertex in body.footprint_vertices or ()
        )
        v, surfaces = _extruded_shell(footprint, half[2])
        return half, v, surfaces, ()
    if body.kind == 'cylinder':
        footprint = tuple(
            (
                float(body.radius_m) * cos(2.0 * pi * i / _CYLINDER_PRISM_SIDES),
                float(body.radius_m) * sin(2.0 * pi * i / _CYLINDER_PRISM_SIDES),
            )
            for i in range(_CYLINDER_PRISM_SIDES)
        )
        v, surfaces = _extruded_shell(footprint, half[2])
        return (
            half,
            v,
            surfaces,
            (f'cylinder_to_{_CYLINDER_PRISM_SIDES}_gon_prism',),
        )
    raise ValueError(
        'unsupported_complex_object: mesh bodies cannot be promoted by '
        'this compiler slice'
    )


def compile_acoustic_object_promotion(
    revision: SceneRevision,
    entity: SceneEntity,
    *,
    participation: ObjectAcousticParticipation,
    material_ref: ExactExternalAuthorityRef | None = None,
) -> AcousticObjectPromotion:
    """Compile one entity body into a versioned acoustic promotion.

    This never mutates the entity or the scene: the caller decides whether
    to persist the returned authority.
    """

    if entity.entity_id not in {
        item.entity_id for item in revision.document.entities
    }:
        raise ValueError('entity does not belong to the SceneRevision')
    if participation == 'unsupported_complex_object':
        raise ValueError(
            'unsupported_complex_object is recorded by participation '
            'assessment, not created as a promotion'
        )
    if (
        participation == 'material_bound_boundary'
        and material_ref is None
    ):
        raise ValueError(
            'material_bound_boundary participation requires an exact '
            'acoustic material authority'
        )
    if entity.body_geometry is not None and entity.body_geometry.kind == 'mesh_asset':
        raise ValueError(
            'unsupported_complex_object: mesh bodies cannot be promoted by '
            'this compiler slice'
        )

    half, local_vertices, face_surfaces, simplification = _derived_shell(
        entity
    )
    rotation = quaternion_to_matrix3(entity.orientation)
    position = (
        float(entity.position.x_m),
        float(entity.position.y_m),
        float(entity.position.z_m),
    )
    vertices = tuple(
        _world_vertex(rotation, position, vertex)
        for vertex in local_vertices
    )
    surfaces = tuple(
        PromotedObjectSurface(
            surface_key=f'{entity.entity_id}:{key}',
            triangle_indices=tuple(triangles),
        )
        for key, triangles in face_surfaces
    )
    geometry = PromotedObjectGeometry(vertices=vertices, surfaces=surfaces)
    representation: AcousticObjectRepresentation = (
        'occlusion_body'
        if participation == 'occlusion_only'
        else 'boundary_shell'
    )

    core = {
        'schema_version': ACOUSTIC_OBJECT_PROMOTION_SCHEMA_VERSION,
        'authority_version': ACOUSTIC_OBJECT_PROMOTION_AUTHORITY_VERSION,
        'scene_revision_id': revision.revision_id,
        'scene_revision_content_hash': revision.content_hash,
        'document_id': revision.document.document_id,
        'entity_id': entity.entity_id,
        'entity_kind': entity.kind,
        'body_geometry_sha256': _entity_body_sha256(entity),
        'participation': participation,
        'acoustic_representation': representation,
        'simplification_declared': list(simplification),
        'ga_participation': 'occlusion',
        'wave_participation': 'none',
        'geometry': geometry.model_dump(mode='json'),
        'compiler_id': ACOUSTIC_OBJECT_COMPILER_ID,
        'compiler_version': ACOUSTIC_OBJECT_COMPILER_VERSION,
    }
    if material_ref is not None:
        core['material_ref'] = material_ref.model_dump(mode='json')
    digest = _digest(core)
    return AcousticObjectPromotion(
        promotion_id=f'acoustic-object-promotion:{digest}',
        semantic_sha256=digest,
        scene_revision_id=revision.revision_id,
        scene_revision_content_hash=revision.content_hash,
        document_id=revision.document.document_id,
        entity_id=entity.entity_id,
        entity_kind=entity.kind,
        body_geometry_sha256=_entity_body_sha256(entity),
        participation=participation,
        acoustic_representation=representation,
        simplification_declared=simplification,
        material_ref=material_ref,
        ga_participation='occlusion',
        wave_participation='none',
        geometry=geometry,
    )


class ObjectPromotionCurrency(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    state: Literal['CURRENT', 'STALE']
    stale_reasons: tuple[str, ...]

    @model_validator(mode='after')
    def validate_state(self) -> 'ObjectPromotionCurrency':
        if self.state == 'CURRENT' and self.stale_reasons:
            raise ValueError('CURRENT promotion cannot carry stale reasons')
        if self.state == 'STALE' and not self.stale_reasons:
            raise ValueError('STALE promotion requires explicit reasons')
        return self


def assess_object_promotion_currency(
    promotion: AcousticObjectPromotion,
    revision: SceneRevision,
) -> ObjectPromotionCurrency:
    """A promoted object goes stale when its pinned body or revision moves."""

    reasons: list[str] = []
    if promotion.scene_revision_id != revision.revision_id:
        reasons.append('scene_revision_moved')
    if promotion.scene_revision_content_hash != revision.content_hash:
        reasons.append('scene_content_changed')
    entity = next(
        (
            item
            for item in revision.document.entities
            if item.entity_id == promotion.entity_id
        ),
        None,
    )
    if entity is None:
        reasons.append('entity_removed')
    elif _entity_body_sha256(entity) != promotion.body_geometry_sha256:
        reasons.append('body_geometry_changed')
    return (
        ObjectPromotionCurrency(state='STALE', stale_reasons=tuple(reasons))
        if reasons
        else ObjectPromotionCurrency(state='CURRENT', stale_reasons=())
    )


class SceneObjectParticipation(BaseModel):
    """Per-entity acoustic participation status for preflight/inspection."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    entity_id: str = Field(min_length=1)
    entity_kind: str = Field(min_length=1)
    state: Literal[
        'visual_only',
        'occlusion_only',
        'rigid_boundary',
        'material_bound_boundary',
        'unsupported_complex_object',
        'stale_promotion',
    ]
    promotion_id: str | None = None


class SceneObjectAcousticAssessment(BaseModel):
    """Scene-wide view of which physical bodies enter the acoustic model."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    scene_revision_id: str = Field(min_length=1)
    entries: tuple[SceneObjectParticipation, ...]
    completeness_warnings: tuple[str, ...]


_PROMOTABLE_KINDS = frozenset({'box', 'cylinder', 'extruded_polygon'})


def assess_scene_acoustic_object_participation(
    revision: SceneRevision,
    promotions: tuple[AcousticObjectPromotion, ...] = (),
) -> SceneObjectAcousticAssessment:
    """Report per-entity acoustic participation plus completeness warnings.

    Physical non-source entities without a promotion stay explicitly
    ``visual_only`` — never silently solver-relevant. Large solver-inert
    bodies raise a model-completeness warning without claiming they dominate
    the result.
    """

    from .cad_scene import PHYSICAL_ENTITY_KINDS

    by_entity: dict[str, AcousticObjectPromotion] = {}
    for promotion in promotions:
        if promotion.entity_id in by_entity:
            raise ValueError('duplicate promotion for entity')
        by_entity[promotion.entity_id] = promotion

    entries: list[SceneObjectParticipation] = []
    warnings: list[str] = []
    for entity in revision.document.entities:
        if entity.kind == 'speaker':
            continue
        if entity.kind not in PHYSICAL_ENTITY_KINDS:
            continue
        promotion = by_entity.get(entity.entity_id)
        body = entity.body_geometry
        body_kind = 'box' if body is None else body.kind
        promotable = body_kind in _PROMOTABLE_KINDS
        if promotion is not None:
            currency = assess_object_promotion_currency(
                promotion, revision
            )
            state = (
                'stale_promotion'
                if currency.state == 'STALE'
                else promotion.participation
            )
            if currency.state == 'STALE':
                warnings.append(
                    f'entity {entity.entity_id} promotion is stale: '
                    + ','.join(currency.stale_reasons)
                )
        elif not promotable:
            state = 'unsupported_complex_object'
        else:
            state = 'visual_only'
            volume = None
            if entity.size_m is not None:
                volume = (
                    float(entity.size_m.x_m)
                    * float(entity.size_m.y_m)
                    * float(entity.size_m.z_m)
                )
            if volume is not None and volume >= _LARGE_INERT_VOLUME_M3:
                warnings.append(
                    f'entity {entity.entity_id} is a large solver-inert '
                    'physical body not promoted into acoustic geometry'
                )
        entries.append(
            SceneObjectParticipation(
                entity_id=entity.entity_id,
                entity_kind=entity.kind,
                state=state,
                promotion_id=(
                    None if promotion is None else promotion.promotion_id
                ),
            )
        )
    return SceneObjectAcousticAssessment(
        scene_revision_id=revision.revision_id,
        entries=tuple(entries),
        completeness_warnings=tuple(sorted(set(warnings))),
    )
