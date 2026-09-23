"""Standard views, camera records, named views and section planes (#545, #629).

This module holds the *non-authoritative* editor-view models. Everything here
is persisted through dedicated repository tables (``editor_camera_states`` and
``editor_named_views``), never inside a SceneRevision, so saving or restoring a
view can never change the document's content hash.

Coordinate convention: all stored vectors are in *domain* space
(+X right, +Y toward the rear, +Z up). The viewport converts to its render
space (Y negated) when applying a state to the VTK camera.
"""

from __future__ import annotations

from enum import Enum
from math import isfinite

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StandardView(str, Enum):
    """The six canonical working views plus free perspective."""

    PERSPECTIVE = 'perspective'
    TOP = 'top'
    FRONT = 'front'
    REAR = 'rear'
    LEFT = 'left'
    RIGHT = 'right'


#: Orthographic views use parallel projection by default.
ORTHOGRAPHIC_VIEWS: tuple[StandardView, ...] = (
    StandardView.TOP,
    StandardView.FRONT,
    StandardView.REAR,
    StandardView.LEFT,
    StandardView.RIGHT,
)

#: Value stored in ``RoomCameraState.standard_view`` when the camera has been
#: orbited away from any canonical orientation.
CUSTOM_VIEW = 'custom'

STANDARD_VIEW_LABELS: dict[StandardView, str] = {
    StandardView.PERSPECTIVE: '透視',
    StandardView.TOP: '上面',
    StandardView.FRONT: '正面',
    StandardView.REAR: '背面',
    StandardView.LEFT: '左側面',
    StandardView.RIGHT: '右側面',
}

#: (view direction, camera up) per orthographic view, in DOMAIN coordinates.
#: The view direction is where the camera looks; position = focal - dir * d.
#: - TOP looks straight down with the screen-up axis pointing to -Y (the
#:   front wall at the top of the page, matching the room sketch convention).
#: - FRONT looks at the front wall from the rear (+Y) toward -Y.
#: - REAR looks from the front toward +Y.
#: - LEFT is the camera on the -X side looking +X; RIGHT mirrors it.
STANDARD_VIEW_GEOMETRY: dict[
    StandardView, tuple[tuple[float, float, float], tuple[float, float, float]]
] = {
    StandardView.TOP: ((0.0, 0.0, -1.0), (0.0, -1.0, 0.0)),
    StandardView.FRONT: ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
    StandardView.REAR: ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    StandardView.LEFT: ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    StandardView.RIGHT: ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
}


def _require_finite(
    value: tuple[float, float, float], field: str
) -> tuple[float, float, float]:
    if not all(isfinite(component) for component in value):
        raise ValueError(f'{field} must contain only finite values')
    return value


class RoomCameraState(BaseModel):
    """One persisted camera, in domain coordinates.

    ``standard_view`` records which canonical view the camera was in when
    captured (``'custom'`` for a free orbit); it is metadata for the view
    menu, not an enforcement that the pose still matches.
    """

    model_config = ConfigDict(extra='forbid')

    schema_version: int = 1
    standard_view: str = CUSTOM_VIEW
    projection: str = 'perspective'  # 'perspective' | 'parallel'
    position: tuple[float, float, float]
    focal_point: tuple[float, float, float]
    view_up: tuple[float, float, float]
    parallel_scale: float | None = None
    view_angle: float | None = None

    @field_validator('position', 'focal_point', 'view_up')
    @classmethod
    def _finite(
        cls, value: tuple[float, float, float], info
    ) -> tuple[float, float, float]:
        return _require_finite(value, info.field_name)

    @field_validator('projection')
    @classmethod
    def _known_projection(cls, value: str) -> str:
        if value not in ('perspective', 'parallel'):
            raise ValueError('projection must be "perspective" or "parallel"')
        return value


class SectionPlaneState(BaseModel):
    """A display-only clipping plane — never feeds solver geometry."""

    model_config = ConfigDict(extra='forbid')

    schema_version: int = 1
    enabled: bool = True
    origin: tuple[float, float, float]
    normal: tuple[float, float, float]
    label: str | None = None

    @field_validator('origin', 'normal')
    @classmethod
    def _finite(
        cls, value: tuple[float, float, float], info
    ) -> tuple[float, float, float]:
        return _require_finite(value, info.field_name)

    @field_validator('normal')
    @classmethod
    def _nonzero(cls, value: tuple[float, float, float]) -> tuple[float, float, float]:
        if sum(component * component for component in value) <= 1e-12:
            raise ValueError('section normal must be non-zero')
        return value


class PersistedViewState(BaseModel):
    """Row payload of ``editor_camera_states`` — the last working camera plus
    the display-only section state, per document. Restored on reopen; never
    part of a SceneRevision.
    """

    model_config = ConfigDict(extra='forbid')

    schema_version: int = 1
    camera: RoomCameraState
    section: SectionPlaneState | None = None


class NamedViewSpec(BaseModel):
    """Payload of one named viewport workspace.

    ``hidden_ids`` is ``None`` when the view does not own a visibility set
    (recall leaves current visibility untouched); otherwise it is the exact
    set of entity ids to hide. Ids that no longer exist are skipped on recall
    and never rebound by name.
    """

    model_config = ConfigDict(extra='forbid')

    schema_version: int = 1
    name: str = Field(min_length=1, max_length=64)
    camera: RoomCameraState
    hidden_ids: tuple[str, ...] | None = None
    focus_entity_id: str | None = None
    section: SectionPlaneState | None = None


__all__ = [
    'CUSTOM_VIEW',
    'ORTHOGRAPHIC_VIEWS',
    'STANDARD_VIEW_GEOMETRY',
    'STANDARD_VIEW_LABELS',
    'NamedViewSpec',
    'PersistedViewState',
    'RoomCameraState',
    'SectionPlaneState',
    'StandardView',
]
