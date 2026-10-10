"""Measurement target-pattern product workflow (#987).

The canonical product path for spatial target sets: choose an anchor,
pick a transparent preset or explicit offsets, preview resolved positions,
materialize exact ``measurement_point`` entities through the pinned
pattern authority, and rebase when the anchor/head moved. Qt widgets hold
no pattern math.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from ..domain.cad_measurement_target_pattern import (
    MaterializedPatternPoint,
    MeasurementTargetPattern,
    PatternAnchorKind,
    PatternOffsetFrame,
    TargetPatternOffset,
)
from ..persistence.cad_target_pattern_repository import CadTargetPatternRepository
from ...cad_repository import SceneRepository
from ...cad_scene import (
    Position3,
    Quaternion4,
    SceneEntity,
    acoustic_reference_position,
    quaternion_to_matrix3,
)
from ...cad_repository import SaveResult
from ...cad_schema import connect_sqlite, require_native_tables
from ...canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash, canonicalize_payload

from ...clock import utc_now_iso as _utc_now


def _preset_offsets(
    preset: str,
    *,
    spacing_m: float,
) -> tuple[TargetPatternOffset, ...]:
    """Transparent presets materialize explicit offsets — no hidden layout."""
    if spacing_m <= 0:
        raise ValueError('spacing must be positive')
    if preset == 'mlp_center':
        steps: tuple[tuple[str, tuple[float, float, float]], ...] = (
            ('center', (0.0, 0.0, 0.0)),
        )
    elif preset == 'mlp_cross':
        steps = (
            ('center', (0.0, 0.0, 0.0)),
            ('left', (-spacing_m, 0.0, 0.0)),
            ('right', (spacing_m, 0.0, 0.0)),
            ('front', (0.0, spacing_m, 0.0)),
            ('back', (0.0, -spacing_m, 0.0)),
            ('up', (0.0, 0.0, spacing_m)),
            ('down', (0.0, 0.0, -spacing_m)),
        )
    elif preset == 'mlp_lateral':
        steps = (
            ('center', (0.0, 0.0, 0.0)),
            ('left', (-spacing_m, 0.0, 0.0)),
            ('right', (spacing_m, 0.0, 0.0)),
        )
    else:
        raise ValueError(f"unknown target pattern preset: {preset!r}")
    return tuple(
        TargetPatternOffset(
            offset_index=index,
            label=f'{preset}/{label}',
            offset_m=offset,
            purpose='measurement',
        )
        for index, (label, offset) in enumerate(steps)
    )


TARGET_PATTERN_PRESETS = ('mlp_center', 'mlp_cross', 'mlp_lateral')


@dataclass(frozen=True, slots=True)
class TargetPatternPreview:
    labels: tuple[str, ...]
    positions: tuple[Position3, ...]


@dataclass(frozen=True, slots=True)
class TargetPatternPresentation:
    pattern_id: str
    anchor_kind: str
    anchor_entity_id: str | None
    anchor_revision_id: str
    pattern_version: int
    point_count: int
    materialized_count: int
    stale: bool


class MeasurementTargetService:
    """Native product surface for target patterns (#987)."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        document_id: str,
    ) -> None:
        self.scene_repository = scene_repository
        self.document_id = document_id
        self.repository = CadTargetPatternRepository(scene_repository)

    # ------------------------------------------------------------------
    # Authoring
    # ------------------------------------------------------------------

    def create_pattern(
        self,
        *,
        anchor_kind: PatternAnchorKind,
        anchor_entity_id: str | None = None,
        explicit_position: Position3 | None = None,
        preset: str = 'mlp_cross',
        spacing_m: float = 0.10,
        offset_frame: PatternOffsetFrame = 'world',
        offsets: tuple[TargetPatternOffset, ...] | None = None,
    ) -> MeasurementTargetPattern:
        offsets = (
            offsets
            if offsets is not None
            else _preset_offsets(preset, spacing_m=spacing_m)
        )
        pattern = build_target_pattern(
            self.scene_repository,
            document_id=self.document_id,
            anchor_kind=anchor_kind,
            anchor_entity_id=anchor_entity_id,
            explicit_position=explicit_position,
            offset_frame=offset_frame,
            offsets=offsets,
            created_at=_utc_now(),
        )
        self.repository.save_pattern(pattern)
        return pattern

    def preview(self, pattern: MeasurementTargetPattern) -> TargetPatternPreview:
        """Resolved positions for 3D preview — never creates evidence."""
        positions = resolved_pattern_positions(pattern)
        return TargetPatternPreview(
            labels=tuple(offset.label for offset in pattern.offsets),
            positions=positions,
        )

    # ------------------------------------------------------------------
    # Materialization / lifecycle
    # ------------------------------------------------------------------

    def materialize(
        self, pattern_id: str
    ) -> tuple[MaterializedPatternPoint, ...]:
        pattern = self.repository.get_pattern(pattern_id)
        if pattern is None:
            raise ValueError(f"ターゲットパターンが存在しません: {pattern_id}")
        _result, points = materialize_target_pattern(
            self.scene_repository,
            self.repository,
            pattern,
            created_at=_utc_now(),
        )
        return points

    def rebase(self, pattern_id: str) -> MeasurementTargetPattern:
        """New pattern version on the current head; history never moves."""
        pattern = self.repository.get_pattern(pattern_id)
        if pattern is None:
            raise ValueError(f"ターゲットパターンが存在しません: {pattern_id}")
        rebased = rebase_target_pattern(
            self.scene_repository,
            pattern,
            created_at=_utc_now(),
        )
        self.repository.save_pattern(rebased)
        return rebased

    def campaign_target_ids(self, pattern_id: str) -> tuple[str, ...]:
        """Exact generated target ids for campaign/prediction consumers."""
        return tuple(
            point.measurement_point_entity_id
            for point in self.repository.list_pattern_points(pattern_id)
        )

    # ------------------------------------------------------------------
    # Presentation
    # ------------------------------------------------------------------

    def anchor_entity_options(self) -> tuple[tuple[str, str], ...]:
        """(kind, entity_id) pairs the Native anchor combo can offer."""
        revision = self.scene_repository.current_head(self.document_id)
        if revision is None:
            return ()
        options = [
            ('seat', entity.entity_id)
            for entity in revision.document.entities
            if entity.kind == 'seat'
        ]
        options.extend(
            ('measurement_point', entity.entity_id)
            for entity in revision.document.entities
            if entity.kind == 'measurement_point'
        )
        return tuple(options)

    def list_presentations(self) -> tuple[TargetPatternPresentation, ...]:
        head = self.scene_repository.current_head(self.document_id)
        head_id = None if head is None else head.revision_id
        presentations: list[TargetPatternPresentation] = []
        for pattern in self.repository.list_patterns(self.document_id):
            points = self.repository.list_pattern_points(pattern.pattern_id)
            presentations.append(
                TargetPatternPresentation(
                    pattern_id=pattern.pattern_id,
                    anchor_kind=pattern.anchor_kind,
                    anchor_entity_id=pattern.anchor_entity_id,
                    anchor_revision_id=pattern.anchor_revision_id,
                    pattern_version=pattern.pattern_version,
                    point_count=len(pattern.offsets),
                    materialized_count=len(points),
                    stale=pattern.anchor_revision_id != head_id,
                )
            )
        return tuple(presentations)


