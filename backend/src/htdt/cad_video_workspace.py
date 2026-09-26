"""Video-geometry authoring workspace for the workflow-first Room UI (#455).

``cad_video_geometry.py`` holds the exact evaluation authority; this module
is the per-document authoring record — which projector/screen/seat entities
participate, which verified ``ProjectorSpecification`` is bound, which
screen/seat geometry overrides apply, and the evaluation policy. Persisted
beside the CAD scene (document-scoped, not scene truth): deleting entities
or switching revisions never corrupts it — stale bindings are reported as
configuration gaps instead of silently evaluating.

Policy defaults are explicit and labelled; no cinema standard is implied.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_direct_view import (
    DirectViewDisplaySpecification,
    DirectViewGeometryRequest,
    DisplayGeometryBinding,
    build_direct_view_geometry_request,
)
from .cad_scene import Position3, SceneDocument
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
)
from .cad_video_geometry import (
    AngleRange,
    ProjectorSpecification,
    ScreenGeometryBinding,
    SeatGeometryBinding,
    SightlineSample,
    VideoGeometryPolicy,
    VideoGeometryRequest,
    build_video_geometry_request,
)
from .r120_geometry_compiler import ExactExternalAuthorityRef


VIDEO_WORKSPACE_SCHEMA_VERSION = 1

# Default sightline sampling: the image center plus aperture corners and edge
# midpoints — the N-series seat-view authority semantics, now user-visible.
DEFAULT_SIGHTLINE_SAMPLES: tuple[SightlineSample, ...] = (
    SightlineSample(sample_id='center', horizontal_fraction=0.5, vertical_fraction=0.5),
    SightlineSample(sample_id='top-left', horizontal_fraction=0.0, vertical_fraction=1.0),
    SightlineSample(sample_id='top-right', horizontal_fraction=1.0, vertical_fraction=1.0),
    SightlineSample(sample_id='bottom-left', horizontal_fraction=0.0, vertical_fraction=0.0),
    SightlineSample(sample_id='bottom-right', horizontal_fraction=1.0, vertical_fraction=0.0),
)

DEFAULT_POLICY = VideoGeometryPolicy(
    horizontal_viewing_angle_deg=AngleRange(minimum_deg=0.0, maximum_deg=50.0),
    vertical_viewing_angle_deg=AngleRange(minimum_deg=0.0, maximum_deg=35.0),
    center_elevation_angle_deg=AngleRange(minimum_deg=0.0, maximum_deg=15.0),
    sightline_samples=DEFAULT_SIGHTLINE_SAMPLES,
    sightline_clearance_m=0.0,
    riser_support_tolerance_m=0.01,
    max_optical_axis_deviation_deg=30.0,
    collision_clearance_m=0.0,
)


def default_seat_binding(entity_id: str) -> SeatGeometryBinding:
    """Generic seated-preset geometry — explicitly *not* authoritative.

    #1056: these numbers have no provenance and are stamped
    ``geometry_source='legacy'`` so eye/head-dependent criteria evaluate
    UNKNOWN; a real project must materialize an exact ListenerPose or
    explicit manual values instead.
    """

    return SeatGeometryBinding(
        entity_id=entity_id,
        row_id='row-1',
        eye_reference_offset_local_m=_offset(0.0, 0.0, 1.10),
        head_center_offset_local_m=_offset(0.0, 0.0, 1.15),
        head_radius_m=0.10,
        riser_entity_id=None,
        geometry_source='legacy',
    )


def _offset(x: float, y: float, z: float) -> Any:
    from .cad_scene import Offset3

    return Offset3(x_m=x, y_m=y, z_m=z)


def default_screen_binding(entity_id: str, *, width_m: float, height_m: float) -> ScreenGeometryBinding:
    """Aperture defaults to the screen's full front face — editable after."""

    return ScreenGeometryBinding(
        entity_id=entity_id,
        visible_width_m=width_m,
        visible_height_m=height_m,
        image_center_offset_local_m=_offset(0.0, 0.0, 0.0),
        frame_clearance_m=0.05,
    )


