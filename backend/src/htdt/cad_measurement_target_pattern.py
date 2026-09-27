"""Reproducible multi-point microphone target patterns (#543).

A MeasurementTargetPattern pins one exact anchor (seat listener reference,
existing measurement point, or explicit world point) inside one exact
SceneRevision, plus a deterministic, immutable offset list declared in an
explicit coordinate frame. Materializing a pattern produces exact
``measurement_point`` SceneEntity targets that carry their lineage
(pattern identity, anchor revision, offset index) — historical points are
never retranslated when the anchor later moves; a moved anchor requires an
explicit rebase producing a new pattern version.

Discrete patterns are never moving-microphone trajectories: an MMM method
would need a separate trajectory/time authority rather than pretending a
continuous sweep is a set of exact point measurements.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from math import isfinite
import sqlite3
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_repository import SaveResult, SceneRepository
from .cad_scene import (
    Position3,
    Quaternion4,
    SceneEntity,
    acoustic_reference_position,
    quaternion_to_matrix3,

)

from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash


PatternAnchorKind = Literal['seat', 'measurement_point', 'explicit_point']
PatternOffsetFrame = Literal['world', 'anchor_local']
PatternPointPurpose = Literal['measurement', 'calibration', 'holdout', 'diagnostic']
TARGET_PATTERN_SCHEMA_VERSION = 'measurement-target-pattern-1'






def _position_payload(position: Position3) -> dict[str, float]:
    return {'x_m': position.x_m, 'y_m': position.y_m, 'z_m': position.z_m}


class TargetPatternOffset(BaseModel):
    """One deterministic pattern point in the declared offset frame."""

    model_config = ConfigDict(frozen=True)

    offset_index: int = Field(ge=0)
    label: str = Field(min_length=1)
    offset_m: tuple[float, float, float]
    purpose: PatternPointPurpose = 'measurement'
    # Acceptance guidance radius for the physical microphone placement; it
    # is guidance authority, never proof the microphone was within it.
    placement_tolerance_m: float | None = Field(default=None, gt=0)

    @model_validator(mode='after')
    def valid_offset(self) -> 'TargetPatternOffset':
        if len(self.offset_m) != 3 or any(not isfinite(float(v)) for v in self.offset_m):
            raise ValueError('pattern offsets require three finite metre values')
        return self


class MeasurementTargetPattern(BaseModel):
    """Immutable anchored, versioned spatial sampling pattern.

    ``anchor_*`` pins the exact anchor semantics inside the exact
    SceneRevision the pattern was authored against; ``offsets`` are the
    complete deterministic target list in ``offset_frame``. The
    ``supersedes_pattern_sha256`` field links an explicit rebase to the exact
    earlier pattern version — never a silent translation.
    """

    model_config = ConfigDict(frozen=True)

    pattern_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    anchor_revision_id: str = Field(min_length=1)
    anchor_revision_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    anchor_kind: PatternAnchorKind
    anchor_entity_id: str | None = Field(default=None, min_length=1)
    anchor_position: Position3
    anchor_orientation: Quaternion4 | None = None
    offset_frame: PatternOffsetFrame = 'world'
    pattern_version: int = Field(ge=1)
    offsets: tuple[TargetPatternOffset, ...] = Field(min_length=1)
    supersedes_pattern_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    created_at: str = Field(min_length=1)
    pattern_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_pattern(self) -> 'MeasurementTargetPattern':
        indices = [offset.offset_index for offset in self.offsets]
        if len(indices) != len(set(indices)):
            raise ValueError('pattern offset indices must be unique')
        if indices != sorted(indices):
            raise ValueError('pattern offsets must be ordered by offset_index')
        if self.anchor_kind == 'explicit_point':
            if self.anchor_entity_id is not None:
                raise ValueError('explicit-point anchors carry no entity id')
            if self.offset_frame == 'anchor_local':
                raise ValueError('explicit-point anchors have no local frame')
        else:
            if self.anchor_entity_id is None:
                raise ValueError('entity anchors require anchor_entity_id')
        if self.pattern_sha256 != _hash(self.identity_payload()):
            raise ValueError('measurement target pattern hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': TARGET_PATTERN_SCHEMA_VERSION,
            'document_id': self.document_id,
            'anchor_revision_id': self.anchor_revision_id,
            'anchor_revision_content_hash': self.anchor_revision_content_hash,
            'anchor_kind': self.anchor_kind,
            'anchor_entity_id': self.anchor_entity_id,
            'anchor_position': _position_payload(self.anchor_position),
            'anchor_orientation': (
                None
                if self.anchor_orientation is None
                else {
                    'w': self.anchor_orientation.w,
                    'x': self.anchor_orientation.x,
                    'y': self.anchor_orientation.y,
                    'z': self.anchor_orientation.z,
                }
            ),
            'offset_frame': self.offset_frame,
            'pattern_version': self.pattern_version,
            'offsets': [
                {
                    'offset_index': offset.offset_index,
                    'label': offset.label,
                    'offset_m': list(offset.offset_m),
                    'purpose': offset.purpose,
                    'placement_tolerance_m': offset.placement_tolerance_m,
                }
                for offset in self.offsets
            ],
            'supersedes_pattern_sha256': self.supersedes_pattern_sha256,
        }


class MaterializedPatternPoint(BaseModel):
    """Lineage record for one exact pattern-generated measurement target.

    The resolved world position is materialized once and frozen here: the
    record always reports the position/anchor revision it was created from,
    so historical evidence never moves when the anchor later moves.
    """

    model_config = ConfigDict(frozen=True)

    point_id: str = Field(min_length=1)
    pattern_id: str = Field(min_length=1)
    pattern_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    pattern_version: int = Field(ge=1)
    document_id: str = Field(min_length=1)
    created_in_revision_id: str = Field(min_length=1)
    anchor_kind: PatternAnchorKind
    anchor_entity_id: str | None = None
    anchor_revision_id: str = Field(min_length=1)
    offset_index: int = Field(ge=0)
    label: str = Field(min_length=1)
    offset_m: tuple[float, float, float]
    position: Position3
    purpose: PatternPointPurpose = 'measurement'
    placement_tolerance_m: float | None = None
    measurement_point_entity_id: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    point_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_point(self) -> 'MaterializedPatternPoint':
        if self.point_sha256 != _hash(self.identity_payload()):
            raise ValueError('materialized pattern point hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': TARGET_PATTERN_SCHEMA_VERSION,
            'pattern_id': self.pattern_id,
            'pattern_sha256': self.pattern_sha256,
            'pattern_version': self.pattern_version,
            'document_id': self.document_id,
            'created_in_revision_id': self.created_in_revision_id,
            'anchor_kind': self.anchor_kind,
            'anchor_entity_id': self.anchor_entity_id,
            'anchor_revision_id': self.anchor_revision_id,
            'offset_index': self.offset_index,
            'label': self.label,
            'offset_m': list(self.offset_m),
            'position': _position_payload(self.position),
            'purpose': self.purpose,
            'placement_tolerance_m': self.placement_tolerance_m,
            'measurement_point_entity_id': self.measurement_point_entity_id,
        }


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
    provisional = MeasurementTargetPattern.model_construct(
        **payload,
        pattern_sha256='0' * 64,
    )
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


class CadTargetPatternRepository:
    """Append-only storage for patterns and materialized point lineage."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self):
        return closing(connect_sqlite(self.path))

    def _initialize(self) -> None:
        with self._connect() as connection, connection:
            require_native_tables(connection, 'cad_measurement_target_patterns', 'cad_materialized_pattern_points')

    def save_pattern(self, pattern: MeasurementTargetPattern) -> None:
        if self.get_pattern(pattern.pattern_id) is not None:
            raise ValueError('target pattern ids are append-only')
        revision = self.scene_repository.get(pattern.anchor_revision_id)
        if revision is None or revision.document_id != pattern.document_id:
            raise ValueError('pattern anchor SceneRevision does not exist')
        if revision.content_hash != pattern.anchor_revision_content_hash:
            raise ValueError('pattern anchor revision content hash mismatch')
        with self._connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_measurement_target_patterns (
                    pattern_id, document_id, pattern_version, pattern_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    pattern.pattern_id,
                    pattern.document_id,
                    pattern.pattern_version,
                    pattern.pattern_sha256,
                    datetime.now(timezone.utc).isoformat(),
                    pattern.model_dump_json(),
                ),
            )

    def list_patterns(
        self,
        document_id: str,
    ) -> tuple[MeasurementTargetPattern, ...]:
        """All persisted patterns of one project, oldest first."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_measurement_target_patterns
                WHERE document_id=?
                ORDER BY created_at_utc, pattern_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            MeasurementTargetPattern.model_validate_json(row['payload_json'])
            for row in rows
        )

    def get_pattern(self, pattern_id: str) -> MeasurementTargetPattern | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_measurement_target_patterns '
                'WHERE pattern_id=?',
                (pattern_id,),
            ).fetchone()
        if row is None:
            return None
        return MeasurementTargetPattern.model_validate_json(row['payload_json'])

    def save_materialized_point(self, point: MaterializedPatternPoint) -> None:
        pattern = self.get_pattern(point.pattern_id)
        if pattern is None or pattern.pattern_sha256 != point.pattern_sha256:
            raise ValueError('materialized point requires the exact persisted pattern')
        revision = self.scene_repository.get(point.created_in_revision_id)
        if revision is None or revision.document_id != point.document_id:
            raise ValueError('materialized point creation revision does not exist')
        entity = revision.document.entity(point.measurement_point_entity_id)
        if entity.kind != 'measurement_point':
            raise ValueError('materialized point must bind a measurement_point entity')
        if entity.position != point.position:
            raise ValueError('materialized point position does not match the entity')
        with self._connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_materialized_pattern_points (
                    point_id, pattern_id, document_id,
                    measurement_point_entity_id, point_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    point.point_id,
                    point.pattern_id,
                    point.document_id,
                    point.measurement_point_entity_id,
                    point.point_sha256,
                    datetime.now(timezone.utc).isoformat(),
                    point.model_dump_json(),
                ),
            )

    def list_pattern_points(
        self,
        pattern_id: str,
    ) -> tuple[MaterializedPatternPoint, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_materialized_pattern_points
                WHERE pattern_id=?
                ORDER BY json_extract(payload_json, '$.offset_index')
                """,
                (pattern_id,),
            ).fetchall()
        return tuple(
            MaterializedPatternPoint.model_validate_json(row['payload_json'])
            for row in rows
        )
