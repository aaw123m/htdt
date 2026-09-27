from __future__ import annotations

from math import sqrt
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_scene import Direction3, Position3
from .r120_geometry_compiler import (
    AcousticRegionAuthority,
    AcousticRegionDeclaration,
    ExactExternalAuthorityRef,
    PortalAuthority,
    PortalBoundaryEdge,
    PortalDeclaration,
    R120CompiledGeometry,
)
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _semantic_hash


PORTAL_SIDE_SEMANTICS = (
    'directed_boundary_edge_loop_normal_from_first_to_second'
)






def _vector(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float]:
    return (
        float(b[0]) - float(a[0]),
        float(b[1]) - float(a[1]),
        float(b[2]) - float(a[2]),
    )


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(float(left) * float(right) for left, right in zip(a, b, strict=True))


def _cross(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float]:
    return (
        float(a[1]) * float(b[2]) - float(a[2]) * float(b[1]),
        float(a[2]) * float(b[0]) - float(a[0]) * float(b[2]),
        float(a[0]) * float(b[1]) - float(a[1]) * float(b[0]),
    )


def _norm(value: Sequence[float]) -> float:
    return sqrt(_dot(value, value))


def _unit(value: Sequence[float]) -> tuple[float, float, float]:
    length = _norm(value)
    if length <= 0.0:
        raise ValueError('zero-length portal direction is invalid')
    return tuple(float(item) / length for item in value)  # type: ignore[return-value]


def _position(value: Sequence[float]) -> Position3:
    return Position3(x_m=float(value[0]), y_m=float(value[1]), z_m=float(value[2]))


def _direction(value: Sequence[float]) -> Direction3:
    unit = _unit(value)
    return Direction3(x=unit[0], y=unit[1], z=unit[2])


def _position_tuple(value: Position3) -> tuple[float, float, float]:
    return (float(value.x_m), float(value.y_m), float(value.z_m))


def _direction_tuple(value: Direction3) -> tuple[float, float, float]:
    return (float(value.x), float(value.y), float(value.z))


def _compiled_vertex(
    compiled: R120CompiledGeometry,
    index: int,
) -> tuple[float, float, float]:
    vertex = compiled.vertices[index]
    return (float(vertex.x_m), float(vertex.y_m), float(vertex.z_m))


class GeometricPortalAperture(BaseModel):
    """Exact R120 Portal aperture resolved for the bounded R150 multi-region lane."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    aperture_id: str = Field(pattern=r'^r150-portal-aperture:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    portal_id: str = Field(min_length=1)
    from_region_id: str = Field(min_length=1)
    to_region_id: str = Field(min_length=1)
    state: Literal['open'] = 'open'
    region_side_semantics: Literal[
        'directed_boundary_edge_loop_normal_from_first_to_second'
    ] = PORTAL_SIDE_SEMANTICS
    boundary_edges: tuple[PortalBoundaryEdge, ...] = Field(min_length=3)
    source_surface_ids: tuple[str, ...] = Field(min_length=1)
    ordered_vertex_indices: tuple[int, ...] = Field(min_length=3)
    ordered_vertices_m: tuple[Position3, ...] = Field(min_length=3)
    plane_point_m: Position3
    normal_from_to: Direction3

    @model_validator(mode='after')
    def validate_identity(self) -> 'GeometricPortalAperture':
        if self.from_region_id == self.to_region_id:
            raise ValueError('Portal aperture regions must be distinct')
        if len(self.ordered_vertex_indices) != len(self.ordered_vertices_m):
            raise ValueError('Portal aperture index/vertex sequence length mismatch')
        if len(set(self.ordered_vertex_indices)) != len(self.ordered_vertex_indices):
            raise ValueError('Portal aperture ordered vertices must be unique')
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('GeometricPortalAperture semantic hash mismatch')
        if self.aperture_id != f'r150-portal-aperture:{expected}':
            raise ValueError('GeometricPortalAperture id mismatch')
        return self

    def semantic_payload(self) -> dict[str, object]:
        return self.model_dump(
            mode='json',
            exclude={'aperture_id', 'semantic_sha256'},
        )


class GeometricPortalGraphEdge(BaseModel):
    """One exact directed adjacency in the R150 Portal graph authority."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    portal_id: str = Field(min_length=1)
    aperture_id: str = Field(pattern=r'^r150-portal-aperture:[0-9a-f]{64}$')
    aperture_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    from_region_id: str = Field(min_length=1)
    to_region_id: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_edge(self) -> 'GeometricPortalGraphEdge':
        if self.from_region_id == self.to_region_id:
            raise ValueError('Portal graph edge regions must be distinct')
        return self