class VideoGeometryWorkspace(BaseModel):
    """Per-document video-geometry authoring record (not scene truth)."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    document_id: str = Field(min_length=1)
    # #1054: Room > Video target type. 'projection' = projector + passive
    # screen; 'direct_view' = a display entity bound to a
    # DirectViewDisplaySpecification — never modeled as a fake projector.
    target_type: Literal['projection', 'direct_view'] = 'projection'
    projector_entity_id: str | None = None
    projector_specification_sha256: str | None = None
    screen_bindings: dict[str, ScreenGeometryBinding] = Field(default_factory=dict)
    # Direct-view target (#1054): the display entity, its geometry binding
    # (active image aperture vs chassis stay distinct there), and the exact
    # specification hash from the canonical direct-view repository.
    display_entity_id: str | None = None
    display_binding: DisplayGeometryBinding | None = None
    display_specification_sha256: str | None = None
    seat_bindings: dict[str, SeatGeometryBinding] = Field(default_factory=dict)
    # Selected listener-pose authority per seat (#632); the seat binding's
    # eye/head offsets are derived from this exact authority, not re-entered.
    seat_pose_refs: dict[str, ExactExternalAuthorityRef] = Field(
        default_factory=dict
    )
    policy: VideoGeometryPolicy = DEFAULT_POLICY
    collision_entity_ids: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_display_binding(self) -> 'VideoGeometryWorkspace':
        if (
            self.display_entity_id is not None
            and self.display_binding is not None
            and self.display_binding.entity_id != self.display_entity_id
        ):
            raise ValueError(
                'display binding must reference the selected display entity'
            )
        return self


def video_workspace_missing_inputs(
    document: SceneDocument,
    workspace: VideoGeometryWorkspace,
) -> tuple[str, ...]:
    """Human-readable list of configuration gaps; empty = ready to evaluate."""

    missing: list[str] = []
    kinds = {entity.entity_id: entity.kind for entity in document.entities}
    if workspace.target_type == 'direct_view':
        # #1054: projector/screen requirements are NOT_APPLICABLE here — a
        # direct-view display is neither a fake projector nor a passive
        # screen. It needs a display entity and its aperture binding.
        if workspace.display_entity_id is None:
            missing.append('ディスプレイ未選択')
        elif kinds.get(workspace.display_entity_id) != 'display':
            missing.append('ディスプレイ参照が無効です')
        if workspace.display_binding is None:
            missing.append('ディスプレイの有効画域が未バインドです')
        return _seat_missing_inputs(document, workspace, missing)
    if workspace.projector_entity_id is None:
        missing.append('プロジェクター未選択')
    elif kinds.get(workspace.projector_entity_id) != 'projector':
        missing.append('プロジェクター参照が無効です')
    if workspace.projector_specification_sha256 is None:
        missing.append('プロジェクター仕様が未バインドです')
    screen_entity = None
    if workspace.screen_bindings:
        screen_id = sorted(workspace.screen_bindings)[0]
        screen_entity = document.entity(screen_id) if kinds.get(screen_id) == 'screen' else None
    if screen_entity is None:
        missing.append('スクリーンの画素設定が未バインドです')
    return _seat_missing_inputs(document, workspace, missing)


def _seat_missing_inputs(
    document: SceneDocument,
    workspace: VideoGeometryWorkspace,
    missing: list[str],
) -> tuple[str, ...]:
    kinds = {entity.entity_id: entity.kind for entity in document.entities}
    for seat_id in workspace.seat_bindings:
        if kinds.get(seat_id) != 'seat':
            missing.append(f'座席 {seat_id} のバインド先が座席ではありません')
    # #1056: eye/head readiness distinguishes exact pose / explicit manual
    # materialization from legacy or absent authority — never silently OK.
    for entity in document.entities:
        if entity.kind != 'seat':
            continue
        binding = workspace.seat_bindings.get(entity.entity_id)
        if binding is None:
            missing.append(
                f'座席 {entity.entity_id} のリスナーポーズ（目・頭位置）が未設定です'
            )
        elif binding.geometry_source == 'legacy':
            missing.append(
                f'座席 {entity.entity_id} の目・頭位置は権威なし（レガシー）です'
            )
    return tuple(missing)


def build_request_from_workspace(
    document: SceneDocument,
    workspace: VideoGeometryWorkspace,
    specification: ProjectorSpecification,
) -> VideoGeometryRequest:
    """Assemble the exact ``VideoGeometryRequest`` for the current scene."""

    if workspace.projector_entity_id is None:
        raise ValueError('プロジェクターが未選択です')
    screen_ids = sorted(workspace.screen_bindings)
    if not screen_ids:
        raise ValueError('スクリーンバインドが未設定です')
    collision_ids = workspace.collision_entity_ids or tuple(
        entity.entity_id
        for entity in document.entities
        if entity.entity_id not in {workspace.projector_entity_id, *screen_ids}
        and entity.kind in _COLLIDABLE_KINDS
    )
    return build_video_geometry_request(
        projector_entity_id=workspace.projector_entity_id,
        projector_specification=specification,
        screen=workspace.screen_bindings[screen_ids[0]],
        seats=tuple(
            workspace.seat_bindings[seat_id] for seat_id in sorted(workspace.seat_bindings)
        ),
        policy=workspace.policy,
        collision_entity_ids=collision_ids,
    )


def build_direct_view_request_from_workspace(
    document: SceneDocument,
    workspace: VideoGeometryWorkspace,
    specification: DirectViewDisplaySpecification | None = None,
) -> DirectViewGeometryRequest:
    """Assemble the exact ``DirectViewGeometryRequest`` (#1054).

    Same seat/ListenerPose authority path as projection — the shared
    ``seat_bindings`` drive eye/head geometry — but the target is a
    ``display`` entity with its active-aperture binding; projector-only
    concepts never enter. The exact display specification pin is optional
    for geometry but required for spec-conformance results.
    """

    if workspace.target_type != 'direct_view':
        raise ValueError('ワークスペースのターゲットがディスプレイではありません')
    if workspace.display_entity_id is None:
        raise ValueError('ディスプレイが未選択です')
    entity = document.entity(workspace.display_entity_id)
    if entity is None or entity.kind != 'display':
        raise ValueError('ディスプレイ参照が無効です')
    if workspace.display_binding is None:
        raise ValueError('ディスプレイの有効画域バインドが未設定です')
    # Direct-view collision evaluation accepts speaker/screen/projector/
    # display entities only — seats and measurement points are excluded.
    collision_ids = workspace.collision_entity_ids or tuple(
        entity.entity_id
        for entity in document.entities
        if entity.entity_id != workspace.display_entity_id
        and entity.kind in {'speaker', 'screen', 'projector', 'display'}
    )
    return build_direct_view_geometry_request(
        display=workspace.display_binding,
        seats=tuple(
            workspace.seat_bindings[seat_id] for seat_id in sorted(workspace.seat_bindings)
        ),
        policy=workspace.policy,
        collision_entity_ids=collision_ids,
        display_specification=specification,
    )


from .cad_scene import PHYSICAL_ENTITY_KINDS  # noqa: E402

_COLLIDABLE_KINDS = frozenset(PHYSICAL_ENTITY_KINDS) | {'seat', 'measurement_point'}


class CadVideoWorkspaceRepository:
    """Document-scoped ``VideoGeometryWorkspace`` persistence (same project DB)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_video_geometry_workspaces')

    def load(self, document_id: str) -> VideoGeometryWorkspace:
        if not document_id:
            raise ValueError('document_id must not be empty')
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_video_geometry_workspaces WHERE document_id=?',
                (document_id,),
            ).fetchone()
        if row is None:
            return VideoGeometryWorkspace(document_id=document_id)
        return VideoGeometryWorkspace.model_validate(json.loads(str(row['payload_json'])))

    def save(self, workspace: VideoGeometryWorkspace) -> None:
        payload = json.dumps(
            workspace.model_dump(mode='json'),
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
        updated_at = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                '''
                INSERT INTO cad_video_geometry_workspaces(document_id, schema_version, updated_at_utc, payload_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    schema_version=excluded.schema_version,
                    updated_at_utc=excluded.updated_at_utc,
                    payload_json=excluded.payload_json
                ''',
                (
                    workspace.document_id,
                    workspace.schema_version,
                    updated_at,
                    payload,
                ),
            )


