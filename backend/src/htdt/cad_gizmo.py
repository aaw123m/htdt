from __future__ import annotations

from collections.abc import Callable
from math import atan2, degrees

import numpy as np
import pyvista as pv
from vtkmodules.vtkRenderingCore import vtkPropPicker


MatrixCallback = Callable[[np.ndarray], None]
RotationCallback = Callable[[int, float], None]
CancelCallback = Callable[[], None]


def _grab_mouse(plotter: pv.Plotter) -> None:
    widget = getattr(plotter, 'interactor', None)
    if widget is not None and hasattr(widget, 'grabMouse'):
        widget.grabMouse()


def _release_mouse(plotter: pv.Plotter) -> None:
    widget = getattr(plotter, 'interactor', None)
    if widget is not None and hasattr(widget, 'releaseMouse'):
        widget.releaseMouse()


def _event_inside_renderer(plotter: pv.Plotter, interactor) -> bool:
    x, y = interactor.GetEventPosition()
    renderer = plotter.iren.get_poked_renderer()
    if renderer is None:
        return False
    ox, oy = renderer.GetOrigin()
    width, height = renderer.GetSize()
    return ox <= x < ox + width and oy <= y < oy + height


def _device_pixel_ratio(plotter: pv.Plotter) -> float:
    widget = getattr(plotter, 'interactor', None)
    if widget is not None and hasattr(widget, 'devicePixelRatioF'):
        return max(float(widget.devicePixelRatioF()), 1.0)
    return 1.0


def _world_to_display(renderer, point: np.ndarray) -> np.ndarray:
    renderer.SetWorldPoint(float(point[0]), float(point[1]), float(point[2]), 1.0)
    renderer.WorldToDisplay()
    x, y, _ = renderer.GetDisplayPoint()
    return np.array((float(x), float(y)), dtype=float)


def _distance_to_segment_2d(point: np.ndarray, start: np.ndarray, end: np.ndarray) -> float:
    segment = end - start
    length_sq = float(np.dot(segment, segment))
    if length_sq <= 1e-12:
        return float(np.linalg.norm(point - start))
    fraction = float(np.clip(np.dot(point - start, segment) / length_sq, 0.0, 1.0))
    closest = start + segment * fraction
    return float(np.linalg.norm(point - closest))


