from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Sequence

from shapely.geometry import Point, Polygon
from shapely.validation import explain_validity


ROOM_GEOMETRY_VERSION = 'room-geometry-2'
_MIN_AREA_M2 = 1e-8


@dataclass(frozen=True)
class PolygonSummary:
    vertex_count: int
    area_m2: float
    perimeter_m: float
    bounds_m: tuple[float, float, float, float]
    convex: bool


def _finite(value: float) -> bool:
    return math.isfinite(float(value))


def polygon_from_vertices(vertices: Sequence[tuple[float, float]]) -> Polygon:
    if len(vertices) < 3:
        raise ValueError('Room polygon must have at least three vertices')
    coords = tuple((float(x), float(y)) for x, y in vertices)
    if any(not _finite(value) for point in coords for value in point):
        raise ValueError('Room polygon contains a non-finite coordinate')
    if coords[0] == coords[-1]:
        raise ValueError('Room polygon must not repeat the first vertex as a closing vertex')
    if len(set(coords)) != len(coords):
        raise ValueError('Room polygon contains duplicate vertices')
    polygon = Polygon(coords)
    if not polygon.is_valid:
        raise ValueError(f'Invalid room polygon: {explain_validity(polygon)}')
    if polygon.is_empty or polygon.area <= _MIN_AREA_M2:
        raise ValueError('Room polygon area is too small')
    return polygon


def validate_polygon_in_reference_box(
    width_m: float,
    depth_m: float,
    vertices: Sequence[tuple[float, float]],
) -> Polygon:
    if width_m <= 0 or depth_m <= 0:
        raise ValueError('Room reference bounds must be positive')
    polygon = polygon_from_vertices(vertices)
    min_x, min_y, max_x, max_y = polygon.bounds
    tolerance = 1e-9
    if min_x < -tolerance or min_y < -tolerance or max_x > width_m + tolerance or max_y > depth_m + tolerance:
        raise ValueError('Room polygon extends outside the reference bounds')
    return polygon


def polygon_covers_xy(polygon: Polygon, x_m: float, y_m: float) -> bool:
    if not _finite(x_m) or not _finite(y_m):
        return False
    return bool(polygon.covers(Point(float(x_m), float(y_m))))


def summarize_polygon(polygon: Polygon) -> PolygonSummary:
    hull = polygon.convex_hull
    tolerance = max(_MIN_AREA_M2, polygon.area * 1e-12)
    return PolygonSummary(
        vertex_count=len(polygon.exterior.coords) - 1,
        area_m2=float(polygon.area),
        perimeter_m=float(polygon.length),
        bounds_m=tuple(float(value) for value in polygon.bounds),  # type: ignore[arg-type]
        convex=abs(float(hull.area) - float(polygon.area)) <= tolerance,
    )


def room_geometry_payload(room: dict[str, Any]) -> dict[str, Any]:
    width = float(room['width_m'])
    depth = float(room['depth_m'])
    height = float(room['height_m'])
    kind = str(room.get('geometry_kind', 'rectangular'))
    reference_box = {'width_m': width, 'depth_m': depth, 'height_m': height}
    if kind == 'reference_box':
        return {
            'classification': 'room_geometry',
            'geometry_version': ROOM_GEOMETRY_VERSION,
            'geometry_kind': kind,
            'exact_footprint_available': False,
            'reference_box': reference_box,
            'footprint_vertices': None,
            'polygon_summary': None,
            'wall_edges': [],
        }
    if kind == 'rectangular':
        vertices = [
            {'vertex_id': 'front_left', 'x_m': 0.0, 'y_m': 0.0},
            {'vertex_id': 'front_right', 'x_m': width, 'y_m': 0.0},
            {'vertex_id': 'rear_right', 'x_m': width, 'y_m': depth},
            {'vertex_id': 'rear_left', 'x_m': 0.0, 'y_m': depth},
        ]
    elif kind == 'polygon_prism':
        raw_vertices = room.get('footprint_vertices')
        if not isinstance(raw_vertices, list):
            raise ValueError('polygon_prism requires footprint_vertices')
        vertices = raw_vertices
    else:
        raise ValueError(f'Unsupported room geometry_kind: {kind}')

    coords = [(float(item['x_m']), float(item['y_m'])) for item in vertices]
    polygon = validate_polygon_in_reference_box(width, depth, coords)
    wall_edges = []
    for index, start in enumerate(vertices):
        end = vertices[(index + 1) % len(vertices)]
        wall_edges.append({
            'edge_id': f"{start['vertex_id']}->{end['vertex_id']}",
            'from_vertex_id': start['vertex_id'],
            'to_vertex_id': end['vertex_id'],
        })
    return {
        'classification': 'room_geometry',
        'geometry_version': ROOM_GEOMETRY_VERSION,
        'geometry_kind': kind,
        'exact_footprint_available': True,
        'reference_box': reference_box,
        'footprint_vertices': vertices,
        'polygon_summary': asdict(summarize_polygon(polygon)),
        'wall_edges': wall_edges,
    }
