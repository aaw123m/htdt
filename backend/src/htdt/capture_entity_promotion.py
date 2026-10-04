"""Capture annotation → SceneEntity promotion executor.

The Capture Inbox ``promote`` surface has always accepted an executor, but
no production executor materialized staged authority records into the scene.
This module is that executor for the ``annotations`` authority kind: every
scene-enterable annotation record becomes a real ``SceneEntity`` appended to
the scoped document's head revision, deterministically identified so a
re-promotion is idempotent instead of duplicating entities.

Honesty rules (no fabricated geometry):

- entity position/orientation come from ``T_world_from_annotation``;
  a promoted world→scene authority for the same (document, coordinate space)
  is applied when one exists, otherwise identity (capture world == scene
  frame convention);
- physical kinds require ``physical_envelope`` width/depth/height — a
  missing envelope blocks that record rather than inventing dimensions;
- ``derived_candidate:<id>`` evidence refs resolve against the plan's
  persisted ``derived/geometry-candidates.json`` and become real
  ``EntityBodyGeometry`` (circle→cylinder or off-center polygon,
  rectangle/ellipse/polygon→extruded_polygon); unresolved candidate refs
  are ignored, not fatal;
- rigid/similarity world→scene transforms only — a general affine
  authority blocks the record (orientation cannot be represented
  faithfully under shear).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from math import cos, isfinite, pi, sin
from typing import Callable, Iterable

from .cad_direct_view import capture_entity_type_to_scene_kind
from .cad_repository import SceneRepository
from .cad_scene import (
    EntityBodyGeometry,
    FootprintVertex,
    Offset3,
    Position3,
    SceneEntity,
    Size3,
    next_unassigned_speaker_role,
    quaternion_from_matrix3,
)
from .capture_authoring import (
    CaptureAuthoringAnnotation,
    CaptureAuthoringService,
)
from .capture_ingestion_transaction import (
    CaptureIngestionPlan,
    CaptureIngestionRepository,
)
from .capture_semantic_promotion import (
    CaptureSemanticPromotionRepository,
    CaptureWorldToSceneAuthority,
)


class CaptureEntityPromotionError(ValueError):
    """Promotion executor failure — recorded as a blocked promotion."""


#: authority kind this executor materializes.
EXECUTABLE_ENTITY_AUTHORITY_KIND = 'annotations'

_DERIVED_CANDIDATES_SCHEMA = 'htdt.capture.derived-geometry-candidates'
_DERIVED_CANDIDATES_PATH = 'derived/geometry-candidates.json'
_DERIVED_REF_PREFIX = 'derived_candidate:'

# Off-center bodies are expressed as sampled polygons so the true plan
# position is preserved; a cylinder/rectangle primitive is only used when
# its natural center coincides with the entity origin within this epsilon.
_CENTER_EPSILON_M = 0.01
_ELLIPSE_SAMPLE_COUNT = 16

#: Blocking conflict kinds carried over from the authoring pass.
_BLOCKING_CONFLICT_KINDS = frozenset({
    'coordinate_space_mismatch',
    'unresolved_reference',
    'unsupported_entity_type',
})


@dataclass(frozen=True)
class EntityPromotionSkip:
    """One annotation record that could not materialize, with its reason."""

    record_id: str
    reason: str


@dataclass(frozen=True)
class EntityPromotionOutcome:
    """Summary of one annotations promotion run."""

    document_id: str
    scene_revision_id: str
    promoted_entity_ids: tuple[str, ...]
    skipped: tuple[EntityPromotionSkip, ...]
    reused_existing_entity_ids: tuple[str, ...]


def promoted_entity_id(record_kind: str, lineage_digest: str, record_id: str) -> str:
    """Deterministic scene id for one promoted record.

    Re-promoting the same lineage record lands the same id, which the
    document uniqueness invariant turns into a no-op instead of a
    duplicate entity.
    """

    digest = sha256(
        f'{record_kind}:{lineage_digest}:{record_id}'.encode('utf-8')
    ).hexdigest()[:16]
    return f'capture-entity-{digest}'


def _identity4() -> tuple[tuple[float, float, float, float], ...]:
    return (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )


def _matrix_from_column_major(values: object) -> tuple[
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
] | None:
    """column_major_4x4_f32 → row-major tuple, or None when malformed."""

    if not isinstance(values, (list, tuple)) or len(values) != 16:
        return None
    try:
        flat = [float(v) for v in values]
    except (TypeError, ValueError):
        return None
    if any(not isfinite(v) for v in flat):
        return None
    return tuple(
        tuple(flat[column * 4 + row] for column in range(4))
        for row in range(4)
    )  # type: ignore[return-value]


def _matmul4(
    left: tuple[tuple[float, float, float, float], ...],
    right: tuple[tuple[float, float, float, float], ...],
) -> tuple[tuple[float, float, float, float], ...]:
    return tuple(
        tuple(
            sum(float(left[row][k]) * float(right[k][column]) for k in range(4))
            for column in range(4)
        )
        for row in range(4)
    )


def _transform_point4(
    matrix: tuple[tuple[float, float, float, float], ...],
    point: tuple[float, float, float],
) -> tuple[float, float, float]:
    x, y, z = point
    return (
        matrix[0][0] * x + matrix[0][1] * y + matrix[0][2] * z + matrix[0][3],
        matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] * z + matrix[1][3],
        matrix[2][0] * x + matrix[2][1] * y + matrix[2][2] * z + matrix[2][3],
    )


def _det3(rows: tuple[tuple[float, float, float], ...]) -> float:
    a, b, c = rows[0]
    d, e, f = rows[1]
    g, h, i = rows[2]
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def _upper3(
    matrix: tuple[tuple[float, float, float, float], ...],
) -> tuple[tuple[float, float, float], ...]:
    return tuple(tuple(matrix[r][c] for c in range(3)) for r in range(3))


_AFFINE_LAST_ROW_EPS = 1e-6
_ORTHOGONALITY_EPS = 1e-6


def _is_affine4(
    matrix: tuple[tuple[float, float, float, float], ...],
) -> bool:
    """Last row must be [0, 0, 0, 1] — projective matrices are unsupported."""

    return (
        abs(matrix[3][0]) <= _AFFINE_LAST_ROW_EPS
        and abs(matrix[3][1]) <= _AFFINE_LAST_ROW_EPS
        and abs(matrix[3][2]) <= _AFFINE_LAST_ROW_EPS
        and abs(matrix[3][3] - 1.0) <= _AFFINE_LAST_ROW_EPS
    )


def _normalized_rotation3(
    matrix: tuple[tuple[float, float, float, float], ...],
) -> tuple[tuple[float, float, float], ...] | None:
    """Upper 3x3 with column scale stripped; None when not rigid/similarity."""

    rows = _upper3(matrix)
    columns = [[rows[r][c] for r in range(3)] for c in range(3)]
    norms = [sum(v * v for v in col) ** 0.5 for col in columns]
    if any(n <= 1e-12 for n in norms):
        return None
    if max(norms) / min(norms) > 1.0 + 1e-6:
        return None  # non-uniform scale — not representable faithfully
    unit = tuple(
        tuple(columns[c][r] / norms[c] for c in range(3)) for r in range(3)
    )
    unit_columns = [[unit[r][c] for r in range(3)] for c in range(3)]
    for i in range(3):
        for j in range(i + 1, 3):
            dot = sum(
                unit_columns[i][k] * unit_columns[j][k] for k in range(3)
            )
            if abs(dot) > _ORTHOGONALITY_EPS:
                return None  # shear — not representable faithfully
    if _det3(unit) <= 0.0:
        return None  # reflection
    return unit


def _uniform_scale(
    matrix: tuple[tuple[float, float, float, float], ...],
) -> float:
    determinant = _det3(_upper3(matrix))
    if determinant <= 0.0:
        return 1.0
    return determinant ** (1.0 / 3.0)


def _inverse3(
    rows: tuple[tuple[float, float, float], ...],
) -> tuple[tuple[float, float, float], ...] | None:
    a, b, c = rows[0]
    d, e, f = rows[1]
    g, h, i = rows[2]
    determinant = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(determinant) <= 1e-18:
        return None
    return (
        ((e * i - f * h) / determinant, (c * h - b * i) / determinant, (b * f - c * e) / determinant),
        ((f * g - d * i) / determinant, (a * i - c * g) / determinant, (c * d - a * f) / determinant),
        ((d * h - e * g) / determinant, (b * g - a * h) / determinant, (a * e - b * d) / determinant),
    )


class CaptureEntityPromotionService:
    """Materialize staged annotation records into a document's scene entities."""

    def __init__(
        self,
        capture_repository: CaptureIngestionRepository,
        scene_repository: SceneRepository,
        *,
        authoring_service: CaptureAuthoringService | None = None,
        semantic_promotion_repository: (
            CaptureSemanticPromotionRepository | None
        ) = None,
    ) -> None:
        self.capture_repository = capture_repository
        self.scene_repository = scene_repository
        self.authoring_service = authoring_service or CaptureAuthoringService(
            capture_repository
        )
        self.semantic_promotion_repository = semantic_promotion_repository

    def promotion_executor(
        self, document_id: str
    ) -> Callable[[CaptureIngestionPlan, str], str]:
        """Inbox ``promote`` executor bound to one target document."""

        def executor(plan: CaptureIngestionPlan, kind: str) -> str:
            if kind != EXECUTABLE_ENTITY_AUTHORITY_KIND:
                raise CaptureEntityPromotionError(
                    f'authority kind {kind!r} has no entity promotion path'
                )
            outcome = self.promote_annotations(plan, document_id)
            return f'scene-entities:{outcome.scene_revision_id}'

        return executor

    def promote_annotations(
        self,
        plan: CaptureIngestionPlan,
        document_id: str,
    ) -> EntityPromotionOutcome:
        """Materialize the plan's annotations into the document head revision."""

        head = self.scene_repository.current_head(document_id)
        if head is None:
            raise CaptureEntityPromotionError(
                f'document {document_id} has no scene revision'
            )
        batch = self.authoring_service.authoring_inputs(plan.lineage_digest)
        if not batch.annotations:
            raise CaptureEntityPromotionError(
                'ingestion carries no annotation records'
            )
        world_to_scene_by_space = self._world_to_scene_by_space(
            plan, document_id
        )
        candidates = self._derived_candidates(plan)

        created: list[SceneEntity] = []
        skipped: list[EntityPromotionSkip] = []
        reused: list[str] = []
        existing_ids = {
            entity.entity_id for entity in head.document.entities
        }
        for annotation in batch.annotations:
            entity_id = promoted_entity_id(
                'annotation', plan.lineage_digest, annotation.record_id
            )
            if entity_id in existing_ids:
                reused.append(entity_id)
                continue
            entity, reason = self._materialize(
                annotation,
                entity_id=entity_id,
                world_to_scene=world_to_scene_by_space.get(
                    annotation.coordinate_space_id, _identity4()
                ),
                candidates=candidates,
                existing_entities=head.document.entities + tuple(created),
            )
            if entity is None:
                skipped.append(
                    EntityPromotionSkip(record_id=annotation.record_id, reason=reason)
                )
            else:
                created.append(entity)
        if not created:
            if reused:
                # Every scene-enterable record is already materialized —
                # re-promotion is a no-op at the head revision, not a
                # blocked attempt.
                return EntityPromotionOutcome(
                    document_id=document_id,
                    scene_revision_id=head.revision_id,
                    promoted_entity_ids=(),
                    skipped=tuple(skipped),
                    reused_existing_entity_ids=tuple(reused),
                )
            reasons = '; '.join(
                f'{skip.record_id}: {skip.reason}' for skip in skipped[:5]
            ) or 'no records evaluated'
            raise CaptureEntityPromotionError(
                f'no annotation records materialized ({reasons})'
            )
        updated = head.document.model_copy(
            update={'entities': head.document.entities + tuple(created)}
        )
        save = self.scene_repository.save(
            updated,
            parent_revision_id=head.revision_id,
        )
        self.scene_repository.set_revision_label(
            save.revision.revision_id,
            label='capture-promotion',
            note=(
                f'capture inbox annotations promotion; '
                f'lineage={plan.lineage_digest[:16]} '
                f'entities={len(created)} '
                f'skipped={len(skipped)}'
            ),
        )
        return EntityPromotionOutcome(
            document_id=document_id,
            scene_revision_id=save.revision.revision_id,
            promoted_entity_ids=tuple(e.entity_id for e in created),
            skipped=tuple(skipped),
            reused_existing_entity_ids=tuple(reused),
        )

    # ---- internals ---------------------------------------------------------

    def _world_to_scene_by_space(
        self,
        plan: CaptureIngestionPlan,
        document_id: str,
    ) -> dict[str, tuple[tuple[float, float, float, float], ...]]:
        """Promoted world→scene authorities for this lineage, keyed by
        coordinate space id.

        The authority is keyed by (target document, coordinate space): the
        semantic promotion path stores one per ingestion run and this
        collects the newest one per space declared in the plan bundle.
        An annotation promotes through ITS OWN space's authority — never
        a sibling space's — and spaces without a recorded authority fall
        back to identity.
        """

        if self.semantic_promotion_repository is None:
            return {}
        spaces = set(plan.bundle.coordinate_space_ids)
        by_space: dict[str, CaptureWorldToSceneAuthority] = {}
        for record in self.semantic_promotion_repository.list_promotions():
            request = (
                self.semantic_promotion_repository.promotion_request(
                    record.promotion_id
                )
            )
            if request is None:
                continue
            if request.target_document_id != document_id:
                continue
            authority = request.world_to_scene_authority
            if authority.coordinate_space_id not in spaces:
                continue
            by_space[authority.coordinate_space_id] = authority
        return {
            space_id: authority.transform.matrix_source_to_scene_m
            for space_id, authority in by_space.items()
        }

    def _derived_candidates(
        self, plan: CaptureIngestionPlan
    ) -> dict[str, dict]:
        """Persisted ``derived_candidate`` geometry by candidate id."""

        for document in plan.supplemental_documents:
            if document.path != _DERIVED_CANDIDATES_PATH:
                continue
            if document.schema != _DERIVED_CANDIDATES_SCHEMA:
                continue
            evidence = self.capture_repository.get_source_evidence(
                document.source_evidence_id
            )
            if evidence is None:
                break
            try:
                payload = json.loads(evidence.payload)
            except (TypeError, ValueError):
                break
            return {
                str(item.get('candidate_id')): item
                for item in payload.get('candidates', ())
                if isinstance(item, dict) and item.get('candidate_id')
            }
        return {}

    def _materialize(
        self,
        annotation: CaptureAuthoringAnnotation,
        *,
        entity_id: str,
        world_to_scene: tuple[tuple[float, float, float, float], ...],
        candidates: dict[str, dict],
        existing_entities: Iterable[SceneEntity],
    ) -> tuple[SceneEntity | None, str]:
        document = annotation.document
        kind = capture_entity_type_to_scene_kind(annotation.entity_type)
        if kind is None:
            return None, (
                f'entity type {annotation.entity_type!r} has no scene kind '
                '(stays suggestion-only)'
            )
        blocking = [
            conflict.detail
            for conflict in annotation.conflicts
            if conflict.kind in _BLOCKING_CONFLICT_KINDS
        ]
        if blocking:
            return None, '; '.join(blocking)

        transform = _matrix_from_column_major(
            (document.get('T_world_from_annotation') or {}).get('values')
        )
        if transform is None:
            return None, 'T_world_from_annotation missing or malformed'
        if not _is_affine4(transform):
            return None, 'entity transform is not affine'
        if not _is_affine4(world_to_scene):
            return None, 'world-to-scene authority transform is not affine'
        scene_transform = _matmul4(world_to_scene, transform)
        rotation = _normalized_rotation3(scene_transform)
        if rotation is None:
            return None, 'entity transform is not rigid/similarity'
        scale = _uniform_scale(scene_transform)
        position = _transform_point4(scene_transform, (0.0, 0.0, 0.0))
        try:
            orientation = quaternion_from_matrix3(rotation)
        except ValueError:
            return None, 'entity rotation is not convertible to a quaternion'

        size, envelope_reason = self._size(document, scale)
        if kind != 'measurement_point' and size is None:
            return None, envelope_reason

        speaker_role: str | None = None
        if kind == 'speaker':
            role = document.get('channel_role')
            if isinstance(role, str) and role.strip():
                speaker_role = role.strip()
            else:
                speaker_role = next_unassigned_speaker_role(
                    entity.speaker_role for entity in existing_entities
                )

        acoustic_offset = self._acoustic_offset(document)
        body_geometry = self._body_geometry(
            annotation,
            candidates=candidates,
            world_to_scene=world_to_scene,
            scene_transform=scene_transform,
            capture_z=transform[2][3],
            scale=scale,
        )
        name = annotation.label or document.get('label') or ''
        if not str(name).strip():
            name = f'capture {annotation.entity_type} {annotation.record_id[:8]}'
        try:
            return SceneEntity(
                entity_id=entity_id,
                kind=kind,
                name=str(name),
                position=Position3(
                    x_m=position[0], y_m=position[1], z_m=position[2]
                ),
                orientation=orientation,
                size_m=size,
                acoustic_reference_offset_m=acoustic_offset,
                speaker_role=speaker_role,
                body_geometry=body_geometry,
            ), ''
        except ValueError as exc:
            return None, f'scene entity validation: {exc}'

    @staticmethod
    def _size(
        document: dict, scale: float
    ) -> tuple[Size3 | None, str]:
        envelope = document.get('physical_envelope')
        if not isinstance(envelope, dict):
            return None, 'physical entity requires physical_envelope'
        width = envelope.get('width_m')
        height = envelope.get('height_m')
        depth = envelope.get('depth_m')
        if width is None or height is None or depth is None:
            return None, 'physical_envelope missing width/height/depth'
        try:
            return Size3(
                x_m=float(width) * scale,
                y_m=float(depth) * scale,
                z_m=float(height) * scale,
            ), ''
        except (TypeError, ValueError) as exc:
            return None, f'physical_envelope invalid: {exc}'

    @staticmethod
    def _acoustic_offset(document: dict) -> Offset3 | None:
        center = document.get('acoustic_center')
        if not isinstance(center, dict):
            return None
        offset = center.get('offset_local_m')
        if not isinstance(offset, (list, tuple)) or len(offset) != 3:
            return None
        try:
            return Offset3(
                x_m=float(offset[0]),
                y_m=float(offset[1]),
                z_m=float(offset[2]),
            )
        except (TypeError, ValueError):
            return None

    def _body_geometry(
        self,
        annotation: CaptureAuthoringAnnotation,
        *,
        candidates: dict[str, dict],
        world_to_scene: tuple[tuple[float, float, float, float], ...],
        scene_transform: tuple[tuple[float, float, float, float], ...],
        capture_z: float,
        scale: float,
    ) -> EntityBodyGeometry | None:
        """Resolve cited ``derived_candidate:`` refs into entity-local bodies.

        The candidate footprint lives in capture-world plan XY; each point is
        transformed to scene space at the entity's own height and then into
        the entity-local frame, so off-center bodies keep their true plan
        position. Circle candidates become ``cylinder`` when centered on the
        entity origin and a sampled ``extruded_polygon`` otherwise.
        """

        candidate_ids = [
            ref[len(_DERIVED_REF_PREFIX):]
            for ref in annotation.evidence_refs
            if ref.startswith(_DERIVED_REF_PREFIX)
        ]
        if not candidate_ids:
            return None
        candidate = next(
            (candidates[cid] for cid in candidate_ids if cid in candidates),
            None,
        )
        if candidate is None:
            return None
        geometry = candidate.get('geometry')
        if not isinstance(geometry, dict):
            return None

        inverse_rotation = _inverse3(_upper3(scene_transform))
        if inverse_rotation is None:
            return None
        translation = (
            scene_transform[0][3],
            scene_transform[1][3],
            scene_transform[2][3],
        )

        def to_local(plan_xy: tuple[float, float]) -> tuple[float, float]:
            # Candidate footprints carry no height; evaluate them at the
            # entity's own capture-world z so local z stays ~0.
            scene_point = _transform_point4(
                world_to_scene, (plan_xy[0], plan_xy[1], capture_z)
            )
            delta = (
                scene_point[0] - translation[0],
                scene_point[1] - translation[1],
                scene_point[2] - translation[2],
            )
            return (
                inverse_rotation[0][0] * delta[0]
                + inverse_rotation[0][1] * delta[1]
                + inverse_rotation[0][2] * delta[2],
                inverse_rotation[1][0] * delta[0]
                + inverse_rotation[1][1] * delta[1]
                + inverse_rotation[1][2] * delta[2],
            )

        def vertices(points: Iterable[tuple[float, float]]):
            return tuple(FootprintVertex(x_m=x, y_m=y) for x, y in points)

        try:
            if 'circle' in geometry:
                circle = geometry['circle']
                cx, cy = float(circle['center']['x']), float(circle['center']['y'])
                radius = float(circle['radius'])
                if not isfinite(radius) or radius <= 0:
                    return None
                lx, ly = to_local((cx, cy))
                if (lx * lx + ly * ly) ** 0.5 <= _CENTER_EPSILON_M:
                    return EntityBodyGeometry(
                        kind='cylinder', radius_m=radius * scale
                    )
                samples = [
                    (
                        lx + radius * scale * cos(2 * pi * i / _ELLIPSE_SAMPLE_COUNT),
                        ly + radius * scale * sin(2 * pi * i / _ELLIPSE_SAMPLE_COUNT),
                    )
                    for i in range(_ELLIPSE_SAMPLE_COUNT)
                ]
                return EntityBodyGeometry(
                    kind='extruded_polygon',
                    footprint_vertices=vertices(samples),
                )
            if 'orientedRectangle' in geometry:
                rect = geometry['orientedRectangle']
                cx = float(rect['center']['x'])
                cy = float(rect['center']['y'])
                width = float(rect['width'])
                depth = float(rect['depth'])
                heading = float(rect['headingRadians'])
                cos_h, sin_h = cos(heading), sin(heading)
                local = []
                for dx, dy in (
                    (-width / 2, -depth / 2),
                    (width / 2, -depth / 2),
                    (width / 2, depth / 2),
                    (-width / 2, depth / 2),
                ):
                    wx = cx + dx * cos_h - dy * sin_h
                    wy = cy + dx * sin_h + dy * cos_h
                    local.append(to_local((wx, wy)))
                return EntityBodyGeometry(
                    kind='extruded_polygon',
                    footprint_vertices=vertices(local),
                )
            if 'ellipse' in geometry:
                ellipse = geometry['ellipse']
                cx = float(ellipse['center']['x'])
                cy = float(ellipse['center']['y'])
                a = float(ellipse['semiMajorAxis'])
                b = float(ellipse['semiMinorAxis'])
                heading = float(ellipse['headingRadians'])
                cos_h, sin_h = cos(heading), sin(heading)
                local = []
                for i in range(_ELLIPSE_SAMPLE_COUNT):
                    angle = 2 * pi * i / _ELLIPSE_SAMPLE_COUNT
                    ex, ey = a * cos(angle), b * sin(angle)
                    wx = cx + ex * cos_h - ey * sin_h
                    wy = cy + ex * sin_h + ey * cos_h
                    local.append(to_local((wx, wy)))
                return EntityBodyGeometry(
                    kind='extruded_polygon',
                    footprint_vertices=vertices(local),
                )
            if 'polygon' in geometry:
                polygon = geometry['polygon']
                points = [
                    (
                        float(vertex['position']['x']),
                        float(vertex['position']['y']),
                    )
                    for vertex in polygon.get('vertices', ())
                    if isinstance(vertex, dict) and 'position' in vertex
                ]
                if len(points) < 3:
                    return None
                return EntityBodyGeometry(
                    kind='extruded_polygon',
                    footprint_vertices=vertices(to_local(p) for p in points),
                )
        except (KeyError, TypeError, ValueError):
            return None
        return None