class TranslationWidget3D:
    """Three-axis translation-only widget for HTDT's physical domain axes."""

    def __init__(
        self,
        plotter: pv.Plotter,
        actor: pv.Actor,
        *,
        interact_callback: MatrixCallback | None = None,
        release_callback: MatrixCallback | None = None,
        cancel_callback: CancelCallback | None = None,
        origin: tuple[float, float, float] | np.ndarray | None = None,
    ) -> None:
        self.plotter = plotter
        self.actor = actor
        self.interact_callback = interact_callback
        self.release_callback = release_callback
        self.cancel_callback = cancel_callback
        # Polar vectors use C=diag(1,-1,1): domain +Y is render -Y.
        self.axes = np.array(((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, 1.0)))
        self.origin = np.asarray(actor.center if origin is None else origin, dtype=float)
        self.actor_length = max(float(actor.GetLength()), 0.25)
        self.handles: list[pv.Actor] = []
        self.selected: pv.Actor | None = None
        self.initial_world: np.ndarray | None = None
        self.pressing = False
        self.matrix = np.eye(4)
        self.observers: list[int] = []
        # Kept for diagnostic/acceptance probes. Runtime hit-testing uses a
        # DPI-aware screen-space distance so the visible arrow need not be widened.
        self.handle_picker = vtkPropPicker()
        self.hit_tolerance_dip = 12.0
        colors = (
            pv.global_theme.axes.x_color,
            pv.global_theme.axes.y_color,
            pv.global_theme.axes.z_color,
        )
        for axis, color in zip(self.axes, colors, strict=True):
            handle = plotter.add_mesh(
                pv.Arrow(start=self.origin, direction=axis, scale=self.actor_length * 0.75),
                color=color,
                lighting=False,
                pickable=True,
                render=False,
            )
            handle.mapper.SetResolveCoincidentTopologyToPolygonOffset()
            handle.mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(0, -20000)
            self.handles.append(handle)
        self.observers.extend(
            (
                plotter.iren.add_observer('MouseMoveEvent', self._move),
                plotter.iren.add_observer('LeftButtonPressEvent', self._press, interactor_style_fallback=False),
                plotter.iren.add_observer('LeftButtonReleaseEvent', self._release, interactor_style_fallback=False),
            )
        )

    @property
    def active_axis_index(self) -> int | None:
        return self.handles.index(self.selected) if self.selected in self.handles else None

    def set_display_delta(self, delta_xyz: tuple[float, float, float]) -> None:
        self.matrix = np.eye(4)
        self.matrix[:3, 3] = np.asarray(delta_xyz, dtype=float)
        self.actor.user_matrix = self.matrix

    def _world_for_translation(self, interactor) -> np.ndarray:
        x, y = interactor.GetEventPosition()
        renderer = self.plotter.iren.get_poked_renderer()
        width, height = renderer.GetSize()
        ndc = np.array((2 * x / width - 1, 2 * y / height - 1, 1.0, 1.0))
        camera = renderer.GetActiveCamera()
        projection = pv.array_from_vtkmatrix(
            camera.GetProjectionTransformMatrix(renderer.GetTiledAspectRatio(), 0, 1)
        )
        camera_coords = np.linalg.inv(projection) @ ndc
        modelview = pv.array_from_vtkmatrix(camera.GetModelViewTransformMatrix())
        world = np.linalg.inv(modelview) @ camera_coords
        return world[:3] * self.actor_length * 2

    def hit_test_display(self, x: float, y: float, renderer) -> bool:
        return self._pick_handle_at(float(x), float(y), renderer) is not None

    def _pick_handle_at(self, x: float, y: float, renderer) -> pv.Actor | None:
        if renderer is None:
            return None
        cursor = np.array((float(x), float(y)), dtype=float)
        tolerance_px = self.hit_tolerance_dip * _device_pixel_ratio(self.plotter)
        best_index: int | None = None
        best_distance = float('inf')
        arrow_length = self.actor_length * 0.75
        for index, axis in enumerate(self.axes):
            start = _world_to_display(renderer, self.origin + axis * arrow_length * 0.12)
            end = _world_to_display(renderer, self.origin + axis * arrow_length)
            distance = _distance_to_segment_2d(cursor, start, end)
            if distance <= tolerance_px and distance < best_distance:
                best_distance = distance
                best_index = index
        return None if best_index is None else self.handles[best_index]

    def _pick_handle(self, interactor) -> pv.Actor | None:
        renderer = self.plotter.iren.get_poked_renderer()
        if renderer is None:
            return None
        x, y = interactor.GetEventPosition()
        return self._pick_handle_at(float(x), float(y), renderer)

    def _move(self, interactor, _event) -> None:
        if self.pressing and self.selected is not None and self.initial_world is not None:
            current = self._world_for_translation(interactor)
            axis = self.axes[self.handles.index(self.selected)]
            delta = axis * float(np.dot(current - self.initial_world, axis))
            self.set_display_delta(tuple(float(value) for value in delta))
            if self.interact_callback:
                self.interact_callback(self.matrix.copy())
            self.plotter.render()
            return
        self.selected = self._pick_handle(interactor)

    def _press(self, interactor, _event) -> None:
        self.selected = self._pick_handle(interactor)
        if self.selected is None:
            return
        self.plotter.enable_trackball_actor_style()
        self.initial_world = self._world_for_translation(interactor)
        self.pressing = True
        _grab_mouse(self.plotter)

    def _release(self, interactor, _event) -> None:
        if not self.pressing:
            return
        if not _event_inside_renderer(self.plotter, interactor):
            if self.cancel_callback:
                self.cancel_callback()
            else:
                self.cancel()
            return
        self.plotter.enable_trackball_style()
        self.pressing = False
        if self.release_callback:
            self.release_callback(self.matrix.copy())
        _release_mouse(self.plotter)

    def cancel(self) -> None:
        self.actor.user_matrix = np.eye(4)
        self.matrix = np.eye(4)
        self.pressing = False
        self.initial_world = None
        _release_mouse(self.plotter)
        self.plotter.enable_trackball_style()
        self.plotter.render()

    def remove(self) -> None:
        _release_mouse(self.plotter)
        for observer in self.observers:
            self.plotter.iren.remove_observer(observer)
        self.observers.clear()
        for handle in self.handles:
            self.plotter.remove_actor(handle, render=False)
        self.handles.clear()


