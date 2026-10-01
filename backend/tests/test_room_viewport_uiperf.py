"""REV25-UIPERF render-path regression tests.

``render_document`` used to clear and rebuild every actor on every call — a
workspace refresh issues several identical ``render_document`` calls back to
back (activate + prediction overlay + focus overlay). These tests pin the
signature dedup: identical inputs must skip the rebuild and only re-present,
any input change must still rebuild, and a skipped render must drop overlay
actors owned by the compositing overlay renderers.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from htdt.cad_scene import (
    Position3,
    Quaternion4,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.room_viewport import (
    RoomOverlayState,
    RoomViewport3D,
    entity_render_meshes,
)


@pytest.fixture(scope="session", autouse=True)
def _app() -> QApplication:
    return QApplication.instance() or QApplication(["pytest-uiperf"])


class _CapturingPlotter:
    """Plotter double tracking rebuilds (clear) and draws (render)."""

    def __init__(self) -> None:
        self.renders = 0
        self.clears = 0
        self.actor_names: list[str] = []
        self.renderer = SimpleNamespace(actors={})
        self.camera = SimpleNamespace(zoom=lambda *a, **k: None)

    def clear(self) -> None:
        self.clears += 1
        self.actor_names.clear()
        self.renderer.actors.clear()

    def set_background(self, *args, **kwargs) -> None:
        pass

    def add_mesh(self, mesh=None, **kwargs):
        name = kwargs.get("name", "")
        self.actor_names.append(name)
        actor = SimpleNamespace(name=name)
        self.renderer.actors[name] = actor
        return actor

    def add_point_labels(self, *args, **kwargs):
        return SimpleNamespace(name="labels")

    def add_legend(self, *args, **kwargs):
        pass

    def add_axes(self, *args, **kwargs):
        pass

    def add_text(self, *args, **kwargs):
        pass

    def remove_actor(self, name, *args, **kwargs) -> None:
        self.renderer.actors.pop(name, None)
        if name in self.actor_names:
            self.actor_names.remove(name)

    def reset_camera(self, *args, **kwargs) -> None:
        pass

    def render(self) -> None:
        self.renders += 1


def _document() -> SceneDocument:
    return SceneDocument(
        document_id="doc-uiperf",
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id="speaker-1",
                kind="speaker",
                name="FL",
                speaker_role="FL",
                position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                orientation=Quaternion4(x=0.0, y=0.0, z=0.0, w=1.0),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
            ),
        ),
    )


def _viewport() -> tuple[RoomViewport3D, _CapturingPlotter]:
    viewport = RoomViewport3D()
    plotter = _CapturingPlotter()
    viewport.plotter = plotter
    return viewport, plotter


def test_identical_render_document_skips_scene_rebuild(_app) -> None:
    viewport, plotter = _viewport()
    document = _document()
    overlays = RoomOverlayState()

    viewport.render_document(document, selected_id=None, overlays=overlays)
    viewport.render_document(document, selected_id=None, overlays=overlays)

    assert plotter.clears == 1
    assert plotter.renders == 2


def test_changed_inputs_still_rebuild(_app) -> None:
    viewport, plotter = _viewport()
    document = _document()
    viewport.render_document(document, selected_id=None, overlays=RoomOverlayState())

    # Overlay flag change — the render is a different picture.
    viewport.render_document(
        document, selected_id=None, overlays=RoomOverlayState(grid=False)
    )
    assert plotter.clears == 2

    # Equal content in a new document object — still identical, still skips.
    viewport.render_document(
        _document(), selected_id=None, overlays=RoomOverlayState(grid=False)
    )
    assert plotter.clears == 2

    # Actually-changed content rebuilds.
    moved = document.model_copy(
        update={
            "entities": (
                document.entities[0].model_copy(
                    update={"position": Position3(x_m=2.0, y_m=1.0, z_m=1.0)}
                ),
            )
        }
    )
    viewport.render_document(
        moved, selected_id=None, overlays=RoomOverlayState(grid=False)
    )
    assert plotter.clears == 3

    # reset_camera always rebuilds.
    viewport.render_document(
        moved,
        selected_id=None,
        overlays=RoomOverlayState(grid=False),
        reset_camera=True,
    )
    assert plotter.clears == 4


def test_skipped_render_drops_overlay_namespaces_only(_app) -> None:
    viewport, plotter = _viewport()
    document = _document()
    overlays = RoomOverlayState()

    viewport.render_document(document, selected_id=None, overlays=overlays)
    # Compositing overlays add their actors after render_document.
    plotter.add_mesh(object(), name="measure-line-1")
    plotter.add_mesh(object(), name="proposal-ghost-x")

    viewport.render_document(document, selected_id=None, overlays=overlays)

    assert plotter.clears == 1
    actors = set(plotter.renderer.actors)
    assert "measure-line-1" not in actors
    assert "proposal-ghost-x" not in actors
    # render_document-owned actors survive the skip untouched.
    assert "room-shell" in actors
    assert "entity-speaker-1" in actors


def test_entity_render_meshes_memoizes_by_content(_app) -> None:
    entity = _document().entities[0]
    first = entity_render_meshes(entity)
    second = entity_render_meshes(entity)
    assert first == second
    assert first[0] is second[0]

    moved = entity.model_copy(
        update={"position": Position3(x_m=2.0, y_m=1.0, z_m=1.0)}
    )
    third = entity_render_meshes(moved)
    assert third[0] is not first[0]