def _resolve_anchor(
    revision,
    *,
    anchor_kind: PatternAnchorKind,
    anchor_entity_id: str | None,
    explicit_position: Position3 | None,
) -> tuple[Position3, Quaternion4 | None]:
    if anchor_kind == 'explicit_point':
        if explicit_position is None:
            raise ValueError('explicit-point anchors require an explicit position')
        return explicit_position, None
    if anchor_entity_id is None:
        raise ValueError('entity anchors require anchor_entity_id')
    try:
        entity = revision.document.entity(anchor_entity_id)
    except KeyError as exc:
        raise ValueError('anchor entity does not exist in the pinned SceneRevision') from exc
    if entity.kind != anchor_kind:
        raise ValueError(
            f'anchor entity kind mismatch: expected {anchor_kind}, found {entity.kind}'
        )
    position = acoustic_reference_position(entity)
    if position is None:
        raise ValueError(
            'anchor entity exposes no acoustic reference position in the '
            'pinned SceneRevision'
        )
    # ``anchor_local`` is only meaningful against a declared entity
    # orientation. For measurement points the orientation is an explicit
    # identity by convention; seats carry their authored orientation.
    return position, entity.orientation


def build_target_pattern(
    scene_repository: SceneRepository,
    *,
    document_id: str,
    anchor_revision_id: str | None = None,
    anchor_kind: PatternAnchorKind,
    anchor_entity_id: str | None = None,
    explicit_position: Position3 | None = None,
    offset_frame: PatternOffsetFrame = 'world',
    offsets: tuple[TargetPatternOffset, ...],
    pattern_version: int = 1,
    supersedes_pattern_sha256: str | None = None,
    created_at: str,
) -> MeasurementTargetPattern:
    """Author an immutable pattern pinned to one exact anchor SceneRevision."""
    revision = (
        scene_repository.get(anchor_revision_id)
        if anchor_revision_id is not None
        else scene_repository.latest(document_id)
    )
    if revision is None or revision.document_id != document_id:
        raise ValueError('anchor SceneRevision does not exist for this document')
    anchor_position, anchor_orientation = _resolve_anchor(
        revision,
        anchor_kind=anchor_kind,
        anchor_entity_id=anchor_entity_id,
        explicit_position=explicit_position,
    )
    ordered = tuple(sorted(offsets, key=lambda item: item.offset_index))
    payload: dict[str, Any] = {
        'pattern_id': str(uuid4()),
        'document_id': document_id,
        'anchor_revision_id': revision.revision_id,
        'anchor_revision_content_hash': revision.content_hash,
        'anchor_kind': anchor_kind,
        'anchor_entity_id': anchor_entity_id,
        'anchor_position': anchor_position,
        'anchor_orientation': anchor_orientation,
        'offset_frame': offset_frame,
        'pattern_version': pattern_version,
        'offsets': ordered,
        'supersedes_pattern_sha256': supersedes_pattern_sha256,
        'created_at': created_at,
    }
    provisional = MeasurementTargetPattern.model_construct(**canonicalize_payload(MeasurementTargetPattern, dict(
        **payload,
        pattern_sha256='0' * 64,
    )))
    return MeasurementTargetPattern(
        **payload,
        pattern_sha256=_hash(provisional.identity_payload()),
    )


