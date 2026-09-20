from __future__ import annotations

from pathlib import Path

import pytest

from htdt.r120_geometry_compiler import (
    GeometryApproximationAuthority,
    PlanarPolygonSurfaceSpec,
    PolyhedralAirVolume,
    PolyhedralAuthorityRef,
    PolyhedralPortal,
    R120PolyhedralGeometryError,
    compile_r120_polyhedral_geometry,
    deserialize_r120_polyhedral_compiled_geometry,
    deserialize_r120_polyhedral_semantic_geometry,
    make_r120_polyhedral_semantic_geometry,
    serialize_r120_polyhedral_compiled_geometry,
    serialize_r120_polyhedral_semantic_geometry,
    validate_r120_polyhedral_topology,
)


def _material(name: str) -> PolyhedralAuthorityRef:
    return PolyhedralAuthorityRef(
        authority_id=f'material:{name}',
        authority_version='fixture-v1',
        semantic_hash_sha256='a' * 64,
    )


def _surface_specs(
    faces: tuple[tuple[str, tuple[int, ...]], ...],
) -> tuple[PlanarPolygonSurfaceSpec, ...]:
    return tuple(
        PlanarPolygonSurfaceSpec(
            surface_key=key,
            semantic_class='room_boundary',
            outer_vertex_indices=loop,
            material_authority=_material(key),
        )
        for key, loop in faces
    )


def _make_geometry(
    vertices: tuple[tuple[float, float, float], ...],
    faces: tuple[tuple[str, tuple[int, ...]], ...],
    *,
    source_geometry_identity: str = 'fixture:explicit-polyhedron',
    source_geometry_kind: str = 'explicit_polyhedral',
    portals: tuple[PolyhedralPortal, ...] = (),
    approximations: tuple[GeometryApproximationAuthority, ...] = (),
):
    return make_r120_polyhedral_semantic_geometry(
        source_geometry_identity=source_geometry_identity,
        source_geometry_kind=source_geometry_kind,
        vertices=vertices,
        surfaces=_surface_specs(faces),
        air_volumes=(
            PolyhedralAirVolume(
                region_id='room-air',
                boundary_surface_keys=tuple(key for key, _ in faces),
            ),
        ),
        portals=portals,
        approximation_authority=approximations,
    )


def _sloped_fixture():
    vertices = (
        (0.0, 0.0, 0.0),  # A
        (2.0, 0.0, 0.0),  # B
        (2.0, 2.0, 0.0),  # C
        (0.0, 2.0, 0.0),  # D
        (0.0, 0.0, 2.0),  # E
        (2.0, 0.0, 2.0),  # F
        (2.0, 2.0, 3.0),  # G
        (0.0, 2.0, 3.0),  # H
    )
    faces = (
        ('floor', (0, 3, 2, 1)),
        ('front', (0, 1, 5, 4)),
        ('right', (1, 2, 6, 5)),
        ('back', (3, 7, 6, 2)),
        ('left', (0, 4, 7, 3)),
        ('ceiling', (4, 5, 6, 7)),
    )
    return vertices, faces


def _prism_fixture():
    vertices = (
        (0.0, 0.0, 0.0),
        (2.0, 0.0, 0.0),
        (2.0, 3.0, 0.0),
        (0.0, 3.0, 0.0),
        (0.0, 0.0, 4.0),
        (2.0, 0.0, 4.0),
        (2.0, 3.0, 4.0),
        (0.0, 3.0, 4.0),
    )
    faces = (
        ('floor', (0, 3, 2, 1)),
        ('front', (0, 1, 5, 4)),
        ('right', (1, 2, 6, 5)),
        ('back', (3, 7, 6, 2)),
        ('left', (0, 4, 7, 3)),
        ('ceiling', (4, 5, 6, 7)),
    )
    return vertices, faces


def _stepped_fixture():
    # Extrusion along +Y of an L-shaped X/Z section.  The upper boundary is a
    # two-level ceiling and therefore exercises both concave polygon handling
    # and explicit vertical step faces.
    section = (
        (0.0, 0.0),
        (2.0, 0.0),
        (2.0, 1.0),
        (1.0, 1.0),
        (1.0, 2.0),
        (0.0, 2.0),
    )
    front = tuple((x, 0.0, z) for x, z in section)
    back = tuple((x, 2.0, z) for x, z in section)
    vertices = front + back
    faces: list[tuple[str, tuple[int, ...]]] = [
        ('front', (0, 1, 2, 3, 4, 5)),
        ('back', (6, 11, 10, 9, 8, 7)),
    ]
    for index in range(len(section)):
        following = (index + 1) % len(section)
        faces.append(
            (
                f'side-{index}',
                (index, 6 + index, 6 + following, following),
            )
        )
    return vertices, tuple(faces)


def _tetra_faces():
    return (
        ('tetra-0', (0, 2, 1)),
        ('tetra-1', (0, 1, 3)),
        ('tetra-2', (0, 3, 2)),
        ('tetra-3', (1, 2, 3)),
    )


