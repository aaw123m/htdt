"""Round-9 viewport regression tests (# R9 audit).

Covers: per-frame render amplification (every scene mutation must funnel into
one draw), the Qt<->VTK pick-coordinate convention, marquee box selection,
click-through pick cycling, hidden-entity label leakage, video-overlay
staleness, and field-explorer slice orientation/scale honesty.

Everything runs against recording fakes or the offscreen Qt platform — no GL
context is required.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from htdt.cad_scene import (
    Position3,
    Quaternion4,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    scene_content_hash,
)
from htdt.cad_spatial_field import FieldPlaneRequest, FieldSliceView
from htdt.room_viewport import RoomOverlayState, RoomViewport3D
from htdt.field_explorer_panel import _slice_pixmap, _slice_stats


@pytest.fixture(scope="session", autouse=True)
def _app() -> QApplication:
    return QApplication.instance() or QApplication(["pytest"])


class _FakeActor:
    def __init__(self, name: str, bounds=None) -> None:
        self.name = name
        self._bounds = bounds or (0.0, 1.0, -1.0, 0.0, 0.0, 1.0)

    def GetBounds(self):  # noqa: N802 - vtk shape
        return self._bounds


class _FakeCamera:
    """Camera double holding position/focal/view-up like vtkCamera."""

    def __init__(self) -> None:
        self.position = (0.0, 0.0, 0.0)
        self.focal_point = (0.0, 0.0, 0.0)
        self.view_up = (0.0, 0.0, 1.0)

    def GetPosition(self):  # noqa: N802 - vtk shape
        return self.position

    def SetPosition(self, *v):  # noqa: N802 - vtk shape
        self.position = tuple(float(x) for x in v)

    def GetFocalPoint(self):  # noqa: N802
        return self.focal_point

    def SetFocalPoint(self, *v):  # noqa: N802
        self.focal_point = tuple(float(x) for x in v)

    def GetViewUp(self):  # noqa: N802
        return self.view_up

    def SetViewUp(self, *v):  # noqa: N802
        self.view_up = tuple(float(x) for x in v)


class _RecordingPlotter:
    """Plotter double recording render calls and every add_* kwarg set."""

    def __init__(self) -> None:
        self.renders = 0
        self.add_calls: list[tuple[str, dict]] = []
        self.point_labels: list[list[str]] = []
        self.camera = _FakeCamera()
        # pyvista's Plotter.render() early-outs while this is True; the fake
        # mirrors that so suppression regressions surface as extra renders.
        self.suppress_rendering = False

    def clear(self) -> None:
        pass

    def set_background(self, *args, **kwargs) -> None:
        pass

    def add_mesh(self, mesh=None, **kwargs):
        self.add_calls.append(("add_mesh", kwargs))
        return _FakeActor(str(kwargs.get("name", "")))

    def add_point_labels(self, points, labels, **kwargs):
        self.add_calls.append(("add_point_labels", kwargs))
        self.point_labels.append(list(labels))
        return _FakeActor("labels")

    def add_legend(self, *args, **kwargs):
        self.add_calls.append(("add_legend", kwargs))
        # pyvista's add_legend has no render kwarg and renders internally —
        # honour suppress_rendering like Plotter.render() does.
        if not self.suppress_rendering:
            self.render()

    def add_axes(self, *args, **kwargs):
        self.add_calls.append(("add_axes", kwargs))

    def add_text(self, *args, **kwargs):
        self.add_calls.append(("add_text", kwargs))

    def remove_actor(self, *args, **kwargs):
        self.add_calls.append(("remove_actor", kwargs))

    def render(self) -> None:
        self.renders += 1


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
        self.interactor = None
        self._renderer = object()

    def get_poked_renderer(self):
        return self._renderer

    def remove_observer(self, *args) -> None:
        pass

    def __getattr__(self, name):
        # Teardown hooks (disable_picking et al.) call whatever the real
        # RenderWindowInteractor exposes; any missing method is a no-op.
        return lambda *args, **kwargs: None


def _document(*entities: SceneEntity) -> SceneDocument:
    return SceneDocument(
        document_id="doc-r9",
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=tuple(entities),
    )


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


def _viewport(_app) -> RoomViewport3D:
    return RoomViewport3D()


def _mouse(
    event_type: QEvent.Type,
    position: tuple[float, float],
    *,
    modifiers=Qt.KeyboardModifier.NoModifier,
    button=Qt.MouseButton.LeftButton,
    buttons=Qt.MouseButton.LeftButton,
) -> QMouseEvent:
    local = QPointF(position[0], position[1])
    return QMouseEvent(event_type, local, local, button, buttons, modifiers)


def test_scene_render_is_a_single_draw_not_one_per_actor(_app) -> None:
    # A mid-size scene must produce exactly one plotter.render() — previously
    # every add_mesh ran a full GL pass, so the same refresh cost ~N draws.
    viewport = _viewport(_app)
    plotter = _RecordingPlotter()
    viewport.plotter = plotter
    viewport.render_document(
        _document(
            _entity("e1", "speaker", position=(1.0, 0.5, 1.0)),
            _entity("e2", "seat", position=(3.0, 2.0, 0.0)),
            _entity("e3", "screen", position=(3.0, 3.9, 1.2)),
        ),
        selected_id="e1",
        selected_ids=("e1", "e2"),
        overlays=RoomOverlayState(grid=True, labels=True, acoustics=True),
        reset_camera=False,
    )
    assert plotter.renders == 1
    assert plotter.add_calls, "expected scene actors to be recorded"
    assert all(
        kwargs.get("render") is False
        for call, kwargs in plotter.add_calls
        if call in {"add_mesh", "add_point_labels", "add_text"}
    )


def test_composite_overlay_helpers_end_in_one_render(_app) -> None:
    # Standalone callers rely on the helper itself rendering; stacked callers
    # rely on deferred_render. Either way each helper must draw exactly once.
    viewport = _viewport(_app)
    plotter = _RecordingPlotter()
    viewport.plotter = plotter
    document = _document(_entity("e1"))
    viewport.render_document(
        document,
        selected_id=None,
        overlays=RoomOverlayState(grid=False),
        reset_camera=False,
    )
    plotter.renders = 0
    plotter.add_calls.clear()

    viewport.render_measure_overlay(
        SimpleNamespace(
            endpoints=[
                SimpleNamespace(position=Position3(x_m=1.0, y_m=1.0, z_m=0.0)),
                SimpleNamespace(position=Position3(x_m=2.0, y_m=2.0, z_m=0.0)),
            ],
            mode="distance",
        ),
    )
    assert plotter.renders == 1

    viewport.render_constraint_overlay(
        SimpleNamespace(constraints=()), None
    )
    assert plotter.renders == 2

    evaluation = SimpleNamespace(
        target=SimpleNamespace(
            scene_content_hash=scene_content_hash(document)
        ),
        projection=None,
        surface=None,
        sightlines=(),
        collisions=(),
        request=SimpleNamespace(seats=()),
    )
    viewport.render_video_overlay(evaluation)
    assert plotter.renders == 3

    viewport.render_search_domain("e1", ())
    assert plotter.renders == 4

    assert all(
        kwargs.get("render") is False
        for call, kwargs in plotter.add_calls
        if call in {"add_mesh", "add_point_labels", "add_text"}
    )


def test_video_overlay_skips_stale_scene_evaluation(_app) -> None:
    viewport = _viewport(_app)
    plotter = _RecordingPlotter()
    viewport.plotter = plotter
    document = _document(_entity("e1"))
    viewport.render_document(
        document,
        selected_id=None,
        overlays=RoomOverlayState(grid=False),
        reset_camera=False,
    )
    plotter.add_calls.clear()
    plotter.renders = 0

    projection = SimpleNamespace(
        lens_position=Position3(x_m=1.0, y_m=2.0, z_m=2.2),
        image_plane_corners=[
            Position3(x_m=1.0, y_m=0.0, z_m=1.0),
            Position3(x_m=2.0, y_m=0.0, z_m=1.0),
            Position3(x_m=2.0, y_m=0.0, z_m=2.0),
            Position3(x_m=1.0, y_m=0.0, z_m=2.0),
        ],
        status="PASS",
        optical_axis_intersection=None,
        screen_image_center=Position3(x_m=1.5, y_m=0.0, z_m=1.5),
    )
    stale = SimpleNamespace(
        target=SimpleNamespace(scene_content_hash="0" * 64),
        projection=projection,
        surface=None,
        sightlines=(),
        collisions=(),
        request=SimpleNamespace(seats=()),
    )
    viewport.render_video_overlay(stale)
    assert plotter.add_calls == []
    assert plotter.renders == 0

    current = SimpleNamespace(
        target=SimpleNamespace(
            scene_content_hash=scene_content_hash(document)
        ),
        projection=projection,
        surface=None,
        sightlines=(),
        collisions=(),
        request=SimpleNamespace(seats=()),
    )
    viewport.render_video_overlay(current)
    assert any(
        name.startswith("video-cone")
        for name in (kwargs.get("name", "") for _c, kwargs in plotter.add_calls)
    )


def test_labels_do_not_leak_for_hidden_entities(_app) -> None:
    viewport = _viewport(_app)
    plotter = _RecordingPlotter()
    viewport.plotter = plotter
    viewport.render_document(
        _document(
            _entity("shown", "seat", name="Shown seat"),
            _entity("gone", "speaker", name="Ghost speaker"),
        ),
        selected_id=None,
        hidden_ids={"gone"},
        overlays=RoomOverlayState(grid=False, labels=True),
        reset_camera=False,
    )
    assert plotter.point_labels == [["Shown seat"]]


def test_pick_entry_points_share_one_coordinate_convention(_app) -> None:
    # Public pick APIs take Qt widget coords (top-left origin, DIP); VTK
    # display coords are produced only at the picker boundary.
    viewport = _viewport(_app)
    viewport.interactor.resize(800, 600)
    picker = _FakePicker()
    viewport.plotter.iren = _FakeIren(picker)

    dpr = float(viewport.interactor.devicePixelRatioF()) or 1.0
    height = float(viewport.interactor.height())
    position = QPointF(120.0, 50.0)

    viewport.pick_actor_at(position)
    expected = (120.0 * dpr, (height - 1.0 - 50.0) * dpr, 0.0)
    assert picker.pick_args[-1] == pytest.approx(expected)

    world = viewport.pick_world_position(position)
    assert world == (1.0, 2.0, 3.0)
    assert picker.pick_args[-1] == pytest.approx(expected)

    # The pick-event position the signals emit is the same convention —
    # entityPicked and emptyClicked must agree so measure/snap consumers see
    # one space (previously entityPicked carried the mirrored VTK y).
    viewport.interactor.GetEventPosition = lambda: (200.0 * dpr, 10.0 * dpr)
    emitted = viewport._last_display_position()
    assert emitted is not None
    assert emitted.x() == pytest.approx(200.0)
    assert emitted.y() == pytest.approx(height - 1.0 - 10.0)

    # Round-trip identity through the widget<->display conversion.
    back = viewport._widget_to_display_position(
        viewport._display_to_widget_position((300.0 * dpr, 40.0 * dpr))
    )
    assert back is not None
    assert back == pytest.approx((300.0 * dpr, 40.0 * dpr))


def test_marquee_selects_entities_intersecting_projected_bounds(_app) -> None:
    viewport = _viewport(_app)
    viewport.resize(800, 600)
    viewport.interactor.resize(800, 600)
    plotter = _RecordingPlotter()
    viewport.plotter = plotter
    inside = _FakeActor("entity-e1", bounds=(0.0, 1.0, -1.0, 0.0, 0.0, 1.0))
    # #955: the candidates read is now a typed boundary — the double must
    # expose the pick collection like the real QtInteractor does.
    viewport.plotter.iren = _FakeIren(_FakePicker(props=(inside,)))
    outside = _FakeActor("entity-e2", bounds=(50.0, 51.0, -51.0, -50.0, 0.0, 1.0))
    # Bypass the scene build: register actors directly.
    viewport._marquee_actors = [("e1", inside), ("e1", inside), ("e2", outside)]
    viewport._actor_entity_ids[id(inside)] = "e1"
    viewport._actor_entity_ids[id(outside)] = "e2"

    # Identity world->screen projection in VTK display space for this test.
    dpr = float(viewport.interactor.devicePixelRatioF()) or 1.0
    height = float(viewport.interactor.height())
    viewport.world_to_screen = lambda p: (p[0] * 10.0 + 50.0, p[2] * 10.0 + 100.0)

    from PySide6.QtCore import QRectF

    # inside: sx in [50,60], sy in [100,110] -> Qt rect y maps to
    # y_vtk = (h-1-y_qt)*dpr, so qt y in [h-1-110, h-1-100].
    assert viewport.interactor.height() == pytest.approx(height)
    qt_top = (height - 1.0 - 110.0) / dpr
    qt_bottom = (height - 1.0 - 100.0) / dpr
    assert height > 100.0
    rect = QRectF(50.0, qt_top, 20.0, qt_bottom - qt_top)
    assert viewport.pick_entities_in_region(rect) == ["e1"]

    released: list[tuple[list[str], bool]] = []
    viewport.entitiesMarqueeSelected.connect(
        lambda ids, additive: released.append((list(ids), additive))
    )

    # Drive the full gesture: press, drag past threshold, release with Shift.
    viewport.eventFilter(
        viewport.interactor,
        _mouse(QEvent.Type.MouseButtonPress, (50.0, qt_top)),
    )
    viewport.eventFilter(
        viewport.interactor,
        _mouse(
            QEvent.Type.MouseMove,
            (70.0, qt_bottom),
            buttons=Qt.MouseButton.LeftButton,
        ),
    )
    viewport.eventFilter(
        viewport.interactor,
        _mouse(
            QEvent.Type.MouseButtonRelease,
            (70.0, qt_bottom),
            modifiers=Qt.KeyboardModifier.ShiftModifier,
        ),
    )
    assert released == [(["e1"], True)]
    # The vtk release pick that follows must be swallowed once.
    assert viewport._suppress_next_pick is True
    viewport._picked_actor(inside)
    assert viewport._suppress_next_pick is False

    picked: list[str] = []
    viewport.entityPicked.connect(lambda eid, _pos: picked.append(eid))
    viewport._picked_actor(inside)
    assert picked == ["e1"]


def test_click_through_cycles_front_to_back_candidates(_app) -> None:
    viewport = _viewport(_app)
    viewport.interactor.resize(800, 600)
    front, mid, rear = _FakeActor("e1"), _FakeActor("e2"), _FakeActor("e3")
    for actor, entity_id in ((front, "e1"), (mid, "e2"), (rear, "e3")):
        viewport._actor_entity_ids[id(actor)] = entity_id
    picker = _FakePicker(actor=front, props=(front, mid, rear))
    viewport.plotter.iren = _FakeIren(picker)
    viewport.interactor.GetEventPosition = lambda: (40.0, 559.0)

    picked: list[str] = []
    viewport.entityPicked.connect(lambda eid, _pos: picked.append(eid))
    for expected in ("e1", "e2", "e3", "e1"):
        viewport._picked_actor(front)
        assert picked[-1] == expected

    # Moving the click point resets the cycle to the picked actor's slot.
    viewport.interactor.GetEventPosition = lambda: (80.0, 559.0)
    viewport._picked_actor(front)
    assert picked[-1] == "e1"
    # The pick callback must read the completed pick's prop list — a nested
    # picker.Pick inside the callback fires EndPickEvent and recurses.
    assert picker.pick_args == []


def test_cycle_state_survives_scene_rebuilds(_app) -> None:
    # Every click -> selection -> workspace re-render -> render_document.
    # If the rebuild wiped the cycle state, repeat clicks would always land
    # on the front entity and click-through could never engage.
    viewport = _viewport(_app)
    viewport.interactor.resize(800, 600)
    plotter = _RecordingPlotter()
    viewport.plotter = plotter
    front, back = _FakeActor("entity-e1"), _FakeActor("entity-e2")
    viewport._actor_entity_ids[id(front)] = "e1"
    viewport._actor_entity_ids[id(back)] = "e2"
    picker = _FakePicker(actor=front, props=(front, back))
    plotter.iren = _FakeIren(picker)
    viewport.interactor.GetEventPosition = lambda: (40.0, 559.0)

    picked: list[str] = []
    viewport.entityPicked.connect(lambda eid, _pos: picked.append(eid))
    viewport._picked_actor(front)
    assert picked[-1] == "e1"
    # The selection upstream triggers a scene rebuild — simulate it.
    viewport.render_document(
        _document(_entity("e1"), _entity("e2")),
        selected_id="e1",
        overlays=RoomOverlayState(grid=False),
        reset_camera=False,
    )
    # Re-register: the rebuild replaced actors with fresh objects.
    front2, back2 = _FakeActor("entity-e1"), _FakeActor("entity-e2")
    viewport._actor_entity_ids[id(front2)] = "e1"
    viewport._actor_entity_ids[id(back2)] = "e2"
    picker._props = (front2, back2)
    viewport._picked_actor(front2)
    assert picked == ["e1", "e2"]


class _EventedPicker(_FakePicker):
    """Picker double whose Pick() fires the pick callback — as vtkPicker
    fires EndPickEvent on every Pick, including nested ones."""

    def Pick(self, x: float, y: float, z: float, renderer) -> int:
        self.pick_args.append((x, y, z))
        viewport = getattr(self, "_viewport", None)
        if self._actor is not None and viewport is not None:
            viewport._picked_actor(self._actor)
        return 1


def test_pick_dispatch_is_reentry_safe(_app) -> None:
    # EndPickEvent fires on every picker.Pick; a nested pick issued while a
    # pick is being dispatched (consumer re-picking, or the callback itself)
    # must be dropped, not recursed into.
    viewport = _viewport(_app)
    viewport.interactor.resize(800, 600)
    actor = _FakeActor("entity-e1")
    viewport._actor_entity_ids[id(actor)] = "e1"
    picker = _EventedPicker(actor=actor, props=(actor,))
    picker._viewport = viewport
    viewport.plotter.iren = _FakeIren(picker)
    viewport.interactor.GetEventPosition = lambda: (40.0, 559.0)

    emitted: list[str] = []
    viewport.entityPicked.connect(lambda eid, _pos: emitted.append(eid))
    viewport.pick_actor_at(QPointF(40.0, 40.0))
    # One outer pick, one dispatch, one emission — no recursion, and the
    # dispatch consumed the completed pick's props instead of re-picking.
    assert emitted == ["e1"]
    assert len(picker.pick_args) == 1


def test_marquee_drag_does_not_rotate_camera(_app) -> None:
    # The trackball style rotates on left-drag; once the marquee owns the
    # gesture the press-time camera pose is restored and further moves are
    # consumed before VTK can orbit mid-box-select.
    viewport = _viewport(_app)
    viewport.interactor.resize(800, 600)
    plotter = _RecordingPlotter()
    viewport.plotter = plotter
    camera = plotter.camera

    press = _mouse(QEvent.Type.MouseButtonPress, (50.0, 50.0))
    assert viewport.eventFilter(viewport.interactor, press) is False
    # Sub-threshold moves still pass through — a plain drag under the click
    # slop keeps orbiting.
    small = _mouse(QEvent.Type.MouseMove, (52.0, 52.0))
    assert viewport.eventFilter(viewport.interactor, small) is False
    # The style already orbited during the slop — simulate that drift.
    camera.SetPosition(9.0, 9.0, 9.0)
    camera.SetViewUp(0.5, 0.5, 0.5)
    big = _mouse(QEvent.Type.MouseMove, (140.0, 140.0))
    assert viewport.eventFilter(viewport.interactor, big) is True
    assert viewport._marquee_active is True
    assert tuple(camera.GetPosition()) == pytest.approx((0.0, 0.0, 0.0))
    assert tuple(camera.GetViewUp()) == pytest.approx((0.0, 0.0, 1.0))
    # Mid-marquee moves are consumed, not delivered to the trackball.
    further = _mouse(QEvent.Type.MouseMove, (160.0, 160.0))
    assert viewport.eventFilter(viewport.interactor, further) is True
    release = _mouse(QEvent.Type.MouseButtonRelease, (160.0, 160.0))
    assert viewport.eventFilter(viewport.interactor, release) is False
    assert viewport._press_camera_state is None


def test_idle_render_timer_is_off(_app) -> None:
    # auto_update=False must leave pyvistaqt's 200ms auto-render timer absent
    # or inactive — every mutation path renders explicitly.
    viewport = _viewport(_app)
    timer = getattr(viewport.plotter, "render_timer", None)
    assert timer is None or not timer.isActive()


def _view(axis_plane: str, rows, *, masked=()) -> FieldSliceView:
    axes = {
        "xy": ("x_m", "y_m", "z_m"),
        "xz": ("x_m", "z_m", "y_m"),
        "yz": ("y_m", "z_m", "x_m"),
    }[axis_plane]
    row_axis, col_axis, _fixed = axes
    return FieldSliceView(
        result_semantic_sha256="a" * 64,
        plane=FieldPlaneRequest(axis_plane=axis_plane, coordinate_m=0.0),
        quantity="spl_db",
        unit="dB SPL",
        rows=tuple(tuple(row) for row in rows),
        row_axis=row_axis,
        column_axis=col_axis,
        row_coordinates_m=tuple(0.5 * i for i in range(len(rows))),
        column_coordinates_m=tuple(0.5 * i for i in range(len(rows[0]))),
        sample_state="exact",
        masked_positions=tuple(masked),
        cache_key_sha256="b" * 64,
    )


def test_field_slice_vertical_planes_draw_elevation_up(_app) -> None:
    # xz view: rows index x_m (screen right), cols index z_m (screen up).
    # The z-highest sample must land on the top row of the image.
    view = _view(
        "xz",
        [
            [0.0, 100.0],   # x0: z0=low value, z1=max value
            [50.0, 80.0],   # x1
        ],
    )
    pixmap = _slice_pixmap(view).toImage()
    top_left = pixmap.pixelColor(0, 0)
    bottom_left = pixmap.pixelColor(0, pixmap.height() - 1)
    # max value -> red end of ramp; min value -> blue end.
    assert top_left.red() > top_left.blue()
    assert bottom_left.blue() > bottom_left.red()


def test_field_slice_plan_view_x_right_y_down(_app) -> None:
    view = _view(
        "xy",
        [
            [0.0, 50.0],   # x0: y0=min, y1=mid
            [50.0, 100.0],  # x1: y0=mid, y1=max
        ],
    )
    pixmap = _slice_pixmap(view).toImage()
    # Max value at (x1,y1) -> bottom-right in screen space (y grows downward).
    bottom_right = pixmap.pixelColor(pixmap.width() - 1, pixmap.height() - 1)
    top_left = pixmap.pixelColor(0, 0)
    assert bottom_right.red() > bottom_right.blue()
    assert top_left.blue() > top_left.red()


def test_field_slice_stats_report_range_and_masked(_app) -> None:
    view = _view("xy", [[1.0, 2.0], [3.0, float("nan")]], masked=((0, 1),))
    lo, hi, hidden = _slice_stats(view)
    assert (lo, hi) == (1.0, 3.0)
    assert hidden == 2