class GeometricPortalGraph(BaseModel):
    """Exact bounded directed topology authority for multi-Portal direct propagation."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    graph_id: str = Field(pattern=r'^r150-portal-graph:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    authority_version: Literal['1'] = '1'
    r120_compiled_geometry_id: str
    r120_compiled_geometry_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    region_authority_id: str
    region_authority_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    portal_authority_id: str
    portal_authority_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    region_ids: tuple[str, ...] = Field(min_length=2)
    directed_edges: tuple[GeometricPortalGraphEdge, ...] = Field(min_length=1)
    traversal_policy: Literal['simple_region_path_v1'] = 'simple_region_path_v1'
    repeated_region_traversal: Literal[False] = False
    repeated_portal_traversal: Literal[False] = False
    deterministic_traversal_order: Literal['portal_id_lexicographic_v1'] = (
        'portal_id_lexicographic_v1'
    )
    maximum_portal_crossings: int = Field(ge=1, le=16)
    maximum_search_states: Literal[4096] = 4096

    @model_validator(mode='after')
    def validate_identity(self) -> 'GeometricPortalGraph':
        if tuple(sorted(set(self.region_ids))) != self.region_ids:
            raise ValueError('Portal graph region ids must be unique and lexicographically sorted')
        portal_ids = [item.portal_id for item in self.directed_edges]
        if len(portal_ids) != len(set(portal_ids)):
            raise ValueError('Portal graph contains duplicate Portal identity')
        aperture_ids = [item.aperture_id for item in self.directed_edges]
        if len(aperture_ids) != len(set(aperture_ids)):
            raise ValueError('Portal graph contains duplicate aperture identity')
        adjacency = [
            (item.from_region_id, item.to_region_id)
            for item in self.directed_edges
        ]
        if len(adjacency) != len(set(adjacency)):
            raise ValueError('Portal graph contains ambiguous parallel directed adjacency')
        region_ids = set(self.region_ids)
        if any(
            item.from_region_id not in region_ids or item.to_region_id not in region_ids
            for item in self.directed_edges
        ):
            raise ValueError('Portal graph edge references an unknown AcousticRegion')
        canonical_edges = tuple(
            sorted(
                self.directed_edges,
                key=lambda item: (item.portal_id, item.from_region_id, item.to_region_id),
            )
        )
        if self.directed_edges != canonical_edges:
            raise ValueError('Portal graph edges must use deterministic Portal identity ordering')
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('GeometricPortalGraph semantic hash mismatch')
        if self.graph_id != f'r150-portal-graph:{expected}':
            raise ValueError('GeometricPortalGraph id mismatch')
        return self

    def semantic_payload(self) -> dict[str, object]:
        return self.model_dump(
            mode='json',
            exclude={'graph_id', 'semantic_sha256'},
        )

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.graph_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


def _triangle_vertex_indices(compiled: R120CompiledGeometry, triangle_index: int) -> tuple[int, int, int]:
    triangle = compiled.triangles[triangle_index]
    return (int(triangle.a), int(triangle.b), int(triangle.c))


def _normalized_edge(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _portal_edge_incidence(
    compiled: R120CompiledGeometry,
    edge: PortalBoundaryEdge,
) -> tuple[int, str] | None:
    target = _normalized_edge(edge.vertex_a, edge.vertex_b)
    matches: list[tuple[int, str]] = []
    for index, triangle in enumerate(compiled.triangles):
        a, b, c = _triangle_vertex_indices(compiled, index)
        edges = (
            _normalized_edge(a, b),
            _normalized_edge(b, c),
            _normalized_edge(c, a),
        )
        if target in edges:
            matches.append((index, triangle.source_surface_id))
    if len(matches) != 1:
        return None
    return matches[0]


def _newell_normal(
    vertices: Sequence[Sequence[float]],
) -> tuple[float, float, float]:
    nx = ny = nz = 0.0
    for index, current in enumerate(vertices):
        nxt = vertices[(index + 1) % len(vertices)]
        nx += (float(current[1]) - float(nxt[1])) * (
            float(current[2]) + float(nxt[2])
        )
        ny += (float(current[2]) - float(nxt[2])) * (
            float(current[0]) + float(nxt[0])
        )
        nz += (float(current[0]) - float(nxt[0])) * (
            float(current[1]) + float(nxt[1])
        )
    return (nx, ny, nz)


def _project_axis_for_normal(normal: Sequence[float]) -> int:
    values = [abs(float(value)) for value in normal]
    return values.index(max(values))


def _project_for_axis(
    point: Sequence[float],
    drop_axis: int,
) -> tuple[float, float]:
    return tuple(
        float(point[index]) for index in range(3) if index != drop_axis
    )  # type: ignore[return-value]


def _orient2d(
    a: Sequence[float],
    b: Sequence[float],
    c: Sequence[float],
) -> float:
    return (
        (float(b[0]) - float(a[0])) * (float(c[1]) - float(a[1]))
        - (float(b[1]) - float(a[1])) * (float(c[0]) - float(a[0]))
    )


def _validate_simple_strictly_convex_aperture(
    vertices: Sequence[Sequence[float]],
    normal: Sequence[float],
    *,
    tolerance_m: float,
) -> None:
    """Bound temporary cap triangulation to an exact simple convex polygon."""

    drop_axis = _project_axis_for_normal(normal)
    polygon = tuple(_project_for_axis(point, drop_axis) for point in vertices)
    edge_lengths = tuple(
        sqrt(
            (polygon[(index + 1) % len(polygon)][0] - point[0]) ** 2
            + (polygon[(index + 1) % len(polygon)][1] - point[1]) ** 2
        )
        for index, point in enumerate(polygon)
    )
    if any(length <= tolerance_m for length in edge_lengths):
        raise ValueError('Portal aperture contains a degenerate boundary edge')
    geometry_scale = max(edge_lengths)
    area_tolerance = max(tolerance_m * geometry_scale, tolerance_m * tolerance_m)

    # Non-adjacent projected boundary edges may not cross or touch. Treat
    # near-collinear/touching cases as ambiguous rather than guessing topology.
    for left in range(len(polygon)):
        left_a = polygon[left]
        left_b = polygon[(left + 1) % len(polygon)]
        for right in range(left + 1, len(polygon)):
            if right == left + 1 or (left == 0 and right == len(polygon) - 1):
                continue
            right_a = polygon[right]
            right_b = polygon[(right + 1) % len(polygon)]
            o1 = _orient2d(left_a, left_b, right_a)
            o2 = _orient2d(left_a, left_b, right_b)
            o3 = _orient2d(right_a, right_b, left_a)
            o4 = _orient2d(right_a, right_b, left_b)
            if any(abs(value) <= area_tolerance for value in (o1, o2, o3, o4)):
                raise ValueError(
                    'Portal aperture boundary is self-touching/collinear or ambiguous '
                    'within declared tolerance'
                )
            if (o1 > 0.0) != (o2 > 0.0) and (o3 > 0.0) != (o4 > 0.0):
                raise ValueError('Portal aperture boundary is self-intersecting')

    turn_sign: bool | None = None
    for index in range(len(polygon)):
        turn = _orient2d(
            polygon[index - 1],
            polygon[index],
            polygon[(index + 1) % len(polygon)],
        )
        if abs(turn) <= area_tolerance:
            raise ValueError(
                'Portal aperture has a collinear/degenerate corner within declared tolerance'
            )
        sign = turn > 0.0
        if turn_sign is None:
            turn_sign = sign
        elif sign != turn_sign:
            raise ValueError(
                'Portal aperture is concave; current exact temporary-cap lane '
                'supports simple strictly-convex polygons only'
            )


def _region_vertex_centroid(
    compiled: R120CompiledGeometry,
    declaration: AcousticRegionDeclaration,
) -> tuple[float, float, float]:
    surface_ids = set(declaration.boundary_surface_ids)
    vertex_indices = {
        vertex_index
        for triangle in compiled.triangles
        if triangle.source_surface_id in surface_ids
        for vertex_index in (triangle.a, triangle.b, triangle.c)
    }
    if not vertex_indices:
        raise ValueError(f'acoustic region {declaration.region_id} has no compiled boundary vertices')
    points = [_compiled_vertex(compiled, index) for index in sorted(vertex_indices)]
    count = float(len(points))
    return (
        sum(point[0] for point in points) / count,
        sum(point[1] for point in points) / count,
        sum(point[2] for point in points) / count,
    )


def _compile_portal_aperture(
    *,
    compiled_geometry: R120CompiledGeometry,
    region_authority: AcousticRegionAuthority,
    declaration: PortalDeclaration,
    tolerance_m: float,
) -> GeometricPortalAperture:
    """Resolve one exact directed Portal aperture without inferring adjacency."""

    region_by_id = {item.region_id: item for item in region_authority.declarations}
    if len(region_by_id) != len(region_authority.declarations):
        raise ValueError('AcousticRegion authority contains duplicate region identity')
    if len(declaration.region_ids) != 2:
        raise ValueError('Portal propagation requires exactly two declared adjacent regions')
    from_region_id, to_region_id = declaration.region_ids
    if from_region_id not in region_by_id or to_region_id not in region_by_id:
        raise ValueError('Portal adjacency references a region outside exact AcousticRegion authority')
    if declaration.state != 'open':
        raise ValueError('Portal propagation requires explicit state=open')
    if declaration.region_side_semantics != PORTAL_SIDE_SEMANTICS:
        raise ValueError('Portal propagation requires explicit directed region-side semantics')
    if len(declaration.boundary_edges) < 3:
        raise ValueError('Portal aperture requires at least three directed boundary edges')

    edges = declaration.boundary_edges
    ordered_indices = [int(edges[0].vertex_a)]
    expected_a = int(edges[0].vertex_a)
    for edge in edges:
        if int(edge.vertex_a) != expected_a:
            raise ValueError('Portal boundary edges must form one ordered directed loop')
        if (
            edge.vertex_a >= len(compiled_geometry.vertices)
            or edge.vertex_b >= len(compiled_geometry.vertices)
        ):
            raise ValueError('Portal boundary edge references a missing compiled vertex')
        incidence = _portal_edge_incidence(compiled_geometry, edge)
        if incidence is None:
            raise ValueError(
                'Portal boundary edge is not an exact single-incidence geometric opening edge'
            )
        _, source_surface_id = incidence
        if source_surface_id != edge.source_surface_id:
            raise ValueError('Portal boundary edge semantic surface identity mismatch')
        ordered_indices.append(int(edge.vertex_b))
        expected_a = int(edge.vertex_b)
    if ordered_indices[-1] != ordered_indices[0]:
        raise ValueError('Portal directed boundary edge loop is not closed')
    ordered_indices = ordered_indices[:-1]
    if len(set(ordered_indices)) != len(ordered_indices):
        raise ValueError('Portal aperture loop repeats a vertex and is ambiguous/non-manifold')

    source_surface_ids = tuple(sorted({edge.source_surface_id for edge in edges}))
    from_region = region_by_id[from_region_id]
    to_region = region_by_id[to_region_id]
    for region in (from_region, to_region):
        if not set(source_surface_ids).issubset(set(region.boundary_surface_ids)):
            raise ValueError(
                'Portal aperture surfaces must be explicit boundary surfaces of both adjacent regions'
            )
    shared_surfaces = set(from_region.boundary_surface_ids).intersection(
        to_region.boundary_surface_ids
    )
    if shared_surfaces != set(source_surface_ids):
        raise ValueError(
            'Portal adjacency is ambiguous: adjacent regions must share exactly the Portal surfaces'
        )

    vertices = tuple(
        _compiled_vertex(compiled_geometry, index)
        for index in ordered_indices
    )
    raw_normal = _newell_normal(vertices)
    if _norm(raw_normal) <= tolerance_m * tolerance_m:
        raise ValueError('Portal aperture directed loop is degenerate')
    normal = _unit(raw_normal)
    plane_point = vertices[0]
    for vertex in vertices[1:]:
        signed = _dot(_vector(plane_point, vertex), normal)
        if abs(signed) > tolerance_m:
            raise ValueError('Portal aperture vertices are not coplanar within R120/R150 tolerance')

    _validate_simple_strictly_convex_aperture(
        vertices,
        normal,
        tolerance_m=tolerance_m,
    )

    from_centroid = _region_vertex_centroid(compiled_geometry, from_region)
    to_centroid = _region_vertex_centroid(compiled_geometry, to_region)
    from_side = _dot(_vector(plane_point, from_centroid), normal)
    to_side = _dot(_vector(plane_point, to_centroid), normal)
    if from_side >= -tolerance_m or to_side <= tolerance_m:
        raise ValueError(
            'Portal directed edge-loop orientation does not point from first region to second region'
        )

    core = {
        'portal_id': declaration.portal_id,
        'from_region_id': from_region_id,
        'to_region_id': to_region_id,
        'state': 'open',
        'region_side_semantics': PORTAL_SIDE_SEMANTICS,
        'boundary_edges': [item.model_dump(mode='json') for item in edges],
        'source_surface_ids': list(source_surface_ids),
        'ordered_vertex_indices': ordered_indices,
        'ordered_vertices_m': [
            _position(item).model_dump(mode='json')
            for item in vertices
        ],
        'plane_point_m': _position(plane_point).model_dump(mode='json'),
        'normal_from_to': _direction(normal).model_dump(mode='json'),
    }
    digest = _semantic_hash(core)
    return GeometricPortalAperture(
        aperture_id=f'r150-portal-aperture:{digest}',
        semantic_sha256=digest,
        **core,
    )


def compile_single_portal_aperture(
    *,
    compiled_geometry: R120CompiledGeometry,
    region_authority: AcousticRegionAuthority,
    declaration: PortalDeclaration,
    tolerance_m: float,
) -> GeometricPortalAperture:
    """Backward-compatible exact two-region / one-Portal compiler."""

    if len(region_authority.declarations) != 2:
        raise ValueError('bounded Portal lane requires exactly two AcousticRegion declarations')
    if set(declaration.region_ids) != {
        item.region_id for item in region_authority.declarations
    }:
        raise ValueError('Portal adjacency does not exactly match the supported two-region topology')
    aperture = _compile_portal_aperture(
        compiled_geometry=compiled_geometry,
        region_authority=region_authority,
        declaration=declaration,
        tolerance_m=tolerance_m,
    )
    for region in region_authority.declarations:
        _validate_region_shell_manifold(
            compiled_geometry,
            region,
            (aperture,),
        )
    return aperture


def compile_portal_graph(
    *,
    compiled_geometry: R120CompiledGeometry,
    region_authority: AcousticRegionAuthority,
    portal_authority: PortalAuthority,
    tolerance_m: float,
    maximum_portal_crossings: int,
) -> tuple[tuple[GeometricPortalAperture, ...], GeometricPortalGraph]:
    """Compile exact Portal apertures plus bounded deterministic directed graph authority."""

    if len(region_authority.declarations) < 2:
        raise ValueError('Portal graph requires at least two AcousticRegion declarations')
    if portal_authority.declaration_mode != 'explicit_list' or not portal_authority.declarations:
        raise ValueError('Portal graph requires an explicit non-empty Portal authority')
    if maximum_portal_crossings < 1 or maximum_portal_crossings > 16:
        raise ValueError('maximum Portal crossings must be within the bounded range 1..16')

    portal_ids = [item.portal_id for item in portal_authority.declarations]
    if len(portal_ids) != len(set(portal_ids)):
        raise ValueError('Portal graph contains duplicate Portal identity')

    unordered_pairs: set[tuple[str, str]] = set()
    declared_surface_ids: set[str] = set()
    apertures: list[GeometricPortalAperture] = []
    for declaration in sorted(
        portal_authority.declarations,
        key=lambda item: item.portal_id,
    ):
        if len(declaration.region_ids) != 2:
            raise ValueError('Portal graph declaration must have exact directed two-region adjacency')
        pair = tuple(sorted(declaration.region_ids))
        if pair in unordered_pairs:
            raise ValueError(
                'Portal graph contains ambiguous parallel Portal adjacency between one region pair'
            )
        unordered_pairs.add(pair)
        aperture = _compile_portal_aperture(
            compiled_geometry=compiled_geometry,
            region_authority=region_authority,
            declaration=declaration,
            tolerance_m=tolerance_m,
        )
        overlap = declared_surface_ids.intersection(aperture.source_surface_ids)
        if overlap:
            raise ValueError(
                'Portal graph reuses semantic Portal surface identity across apertures: '
                f'{sorted(overlap)}'
            )
        declared_surface_ids.update(aperture.source_surface_ids)
        apertures.append(aperture)

    aperture_tuple = tuple(apertures)
    for region in region_authority.declarations:
        incident = tuple(
            aperture
            for aperture in aperture_tuple
            if region.region_id in (aperture.from_region_id, aperture.to_region_id)
        )
        _validate_region_shell_manifold(
            compiled_geometry,
            region,
            incident,
        )

    edges = tuple(
        GeometricPortalGraphEdge(
            portal_id=aperture.portal_id,
            aperture_id=aperture.aperture_id,
            aperture_sha256=aperture.semantic_sha256,
            from_region_id=aperture.from_region_id,
            to_region_id=aperture.to_region_id,
        )
        for aperture in aperture_tuple
    )
    core = {
        'authority_version': '1',
        'r120_compiled_geometry_id': compiled_geometry.compiled_geometry_id,
        'r120_compiled_geometry_sha256': compiled_geometry.compiled_hash_sha256,
        'region_authority_id': region_authority.authority_id,
        'region_authority_sha256': region_authority.semantic_hash_sha256,
        'portal_authority_id': portal_authority.authority_id,
        'portal_authority_sha256': portal_authority.semantic_hash_sha256,
        'region_ids': sorted(item.region_id for item in region_authority.declarations),
        'directed_edges': [item.model_dump(mode='json') for item in edges],
        'traversal_policy': 'simple_region_path_v1',
        'repeated_region_traversal': False,
        'repeated_portal_traversal': False,
        'deterministic_traversal_order': 'portal_id_lexicographic_v1',
        'maximum_portal_crossings': int(maximum_portal_crossings),
        'maximum_search_states': 4096,
    }
    digest = _semantic_hash(core)
    graph = GeometricPortalGraph(
        graph_id=f'r150-portal-graph:{digest}',
        semantic_sha256=digest,
        **core,
    )
    return aperture_tuple, graph


def enumerate_simple_directed_region_paths(
    graph: GeometricPortalGraph,
    *,
    source_region_id: str,
    receiver_region_id: str,
) -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    """Enumerate bounded simple directed paths in deterministic Portal-id order."""

    region_ids = set(graph.region_ids)
    if source_region_id not in region_ids or receiver_region_id not in region_ids:
        raise ValueError('Portal graph traversal endpoint references an unknown region')
    if source_region_id == receiver_region_id:
        return (((source_region_id,), ()),)

    outgoing: dict[str, list[GeometricPortalGraphEdge]] = {}
    for edge in graph.directed_edges:
        outgoing.setdefault(edge.from_region_id, []).append(edge)
    for edges in outgoing.values():
        edges.sort(key=lambda item: (item.portal_id, item.to_region_id))

    results: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    search_states = 0

    def visit(
        region_id: str,
        regions: tuple[str, ...],
        portals: tuple[str, ...],
    ) -> None:
        nonlocal search_states
        search_states += 1
        if search_states > graph.maximum_search_states:
            raise ValueError(
                'Portal graph simple-path search exceeded deterministic maximum_search_states'
            )
        if region_id == receiver_region_id:
            results.append((regions, portals))
            return
        if len(portals) >= graph.maximum_portal_crossings:
            return
        for edge in outgoing.get(region_id, ()):
            if edge.portal_id in portals:
                continue
            if edge.to_region_id in regions:
                continue
            visit(
                edge.to_region_id,
                regions + (edge.to_region_id,),
                portals + (edge.portal_id,),
            )

    visit(source_region_id, (source_region_id,), ())
    return tuple(results)


def directed_region_reachable(
    graph: GeometricPortalGraph,
    *,
    source_region_id: str,
    receiver_region_id: str,
) -> bool:
    """Bounded-state reachability ignoring crossing limit, still without repeated regions."""

    if source_region_id == receiver_region_id:
        return True
    outgoing: dict[str, tuple[str, ...]] = {}
    for region_id in graph.region_ids:
        outgoing[region_id] = tuple(
            edge.to_region_id
            for edge in sorted(
                (
                    edge
                    for edge in graph.directed_edges
                    if edge.from_region_id == region_id
                ),
                key=lambda item: (item.portal_id, item.to_region_id),
            )
        )
    frontier = [source_region_id]
    visited = {source_region_id}
    while frontier:
        current = frontier.pop(0)
        for nxt in outgoing.get(current, ()):
            if nxt == receiver_region_id:
                return True
            if nxt not in visited:
                visited.add(nxt)
                frontier.append(nxt)
    return False


def _triangle_points(
    compiled: R120CompiledGeometry,
    triangle_index: int,
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]:
    a, b, c = _triangle_vertex_indices(compiled, triangle_index)
    return (
        _compiled_vertex(compiled, a),
        _compiled_vertex(compiled, b),
        _compiled_vertex(compiled, c),
    )


def _point_on_triangle(
    point: Sequence[float],
    triangle: tuple[Sequence[float], Sequence[float], Sequence[float]],
    *,
    tolerance_m: float,
) -> bool:
    edge1 = _vector(triangle[0], triangle[1])
    edge2 = _vector(triangle[0], triangle[2])
    normal = _cross(edge1, edge2)
    normal_length = _norm(normal)
    if normal_length <= tolerance_m * tolerance_m:
        return False
    signed = _dot(_vector(triangle[0], point), normal) / normal_length
    if abs(signed) > tolerance_m:
        return False
    v0 = edge1
    v1 = edge2
    v2 = _vector(triangle[0], point)
    d00 = _dot(v0, v0)
    d01 = _dot(v0, v1)
    d11 = _dot(v1, v1)
    d20 = _dot(v2, v0)
    d21 = _dot(v2, v1)
    denom = d00 * d11 - d01 * d01
    if abs(denom) <= tolerance_m * tolerance_m:
        return False
    v = (d11 * d20 - d01 * d21) / denom
    w = (d00 * d21 - d01 * d20) / denom
    u = 1.0 - v - w
    scaled = min(0.25, tolerance_m / max(_norm(edge1), _norm(edge2), tolerance_m))
    return u >= -scaled and v >= -scaled and w >= -scaled


def _ray_triangle_parameter(
    start: Sequence[float],
    end: Sequence[float],
    triangle: tuple[Sequence[float], Sequence[float], Sequence[float]],
    *,
    tolerance_m: float,
) -> float | None:
    direction = _vector(start, end)
    edge1 = _vector(triangle[0], triangle[1])
    edge2 = _vector(triangle[0], triangle[2])
    pvec = _cross(direction, edge2)
    determinant = _dot(edge1, pvec)
    scale = max(_norm(direction) * _norm(edge1) * _norm(edge2), tolerance_m)
    relative = min(0.25, tolerance_m / max(_norm(direction), _norm(edge1), _norm(edge2), tolerance_m))
    if abs(determinant) <= scale * relative:
        return None
    inv = 1.0 / determinant
    tvec = _vector(triangle[0], start)
    u = _dot(tvec, pvec) * inv
    if u < -relative or u > 1.0 + relative:
        return None
    qvec = _cross(tvec, edge1)
    v = _dot(direction, qvec) * inv
    if v < -relative or u + v > 1.0 + relative:
        return None
    t = _dot(edge2, qvec) * inv
    endpoint = min(0.25, tolerance_m / max(_norm(direction), tolerance_m))
    if t <= endpoint or t >= 1.0 - endpoint:
        return None
    return t


def _portal_cap_triangles(
    aperture: GeometricPortalAperture,
) -> tuple[
    tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]],
    ...,
]:
    vertices = tuple(_position_tuple(item) for item in aperture.ordered_vertices_m)
    return tuple(
        (vertices[0], vertices[index], vertices[index + 1])
        for index in range(1, len(vertices) - 1)
    )


def _incident_portal_apertures(
    region: AcousticRegionDeclaration,
    apertures: Sequence[GeometricPortalAperture],
) -> tuple[GeometricPortalAperture, ...]:
    return tuple(
        aperture
        for aperture in apertures
        if region.region_id in (aperture.from_region_id, aperture.to_region_id)
    )


def _region_shell_triangles(
    compiled_geometry: R120CompiledGeometry,
    region: AcousticRegionDeclaration,
    apertures: Sequence[GeometricPortalAperture],
) -> tuple[
    tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]],
    ...,
]:
    surface_ids = set(region.boundary_surface_ids)
    return tuple(
        _triangle_points(compiled_geometry, index)
        for index, triangle in enumerate(compiled_geometry.triangles)
        if triangle.source_surface_id in surface_ids
    ) + tuple(
        triangle
        for aperture in _incident_portal_apertures(region, apertures)
        for triangle in _portal_cap_triangles(aperture)
    )


def _validate_region_shell_manifold(
    compiled_geometry: R120CompiledGeometry,
    region: AcousticRegionDeclaration,
    apertures: Sequence[GeometricPortalAperture],
) -> None:
    """Require each region shell, closed by all exact incident Portal caps, to be manifold."""

    surface_ids = set(region.boundary_surface_ids)
    edge_counts: dict[tuple[int, int], int] = {}
    for triangle in compiled_geometry.triangles:
        if triangle.source_surface_id not in surface_ids:
            continue
        for a, b in (
            (int(triangle.a), int(triangle.b)),
            (int(triangle.b), int(triangle.c)),
            (int(triangle.c), int(triangle.a)),
        ):
            edge = _normalized_edge(a, b)
            edge_counts[edge] = edge_counts.get(edge, 0) + 1

    for aperture in _incident_portal_apertures(region, apertures):
        loop = aperture.ordered_vertex_indices
        for index in range(1, len(loop) - 1):
            for a, b in (
                (loop[0], loop[index]),
                (loop[index], loop[index + 1]),
                (loop[index + 1], loop[0]),
            ):
                edge = _normalized_edge(int(a), int(b))
                edge_counts[edge] = edge_counts.get(edge, 0) + 1

    if not edge_counts or any(count != 2 for count in edge_counts.values()):
        raise ValueError(
            f'acoustic region {region.region_id} is not a closed manifold when '
            'all exact incident Portal apertures are capped'
        )


def region_membership_with_portal_caps(
    *,
    compiled_geometry: R120CompiledGeometry,
    region: AcousticRegionDeclaration,
    apertures: Sequence[GeometricPortalAperture],
    point: Sequence[float],
    tolerance_m: float,
) -> Literal['inside', 'outside', 'boundary', 'ambiguous']:
    triangles = _region_shell_triangles(
        compiled_geometry,
        region,
        apertures,
    )
    if not triangles:
        return 'ambiguous'
    if any(
        _point_on_triangle(point, triangle, tolerance_m=tolerance_m)
        for triangle in triangles
    ):
        return 'boundary'

    bounds = compiled_geometry.bounding_volume
    diagonal = sqrt(
        (bounds.max_x_m - bounds.min_x_m) ** 2
        + (bounds.max_y_m - bounds.min_y_m) ** 2
        + (bounds.max_z_m - bounds.min_z_m) ** 2
    )
    ray_length = max(1.0, diagonal * 4.0)
    directions = (
        _unit((1.0, 0.3713906763541037, 0.217031)),
        _unit((-0.419, 1.0, 0.163)),
        _unit((0.271, -0.337, 1.0)),
    )
    decisions: list[bool] = []
    t_tolerance = max(1.0e-12, tolerance_m / ray_length * 4.0)
    for direction in directions:
        end = tuple(
            float(point[index]) + ray_length * direction[index]
            for index in range(3)
        )
        hits = sorted(
            hit
            for triangle in triangles
            if (
                hit := _ray_triangle_parameter(
                    point,
                    end,
                    triangle,
                    tolerance_m=tolerance_m,
                )
            )
            is not None
        )
        distinct: list[float] = []
        for hit in hits:
            if not distinct or abs(hit - distinct[-1]) > t_tolerance:
                distinct.append(hit)
        decisions.append(len(distinct) % 2 == 1)
    if all(item == decisions[0] for item in decisions):
        return 'inside' if decisions[0] else 'outside'
    return 'ambiguous'


def region_membership_with_portal_cap(
    *,
    compiled_geometry: R120CompiledGeometry,
    region: AcousticRegionDeclaration,
    aperture: GeometricPortalAperture,
    point: Sequence[float],
    tolerance_m: float,
) -> Literal['inside', 'outside', 'boundary', 'ambiguous']:
    """Backward-compatible one-cap membership wrapper."""

    return region_membership_with_portal_caps(
        compiled_geometry=compiled_geometry,
        region=region,
        apertures=(aperture,),
        point=point,
        tolerance_m=tolerance_m,
    )


def region_segment_membership_with_portal_caps(
    *,
    compiled_geometry: R120CompiledGeometry,
    region: AcousticRegionDeclaration,
    apertures: Sequence[GeometricPortalAperture],
    start: Sequence[float],
    end: Sequence[float],
    tolerance_m: float,
) -> Literal['valid', 'invalid', 'ambiguous']:
    """Prove an open segment remains in one capped AcousticRegion shell."""

    segment_length = _norm(_vector(start, end))
    if segment_length <= tolerance_m:
        return 'invalid'
    start_membership = region_membership_with_portal_caps(
        compiled_geometry=compiled_geometry,
        region=region,
        apertures=apertures,
        point=start,
        tolerance_m=tolerance_m,
    )
    end_membership = region_membership_with_portal_caps(
        compiled_geometry=compiled_geometry,
        region=region,
        apertures=apertures,
        point=end,
        tolerance_m=tolerance_m,
    )
    if start_membership == 'ambiguous' or end_membership == 'ambiguous':
        return 'ambiguous'
    if start_membership not in ('inside', 'boundary') or end_membership not in (
        'inside',
        'boundary',
    ):
        return 'invalid'

    midpoint = tuple(
        (float(start[index]) + float(end[index])) * 0.5
        for index in range(3)
    )
    if (
        region_membership_with_portal_caps(
            compiled_geometry=compiled_geometry,
            region=region,
            apertures=apertures,
            point=midpoint,
            tolerance_m=tolerance_m,
        )
        != 'inside'
    ):
        return 'invalid'

    triangles = _region_shell_triangles(
        compiled_geometry,
        region,
        apertures,
    )
    if any(
        _ray_triangle_parameter(
            start,
            end,
            triangle,
            tolerance_m=tolerance_m,
        )
        is not None
        for triangle in triangles
    ):
        return 'invalid'
    return 'valid'


def _project_axis(normal: Sequence[float]) -> int:
    values = [abs(float(value)) for value in normal]
    return values.index(max(values))


def _project(point: Sequence[float], drop_axis: int) -> tuple[float, float]:
    return tuple(
        float(point[index]) for index in range(3) if index != drop_axis
    )  # type: ignore[return-value]


def _point_on_segment_2d(
    point: Sequence[float],
    a: Sequence[float],
    b: Sequence[float],
    *,
    tolerance_m: float,
) -> bool:
    ab = (float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))
    ap = (float(point[0]) - float(a[0]), float(point[1]) - float(a[1]))
    cross = ab[0] * ap[1] - ab[1] * ap[0]
    length = sqrt(ab[0] * ab[0] + ab[1] * ab[1])
    if length <= tolerance_m or abs(cross) / length > tolerance_m:
        return False
    dot = ap[0] * ab[0] + ap[1] * ab[1]
    return -tolerance_m <= dot <= length * length + tolerance_m


def point_in_portal_aperture(
    aperture: GeometricPortalAperture,
    point: Sequence[float],
    *,
    tolerance_m: float,
) -> bool:
    normal = _direction_tuple(aperture.normal_from_to)
    plane_point = _position_tuple(aperture.plane_point_m)
    if abs(_dot(_vector(plane_point, point), normal)) > tolerance_m:
        return False
    axis = _project_axis(normal)
    polygon = [_project(_position_tuple(item), axis) for item in aperture.ordered_vertices_m]
    projected = _project(point, axis)
    for index, current in enumerate(polygon):
        nxt = polygon[(index + 1) % len(polygon)]
        if _point_on_segment_2d(projected, current, nxt, tolerance_m=tolerance_m):
            return True
    inside = False
    x, y = projected
    for index, current in enumerate(polygon):
        nxt = polygon[(index + 1) % len(polygon)]
        x1, y1 = current
        x2, y2 = nxt
        if (y1 > y) == (y2 > y):
            continue
        x_hit = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
        if x_hit > x:
            inside = not inside
    return inside


def resolve_direct_portal_crossing(
    aperture: GeometricPortalAperture,
    *,
    start: Sequence[float],
    end: Sequence[float],
    from_region_id: str,
    to_region_id: str,
    tolerance_m: float,
) -> tuple[float, float, float] | None:
    if (
        from_region_id != aperture.from_region_id
        or to_region_id != aperture.to_region_id
    ):
        return None
    normal = _direction_tuple(aperture.normal_from_to)
    plane_point = _position_tuple(aperture.plane_point_m)
    start_side = _dot(_vector(plane_point, start), normal)
    end_side = _dot(_vector(plane_point, end), normal)
    if start_side >= -tolerance_m or end_side <= tolerance_m:
        return None
    direction = _vector(start, end)
    denominator = _dot(direction, normal)
    if denominator <= tolerance_m:
        return None
    t = -start_side / denominator
    segment_length = _norm(direction)
    endpoint_tolerance = min(0.25, tolerance_m / max(segment_length, tolerance_m))
    if t <= endpoint_tolerance or t >= 1.0 - endpoint_tolerance:
        return None
    point = tuple(float(start[index]) + t * direction[index] for index in range(3))
    if not point_in_portal_aperture(aperture, point, tolerance_m=tolerance_m):
        return None
    return point  # type: ignore[return-value]