def test_sloped_ceiling_is_valid_closed_polyhedral_volume() -> None:
    vertices, faces = _sloped_fixture()
    geometry = _make_geometry(vertices, faces)

    report = validate_r120_polyhedral_topology(geometry)
    compiled = compile_r120_polyhedral_geometry(geometry)

    assert report.valid is True
    assert report.closed_volume_region_ids == ('room-air',)
    assert compiled.readiness.wave_representation == 'READY'
    assert compiled.readiness.ga_representation == 'READY'
    assert compiled.readiness.wave_numerical_validation_status == 'NOT_VALIDATED'
    assert compiled.region_volume_evidence[0].enclosed_volume_m3 == pytest.approx(10.0)


def test_stepped_floor_ceiling_geometry_is_valid_and_deterministically_triangulated() -> None:
    vertices, faces = _stepped_fixture()
    geometry = _make_geometry(vertices, faces)

    report = validate_r120_polyhedral_topology(geometry)
    compiled = compile_r120_polyhedral_geometry(geometry)

    assert report.valid is True
    assert compiled.region_volume_evidence[0].enclosed_volume_m3 == pytest.approx(6.0)
    assert len(compiled.triangles) == 20
    assert all(mapping.compiled_triangle_indices for mapping in compiled.surface_mapping)


def test_compile_identity_is_stable_across_input_order_and_loop_rotation() -> None:
    vertices, faces = _sloped_fixture()
    first = _make_geometry(vertices, faces)

    order = tuple(reversed(range(len(vertices))))
    old_to_new = {old: new for new, old in enumerate(order)}
    reordered_vertices = tuple(vertices[old] for old in order)
    reordered_faces = []
    for key, loop in reversed(faces):
        rotated = loop[1:] + loop[:1]
        reordered_faces.append(
            (key, tuple(old_to_new[index] for index in rotated))
        )
    second = _make_geometry(reordered_vertices, tuple(reordered_faces))

    assert second.geometry_id == first.geometry_id
    assert second.semantic_hash_sha256 == first.semantic_hash_sha256

    compiled_first = compile_r120_polyhedral_geometry(first)
    compiled_second = compile_r120_polyhedral_geometry(second)
    assert compiled_second.topology_identity_sha256 == compiled_first.topology_identity_sha256
    assert compiled_second.compiled_hash_sha256 == compiled_first.compiled_hash_sha256


def test_material_identity_is_preserved_per_surface() -> None:
    vertices, faces = _sloped_fixture()
    geometry = _make_geometry(vertices, faces)
    compiled = compile_r120_polyhedral_geometry(geometry)

    expected = {
        surface.surface_key: surface.material_authority
        for surface in geometry.surfaces
    }
    actual = {
        mapping.source_surface_key: mapping.material_authority
        for mapping in compiled.surface_mapping
    }
    assert actual == expected


