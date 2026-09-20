from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
import json
from math import isfinite, sqrt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


R120_POLYHEDRAL_COMPILER_ID = 'htdt.r120.polyhedral_geometry_compiler'
R120_POLYHEDRAL_COMPILER_VERSION = '1'

SurfaceSemanticClass = Literal['room_boundary', 'object_surface', 'unknown']
ApproximationStatus = Literal['exact_planar', 'bounded_planar_tessellation']
RepresentationState = Literal['READY', 'UNSUPPORTED']
TopologySeverity = Literal['info', 'error']


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _semantic_hash(payload: object) -> str:
    return sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


class R120PolyhedralGeometryError(ValueError):
    pass


class PolyhedralAuthorityRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    authority_id: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class PolyhedralVertex(BaseModel):
    model_config = ConfigDict(frozen=True)

    x_m: float
    y_m: float
    z_m: float

    @field_validator('x_m', 'y_m', 'z_m')
    @classmethod
    def finite_coordinate(cls, value: float) -> float:
        value = float(value)
        if not isfinite(value):
            raise ValueError('polyhedral vertex coordinates must be finite')
        return value

    def point(self) -> tuple[float, float, float]:
        return (self.x_m, self.y_m, self.z_m)


class PlanarPolygonSurfaceSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    surface_key: str = Field(min_length=1)
    semantic_class: SurfaceSemanticClass = 'room_boundary'
    outer_vertex_indices: tuple[int, ...]
    hole_vertex_indices: tuple[tuple[int, ...], ...] = ()
    material_authority: PolyhedralAuthorityRef | None = None

    @model_validator(mode='after')
    def validate_loops(self) -> 'PlanarPolygonSurfaceSpec':
        _validate_index_loop(self.outer_vertex_indices, 'outer polygon')
        for hole in self.hole_vertex_indices:
            _validate_index_loop(hole, 'polygon hole')
        return self


class PlanarPolygonSurface(BaseModel):
    model_config = ConfigDict(frozen=True)

    surface_id: str = Field(pattern=r'^r120-polyhedral-surface:[0-9a-f]{64}$')
    surface_key: str = Field(min_length=1)
    semantic_class: SurfaceSemanticClass
    outer_vertex_indices: tuple[int, ...]
    hole_vertex_indices: tuple[tuple[int, ...], ...] = ()
    material_authority: PolyhedralAuthorityRef | None = None

    @model_validator(mode='after')
    def validate_surface_membership(self) -> 'PlanarPolygonSurface':
        _validate_index_loop(self.outer_vertex_indices, 'outer polygon')
        for hole in self.hole_vertex_indices:
            _validate_index_loop(hole, 'polygon hole')
        return self


class PolyhedralAirVolume(BaseModel):
    model_config = ConfigDict(frozen=True)

    region_id: str = Field(min_length=1)
    boundary_surface_keys: tuple[str, ...]

    @model_validator(mode='after')
    def validate_membership(self) -> 'PolyhedralAirVolume':
        if not self.boundary_surface_keys:
            raise ValueError('polyhedral air volume requires boundary surfaces')
        if len(self.boundary_surface_keys) != len(set(self.boundary_surface_keys)):
            raise ValueError('polyhedral air volume boundary surfaces must be unique')
        return self


class PolyhedralPortal(BaseModel):
    model_config = ConfigDict(frozen=True)

    portal_id: str = Field(min_length=1)
    region_ids: tuple[str, ...]
    surface_keys: tuple[str, ...]

    @model_validator(mode='after')
    def validate_portal(self) -> 'PolyhedralPortal':
        if len(self.region_ids) not in (1, 2):
            raise ValueError('polyhedral portal must bind one or two regions')
        if len(self.region_ids) != len(set(self.region_ids)):
            raise ValueError('polyhedral portal region ids must be unique')
        if not self.surface_keys:
            raise ValueError('polyhedral portal requires at least one explicit surface')
        if len(self.surface_keys) != len(set(self.surface_keys)):
            raise ValueError('polyhedral portal surfaces must be unique')
        return self