def _world_offset(entity, offset) -> tuple[float, float, float]:
    """Rotate an entity-local offset by the entity's orientation into world space."""

    from .cad_scene import quaternion_to_matrix3

    matrix = quaternion_to_matrix3(entity.orientation)
    local = (float(offset.x_m), float(offset.y_m), float(offset.z_m))
    rotated = tuple(
        sum(matrix[row][column] * local[column] for column in range(3))
        for row in range(3)
    )
    return (
        float(entity.position.x_m) + rotated[0],
        float(entity.position.y_m) + rotated[1],
        float(entity.position.z_m) + rotated[2],
    )


def seat_eye_world(entity, binding: SeatGeometryBinding) -> tuple[float, float, float]:
    """World-space eye position the sightline evaluator consumes (#455).

    #1056: a legacy (unauthoritative) binding has no exact eye point —
    callers must refuse rather than present guessed geometry as the
    listener's viewpoint.
    """

    if entity.kind != 'seat':
        raise ValueError('seat binding must reference a seat SceneEntity')
    if binding.geometry_source == 'legacy':
        raise ValueError(
            'seat has no listener-pose or explicit eye authority'
        )
    return _world_offset(entity, binding.eye_reference_offset_local_m)


def screen_image_center_world(entity, binding: ScreenGeometryBinding) -> tuple[float, float, float]:
    """World-space screen image centre a seat camera should aim at."""

    if entity.kind != 'screen':
        raise ValueError('screen binding must reference a screen SceneEntity')
    return _world_offset(entity, binding.image_center_offset_local_m)


def display_image_center_world(entity, binding: DisplayGeometryBinding) -> tuple[float, float, float]:
    """World-space direct-view image centre a seat camera should aim at."""

    if entity.kind != 'display':
        raise ValueError('display binding must reference a display SceneEntity')
    return _world_offset(entity, binding.image_center_offset_local_m)