def resolved_pattern_positions(pattern: MeasurementTargetPattern) -> tuple[Position3, ...]:
    """Deterministically resolve every offset into world coordinates.

    ``anchor_local`` rotates the offset by the anchor entity's declared
    orientation — listener-local axes are never inferred from a seat body
    box; without a declared orientation the frame is meaningless and the
    pattern could not have been authored.
    """
    if pattern.offset_frame == 'anchor_local':
        if pattern.anchor_orientation is None:
            raise ValueError('anchor_local frame requires a declared anchor orientation')
        matrix = quaternion_to_matrix3(pattern.anchor_orientation)
    else:
        matrix = None
    resolved: list[Position3] = []
    for offset in pattern.offsets:
        local = offset.offset_m
        if matrix is None:
            dx, dy, dz = local
        else:
            dx = sum(matrix[0][column] * local[column] for column in range(3))
            dy = sum(matrix[1][column] * local[column] for column in range(3))
            dz = sum(matrix[2][column] * local[column] for column in range(3))
        resolved.append(
            Position3(
                x_m=pattern.anchor_position.x_m + dx,
                y_m=pattern.anchor_position.y_m + dy,
                z_m=pattern.anchor_position.z_m + dz,
            )
        )
    return tuple(resolved)


def materialize_target_pattern(
    scene_repository: SceneRepository,
    pattern_repository: 'CadTargetPatternRepository',
    pattern: MeasurementTargetPattern,
    *,
    created_at: str,
) -> tuple[SaveResult, tuple[MaterializedPatternPoint, ...]]:
    """Create exact ``measurement_point`` SceneEntity targets for the pattern.

    The points are appended to a new SceneRevision saved as the document
    head; each returned lineage record binds the generated entity id, the
    pattern identity/version, the anchor revision and the resolved world
    position — immutable evidence that survives later anchor moves.
    """
    base_revision = scene_repository.get(pattern.anchor_revision_id)
    if base_revision is None or base_revision.document_id != pattern.document_id:
        raise ValueError('pattern anchor SceneRevision is unavailable')
    # Draft guard: materialization writes a new head outside the owning
    # workspace's working document; a persisted draft would be orphaned.
    if scene_repository.recovery(pattern.document_id) is not None:
        raise ValueError(
            '未保存の部屋の下書きを保存または破棄してからターゲットを生成してください'
        )
    head = scene_repository.latest(pattern.document_id)
    if head is None or head.revision_id != base_revision.revision_id:
        raise ValueError(
            'materialization requires the pinned anchor revision to be the '
            "document's current head; rebase the pattern explicitly instead"
        )
    positions = resolved_pattern_positions(pattern)
    entities = list(head.document.entities)
    points: list[tuple[TargetPatternOffset, dict[str, Any]]] = []
    new_entities: list[SceneEntity] = []
    for offset, position in zip(pattern.offsets, positions, strict=True):
        entity_id = str(uuid4())
        new_entities.append(
            SceneEntity(
                entity_id=entity_id,
                kind='measurement_point',
                name=offset.label,
                position=position,
            )
        )
        payload: dict[str, Any] = {
            'point_id': str(uuid4()),
            'pattern_id': pattern.pattern_id,
            'pattern_sha256': pattern.pattern_sha256,
            'pattern_version': pattern.pattern_version,
            'document_id': pattern.document_id,
            'created_in_revision_id': '',
            'anchor_kind': pattern.anchor_kind,
            'anchor_entity_id': pattern.anchor_entity_id,
            'anchor_revision_id': pattern.anchor_revision_id,
            'offset_index': offset.offset_index,
            'label': offset.label,
            'offset_m': offset.offset_m,
            'position': position,
            'purpose': offset.purpose,
            'placement_tolerance_m': offset.placement_tolerance_m,
            'measurement_point_entity_id': entity_id,
            'created_at': created_at,
        }
        points.append((offset, payload))
    document = head.document.model_copy(update={'entities': (*entities, *new_entities)})
    result = scene_repository.save(document, parent_revision_id=head.revision_id)
    materialized: list[MaterializedPatternPoint] = []
    for offset, payload in points:
        payload['created_in_revision_id'] = result.revision.revision_id
        provisional = MaterializedPatternPoint.model_construct(
            **payload,
            point_sha256='0' * 64,
        )
        point = MaterializedPatternPoint(
            **payload,
            point_sha256=_hash(provisional.identity_payload()),
        )
        pattern_repository.save_materialized_point(point)
        materialized.append(point)
    return result, tuple(materialized)