class GeometryApproximationAuthority(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_geometry_identity: str = Field(min_length=1)
    tolerance_m: float = Field(gt=0)
    maximum_deviation_m: float = Field(ge=0)
    generated_surface_keys: tuple[str, ...]
    generated_surface_count: int = Field(ge=1)
    algorithm_id: str = Field(min_length=1)
    algorithm_version: str = Field(min_length=1)
    approximation_status: ApproximationStatus

    @field_validator('tolerance_m', 'maximum_deviation_m')
    @classmethod
    def finite_distance(cls, value: float) -> float:
        value = float(value)
        if not isfinite(value):
            raise ValueError('approximation distances must be finite')
        return value

    @model_validator(mode='after')
    def validate_approximation(self) -> 'GeometryApproximationAuthority':
        if self.generated_surface_count != len(self.generated_surface_keys):
            raise ValueError('generated surface count must match generated surface keys')
        if len(self.generated_surface_keys) != len(set(self.generated_surface_keys)):
            raise ValueError('generated surface keys must be unique')
        if (
            self.approximation_status == 'bounded_planar_tessellation'
            and self.maximum_deviation_m > self.tolerance_m
        ):
            raise ValueError('bounded planar tessellation exceeds declared tolerance')
        if (
            self.approximation_status == 'exact_planar'
            and self.maximum_deviation_m != 0.0
        ):
            raise ValueError('exact planar approximation must have zero deviation')
        return self


class R120PolyhedralSemanticGeometry(BaseModel):
    """Explicit solver-neutral semantic geometry; never a visual-mesh promotion shortcut."""

    model_config = ConfigDict(frozen=True)

    geometry_id: str = Field(pattern=r'^r120-polyhedral-semantic-geometry:[0-9a-f]{64}$')
    semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    authority_version: Literal['1'] = '1'
    source_geometry_identity: str = Field(min_length=1)
    source_geometry_kind: Literal['explicit_polyhedral', 'bounded_planar_tessellation']
    vertices: tuple[PolyhedralVertex, ...]
    surfaces: tuple[PlanarPolygonSurface, ...]
    air_volumes: tuple[PolyhedralAirVolume, ...]
    portals: tuple[PolyhedralPortal, ...] = ()
    approximation_authority: tuple[GeometryApproximationAuthority, ...] = ()

    @model_validator(mode='after')
    def validate_identity_and_canonical_order(self) -> 'R120PolyhedralSemanticGeometry':
        if not self.vertices:
            raise ValueError('polyhedral semantic geometry requires vertices')
        if not self.surfaces:
            raise ValueError('polyhedral semantic geometry requires surfaces')
        if not self.air_volumes:
            raise ValueError('polyhedral semantic geometry requires at least one air volume')

        vertex_keys = [vertex.point() for vertex in self.vertices]
        if vertex_keys != sorted(vertex_keys):
            raise ValueError('polyhedral vertices must use canonical lexicographic order')
        if len(vertex_keys) != len(set(vertex_keys)):
            raise ValueError('duplicate coincident vertices are not canonical authority')

        surface_keys = [surface.surface_key for surface in self.surfaces]
        if surface_keys != sorted(surface_keys) or len(surface_keys) != len(set(surface_keys)):
            raise ValueError('polyhedral surfaces must have unique canonical surface-key order')
        volume_ids = [volume.region_id for volume in self.air_volumes]
        if volume_ids != sorted(volume_ids) or len(volume_ids) != len(set(volume_ids)):
            raise ValueError('air volumes must have unique canonical region-id order')
        portal_ids = [portal.portal_id for portal in self.portals]
        if portal_ids != sorted(portal_ids) or len(portal_ids) != len(set(portal_ids)):
            raise ValueError('portals must have unique canonical portal-id order')

        for surface in self.surfaces:
            expected_surface_id = _surface_id(
                surface.surface_key,
                surface.outer_vertex_indices,
                surface.hole_vertex_indices,
                self.vertices,
            )
            if surface.surface_id != expected_surface_id:
                raise ValueError('polyhedral surface identity mismatch')
            if surface.outer_vertex_indices != _canonical_directed_loop(
                surface.outer_vertex_indices
            ):
                raise ValueError('surface outer loop is not canonically rotated')
            if tuple(sorted(surface.hole_vertex_indices)) != surface.hole_vertex_indices:
                raise ValueError('surface holes must be canonically ordered')
            for hole in surface.hole_vertex_indices:
                if hole != _canonical_directed_loop(hole):
                    raise ValueError('surface hole loop is not canonically rotated')
            for index in (*surface.outer_vertex_indices, *sum(surface.hole_vertex_indices, ())):
                if index >= len(self.vertices):
                    raise ValueError('surface references vertex outside canonical vertex array')

        known_surfaces = set(surface_keys)
        for volume in self.air_volumes:
            if tuple(sorted(volume.boundary_surface_keys)) != volume.boundary_surface_keys:
                raise ValueError('air-volume surface keys must be canonical')
            if not set(volume.boundary_surface_keys) <= known_surfaces:
                raise ValueError('air volume references unknown surface')
        known_regions = set(volume_ids)
        for portal in self.portals:
            if tuple(sorted(portal.region_ids)) != portal.region_ids:
                raise ValueError('portal region ids must be canonical')
            if tuple(sorted(portal.surface_keys)) != portal.surface_keys:
                raise ValueError('portal surface keys must be canonical')
            if not set(portal.region_ids) <= known_regions:
                raise ValueError('portal references unknown region')
            if not set(portal.surface_keys) <= known_surfaces:
                raise ValueError('portal references unknown surface')

        approximation_order = [
            (
                item.source_geometry_identity,
                item.algorithm_id,
                item.algorithm_version,
                item.generated_surface_keys,
            )
            for item in self.approximation_authority
        ]
        if approximation_order != sorted(approximation_order):
            raise ValueError('approximation authority must use canonical order')
        for item in self.approximation_authority:
            if not set(item.generated_surface_keys) <= known_surfaces:
                raise ValueError('approximation authority references unknown generated surface')

        core = self.model_dump(mode='json', exclude={'geometry_id', 'semantic_hash_sha256'})
        expected = _semantic_hash(core)
        if self.semantic_hash_sha256 != expected:
            raise ValueError('polyhedral semantic geometry hash mismatch')
        if self.geometry_id != f'r120-polyhedral-semantic-geometry:{expected}':
            raise ValueError('polyhedral semantic geometry id mismatch')
        return self


class TopologyFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1)
    severity: TopologySeverity
    message: str = Field(min_length=1)
    surface_keys: tuple[str, ...] = ()
    region_ids: tuple[str, ...] = ()


class R120PolyhedralTopologyReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    report_id: str = Field(pattern=r'^r120-polyhedral-topology:[0-9a-f]{64}$')
    report_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    authority_version: Literal['1'] = '1'
    exact_geometry_id: str = Field(pattern=r'^r120-polyhedral-semantic-geometry:[0-9a-f]{64}$')
    exact_geometry_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    topology_tolerance_m: float = Field(gt=0)
    findings: tuple[TopologyFinding, ...]
    closed_volume_region_ids: tuple[str, ...]
    invalid_region_ids: tuple[str, ...]
    valid: bool

    @model_validator(mode='after')
    def validate_identity(self) -> 'R120PolyhedralTopologyReport':
        if self.valid != all(item.severity != 'error' for item in self.findings):
            raise ValueError('topology report valid flag does not match findings')
        core = self.model_dump(mode='json', exclude={'report_id', 'report_hash_sha256'})
        expected = _semantic_hash(core)
        if self.report_hash_sha256 != expected:
            raise ValueError('polyhedral topology report hash mismatch')
        if self.report_id != f'r120-polyhedral-topology:{expected}':
            raise ValueError('polyhedral topology report id mismatch')
        return self


class PolyhedralCompiledTriangle(BaseModel):
    model_config = ConfigDict(frozen=True)

    a: int = Field(ge=0)
    b: int = Field(ge=0)
    c: int = Field(ge=0)
    source_surface_id: str = Field(pattern=r'^r120-polyhedral-surface:[0-9a-f]{64}$')
    source_surface_key: str = Field(min_length=1)

    @model_validator(mode='after')
    def distinct_vertices(self) -> 'PolyhedralCompiledTriangle':
        if len({self.a, self.b, self.c}) != 3:
            raise ValueError('compiled polyhedral triangle indices must be distinct')
        return self


