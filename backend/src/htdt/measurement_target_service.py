"""Measurement target-pattern product workflow (#987).

The canonical product path for spatial target sets: choose an anchor,
pick a transparent preset or explicit offsets, preview resolved positions,
materialize exact ``measurement_point`` entities through the pinned
pattern authority, and rebase when the anchor/head moved. Qt widgets hold
no pattern math.
"""

from __future__ import annotations

from dataclasses import dataclass

from .cad_measurement_target_pattern import (
    CadTargetPatternRepository,
    MaterializedPatternPoint,
    MeasurementTargetPattern,
    PatternAnchorKind,
    PatternOffsetFrame,
    TargetPatternOffset,
    build_target_pattern,
    materialize_target_pattern,
    rebase_target_pattern,
    resolved_pattern_positions,
)
from .cad_repository import SceneRepository
from .cad_scene import Position3
from .clock import utc_now_iso as _utc_now


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