class RotationWidget3D:
    """Three-axis world rotation widget with a picker isolated from scene selection."""

    def __init__(
        self,
        plotter: pv.Plotter,
        actor: pv.Actor,
        *,
        interact_callback: RotationCallback | None = None,
        release_callback: RotationCallback | None = None,
        cancel_callback: CancelCallback | None = None,
        origin: tuple[float, float, float] | np.ndarray | None = None,
    ) -> None:
        self.plotter = plotter
        self.actor = actor
        self.interact_callback = interact_callback
        self.release_callback = release_callback
        self.cancel_callback = cancel_callback
        self.origin = np.asarray(actor.center if origin is None else origin, dtype=float)
        self.actor_length = max(float(actor.GetLength()), 0.25)
        self.radius = self.actor_length * 0.82
        # Axial vectors under reflection use det(C)*C = -C. These normals make a
        # positive widget angle equal a positive rotation about the HTDT domain axis.
        self.axes = np.array(((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, -1.0)))
        self.handles: list[pv.Actor] = []
        self.selected: pv.Actor | None = None
        self.initial_vector: np.ndarray | None = None
        self.pressing = False
        self.angle_deg = 0.0
        self.matrix = np.eye(4)
        self.observers: list[int] = []
        self.handle_picker = vtkPropPicker()
        colors = (
            pv.global_theme.axes.x_color,
            pv.global_theme.axes.y_color,
            pv.global_theme.axes.z_color,
        )
        for axis, color in zip(self.axes, colors, strict=True):
            ring = pv.Polygon(
                center=self.origin,
                radius=self.radius,
                normal=axis,
                n_sides=96,
                fill=False,
            ).tube(radius=max(self.radius * 0.040, 0.008), n_sides=12)
            handle = plotter.add_mesh(
                ring,
                color=color,
                lighting=False,
                pickable=True,
                render=False,
            )
            handle.mapper.SetResolveCoincidentTopologyToPolygonOffset()
            handle.mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(0, -20000)
            self.handles.append(handle)
        self.observers.extend(
            (
                plotter.iren.add_observer('MouseMoveEvent', self._move),
                plotter.iren.add_observer('LeftButtonPressEvent', self._press, interactor_style_fallback=False),
                plotter.iren.add_observer('LeftButtonReleaseEvent', self._release, interactor_style_fallback=False),
            )
        )

    @property
    def active_axis_index(self) -> int | None:
        return self.handles.index(self.selected) if self.selected in self.handles else None

    def hit_test_display(self, x: float, y: float, renderer) -> bool:
        return self._pick_handle_at(float(x), float(y), renderer) is not None

    def _pick_handle_at(self, x: float, y: float, renderer) -> pv.Actor | None:
        if renderer is None:
            return None
        self.handle_picker.Pick(int(round(x)), int(round(y)), 0, renderer)
        picked = self.handle_picker.GetActor()
        return picked if picked in self.handles else None

    def _pick_handle(self, interactor) -> pv.Actor | None:
        renderer = self.plotter.iren.get_poked_renderer()
        if renderer is None:
            return None
        x, y = interactor.GetEventPosition()
        return self._pick_handle_at(float(x), float(y), renderer)

    def _world_ray(self, interactor) -> tuple[np.ndarray, np.ndarray]:
        x, y = interactor.GetEventPosition()
        renderer = self.plotter.iren.get_poked_renderer()
        points: list[np.ndarray] = []
        for depth in (0.0, 1.0):
            renderer.SetDisplayPoint(x, y, depth)
            renderer.DisplayToWorld()
            value = np.asarray(renderer.GetWorldPoint(), dtype=float)
            points.append(value[:3] if abs(value[3]) <= 1e-12 else value[:3] / value[3])
        return points[0], points[1]

    def _plane_vector(self, interactor, axis: np.ndarray) -> np.ndarray | None:
        near, far = self._world_ray(interactor)
        direction = far - near
        denominator = float(np.dot(direction, axis))
        if abs(denominator) <= 1e-9:
            return None
        distance = float(np.dot(self.origin - near, axis)) / denominator
        point = near + direction * distance
        vector = point - self.origin
        length = float(np.linalg.norm(vector))
        if length <= 1e-9:
            return None
        return vector / length

    @staticmethod
    def _rotation_about(origin: np.ndarray, axis: np.ndarray, angle_deg: float) -> np.ndarray:
        angle = np.deg2rad(angle_deg)
        x, y, z = axis / np.linalg.norm(axis)
        c = float(np.cos(angle))
        s = float(np.sin(angle))
        one = 1.0 - c
        rotation = np.array(
            (
                (c + x * x * one, x * y * one - z * s, x * z * one + y * s),
                (y * x * one + z * s, c + y * y * one, y * z * one - x * s),
                (z * x * one - y * s, z * y * one + x * s, c + z * z * one),
            )
        )
        result = np.eye(4)
        result[:3, :3] = rotation
        before = np.eye(4)
        before[:3, 3] = -origin
        after = np.eye(4)
        after[:3, 3] = origin
        return after @ result @ before

    def set_display_angle(self, angle_deg: float) -> None:
        if self.selected not in self.handles:
            return
        axis = self.axes[self.handles.index(self.selected)]
        self.angle_deg = float(angle_deg)
        self.matrix = self._rotation_about(self.origin, axis, self.angle_deg)
        self.actor.user_matrix = self.matrix

    def _move(self, interactor, _event) -> None:
        if self.pressing and self.selected is not None and self.initial_vector is not None:
            axis_index = self.handles.index(self.selected)
            axis = self.axes[axis_index]
            current = self._plane_vector(interactor, axis)
            if current is None:
                return
            sine = float(np.dot(axis, np.cross(self.initial_vector, current)))
            cosine = float(np.dot(self.initial_vector, current))
            self.set_display_angle(degrees(atan2(sine, cosine)))
            if self.interact_callback:
                self.interact_callback(axis_index, self.angle_deg)
            self.plotter.render()
            return
        self.selected = self._pick_handle(interactor)

    def _press(self, interactor, _event) -> None:
        self.selected = self._pick_handle(interactor)
        if self.selected is None:
            return
        axis = self.axes[self.handles.index(self.selected)]
        initial = self._plane_vector(interactor, axis)
        if initial is None:
            self.selected = None
            return
        self.plotter.enable_trackball_actor_style()
        self.initial_vector = initial
        self.angle_deg = 0.0
        self.matrix = np.eye(4)
        self.pressing = True
        _grab_mouse(self.plotter)

    def _release(self, interactor, _event) -> None:
        if not self.pressing or self.selected is None:
            return
        if not _event_inside_renderer(self.plotter, interactor):
            if self.cancel_callback:
                self.cancel_callback()
            else:
                self.cancel()
            return
        axis_index = self.handles.index(self.selected)
        self.plotter.enable_trackball_style()
        self.pressing = False
        if self.release_callback:
            self.release_callback(axis_index, self.angle_deg)
        _release_mouse(self.plotter)

    def cancel(self) -> None:
        self.actor.user_matrix = np.eye(4)
        self.matrix = np.eye(4)
        self.angle_deg = 0.0
        self.pressing = False
        self.initial_vector = None
        _release_mouse(self.plotter)
        self.plotter.enable_trackball_style()
        self.plotter.render()

    def remove(self) -> None:
        _release_mouse(self.plotter)
        for observer in self.observers:
            self.plotter.iren.remove_observer(observer)
        self.observers.clear()
        for handle in self.handles:
            self.plotter.remove_actor(handle, render=False)
        self.handles.clear()
