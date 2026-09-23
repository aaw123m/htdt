from __future__ import annotations

import numpy as np
import pytest

from htdt.analysis_markers import analysis_marker_polydata, render_analysis_marker_cloud


class _FakePlotter:
    def __init__(self) -> None:
        self.remove_calls: list[tuple[tuple, dict]] = []
        self.add_calls: list[tuple[tuple, dict]] = []
        self.render_count = 0
        self.actor = object()

    def remove_actor(self, *args, **kwargs):
        self.remove_calls.append((args, kwargs))
        return None

    def add_mesh(self, *args, **kwargs):
        self.add_calls.append((args, kwargs))
        return self.actor

    def render(self) -> None:
        self.render_count += 1


def test_analysis_marker_polydata_maps_domain_y_and_keeps_10000_points() -> None:
    x, y = np.meshgrid(np.linspace(0.0, 6.0, 100), np.linspace(0.0, 4.0, 100))
    points = np.column_stack((x.ravel(), y.ravel(), np.full(10_000, 1.1)))

    cloud = analysis_marker_polydata(points)

    assert cloud.n_points == 10_000
    assert np.allclose(cloud.points[:, 0], points[:, 0])
    assert np.allclose(cloud.points[:, 1], -points[:, 1])
    assert np.allclose(cloud.points[:, 2], points[:, 2])


def test_render_analysis_marker_cloud_uses_one_non_pickable_mesh_actor() -> None:
    plotter = _FakePlotter()
    points = np.asarray(((0.0, 0.0, 0.1), (1.0, 2.0, 0.2), (2.0, 1.0, 0.3)))

    rendered = render_analysis_marker_cloud(
        plotter,
        points,
        actor_name='candidate-cloud',
        point_size=7.0,
    )

    assert rendered.actor is plotter.actor
    assert rendered.actor_name == 'candidate-cloud'
    assert rendered.point_count == 3
    assert len(plotter.add_calls) == 1
    mesh = plotter.add_calls[0][0][0]
    kwargs = plotter.add_calls[0][1]
    assert mesh.n_points == 3
    assert kwargs['name'] == 'candidate-cloud'
    assert kwargs['style'] == 'points'
    assert kwargs['render_points_as_spheres'] is True
    assert kwargs['pickable'] is False
    assert kwargs['render'] is False
    assert plotter.render_count == 1


def test_analysis_marker_cloud_rejects_invalid_points() -> None:
    with pytest.raises(ValueError, match='shape'):
        analysis_marker_polydata(np.zeros((5, 2)))
    with pytest.raises(ValueError, match='at least one'):
        analysis_marker_polydata(np.zeros((0, 3)))
    invalid = np.zeros((2, 3))
    invalid[0, 1] = np.nan
    with pytest.raises(ValueError, match='finite'):
        analysis_marker_polydata(invalid)
