"""3D gizmo math (translation/rotation widgets) — round 2 coverage.

The widget classes need a live VTK interactor, but the geometry helpers are
pure math and carry real correctness risk: a wrong rotation axis convention
or segment-distance clamp shows up as mis-transformed actors in the CAD
viewport.
"""

from __future__ import annotations

import numpy as np
import pytest

from htdt.cad_gizmo import (
    RotationWidget3D,
    _device_pixel_ratio,
    _distance_to_segment_2d,
    _event_inside_renderer,
    _grab_mouse,
    _release_mouse,
)


# --- _distance_to_segment_2d ----------------------------------------------------

def test_distance_to_segment_interior_point() -> None:
    segment_start = np.array([0.0, 0.0])
    segment_end = np.array([4.0, 0.0])
    point = np.array([2.0, 3.0])
    assert _distance_to_segment_2d(point, segment_start, segment_end) == pytest.approx(3.0)


def test_distance_to_segment_clamps_to_endpoints() -> None:
    start = np.array([0.0, 0.0])
    end = np.array([4.0, 0.0])
    # Beyond the end → distance to the endpoint, not the line.
    assert _distance_to_segment_2d(np.array([7.0, 0.0]), start, end) == pytest.approx(3.0)
    # Before the start → distance to the start.
    assert _distance_to_segment_2d(np.array([-2.0, 0.0]), start, end) == pytest.approx(2.0)


def test_distance_to_segment_degenerate_is_point_distance() -> None:
    point_on_itself = np.array([1.0, 1.0])
    assert _distance_to_segment_2d(
        np.array([4.0, 5.0]), point_on_itself, point_on_itself
    ) == pytest.approx(5.0)


def test_distance_to_segment_on_segment_is_zero() -> None:
    assert _distance_to_segment_2d(
        np.array([2.0, 0.0]), np.array([0.0, 0.0]), np.array([4.0, 0.0])
    ) == pytest.approx(0.0)


# --- RotationWidget3D._rotation_about -------------------------------------------

def test_rotation_about_z_axis_right_hand_rule() -> None:
    origin = np.array([0.0, 0.0, 0.0])
    axis = np.array([0.0, 0.0, 1.0])
    matrix = RotationWidget3D._rotation_about(origin, axis, 90.0)
    rotated = matrix @ np.array([1.0, 0.0, 0.0, 1.0])
    np.testing.assert_allclose(rotated[:3], (0.0, 1.0, 0.0), atol=1e-12)


def test_rotation_about_x_axis_180_flips_y() -> None:
    matrix = RotationWidget3D._rotation_about(
        np.zeros(3), np.array([1.0, 0.0, 0.0]), 180.0
    )
    rotated = matrix @ np.array([0.0, 1.0, 0.0, 1.0])
    np.testing.assert_allclose(rotated[:3], (0.0, -1.0, 0.0), atol=1e-12)


def test_rotation_axis_is_normalized_internally() -> None:
    # A non-unit axis must not scale the rotation.
    matrix = RotationWidget3D._rotation_about(
        np.zeros(3), np.array([0.0, 0.0, 7.0]), 90.0
    )
    rotated = matrix @ np.array([1.0, 0.0, 0.0, 1.0])
    np.testing.assert_allclose(rotated[:3], (0.0, 1.0, 0.0), atol=1e-12)


def test_rotation_about_off_center_pivot_keeps_pivot_fixed() -> None:
    origin = np.array([5.0, 3.0, 2.0])
    matrix = RotationWidget3D._rotation_about(origin, np.array([0.0, 0.0, 1.0]), 137.0)
    rotated_pivot = matrix @ np.append(origin, 1.0)
    np.testing.assert_allclose(rotated_pivot[:3], origin, atol=1e-12)


def test_rotation_zero_angle_is_identity() -> None:
    matrix = RotationWidget3D._rotation_about(
        np.array([1.0, 2.0, 3.0]), np.array([0.3, -0.4, 0.5]), 0.0
    )
    np.testing.assert_allclose(matrix, np.eye(4), atol=1e-12)


def test_rotation_preserves_distance_from_axis() -> None:
    origin = np.array([1.0, 1.0, 0.0])
    axis = np.array([0.0, 0.0, 1.0])
    point = np.array([3.0, 1.0, 4.0])
    matrix = RotationWidget3D._rotation_about(origin, axis, 63.0)
    rotated = (matrix @ np.append(point, 1.0))[:3]
    # Distance to the axis line is invariant under rotation about it.
    radial_before = np.linalg.norm(point[:2] - origin[:2])
    radial_after = np.linalg.norm(rotated[:2] - origin[:2])
    assert radial_after == pytest.approx(radial_before)
    assert rotated[2] == pytest.approx(point[2])


# --- Interactor-facing helpers (stubbed) ----------------------------------------


class _FakeInteractor:
    def __init__(self, x: float, y: float) -> None:
        self._pos = (x, y)
        self.grabbed = False
        self.released = False

    def GetEventPosition(self):
        return self._pos

    def grabMouse(self):
        self.grabbed = True

    def releaseMouse(self):
        self.released = True


class _FakeRenderer:
    def __init__(self, origin=(0, 0), size=(100, 100)) -> None:
        self._origin = origin
        self._size = size

    def GetOrigin(self):
        return self._origin

    def GetSize(self):
        return self._size


class _FakeIren:
    def __init__(self, renderer) -> None:
        self._renderer = renderer

    def get_poked_renderer(self):
        return self._renderer


class _FakePlotter:
    def __init__(self, renderer, interactor=None) -> None:
        self.iren = _FakeIren(renderer)
        self.interactor = interactor


def test_event_inside_renderer_bounds() -> None:
    plotter = _FakePlotter(_FakeRenderer(origin=(10, 20), size=(100, 50)))
    assert _event_inside_renderer(plotter, _FakeInteractor(10, 20))
    assert _event_inside_renderer(plotter, _FakeInteractor(109, 69))
    # Edges are exclusive on the far side.
    assert not _event_inside_renderer(plotter, _FakeInteractor(110, 70))
    assert not _event_inside_renderer(plotter, _FakeInteractor(9, 30))


def test_event_inside_renderer_none_renderer_is_outside() -> None:
    plotter = _FakePlotter(None)
    assert not _event_inside_renderer(plotter, _FakeInteractor(0, 0))


def test_device_pixel_ratio_defaults_and_reads() -> None:
    class _Dpi:
        def __init__(self, ratio):
            self._ratio = ratio

        def devicePixelRatioF(self):
            return self._ratio

    plotter = _FakePlotter(None, interactor=_Dpi(2.5))
    assert _device_pixel_ratio(plotter) == pytest.approx(2.5)
    # Missing interactor → safe 1.0.
    assert _device_pixel_ratio(_FakePlotter(None)) == pytest.approx(1.0)
    # A bogus ratio must never divide-by-zero downstream.
    assert _device_pixel_ratio(_FakePlotter(None, _Dpi(0.0))) == pytest.approx(1.0)


def test_grab_release_mouse_are_capability_guarded() -> None:
    interactor = _FakeInteractor(0, 0)
    plotter = _FakePlotter(None, interactor=interactor)
    _grab_mouse(plotter)
    assert interactor.grabbed
    _release_mouse(plotter)
    assert interactor.released
    # A plotter without an interactor must not raise.
    _grab_mouse(_FakePlotter(None))
    _release_mouse(_FakePlotter(None))
