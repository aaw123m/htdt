"""Issue #983 — overlapping-pick candidate chooser.

When one click resolves more than one entity, the viewport shows a
cursor-side chooser (``{i}/{n}`` header + ``name · kind`` rows + explicit
"cannot operate" reasons) instead of hiding the click-through cycle. These
tests drive the picker dispatch the same way the R9 suite does — recording
fakes, offscreen Qt platform, no GL context.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication

from htdt.cad_scene import (
    Position3,
    Quaternion4,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_view_state import SectionPlaneState
from htdt.room_viewport import RoomOverlayState, RoomViewport3D


@pytest.fixture(scope="session", autouse=True)
def _app() -> QApplication:
    return QApplication.instance() or QApplication(["pytest"])


class _FakeActor:
    def __init__(self, name: str) -> None:
        self.name = name

    def GetBounds(self):  # noqa: N802 - vtk shape
        return (0.0, 1.0, -1.0, 0.0, 0.0, 1.0)


class _FakeCamera:
    def __init__(self) -> None:
        self.position = (0.0, 0.0, 10.0)
        self.focal_point = (0.0, 0.0, 0.0)
        self.view_up = (0.0, 0.0, 1.0)

    def GetPosition(self):  # noqa: N802
        return self.position

    def SetPosition(self, *v):  # noqa: N802
        self.position = tuple(float(x) for x in v)

    def GetFocalPoint(self):  # noqa: N802
        return self.focal_point

    def SetFocalPoint(self, *v):  # noqa: N802
        self.focal_point = tuple(float(x) for x in v)

    def GetViewUp(self):  # noqa: N802
        return self.view_up

    def SetViewUp(self, *v):  # noqa: N802
        self.view_up = tuple(float(x) for x in v)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class _RecordingPlotter:
    def __init__(self) -> None:
        self.renders = 0
        self.add_calls: list[tuple[str, dict]] = []
        self.removed: list[str] = []
        self.camera = _FakeCamera()
        self.render_window = SimpleNamespace(GetSize=lambda: (800, 600))
        self.bounds = (0.0, 6.0, 0.0, 4.0, 0.0, 2.4)
        self.suppress_rendering = False

    def add_mesh(self, mesh=None, **kwargs):
        self.add_calls.append(("add_mesh", kwargs))
        return _FakeActor(str(kwargs.get("name", "")))

    def remove_actor(self, name=None, **kwargs):
        self.removed.append(str(name))
        self.add_calls.append(("remove_actor", kwargs))

    def render(self) -> None:
        if not self.suppress_rendering:
            self.renders += 1

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class _FakePicker:
    def __init__(self, *, props=(), actor=None) -> None:
        self.pick_args: list[tuple[float, float, float]] = []
        self._props = props
        self._actor = actor

    def Pick(self, x: float, y: float, z: float, renderer) -> int:
        self.pick_args.append((x, y, z))
        return 1

    def GetActor(self):
        return self._actor

    def GetPickPosition(self):
        return (1.0, 2.0, 3.0)

    def GetProp3Ds(self):
        props = self._props
        if not props:
            return None

        class _Props:
            def __init__(self, items) -> None:
                self._items = list(items)
                self._i = 0

            def InitTraversal(self):
                self._i = 0

            def GetNextProp(self):
                if self._i < len(self._items):
                    item = self._items[self._i]
                    self._i += 1
                    return item
                return None

        return _Props(props)


class _FakeIren:
    def __init__(self, picker) -> None:
        self.picker = picker
        self._renderer = object()

    def get_poked_renderer(self):
        return self._renderer

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _entity(entity_id: str, kind: str = "seat", *, name: str | None = None,
            position=(1.0, 1.0, 0.0)) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind=kind,
        name=name or entity_id,
        position=Position3(x_m=position[0], y_m=position[1], z_m=position[2]),
        orientation=Quaternion4(),
        speaker_role="FL" if kind == "speaker" else None,
        size_m=Size3(x_m=0.5, y_m=0.5, z_m=1.0),
    )


def _document(*entities: SceneEntity) -> SceneDocument:
    return SceneDocument(
        document_id="doc-983",
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=tuple(entities),
    )


def _render_document(viewport: RoomViewport3D, **overrides) -> SceneDocument:
    document = overrides.pop(
        "document",
        _document(
            _entity("e1", "speaker", name="Front L"),
            _entity("e2", "seat", name="Seat A1"),
            _entity("e3", "furniture", name="Rear cabinet"),
        ),
    )
    kwargs = dict(
        selected_id=None,
        overlays=RoomOverlayState(grid=False),
        reset_camera=False,
    )
    kwargs.update(overrides)
    viewport.render_document(document, **kwargs)
    return document


def _make_viewport(**render_overrides):
    """Viewport with a rendered 3-entity document and a stacked pick."""
    viewport = RoomViewport3D()
    viewport.interactor.resize(800, 600)
    plotter = _RecordingPlotter()
    viewport.plotter = plotter
    document = _render_document(viewport, **render_overrides)
    actors = (
        _FakeActor("entity-e1"),
        _FakeActor("entity-e2"),
        _FakeActor("entity-e3"),
    )
    # The rebuild's real actors are irrelevant to pick dispatch — register
    # the stacked fakes directly, same as test_room_viewport_r9 does.
    viewport._actor_entity_ids.clear()
    for actor, entity_id in zip(actors, ("e1", "e2", "e3")):
        viewport._actor_entity_ids[id(actor)] = entity_id
    picker = _FakePicker(actor=actors[0], props=actors)
    plotter.iren = _FakeIren(picker)
    viewport.interactor.GetEventPosition = lambda: (40.0, 559.0)
    return viewport, plotter, picker, actors, document


def _pick_front(viewport: RoomViewport3D, actors) -> None:
    viewport._picked_actor(actors[0])


def _mouse(event_type: QEvent.Type, position: tuple[float, float]) -> QMouseEvent:
    local = QPointF(position[0], position[1])
    return QMouseEvent(
        event_type, local, local,
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def _key(key) -> QKeyEvent:
    return QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)


def _send_key(viewport: RoomViewport3D, key) -> bool:
    assert viewport._pick_key_filter is not None
    return viewport._pick_key_filter.eventFilter(viewport.interactor, _key(key))


def _send_wheel(viewport: RoomViewport3D, delta: int) -> bool:
    assert viewport._pick_key_filter is not None
    event = QWheelEvent(
        QPointF(40.0, 40.0),
        QPointF(40.0, 40.0),
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    return viewport._pick_key_filter.eventFilter(viewport.interactor, event)


def _row_texts(viewport: RoomViewport3D) -> list[str]:
    return [row.accessibleName() for row in viewport._pick_popover._rows]


def test_multi_candidate_pick_opens_chooser(_app) -> None:
    viewport, plotter, picker, actors, _doc = _make_viewport()
    picked: list[str] = []
    viewport.entityPicked.connect(lambda eid, _pos: picked.append(eid))

    _pick_front(viewport, actors)

    assert picked == ["e1"]
    assert viewport.pick_candidates_active
    # The chooser is a real top-level (ToolTip) window — an alien child of
    # the interactor would lose clicks to the VTK render HWND (found on the
    # real Windows GUI: row clicks picked entities behind the popover).
    assert viewport._pick_popover.isWindow()
    assert viewport.pick_candidate_ids() == ("e1", "e2", "e3")
    assert viewport._pick_popover.header.text() == "重なり候補 1/3"
    rows = _row_texts(viewport)
    assert rows[0].startswith("1. Front L · スピーカー")
    assert rows[1].startswith("2. Seat A1 · 座席")
    assert rows[2].startswith("3. Rear cabinet · 家具")
    # The chooser reused the dispatched pick's prop list — zero re-picks.
    assert picker.pick_args == []


def test_single_candidate_pick_never_opens_chooser(_app) -> None:
    viewport = RoomViewport3D()
    viewport.interactor.resize(800, 600)
    viewport.plotter = _RecordingPlotter()
    _render_document(viewport)
    actor = _FakeActor("entity-e1")
    viewport._actor_entity_ids[id(actor)] = "e1"
    picker = _FakePicker(actor=actor, props=(actor,))
    viewport.plotter.iren = _FakeIren(picker)
    viewport.interactor.GetEventPosition = lambda: (40.0, 559.0)

    picked: list[str] = []
    viewport.entityPicked.connect(lambda eid, _pos: picked.append(eid))
    viewport._picked_actor(actor)

    assert picked == ["e1"]
    assert not viewport.pick_candidates_active
    assert viewport._pick_popover is None or viewport._pick_popover.isHidden()


def test_keys_and_wheel_step_the_armed_candidate(_app) -> None:
    viewport, _plotter, _picker, actors, _doc = _make_viewport()
    _pick_front(viewport, actors)
    assert viewport.pick_candidates_active

    assert _send_key(viewport, Qt.Key.Key_Down)
    assert viewport._pick_index == 1
    assert _send_key(viewport, Qt.Key.Key_Down)
    assert viewport._pick_index == 2
    # Down past the last wraps to the front-most candidate.
    assert _send_key(viewport, Qt.Key.Key_Tab)
    assert viewport._pick_index == 0
    assert _send_key(viewport, Qt.Key.Key_Up)
    assert viewport._pick_index == 2
    assert _send_key(viewport, Qt.Key.Key_Backtab)
    assert viewport._pick_index == 1
    # Wheel: scroll down moves toward the back, up toward the front.
    assert _send_wheel(viewport, -120)
    assert viewport._pick_index == 2
    assert _send_wheel(viewport, 120)
    assert viewport._pick_index == 1
    # The header tracks the armed position.
    assert viewport._pick_popover.header.text() == "重なり候補 2/3"


def test_unrelated_keys_fall_through(_app) -> None:
    viewport, _p, _pk, actors, _doc = _make_viewport()
    _pick_front(viewport, actors)
    assert _send_key(viewport, Qt.Key.Key_X) is False
    assert viewport._pick_index == 0
    assert viewport.pick_candidates_active


def test_shortcut_override_is_claimed_for_chooser_keys(_app) -> None:
    viewport, _p, _pk, actors, _doc = _make_viewport()
    _pick_front(viewport, actors)
    event = QKeyEvent(
        QEvent.Type.ShortcutOverride,
        Qt.Key.Key_Escape,
        Qt.KeyboardModifier.NoModifier,
    )
    assert viewport._pick_key_filter.eventFilter(viewport.interactor, event)
    assert event.isAccepted()
    other = QKeyEvent(
        QEvent.Type.ShortcutOverride,
        Qt.Key.Key_M,
        Qt.KeyboardModifier.NoModifier,
    )
    assert not viewport._pick_key_filter.eventFilter(viewport.interactor, other)


def test_enter_confirms_armed_candidate(_app) -> None:
    viewport, _plotter, _picker, actors, _doc = _make_viewport()
    picked: list[tuple[str, object]] = []
    viewport.entityPicked.connect(lambda eid, pos: picked.append((eid, pos)))

    _pick_front(viewport, actors)
    _send_key(viewport, Qt.Key.Key_Down)
    _send_key(viewport, Qt.Key.Key_Return)

    assert picked[-1][0] == "e2"
    assert picked[-1][1] is not None
    assert not viewport.pick_candidates_active
    # Confirming advanced the click-through cursor too — a repeat click at
    # the same spot continues past the confirmed candidate.
    viewport._picked_actor(actors[0])
    assert picked[-1][0] == "e3"


def test_row_click_confirms_that_candidate(_app) -> None:
    viewport, _p, _pk, actors, _doc = _make_viewport()
    picked: list[str] = []
    viewport.entityPicked.connect(lambda eid, _pos: picked.append(eid))
    _pick_front(viewport, actors)

    viewport._pick_popover.activated.emit("e3")
    assert picked[-1] == "e3"
    assert not viewport.pick_candidates_active


def test_escape_dismisses_without_selecting(_app) -> None:
    viewport, _p, _pk, actors, _doc = _make_viewport()
    picked: list[str] = []
    viewport.entityPicked.connect(lambda eid, _pos: picked.append(eid))
    _pick_front(viewport, actors)
    _send_key(viewport, Qt.Key.Key_Down)
    before = len(picked)

    _send_key(viewport, Qt.Key.Key_Escape)

    assert not viewport.pick_candidates_active
    assert len(picked) == before
    assert viewport.pick_candidate_ids() == ()


def test_locked_candidate_rows_state_the_reason(_app) -> None:
    viewport, _p, _pk, actors, _doc = _make_viewport(locked_ids={"e2"})
    _pick_front(viewport, actors)
    rows = _row_texts(viewport)
    assert "ロック中" in rows[1]
    assert "ロック中" not in rows[0]


def test_workspace_reason_provider_marks_uneditable_rows(_app) -> None:
    viewport, _p, _pk, actors, _doc = _make_viewport()
    viewport.pick_candidate_reason_provider = lambda _eid: "編集不可"
    _pick_front(viewport, actors)
    rows = _row_texts(viewport)
    assert all("編集不可" in row for row in rows)


def test_step_preview_highlight_is_render_only(_app) -> None:
    viewport, plotter, _pk, actors, document = _make_viewport()
    _pick_front(viewport, actors)
    plotter.add_calls.clear()
    plotter.removed.clear()

    _send_key(viewport, Qt.Key.Key_Down)

    previews = [
        kwargs
        for call, kwargs in plotter.add_calls
        if call == "add_mesh"
        and str(kwargs.get("name", "")).startswith("pick-candidate-")
    ]
    assert previews, "expected a preview envelope actor"
    assert all(
        kwargs["pickable"] is False and kwargs["render"] is False
        for kwargs in previews
    )
    assert any("pick-candidate-e2" in str(kwargs["name"]) for kwargs in previews)
    # Preview-only means the document is untouched — SceneRevision inputs
    # are never written from the chooser.
    assert viewport._document == document


def test_preview_skips_already_selected_candidate(_app) -> None:
    viewport, plotter, _pk, actors, _doc = _make_viewport(selected_id="e1")
    _pick_front(viewport, actors)
    # e1 is armed AND already selected — the real selection outline is the
    # highlight; no preview envelope is drawn for it.
    assert viewport._pick_preview_name is None
    _send_key(viewport, Qt.Key.Key_Down)
    assert viewport._pick_preview_name == "pick-candidate-e2"


def test_camera_move_discards_stale_candidates(_app) -> None:
    viewport, _p, _pk, actors, _doc = _make_viewport()
    _pick_front(viewport, actors)
    viewport.orbit_by(QPointF(4.0, 2.0))
    assert not viewport.pick_candidates_active

    _pick_front(viewport, actors)
    viewport.pan_by(QPointF(8.0, 0.0))
    assert not viewport.pick_candidates_active

    _pick_front(viewport, actors)
    viewport.zoom_by(1.0, QPointF(40.0, 40.0))
    assert not viewport.pick_candidates_active

    _pick_front(viewport, actors)
    viewport.apply_standard_view("top")
    assert not viewport.pick_candidates_active


def test_section_change_discards_stale_candidates(_app) -> None:
    viewport, _p, _pk, actors, _doc = _make_viewport()
    _pick_front(viewport, actors)
    viewport.set_aux_render_state(
        section=SectionPlaneState(origin=(0.0, 0.0, 1.0), normal=(0.0, 0.0, 1.0))
    )
    assert not viewport.pick_candidates_active


def test_document_change_discards_stale_candidates(_app) -> None:
    viewport, _p, _pk, actors, _doc = _make_viewport()
    _pick_front(viewport, actors)
    _render_document(
        viewport,
        document=_document(
            _entity("e1", "speaker", name="Front L"),
            _entity("e9", "seat", name="New seat"),
        ),
    )
    assert not viewport.pick_candidates_active


def test_same_document_refresh_keeps_chooser(_app) -> None:
    # Selection re-renders rebuild the actors but the hit stack is still
    # valid — the chooser survives with refreshed rows and a re-added
    # preview envelope.
    viewport, plotter, _pk, actors, document = _make_viewport()
    _pick_front(viewport, actors)
    _send_key(viewport, Qt.Key.Key_Down)
    assert viewport._pick_preview_name == "pick-candidate-e2"

    _render_document(viewport, document=document, selected_id="e1")

    assert viewport.pick_candidates_active
    assert viewport.pick_candidate_ids() == ("e1", "e2", "e3")
    assert viewport._pick_preview_name == "pick-candidate-e2"


def test_hidden_set_change_discards_stale_candidates(_app) -> None:
    viewport, _p, _pk, actors, document = _make_viewport()
    _pick_front(viewport, actors)
    _render_document(viewport, document=document, hidden_ids={"e3"})
    assert not viewport.pick_candidates_active


def test_marquee_activation_dismisses_chooser(_app) -> None:
    viewport, _p, _pk, actors, _doc = _make_viewport()
    _pick_front(viewport, actors)
    viewport.eventFilter(
        viewport.interactor, _mouse(QEvent.Type.MouseButtonPress, (40.0, 40.0))
    )
    viewport.eventFilter(
        viewport.interactor,
        _mouse(QEvent.Type.MouseMove, (80.0, 80.0)),
    )
    assert viewport._marquee_active
    assert not viewport.pick_candidates_active


def test_armed_gizmo_suppresses_the_chooser(_app) -> None:
    viewport, _p, _pk, actors, _doc = _make_viewport()
    _pick_front(viewport, actors)
    assert viewport.pick_candidates_active

    # RoomEntityTransformController._arm does exactly this pairing.
    viewport.pick_popover_enabled = False
    viewport.dismiss_pick_candidates()
    assert not viewport.pick_candidates_active

    viewport._picked_actor(actors[0])
    assert not viewport.pick_candidates_active
    assert viewport.pick_candidate_ids() == ()


def test_no_scene_repicks_through_chooser_lifecycle(_app) -> None:
    # #867 bench contract: opening, cycling, confirming and dismissing the
    # chooser never performs an additional picker.Pick — the picker's own
    # Prop3D stack is the candidate list.
    viewport, _p, picker, actors, _doc = _make_viewport()
    _pick_front(viewport, actors)
    _send_key(viewport, Qt.Key.Key_Down)
    _send_key(viewport, Qt.Key.Key_Up)
    _send_wheel(viewport, -120)
    _send_key(viewport, Qt.Key.Key_Escape)
    _pick_front(viewport, actors)
    _send_key(viewport, Qt.Key.Key_Return)
    assert picker.pick_args == []


def test_repeat_click_cycling_still_works_with_chooser(_app) -> None:
    # The classic input-free click-through path is preserved — clicks at
    # the same spot still cycle; the chooser just follows the cycle index.
    viewport, _p, _pk, actors, _doc = _make_viewport()
    picked: list[str] = []
    viewport.entityPicked.connect(lambda eid, _pos: picked.append(eid))
    for expected in ("e1", "e2", "e3", "e1"):
        viewport._picked_actor(actors[0])
        assert picked[-1] == expected
    assert viewport._pick_popover.header.text() == "重なり候補 1/3"