def test_non_manifold_edge_fails_closed() -> None:
    vertices = (
        (0.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
        (0.5, 0.5, 0.5),
    )
    faces = _tetra_faces() + (('extra-edge-face', (0, 1, 4)),)
    geometry = _make_geometry(vertices, faces)

    report = validate_r120_polyhedral_topology(geometry)

    assert report.valid is False
    assert 'non_manifold_edge' in {item.code for item in report.findings}
    with pytest.raises(R120PolyhedralGeometryError, match='failed closed'):
        compile_r120_polyhedral_geometry(geometry)


def test_open_volume_fails_closed() -> None:
    vertices = (
        (0.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
    )
    geometry = _make_geometry(vertices, _tetra_faces()[:-1])

    report = validate_r120_polyhedral_topology(geometry)

    assert report.valid is False
    assert 'open_volume' in {item.code for item in report.findings}
    with pytest.raises(R120PolyhedralGeometryError, match='failed closed'):
        compile_r120_polyhedral_geometry(geometry)


def test_bounded_tessellation_error_authority_is_explicit_and_preserved() -> None:
    vertices, faces = _sloped_fixture()
    approximation = GeometryApproximationAuthority(
        source_geometry_identity='cad-surface:curved-shell-17',
        tolerance_m=0.01,
        maximum_deviation_m=0.004,
        generated_surface_keys=('right', 'ceiling'),
        generated_surface_count=2,
        algorithm_id='fixture.bounded-planar-tessellation',
        algorithm_version='1',
        approximation_status='bounded_planar_tessellation',
    )
    geometry = _make_geometry(
        vertices,
        faces,
        source_geometry_identity='cad-body:curved-source',
        source_geometry_kind='bounded_planar_tessellation',
        approximations=(approximation,),
    )
    compiled = compile_r120_polyhedral_geometry(geometry)

    assert geometry.approximation_authority[0].source_geometry_identity == (
        'cad-surface:curved-shell-17'
    )
    assert geometry.approximation_authority[0].tolerance_m == 0.01
    assert geometry.approximation_authority[0].maximum_deviation_m == 0.004
    assert geometry.approximation_authority[0].generated_surface_count == 2
    assert geometry.approximation_authority[0].algorithm_version == '1'
    assert geometry.approximation_authority[0].approximation_status == (
        'bounded_planar_tessellation'
    )
    assert compiled.approximation_authority == geometry.approximation_authority

    with pytest.raises(ValueError, match='exceeds declared tolerance'):
        GeometryApproximationAuthority(
            source_geometry_identity='cad-surface:bad',
            tolerance_m=0.01,
            maximum_deviation_m=0.02,
            generated_surface_keys=('ceiling',),
            generated_surface_count=1,
            algorithm_id='fixture.bounded-planar-tessellation',
            algorithm_version='1',
            approximation_status='bounded_planar_tessellation',
        )


def test_save_reopen_preserves_semantic_and_compiled_identity(tmp_path: Path) -> None:
    vertices, faces = _sloped_fixture()
    geometry = _make_geometry(vertices, faces)
    compiled = compile_r120_polyhedral_geometry(geometry)

    semantic_path = tmp_path / 'r120b-semantic.json'
    compiled_path = tmp_path / 'r120b-compiled.json'
    semantic_path.write_text(
        serialize_r120_polyhedral_semantic_geometry(geometry),
        encoding='utf-8',
    )
    compiled_path.write_text(
        serialize_r120_polyhedral_compiled_geometry(compiled),
        encoding='utf-8',
    )

    reopened_geometry = deserialize_r120_polyhedral_semantic_geometry(
        semantic_path.read_text(encoding='utf-8')
    )
    reopened_compiled = deserialize_r120_polyhedral_compiled_geometry(
        compiled_path.read_text(encoding='utf-8')
    )

    assert reopened_geometry.geometry_id == geometry.geometry_id
    assert reopened_geometry.semantic_hash_sha256 == geometry.semantic_hash_sha256
    assert reopened_compiled.compiled_geometry_id == compiled.compiled_geometry_id
    assert reopened_compiled.compiled_hash_sha256 == compiled.compiled_hash_sha256


def test_room_prism_regression_remains_supported() -> None:
    vertices, faces = _prism_fixture()
    geometry = _make_geometry(vertices, faces)
    compiled = compile_r120_polyhedral_geometry(geometry)

    assert len(compiled.triangles) == 12
    assert compiled.region_volume_evidence[0].enclosed_volume_m3 == pytest.approx(24.0)
    assert compiled.readiness.wave_representation == 'READY'
    assert compiled.readiness.ga_representation == 'READY'


def test_portal_does_not_promote_wave_support_from_ga_support() -> None:
    vertices, faces = _sloped_fixture()
    geometry = _make_geometry(
        vertices,
        faces,
        portals=(
            PolyhedralPortal(
                portal_id='explicit-ceiling-portal',
                region_ids=('room-air',),
                surface_keys=('ceiling',),
            ),
        ),
    )
    compiled = compile_r120_polyhedral_geometry(geometry)

    assert compiled.readiness.ga_representation == 'READY'
    assert compiled.readiness.wave_representation == 'UNSUPPORTED'
    assert compiled.readiness.wave_unsupported_reasons == (
        'portal_wave_representation_not_authorized',
    )


def test_invalid_portal_surface_relationship_is_diagnostic_and_fail_closed() -> None:
    vertices, faces = _sloped_fixture()
    # The referenced surface exists semantically but is deliberately omitted
    # from the region boundary, so the Portal relationship is not authorized.
    specs = _surface_specs(faces)
    geometry = make_r120_polyhedral_semantic_geometry(
        source_geometry_identity='fixture:invalid-portal',
        source_geometry_kind='explicit_polyhedral',
        vertices=vertices,
        surfaces=specs,
        air_volumes=(
            PolyhedralAirVolume(
                region_id='room-air',
                boundary_surface_keys=tuple(
                    key for key, _ in faces if key != 'ceiling'
                ),
            ),
        ),
        portals=(
            PolyhedralPortal(
                portal_id='bad-portal',
                region_ids=('room-air',),
                surface_keys=('ceiling',),
            ),
        ),
    )

    report = validate_r120_polyhedral_topology(geometry)

    assert report.valid is False
    assert 'invalid_portal_surface_relationship' in {
        item.code for item in report.findings
    }
    with pytest.raises(R120PolyhedralGeometryError):
        compile_r120_polyhedral_geometry(geometry)


def test_identical_closed_regions_are_reported_as_overlap() -> None:
    vertices, faces = _prism_fixture()
    geometry = make_r120_polyhedral_semantic_geometry(
        source_geometry_identity='fixture:overlapping-regions',
        source_geometry_kind='explicit_polyhedral',
        vertices=vertices,
        surfaces=_surface_specs(faces),
        air_volumes=(
            PolyhedralAirVolume(
                region_id='region-a',
                boundary_surface_keys=tuple(key for key, _ in faces),
            ),
            PolyhedralAirVolume(
                region_id='region-b',
                boundary_surface_keys=tuple(reversed([key for key, _ in faces])),
            ),
        ),
    )

    report = validate_r120_polyhedral_topology(geometry)

    assert report.valid is False
    assert 'region_overlap' in {item.code for item in report.findings}
