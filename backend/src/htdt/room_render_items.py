"""Viewport render-item value types — Qt-free (#807 boundary refactor).

The overlay/underlay item dataclasses carried through the 3D scene are
plain pyvista-facing values; modules at domain rank (geometric constraints,
review packaging) use them without pulling in the Qt viewport.
``room_viewport`` re-exports them, so existing ``room_viewport import``
paths keep working.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyvista as pv

from .room_operational_clearance import OPERATIONAL_ZONE_KIND_VOCAB


@dataclass(frozen=True, slots=True)
class RoomOverlayState:
    grid: bool = True
    labels: bool = False
    acoustics: bool = False
    focus_selection: bool = False
    hidden_ids: frozenset[str] = frozenset()
    guides_visible: bool = True
    # #1013: read-only lighting-scene preview (desired/commanded/read-back/
    # measured state glyphs on exact-bound fixtures). Explanation symbols
    # only — never a photometric render.
    lighting_scene: bool = False
    # #1010: read-only 運用クリアランス layer — declared operational-zone
    # XY footprints + operational_clearance_conflicts highlights.
    operational_clearance: bool = False
    # Zone-kind filter for the clearance layer; defaults to every kind.
    operational_zone_kinds: frozenset[str] = frozenset(OPERATIONAL_ZONE_KIND_VOCAB)
    # #1004: read-only as-built survey overlay — None or one of
    # ('tier', 'uncertainty_mm', 'verification', 'delta_mm').
    survey_mode: str | None = None


@dataclass(frozen=True, slots=True)
class UnderlayRenderItem:
    """One floor-plan underlay resolved for rendering (#534).

    All geometry is in domain coordinates; the viewport maps to render space.
    ``quad_domain`` is the 4-corner raster quad (``None`` for vector-only
    underlays), ``image`` a uint8 HxWx3/4 array consumed by ``pv.Texture``,
    and ``segments_domain`` optional line segments (DXF vectors).
    """

    underlay_id: str
    name: str
    quad_domain: tuple[tuple[float, float, float], ...] | None
    image: np.ndarray | None
    segments_domain: tuple[tuple[tuple[float, float, float], tuple[float, float, float]], ...]
    opacity: float
    elevation_m: float
    # True when the record references a blob the store no longer carries —
    # the underlay renders empty, so surfaces must label it missing rather
    # than present it as a normal-but-blank underlay.
    missing_source: bool = False


@dataclass(frozen=True, slots=True)
class GuideRenderItem:
    """One construction guide line segment in domain coordinates (#618)."""

    start: tuple[float, float, float]
    end: tuple[float, float, float]
    label: str | None = None


@dataclass(frozen=True, slots=True)
class CableRouteEndpointItem:
    """One cable-run endpoint resolved for overlay rendering (#1011).

    ``position`` is a domain-coordinate (x, y, z) metres triple — present
    only when the endpoint is exactly bound to an entity that exists in
    the current head document (status ``'bound'``).
    """

    label: str
    position: tuple[float, float, float] | None
    status: str  # 'bound' | 'unbound' | 'missing'


@dataclass(frozen=True, slots=True)
class CableRouteSegmentRouteItem:
    """Recorded waypoint polyline for one segment, domain coordinates."""

    segment_sequence: int
    points: tuple[tuple[float, float, float], ...]


@dataclass(frozen=True, slots=True)
class CableRouteOverlayItem:
    """One cable run resolved for the honest route overlay (#1011).

    ``route_state`` drives what may be drawn: ``'unregistered'`` renders
    bound endpoints plus the unregistered-route label and never a line;
    ``'registered'`` may connect only the explicitly recorded waypoints of
    ``waypoint_segments``; ``'stale'`` draws endpoints and a stale label —
    the recorded line is never presented as the current route.
    """

    run_id: str
    label: str
    route_state: str  # 'unregistered' | 'registered' | 'stale'
    from_endpoint: CableRouteEndpointItem
    to_endpoint: CableRouteEndpointItem
    waypoint_segments: tuple[CableRouteSegmentRouteItem, ...] = ()
    record_kind: str = 'design'  # 'design' | 'as_built'

__all__ = [
    'CableRouteEndpointItem',
    'CableRouteOverlayItem',
    'CableRouteSegmentRouteItem',
    'GuideRenderItem',
    'RoomOverlayState',
    'UnderlayRenderItem',
]