def rebase_target_pattern(
    scene_repository: SceneRepository,
    pattern: MeasurementTargetPattern,
    *,
    created_at: str,
) -> MeasurementTargetPattern:
    """Author a new pattern version on the document's current head.

    Historical materialized points keep their original anchor revision and
    positions; the rebased pattern re-resolves the anchor on the current head
    and links to the exact previous version via
    ``supersedes_pattern_sha256`` — geometry changes are explicit, never a
    silent translation of historical evidence.
    """
    if pattern.anchor_kind == 'explicit_point':
        # An explicit world point has no entity to re-resolve; a rebase just
        # re-pins the revision while keeping the declared position.
        anchor_entity_id = None
    else:
        anchor_entity_id = pattern.anchor_entity_id
    return build_target_pattern(
        scene_repository,
        document_id=pattern.document_id,
        anchor_revision_id=None,
        anchor_kind=pattern.anchor_kind,
        anchor_entity_id=anchor_entity_id,
        explicit_position=(
            pattern.anchor_position if pattern.anchor_kind == 'explicit_point' else None
        ),
        offset_frame=pattern.offset_frame,
        offsets=pattern.offsets,
        pattern_version=pattern.pattern_version + 1,
        supersedes_pattern_sha256=pattern.pattern_sha256,
        created_at=created_at,
    )