class PolyhedralCompiledSurfaceMapping(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_surface_id: str = Field(pattern=r'^r120-polyhedral-surface:[0-9a-f]{64}$')
    source_surface_key: str = Field(min_length=1)
    material_authority: PolyhedralAuthorityRef
    compiled_triangle_indices: tuple[int, ...]
    holes_present: bool


class RegionVolumeEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    region_id: str = Field(min_length=1)
    enclosed_volume_m3: float | None = Field(default=None, ge=0)


class R120PolyhedralRepresentationReadiness(BaseModel):
    model_config = ConfigDict(frozen=True)

    wave_representation: RepresentationState
    ga_representation: RepresentationState
    wave_unsupported_reasons: tuple[str, ...] = ()
    ga_unsupported_reasons: tuple[str, ...] = ()
    wave_numerical_validation_status: Literal['NOT_VALIDATED'] = 'NOT_VALIDATED'


class R120PolyhedralCompiledGeometry(BaseModel):
    model_config = ConfigDict(frozen=True)

    compiled_geometry_id: str = Field(pattern=r'^r120-polyhedral-compiled:[0-9a-f]{64}$')
    compiled_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    authority_version: Literal['1'] = '1'
    compiler_id: Literal['htdt.r120.polyhedral_geometry_compiler'] = R120_POLYHEDRAL_COMPILER_ID
    compiler_version: Literal['1'] = R120_POLYHEDRAL_COMPILER_VERSION
    exact_semantic_geometry_id: str = Field(
        pattern=r'^r120-polyhedral-semantic-geometry:[0-9a-f]{64}$'
    )
    exact_semantic_geometry_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    exact_topology_report_id: str = Field(pattern=r'^r120-polyhedral-topology:[0-9a-f]{64}$')
    exact_topology_report_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    vertices: tuple[PolyhedralVertex, ...]
    triangles: tuple[PolyhedralCompiledTriangle, ...]
    surface_mapping: tuple[PolyhedralCompiledSurfaceMapping, ...]
    topology_identity_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    region_volume_evidence: tuple[RegionVolumeEvidence, ...]
    approximation_authority: tuple[GeometryApproximationAuthority, ...]
    readiness: R120PolyhedralRepresentationReadiness

    @model_validator(mode='after')
    def validate_identity(self) -> 'R120PolyhedralCompiledGeometry':
        expected_topology = _compiled_topology_identity(
            self.vertices,
            self.triangles,
            self.surface_mapping,
        )
        if self.topology_identity_sha256 != expected_topology:
            raise ValueError('polyhedral compiled topology identity mismatch')
        core = self.model_dump(mode='json', exclude={'compiled_geometry_id', 'compiled_hash_sha256'})
        expected = _semantic_hash(core)
        if self.compiled_hash_sha256 != expected:
            raise ValueError('polyhedral compiled geometry hash mismatch')
        if self.compiled_geometry_id != f'r120-polyhedral-compiled:{expected}':
            raise ValueError('polyhedral compiled geometry id mismatch')
        return self


def make_r120_polyhedral_semantic_geometry(
    *,
    source_geometry_identity: str,
    source_geometry_kind: Literal['explicit_polyhedral', 'bounded_planar_tessellation'],
    vertices: tuple[PolyhedralVertex | tuple[float, float, float], ...],
    surfaces: tuple[PlanarPolygonSurfaceSpec, ...],
    air_volumes: tuple[PolyhedralAirVolume, ...],
    portals: tuple[PolyhedralPortal, ...] = (),
    approximation_authority: tuple[GeometryApproximationAuthority, ...] = (),
) -> R120PolyhedralSemanticGeometry:
    if not vertices:
        raise R120PolyhedralGeometryError('polyhedral geometry requires vertices')
    canonical_input_vertices = tuple(
        item if isinstance(item, PolyhedralVertex) else PolyhedralVertex(
            x_m=item[0], y_m=item[1], z_m=item[2]
        )
        for item in vertices
    )
    coordinates = [item.point() for item in canonical_input_vertices]
    if len(coordinates) != len(set(coordinates)):
        raise R120PolyhedralGeometryError(
            'duplicate coincident vertices are rejected to preserve topology identity'
        )
    ordered_old_indices = sorted(range(len(coordinates)), key=lambda index: coordinates[index])
    old_to_new = {old: new for new, old in enumerate(ordered_old_indices)}
    canonical_vertices = tuple(canonical_input_vertices[index] for index in ordered_old_indices)

    surface_keys = [item.surface_key for item in surfaces]
    if len(surface_keys) != len(set(surface_keys)):
        raise R120PolyhedralGeometryError('polyhedral surface keys must be unique')

    canonical_surfaces: list[PlanarPolygonSurface] = []
    for spec in surfaces:
        outer = _canonical_directed_loop(
            tuple(old_to_new[index] for index in spec.outer_vertex_indices)
        )
        holes = tuple(
            sorted(
                _canonical_directed_loop(tuple(old_to_new[index] for index in hole))
                for hole in spec.hole_vertex_indices
            )
        )
        surface_id = _surface_id(
            spec.surface_key,
            outer,
            holes,
            canonical_vertices,
        )
        canonical_surfaces.append(
            PlanarPolygonSurface(
                surface_id=surface_id,
                surface_key=spec.surface_key,
                semantic_class=spec.semantic_class,
                outer_vertex_indices=outer,
                hole_vertex_indices=holes,
                material_authority=spec.material_authority,
            )
        )
    canonical_surfaces.sort(key=lambda item: item.surface_key)

    canonical_volumes = tuple(
        sorted(
            (
                PolyhedralAirVolume(
                    region_id=item.region_id,
                    boundary_surface_keys=tuple(sorted(item.boundary_surface_keys)),
                )
                for item in air_volumes
            ),
            key=lambda item: item.region_id,
        )
    )
    canonical_portals = tuple(
        sorted(
            (
                PolyhedralPortal(
                    portal_id=item.portal_id,
                    region_ids=tuple(sorted(item.region_ids)),
                    surface_keys=tuple(sorted(item.surface_keys)),
                )
                for item in portals
            ),
            key=lambda item: item.portal_id,
        )
    )
    normalized_approximations = tuple(
        GeometryApproximationAuthority(
            source_geometry_identity=item.source_geometry_identity,
            tolerance_m=item.tolerance_m,
            maximum_deviation_m=item.maximum_deviation_m,
            generated_surface_keys=tuple(sorted(item.generated_surface_keys)),
            generated_surface_count=item.generated_surface_count,
            algorithm_id=item.algorithm_id,
            algorithm_version=item.algorithm_version,
            approximation_status=item.approximation_status,
        )
        for item in approximation_authority
    )
    canonical_approximations = tuple(
        sorted(
            normalized_approximations,
            key=lambda item: (
                item.source_geometry_identity,
                item.algorithm_id,
                item.algorithm_version,
                item.generated_surface_keys,
            ),
        )
    )

    core = {
        'authority_version': '1',
        'source_geometry_identity': source_geometry_identity,
        'source_geometry_kind': source_geometry_kind,
        'vertices': canonical_vertices,
        'surfaces': tuple(canonical_surfaces),
        'air_volumes': canonical_volumes,
        'portals': canonical_portals,
        'approximation_authority': canonical_approximations,
    }
    digest = _semantic_hash(_jsonable(core))
    return R120PolyhedralSemanticGeometry(
        geometry_id=f'r120-polyhedral-semantic-geometry:{digest}',
        semantic_hash_sha256=digest,
        **core,
    )


def validate_r120_polyhedral_topology(
    geometry: R120PolyhedralSemanticGeometry,
    *,
    tolerance_m: float = 1.0e-9,
) -> R120PolyhedralTopologyReport:
    tolerance_m = float(tolerance_m)
    if not isfinite(tolerance_m) or tolerance_m <= 0:
        raise ValueError('topology tolerance must be positive and finite')

    findings: list[TopologyFinding] = []
    surface_by_key = {surface.surface_key: surface for surface in geometry.surfaces}
    surface_geometry: dict[str, tuple[tuple[float, float, float], tuple[tuple[float, float], ...], int]] = {}
    duplicate_signatures: dict[tuple[object, ...], str] = {}

    for surface in geometry.surfaces:
        if surface.material_authority is None:
            findings.append(
                TopologyFinding(
                    code='missing_material_reference',
                    severity='error',
                    message='Acoustic surface has no explicit material authority reference.',
                    surface_keys=(surface.surface_key,),
                )
            )
        indices = surface.outer_vertex_indices
        points = tuple(geometry.vertices[index].point() for index in indices)
        plane = _plane_for_points(points, tolerance_m)
        if plane is None:
            findings.append(
                TopologyFinding(
                    code='zero_area_face',
                    severity='error',
                    message='Surface has no stable non-collinear plane.',
                    surface_keys=(surface.surface_key,),
                )
            )
            continue
        origin, normal = plane
        all_indices = (
            surface.outer_vertex_indices,
            *surface.hole_vertex_indices,
        )
        nonplanar = False
        for loop in all_indices:
            for index in loop:
                distance = abs(_dot3(_sub3(geometry.vertices[index].point(), origin), normal))
                if distance > tolerance_m:
                    nonplanar = True
        if nonplanar:
            findings.append(
                TopologyFinding(
                    code='non_planar_surface',
                    severity='error',
                    message='Surface vertices exceed the declared topology planarity tolerance.',
                    surface_keys=(surface.surface_key,),
                )
            )
            continue

        drop_axis = _dominant_axis(normal)
        outer_2d = tuple(_project_point(point, drop_axis) for point in points)
        if _loop_self_intersects(outer_2d, tolerance_m):
            findings.append(
                TopologyFinding(
                    code='surface_self_intersection',
                    severity='error',
                    message='Surface outer polygon self-intersects.',
                    surface_keys=(surface.surface_key,),
                )
            )
        outer_area = _signed_area_2d(outer_2d)
        holes_2d: list[tuple[tuple[float, float], ...]] = []
        net_area = abs(outer_area)
        invalid_hole = False
        for hole in surface.hole_vertex_indices:
            hole_points = tuple(geometry.vertices[index].point() for index in hole)
            projected = tuple(_project_point(point, drop_axis) for point in hole_points)
            holes_2d.append(projected)
            hole_area = _signed_area_2d(projected)
            if abs(hole_area) <= tolerance_m * tolerance_m:
                invalid_hole = True
            if _loop_self_intersects(projected, tolerance_m):
                invalid_hole = True
            if outer_area * hole_area >= 0:
                invalid_hole = True
            if not _point_in_polygon_2d(projected[0], outer_2d, tolerance_m):
                invalid_hole = True
            if _loops_intersect(projected, outer_2d, tolerance_m):
                invalid_hole = True
            net_area -= abs(hole_area)
        for left_index, left in enumerate(holes_2d):
            for right in holes_2d[left_index + 1:]:
                if _loops_intersect(left, right, tolerance_m):
                    invalid_hole = True
                elif _point_in_polygon_2d(left[0], right, tolerance_m):
                    invalid_hole = True
                elif _point_in_polygon_2d(right[0], left, tolerance_m):
                    invalid_hole = True
        if invalid_hole:
            findings.append(
                TopologyFinding(
                    code='invalid_hole',
                    severity='error',
                    message='Polygon hole is degenerate, outside the outer loop, intersecting, or has invalid orientation.',
                    surface_keys=(surface.surface_key,),
                )
            )
        if net_area <= tolerance_m * tolerance_m:
            findings.append(
                TopologyFinding(
                    code='zero_area_face',
                    severity='error',
                    message='Surface has zero or negative net polygon area.',
                    surface_keys=(surface.surface_key,),
                )
            )

        signature = _face_signature(surface)
        prior = duplicate_signatures.get(signature)
        if prior is not None:
            findings.append(
                TopologyFinding(
                    code='duplicate_face',
                    severity='error',
                    message='Two semantic surfaces describe the same polygon face.',
                    surface_keys=tuple(sorted((prior, surface.surface_key))),
                )
            )
        else:
            duplicate_signatures[signature] = surface.surface_key
        surface_geometry[surface.surface_key] = (normal, outer_2d, drop_axis)

    closed_regions: list[str] = []
    invalid_regions: list[str] = []
    volume_triangles: dict[str, tuple[tuple[int, int, int], ...]] = {}

    for volume in geometry.air_volumes:
        region_findings_before = len([item for item in findings if item.severity == 'error'])
        edge_uses: dict[tuple[int, int], list[tuple[str, int, int]]] = defaultdict(list)
        region_triangles: list[tuple[int, int, int]] = []
        triangulation_failed = False

        for surface_key in volume.boundary_surface_keys:
            surface = surface_by_key[surface_key]
            for loop in (surface.outer_vertex_indices, *surface.hole_vertex_indices):
                for index, start in enumerate(loop):
                    end = loop[(index + 1) % len(loop)]
                    edge_uses[_normalized_edge(start, end)].append(
                        (surface_key, start, end)
                    )
            if surface.hole_vertex_indices:
                triangulation_failed = True
                continue
            try:
                region_triangles.extend(
                    _triangulate_surface_indices(geometry.vertices, surface, tolerance_m)
                )
            except R120PolyhedralGeometryError:
                triangulation_failed = True

        open_edges = [edge for edge, uses in edge_uses.items() if len(uses) == 1]
        non_manifold_edges = [edge for edge, uses in edge_uses.items() if len(uses) > 2]
        same_orientation_edges = [
            edge
            for edge, uses in edge_uses.items()
            if len(uses) == 2
            and uses[0][1] == uses[1][1]
            and uses[0][2] == uses[1][2]
        ]
        if open_edges:
            findings.append(
                TopologyFinding(
                    code='open_volume',
                    severity='error',
                    message=f'Closed air volume has {len(open_edges)} boundary edge(s) used only once.',
                    region_ids=(volume.region_id,),
                )
            )
        if non_manifold_edges:
            findings.append(
                TopologyFinding(
                    code='non_manifold_edge',
                    severity='error',
                    message=f'Air volume has {len(non_manifold_edges)} edge(s) used by more than two faces.',
                    region_ids=(volume.region_id,),
                )
            )
        if same_orientation_edges:
            findings.append(
                TopologyFinding(
                    code='surface_orientation_inconsistent',
                    severity='error',
                    message=f'Air volume has {len(same_orientation_edges)} shared edge(s) with identical directed orientation.',
                    region_ids=(volume.region_id,),
                )
            )
        if triangulation_failed:
            if any(surface_by_key[key].hole_vertex_indices for key in volume.boundary_surface_keys):
                findings.append(
                    TopologyFinding(
                        code='hole_triangulation_not_in_topology_gate',
                        severity='info',
                        message='Valid holes are topology-authorized but indexed triangle compilation is separately unsupported.',
                        region_ids=(volume.region_id,),
                    )
                )
            else:
                findings.append(
                    TopologyFinding(
                        code='polygon_triangulation_failed',
                        severity='error',
                        message='A planar polygon could not be deterministically triangulated.',
                        region_ids=(volume.region_id,),
                    )
                )
        if not triangulation_failed and not open_edges and not non_manifold_edges and not same_orientation_edges:
            signed_volume = _signed_volume_from_triangles(geometry.vertices, tuple(region_triangles))
            if signed_volume <= tolerance_m ** 3:
                findings.append(
                    TopologyFinding(
                        code='volume_orientation_inward_or_degenerate',
                        severity='error',
                        message='Closed region must use consistent outward surface orientation and positive enclosed volume.',
                        region_ids=(volume.region_id,),
                    )
                )
            else:
                volume_triangles[volume.region_id] = tuple(region_triangles)
                if _volume_has_detectable_self_intersection(
                    geometry.vertices,
                    tuple(region_triangles),
                    tolerance_m,
                ):
                    findings.append(
                        TopologyFinding(
                            code='volume_self_intersection',
                            severity='error',
                            message='Non-adjacent boundary triangles intersect.',
                            region_ids=(volume.region_id,),
                        )
                    )

        region_findings_after = len([item for item in findings if item.severity == 'error'])
        if region_findings_after == region_findings_before:
            closed_regions.append(volume.region_id)
        else:
            invalid_regions.append(volume.region_id)

    region_lookup = {volume.region_id: volume for volume in geometry.air_volumes}
    for portal in geometry.portals:
        bad = False
        for region_id in portal.region_ids:
            volume = region_lookup.get(region_id)
            if volume is None or not set(portal.surface_keys) <= set(volume.boundary_surface_keys):
                bad = True
        if len(portal.region_ids) == 2:
            left = set(region_lookup[portal.region_ids[0]].boundary_surface_keys)
            right = set(region_lookup[portal.region_ids[1]].boundary_surface_keys)
            if not set(portal.surface_keys) <= left.intersection(right):
                bad = True
        if bad:
            findings.append(
                TopologyFinding(
                    code='invalid_portal_surface_relationship',
                    severity='error',
                    message='Portal surfaces must be explicit boundary surfaces of every declared adjacent region.',
                    surface_keys=portal.surface_keys,
                    region_ids=portal.region_ids,
                )
            )

    valid_region_ids = set(volume_triangles)
    for left_index, left in enumerate(geometry.air_volumes):
        if left.region_id not in valid_region_ids:
            continue
        for right in geometry.air_volumes[left_index + 1:]:
            if right.region_id not in valid_region_ids:
                continue
            same_closed_boundary = (
                set(left.boundary_surface_keys) == set(right.boundary_surface_keys)
            )
            if same_closed_boundary or _volumes_overlap_detectably(
                geometry.vertices,
                volume_triangles[left.region_id],
                volume_triangles[right.region_id],
                tolerance_m,
            ):
                findings.append(
                    TopologyFinding(
                        code='region_overlap',
                        severity='error',
                        message='Closed air volumes overlap in their interior or cross each other.',
                        region_ids=(left.region_id, right.region_id),
                    )
                )
                invalid_regions.extend((left.region_id, right.region_id))

    findings = sorted(
        findings,
        key=lambda item: (
            item.severity,
            item.code,
            item.region_ids,
            item.surface_keys,
            item.message,
        ),
    )
    invalid_region_ids = tuple(sorted(set(invalid_regions)))
    closed_volume_region_ids = tuple(
        sorted(region_id for region_id in closed_regions if region_id not in invalid_region_ids)
    )
    valid = all(item.severity != 'error' for item in findings)
    core = {
        'authority_version': '1',
        'exact_geometry_id': geometry.geometry_id,
        'exact_geometry_hash_sha256': geometry.semantic_hash_sha256,
        'topology_tolerance_m': tolerance_m,
        'findings': tuple(findings),
        'closed_volume_region_ids': closed_volume_region_ids,
        'invalid_region_ids': invalid_region_ids,
        'valid': valid,
    }
    digest = _semantic_hash(_jsonable(core))
    return R120PolyhedralTopologyReport(
        report_id=f'r120-polyhedral-topology:{digest}',
        report_hash_sha256=digest,
        **core,
    )


def compile_r120_polyhedral_geometry(
    geometry: R120PolyhedralSemanticGeometry,
    *,
    topology_tolerance_m: float = 1.0e-9,
) -> R120PolyhedralCompiledGeometry:
    report = validate_r120_polyhedral_topology(
        geometry,
        tolerance_m=topology_tolerance_m,
    )
    if not report.valid:
        codes = tuple(sorted({item.code for item in report.findings if item.severity == 'error'}))
        raise R120PolyhedralGeometryError(
            f'polyhedral topology validation failed closed: {codes}'
        )

    triangles: list[PolyhedralCompiledTriangle] = []
    mappings: list[PolyhedralCompiledSurfaceMapping] = []
    surface_by_key = {surface.surface_key: surface for surface in geometry.surfaces}
    holes_present = False
    for surface in geometry.surfaces:
        start = len(triangles)
        if surface.hole_vertex_indices:
            holes_present = True
            generated: tuple[tuple[int, int, int], ...] = ()
        else:
            generated = _triangulate_surface_indices(
                geometry.vertices,
                surface,
                topology_tolerance_m,
            )
        for a, b, c in generated:
            triangles.append(
                PolyhedralCompiledTriangle(
                    a=a,
                    b=b,
                    c=c,
                    source_surface_id=surface.surface_id,
                    source_surface_key=surface.surface_key,
                )
            )
        mappings.append(
            PolyhedralCompiledSurfaceMapping(
                source_surface_id=surface.surface_id,
                source_surface_key=surface.surface_key,
                material_authority=surface.material_authority,
                compiled_triangle_indices=tuple(range(start, len(triangles))),
                holes_present=bool(surface.hole_vertex_indices),
            )
        )

    wave_reasons: list[str] = []
    ga_reasons: list[str] = []
    if holes_present:
        wave_reasons.append('polygon_hole_triangulation_unsupported')
        ga_reasons.append('polygon_hole_triangulation_unsupported')
    if len(geometry.air_volumes) != 1:
        wave_reasons.append('multi_region_wave_representation_not_authorized')
    if geometry.portals:
        wave_reasons.append('portal_wave_representation_not_authorized')

    wave_state: RepresentationState = 'UNSUPPORTED' if wave_reasons else 'READY'
    ga_state: RepresentationState = 'UNSUPPORTED' if ga_reasons else 'READY'
    readiness = R120PolyhedralRepresentationReadiness(
        wave_representation=wave_state,
        ga_representation=ga_state,
        wave_unsupported_reasons=tuple(wave_reasons),
        ga_unsupported_reasons=tuple(ga_reasons),
    )

    region_volume_evidence: list[RegionVolumeEvidence] = []
    compiled_triangle_indices_by_surface = {
        mapping.source_surface_key: mapping.compiled_triangle_indices
        for mapping in mappings
    }
    for volume in geometry.air_volumes:
        region_triangles: list[tuple[int, int, int]] = []
        complete = True
        for surface_key in volume.boundary_surface_keys:
            mapping_indices = compiled_triangle_indices_by_surface[surface_key]
            if not mapping_indices:
                complete = False
                break
            for triangle_index in mapping_indices:
                item = triangles[triangle_index]
                region_triangles.append((item.a, item.b, item.c))
        volume_value = (
            _signed_volume_from_triangles(geometry.vertices, tuple(region_triangles))
            if complete
            else None
        )
        region_volume_evidence.append(
            RegionVolumeEvidence(
                region_id=volume.region_id,
                enclosed_volume_m3=volume_value,
            )
        )

    topology_identity = _compiled_topology_identity(
        geometry.vertices,
        tuple(triangles),
        tuple(mappings),
    )
    core = {
        'authority_version': '1',
        'compiler_id': R120_POLYHEDRAL_COMPILER_ID,
        'compiler_version': R120_POLYHEDRAL_COMPILER_VERSION,
        'exact_semantic_geometry_id': geometry.geometry_id,
        'exact_semantic_geometry_hash_sha256': geometry.semantic_hash_sha256,
        'exact_topology_report_id': report.report_id,
        'exact_topology_report_hash_sha256': report.report_hash_sha256,
        'vertices': geometry.vertices,
        'triangles': tuple(triangles),
        'surface_mapping': tuple(mappings),
        'topology_identity_sha256': topology_identity,
        'region_volume_evidence': tuple(region_volume_evidence),
        'approximation_authority': geometry.approximation_authority,
        'readiness': readiness,
    }
    digest = _semantic_hash(_jsonable(core))
    return R120PolyhedralCompiledGeometry(
        compiled_geometry_id=f'r120-polyhedral-compiled:{digest}',
        compiled_hash_sha256=digest,
        **core,
    )


def serialize_r120_polyhedral_semantic_geometry(
    geometry: R120PolyhedralSemanticGeometry,
) -> str:
    return _canonical_json(geometry.model_dump(mode='json'))


def deserialize_r120_polyhedral_semantic_geometry(
    payload: str,
) -> R120PolyhedralSemanticGeometry:
    return R120PolyhedralSemanticGeometry.model_validate(json.loads(payload))


def serialize_r120_polyhedral_compiled_geometry(
    compiled: R120PolyhedralCompiledGeometry,
) -> str:
    return _canonical_json(compiled.model_dump(mode='json'))


def deserialize_r120_polyhedral_compiled_geometry(
    payload: str,
) -> R120PolyhedralCompiledGeometry:
    return R120PolyhedralCompiledGeometry.model_validate(json.loads(payload))


def _validate_index_loop(loop: tuple[int, ...], label: str) -> None:
    if len(loop) < 3:
        raise ValueError(f'{label} requires at least three vertices')
    if len(loop) != len(set(loop)):
        raise ValueError(f'{label} cannot repeat vertex indices')
    if any(index < 0 for index in loop):
        raise ValueError(f'{label} vertex indices must be non-negative')


def _surface_id(
    surface_key: str,
    outer: tuple[int, ...],
    holes: tuple[tuple[int, ...], ...],
    vertices: tuple[PolyhedralVertex, ...],
) -> str:
    referenced = tuple(sorted(set(outer).union(*(set(hole) for hole in holes))))
    digest = _semantic_hash(
        {
            'surface_key': surface_key,
            'outer_vertex_indices': outer,
            'hole_vertex_indices': holes,
            'referenced_vertex_coordinates_m': [
                vertices[index].model_dump(mode='json') for index in referenced
            ],
        }
    )
    return f'r120-polyhedral-surface:{digest}'


def _canonical_directed_loop(loop: tuple[int, ...]) -> tuple[int, ...]:
    if not loop:
        return loop
    rotations = tuple(loop[index:] + loop[:index] for index in range(len(loop)))
    return min(rotations)


def _canonical_undirected_loop(loop: tuple[int, ...]) -> tuple[int, ...]:
    forward = _canonical_directed_loop(loop)
    reverse = _canonical_directed_loop(tuple(reversed(loop)))
    return min(forward, reverse)


def _face_signature(surface: PlanarPolygonSurface) -> tuple[object, ...]:
    return (
        _canonical_undirected_loop(surface.outer_vertex_indices),
        tuple(
            sorted(_canonical_undirected_loop(hole) for hole in surface.hole_vertex_indices)
        ),
    )


def _normalized_edge(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _sub3(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (left[0] - right[0], left[1] - right[1], left[2] - right[2])


def _dot3(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> float:
    return left[0] * right[0] + left[1] * right[1] + left[2] * right[2]


def _cross3(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _norm3(value: tuple[float, float, float]) -> float:
    return sqrt(_dot3(value, value))


def _plane_for_points(
    points: tuple[tuple[float, float, float], ...],
    tolerance_m: float,
) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    origin = points[0]
    for left_index in range(1, len(points) - 1):
        left = _sub3(points[left_index], origin)
        for right_index in range(left_index + 1, len(points)):
            right = _sub3(points[right_index], origin)
            normal = _cross3(left, right)
            length = _norm3(normal)
            if length > tolerance_m * tolerance_m:
                return (
                    origin,
                    (normal[0] / length, normal[1] / length, normal[2] / length),
                )
    return None


def _dominant_axis(normal: tuple[float, float, float]) -> int:
    values = (abs(normal[0]), abs(normal[1]), abs(normal[2]))
    return max(range(3), key=lambda index: values[index])


def _project_point(
    point: tuple[float, float, float],
    drop_axis: int,
) -> tuple[float, float]:
    if drop_axis == 0:
        return (point[1], point[2])
    if drop_axis == 1:
        return (point[0], point[2])
    return (point[0], point[1])


def _signed_area_2d(points: tuple[tuple[float, float], ...]) -> float:
    return 0.5 * sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))
    )


def _orientation_2d(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _point_on_segment_2d(
    point: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
    tolerance: float,
) -> bool:
    if abs(_orientation_2d(a, b, point)) > tolerance:
        return False
    return (
        min(a[0], b[0]) - tolerance <= point[0] <= max(a[0], b[0]) + tolerance
        and min(a[1], b[1]) - tolerance <= point[1] <= max(a[1], b[1]) + tolerance
    )


def _segments_intersect_2d(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
    tolerance: float,
) -> bool:
    o1 = _orientation_2d(a, b, c)
    o2 = _orientation_2d(a, b, d)
    o3 = _orientation_2d(c, d, a)
    o4 = _orientation_2d(c, d, b)
    if (o1 > tolerance and o2 < -tolerance or o1 < -tolerance and o2 > tolerance) and (
        o3 > tolerance and o4 < -tolerance or o3 < -tolerance and o4 > tolerance
    ):
        return True
    return any(
        (
            abs(o1) <= tolerance and _point_on_segment_2d(c, a, b, tolerance),
            abs(o2) <= tolerance and _point_on_segment_2d(d, a, b, tolerance),
            abs(o3) <= tolerance and _point_on_segment_2d(a, c, d, tolerance),
            abs(o4) <= tolerance and _point_on_segment_2d(b, c, d, tolerance),
        )
    )


def _loop_self_intersects(
    points: tuple[tuple[float, float], ...],
    tolerance: float,
) -> bool:
    count = len(points)
    for left in range(count):
        left_next = (left + 1) % count
        for right in range(left + 1, count):
            right_next = (right + 1) % count
            if left == right or left_next == right or right_next == left:
                continue
            if left == 0 and right_next == 0:
                continue
            if _segments_intersect_2d(
                points[left],
                points[left_next],
                points[right],
                points[right_next],
                tolerance,
            ):
                return True
    return False


def _loops_intersect(
    left: tuple[tuple[float, float], ...],
    right: tuple[tuple[float, float], ...],
    tolerance: float,
) -> bool:
    for left_index in range(len(left)):
        for right_index in range(len(right)):
            if _segments_intersect_2d(
                left[left_index],
                left[(left_index + 1) % len(left)],
                right[right_index],
                right[(right_index + 1) % len(right)],
                tolerance,
            ):
                return True
    return False


def _point_in_polygon_2d(
    point: tuple[float, float],
    polygon: tuple[tuple[float, float], ...],
    tolerance: float,
) -> bool:
    inside = False
    for index, a in enumerate(polygon):
        b = polygon[(index + 1) % len(polygon)]
        if _point_on_segment_2d(point, a, b, tolerance):
            return True
        if (a[1] > point[1]) != (b[1] > point[1]):
            x_at_y = a[0] + (point[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if x_at_y > point[0]:
                inside = not inside
    return inside


def _triangulate_surface_indices(
    vertices: tuple[PolyhedralVertex, ...],
    surface: PlanarPolygonSurface,
    tolerance: float,
) -> tuple[tuple[int, int, int], ...]:
    if surface.hole_vertex_indices:
        raise R120PolyhedralGeometryError(
            'deterministic indexed triangle compilation does not yet support polygon holes'
        )
    loop = list(surface.outer_vertex_indices)
    points3 = tuple(vertices[index].point() for index in loop)
    plane = _plane_for_points(points3, tolerance)
    if plane is None:
        raise R120PolyhedralGeometryError('cannot triangulate zero-area polygon')
    _, normal = plane
    drop_axis = _dominant_axis(normal)
    projected = {index: _project_point(vertices[index].point(), drop_axis) for index in loop}
    area = _signed_area_2d(tuple(projected[index] for index in loop))
    if abs(area) <= tolerance * tolerance:
        raise R120PolyhedralGeometryError('cannot triangulate zero-area polygon')
    orientation = 1.0 if area > 0 else -1.0
    triangles: list[tuple[int, int, int]] = []
    remaining = loop[:]

    while len(remaining) > 3:
        ear_index: int | None = None
        for index in range(len(remaining)):
            previous = remaining[(index - 1) % len(remaining)]
            current = remaining[index]
            following = remaining[(index + 1) % len(remaining)]
            cross = _orientation_2d(
                projected[previous],
                projected[current],
                projected[following],
            )
            if orientation * cross <= tolerance:
                continue
            if any(
                candidate not in (previous, current, following)
                and _point_in_triangle_2d(
                    projected[candidate],
                    projected[previous],
                    projected[current],
                    projected[following],
                    tolerance,
                )
                for candidate in remaining
            ):
                continue
            ear_index = index
            triangles.append((previous, current, following))
            break
        if ear_index is None:
            raise R120PolyhedralGeometryError('deterministic ear clipping failed')
        remaining.pop(ear_index)
    triangles.append((remaining[0], remaining[1], remaining[2]))
    return tuple(triangles)


def _point_in_triangle_2d(
    point: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    tolerance: float,
) -> bool:
    o1 = _orientation_2d(a, b, point)
    o2 = _orientation_2d(b, c, point)
    o3 = _orientation_2d(c, a, point)
    has_negative = o1 < -tolerance or o2 < -tolerance or o3 < -tolerance
    has_positive = o1 > tolerance or o2 > tolerance or o3 > tolerance
    return not (has_negative and has_positive)


def _signed_volume_from_triangles(
    vertices: tuple[PolyhedralVertex, ...],
    triangles: tuple[tuple[int, int, int], ...],
) -> float:
    signed_six_volume = 0.0
    for a_index, b_index, c_index in triangles:
        a = vertices[a_index].point()
        b = vertices[b_index].point()
        c = vertices[c_index].point()
        signed_six_volume += _dot3(a, _cross3(b, c))
    return signed_six_volume / 6.0


def _ray_triangle_distance(
    origin: tuple[float, float, float],
    direction: tuple[float, float, float],
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    c: tuple[float, float, float],
    tolerance: float,
) -> float | None:
    edge1 = _sub3(b, a)
    edge2 = _sub3(c, a)
    h = _cross3(direction, edge2)
    determinant = _dot3(edge1, h)
    if abs(determinant) <= tolerance:
        return None
    inverse = 1.0 / determinant
    s = _sub3(origin, a)
    u = inverse * _dot3(s, h)
    if u < -tolerance or u > 1.0 + tolerance:
        return None
    q = _cross3(s, edge1)
    v = inverse * _dot3(direction, q)
    if v < -tolerance or u + v > 1.0 + tolerance:
        return None
    distance = inverse * _dot3(edge2, q)
    return distance if distance > tolerance else None


def _point_on_triangle_3d(
    point: tuple[float, float, float],
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    c: tuple[float, float, float],
    tolerance: float,
) -> bool:
    normal = _cross3(_sub3(b, a), _sub3(c, a))
    normal_length = _norm3(normal)
    if normal_length <= tolerance:
        return False
    distance = abs(_dot3(_sub3(point, a), normal)) / normal_length
    if distance > tolerance:
        return False
    drop_axis = _dominant_axis(normal)
    return _point_in_triangle_2d(
        _project_point(point, drop_axis),
        _project_point(a, drop_axis),
        _project_point(b, drop_axis),
        _project_point(c, drop_axis),
        tolerance,
    )


def _point_strictly_inside_volume(
    point: tuple[float, float, float],
    vertices: tuple[PolyhedralVertex, ...],
    triangles: tuple[tuple[int, int, int], ...],
    tolerance: float,
) -> bool:
    for triangle in triangles:
        a, b, c = (vertices[index].point() for index in triangle)
        if _point_on_triangle_3d(point, a, b, c, tolerance):
            return False
    direction = (1.0, 0.3713906763541037, 0.52999894000318)
    hits: list[float] = []
    for triangle in triangles:
        a, b, c = (vertices[index].point() for index in triangle)
        distance = _ray_triangle_distance(point, direction, a, b, c, tolerance)
        if distance is not None:
            hits.append(distance)
    unique_hits: list[float] = []
    for value in sorted(hits):
        if not unique_hits or abs(value - unique_hits[-1]) > tolerance * 10.0:
            unique_hits.append(value)
    return len(unique_hits) % 2 == 1


def _segment_intersects_triangle_strict(
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    c: tuple[float, float, float],
    tolerance: float,
) -> bool:
    direction = _sub3(end, start)
    length = _norm3(direction)
    if length <= tolerance:
        return False
    unit = (direction[0] / length, direction[1] / length, direction[2] / length)
    distance = _ray_triangle_distance(start, unit, a, b, c, tolerance)
    return distance is not None and tolerance < distance < length - tolerance


def _volume_has_detectable_self_intersection(
    vertices: tuple[PolyhedralVertex, ...],
    triangles: tuple[tuple[int, int, int], ...],
    tolerance: float,
) -> bool:
    for left_index, left in enumerate(triangles):
        left_set = set(left)
        for right in triangles[left_index + 1:]:
            if left_set.intersection(right):
                continue
            right_points = tuple(vertices[index].point() for index in right)
            left_points = tuple(vertices[index].point() for index in left)
            for edge_start, edge_end in ((0, 1), (1, 2), (2, 0)):
                if _segment_intersects_triangle_strict(
                    left_points[edge_start],
                    left_points[edge_end],
                    right_points[0],
                    right_points[1],
                    right_points[2],
                    tolerance,
                ):
                    return True
                if _segment_intersects_triangle_strict(
                    right_points[edge_start],
                    right_points[edge_end],
                    left_points[0],
                    left_points[1],
                    left_points[2],
                    tolerance,
                ):
                    return True
    return False


def _volumes_overlap_detectably(
    vertices: tuple[PolyhedralVertex, ...],
    left: tuple[tuple[int, int, int], ...],
    right: tuple[tuple[int, int, int], ...],
    tolerance: float,
) -> bool:
    left_vertex_indices = sorted({index for triangle in left for index in triangle})
    right_vertex_indices = sorted({index for triangle in right for index in triangle})
    if any(
        _point_strictly_inside_volume(vertices[index].point(), vertices, right, tolerance)
        for index in left_vertex_indices
    ):
        return True
    if any(
        _point_strictly_inside_volume(vertices[index].point(), vertices, left, tolerance)
        for index in right_vertex_indices
    ):
        return True
    for left_triangle in left:
        left_points = tuple(vertices[index].point() for index in left_triangle)
        for right_triangle in right:
            right_points = tuple(vertices[index].point() for index in right_triangle)
            for edge_start, edge_end in ((0, 1), (1, 2), (2, 0)):
                if _segment_intersects_triangle_strict(
                    left_points[edge_start],
                    left_points[edge_end],
                    right_points[0],
                    right_points[1],
                    right_points[2],
                    tolerance,
                ):
                    return True
                if _segment_intersects_triangle_strict(
                    right_points[edge_start],
                    right_points[edge_end],
                    left_points[0],
                    left_points[1],
                    left_points[2],
                    tolerance,
                ):
                    return True
    return False


def _compiled_topology_identity(
    vertices: tuple[PolyhedralVertex, ...],
    triangles: tuple[PolyhedralCompiledTriangle, ...],
    mappings: tuple[PolyhedralCompiledSurfaceMapping, ...],
) -> str:
    payload = {
        'vertices': [item.model_dump(mode='json') for item in vertices],
        'triangles': [item.model_dump(mode='json') for item in triangles],
        'surface_mapping': [
            {
                'source_surface_id': item.source_surface_id,
                'source_surface_key': item.source_surface_key,
                'compiled_triangle_indices': list(item.compiled_triangle_indices),
                'holes_present': item.holes_present,
            }
            for item in mappings
        ],
    }
    return _semantic_hash(payload)


def _jsonable(payload: object) -> object:
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode='json')
    if isinstance(payload, dict):
        return {key: _jsonable(value) for key, value in payload.items()}
    if isinstance(payload, tuple):
        return [_jsonable(value) for value in payload]
    if isinstance(payload, list):
        return [_jsonable(value) for value in payload]
    return payload
