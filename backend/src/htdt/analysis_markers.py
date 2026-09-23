from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pyvista as pv


@dataclass(frozen=True)
class AnalysisMarkerCloudRender:
    """One bulk VTK actor representing many non-editable analysis markers."""

    actor_name: str
    point_count: int
    actor: Any


def analysis_marker_polydata(
    domain_xyz: Sequence[Sequence[float]] | np.ndarray,
) -> pv.PolyData:
    """Convert HTDT +X right/+Y rear/+Z up points into one render-space PolyData."""

    points = np.asarray(domain_xyz, dtype=float)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError('analysis marker points must have shape (N, 3)')
    if points.shape[0] < 1:
        raise ValueError('analysis marker cloud must contain at least one point')
    if not np.isfinite(points).all():
        raise ValueError('analysis marker points must be finite')

    render_points = points.copy()
    render_points[:, 1] *= -1.0
    return pv.PolyData(render_points)


def render_analysis_marker_cloud(
    plotter: Any,
    domain_xyz: Sequence[Sequence[float]] | np.ndarray,
    *,
    actor_name: str = 'analysis-marker-cloud',
    point_size: float = 5.0,
    render: bool = True,
) -> AnalysisMarkerCloudRender:
    """Render a marker cloud with exactly one mesh actor, never one actor per marker."""

    if not actor_name:
        raise ValueError('actor_name must not be empty')
    if not np.isfinite(float(point_size)) or float(point_size) <= 0.0:
        raise ValueError('point_size must be finite and > 0')

    cloud = analysis_marker_polydata(domain_xyz)
    try:
        plotter.remove_actor(actor_name, reset_camera=False, render=False)
    except Exception:
        pass

    actor = plotter.add_mesh(
        cloud,
        name=actor_name,
        style='points',
        point_size=float(point_size),
        render_points_as_spheres=True,
        pickable=False,
        lighting=False,
        render=False,
    )
    if render:
        plotter.render()
    return AnalysisMarkerCloudRender(
        actor_name=actor_name,
        point_count=int(cloud.n_points),
        actor=actor,
    )
