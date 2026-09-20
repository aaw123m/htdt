from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from htdt.acoustic_pffdtd_polyhedral_geometry import (
    canonical_polyhedral_triangles,
    classify_point_in_closed_polyhedron,
    compile_r120b_polyhedral_to_pffdtd,
    load_r120b_polyhedral_authorities,
    register_r120b_polyhedral_authorities,
)
from htdt.cad_candidate_wave_execution import (
    CandidateResourceConfiguration,
    CandidateWaveExecutionError,
    ExactJsonAuthorityStore,
    build_pffdtd_candidate_configuration,
)
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef
from htdt.r120_polyhedral_geometry import (
    PlanarPolygonSurfaceSpec,
    PolyhedralAirVolume,
    PolyhedralAuthorityRef,
    PolyhedralPortal,
    R120PolyhedralGeometryError,
    compile_r120_polyhedral_geometry,
    make_r120_polyhedral_semantic_geometry,
)


def _ref(prefix: str) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=prefix,
        authority_version='1',
        semantic_hash_sha256='b' * 64,
    )


def _material(name: str) -> PolyhedralAuthorityRef:
    return PolyhedralAuthorityRef(
        authority_id=f'material:{name}',
        authority_version='1',
        semantic_hash_sha256='a' * 64,
    )


def _configuration(
    *,
    points_per_wavelength: float = 8.0,
    max_grid_cells: int = 200_000,
):
    density = _ref('density:test')
    humidity = _ref('humidity:test')
    return build_pffdtd_candidate_configuration(
        expected_pffdtd_commit_sha='a' * 40,
        fmax_hz=100.0,
        points_per_wavelength=points_per_wavelength,
        duration_s=0.03,
        frequency_samples_hz=(40.0, 80.0),
        density_kg_m3=1.2,
        density_authority_ref=density,
        relative_humidity_percent=50.0,
        humidity_authority_ref=humidity,
        resource=CandidateResourceConfiguration(
            solver_threads=1,
            setup_processes=1,
            max_grid_cells=max_grid_cells,
            max_time_steps=1_000,
            max_output_bytes=32 * 1024 * 1024,
            max_solver_wall_seconds=60.0,
        ),
    )


def _surface_specs(
    faces: tuple[tuple[str, tuple[int, ...]], ...],
    *,
    material_name: str = 'rigid',
):
    return tuple(
        PlanarPolygonSurfaceSpec(
            surface_key=key,
            semantic_class='room_boundary',
            outer_vertex_indices=loop,
            material_authority=_material(material_name),
        )
        for key, loop in faces
    )


def _geometry(
    vertices,
    faces,
    *,
    source_identity: str = 'fixture:r130d',
    volumes=None,
    portals=(),
):
    if volumes is None:
        volumes = (
            PolyhedralAirVolume(
                region_id='room-air',
                boundary_surface_keys=tuple(key for key, _ in faces),
            ),
        )
    return make_r120_polyhedral_semantic_geometry(
        source_geometry_identity=source_identity,
        source_geometry_kind='explicit_polyhedral',
        vertices=tuple(vertices),
        surfaces=_surface_specs(tuple(faces)),
        air_volumes=tuple(volumes),
        portals=tuple(portals),
    )


def _rectangular_fixture():
    vertices = (
        (0.0, 0.0, 0.0),
        (4.0, 0.0, 0.0),
        (4.0, 4.0, 0.0),
        (0.0, 4.0, 0.0),
        (0.0, 0.0, 4.0),
        (4.0, 0.0, 4.0),
        (4.0, 4.0, 4.0),
        (0.0, 4.0, 4.0),
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


def _sloped_same_bbox_fixture():
    vertices = (
        (0.0, 0.0, 0.0),
        (4.0, 0.0, 0.0),
        (4.0, 4.0, 0.0),
        (0.0, 4.0, 0.0),
        (0.0, 0.0, 4.0),
        (4.0, 0.0, 4.0),
        (4.0, 4.0, 3.0),
        (0.0, 4.0, 3.0),
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


def _concave_fixture():
    # Extrude a concave X/Z section along +Y. The missing top-right quadrant
    # is inside the bounding box but outside the actual room.
    section = (
        (0.0, 0.0),
        (4.0, 0.0),
        (4.0, 3.0),
        (3.0, 3.0),
        (3.0, 4.0),
        (0.0, 4.0),
    )
    front = tuple((x, 0.0, z) for x, z in section)
    back = tuple((x, 4.0, z) for x, z in section)
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


def _compile(vertices, faces, *, source_identity='fixture:r130d'):
    semantic = _geometry(
        vertices,
        faces,
        source_identity=source_identity,
    )
    return semantic, compile_r120_polyhedral_geometry(semantic)


def _adapt(semantic, compiled, *, configuration=None, tolerance=1.0e-9):
    return compile_r120b_polyhedral_to_pffdtd(
        semantic=semantic,
        compiled=compiled,
        source_position_m=(1.5, 2.0, 2.0),
        receivers=(('receiver-1', (2.5, 2.0, 2.0)),),
        sound_speed_m_s=343.2,
        configuration=configuration or _configuration(),
        containment_tolerance_m=tolerance,
        topology_tolerance_m=1.0e-9,
        source_name='speaker-source',
    )


def test_rectangular_and_sloped_same_bbox_do_not_collapse_to_same_solver_geometry():
    rectangular = _compile(*_rectangular_fixture())
    sloped = _compile(*_sloped_same_bbox_fixture())

    rectangular_rep, rectangular_model = _adapt(*rectangular)
    sloped_rep, sloped_model = _adapt(*sloped)

    assert rectangular_rep.bounding_box_min_m == sloped_rep.bounding_box_min_m
    assert rectangular_rep.bounding_box_max_m == sloped_rep.bounding_box_max_m
    assert rectangular_rep.grid_origin_m == sloped_rep.grid_origin_m
    assert rectangular_rep.grid_dimensions == sloped_rep.grid_dimensions
    assert rectangular_rep.generated_geometry_sha256 != (
        sloped_rep.generated_geometry_sha256
    )
    assert rectangular_rep.solver_model_sha256 != sloped_rep.solver_model_sha256
    assert rectangular_model['mats_hash']['_RIGID']['tris'] != (
        sloped_model['mats_hash']['_RIGID']['tris']
    ) or rectangular_model['mats_hash']['_RIGID']['pts'] != (
        sloped_model['mats_hash']['_RIGID']['pts']
    )


def test_concave_geometry_uses_exact_interior_not_bounding_box():
    semantic, compiled = _compile(*_concave_fixture())
    points = tuple(vertex.point() for vertex in compiled.vertices)
    triangles = tuple(
        (a, b, c)
        for _, _, a, b, c in canonical_polyhedral_triangles(compiled)
    )

    assert classify_point_in_closed_polyhedron(
        point_m=(1.0, 2.0, 2.0),
        vertices=points,
        triangles=triangles,
        tolerance_m=1.0e-9,
    ) == 'inside'
    assert classify_point_in_closed_polyhedron(
        point_m=(3.5, 2.0, 3.5),
        vertices=points,
        triangles=triangles,
        tolerance_m=1.0e-9,
    ) == 'outside'

    representation, _ = compile_r120b_polyhedral_to_pffdtd(
        semantic=semantic,
        compiled=compiled,
        source_position_m=(1.0, 2.0, 2.0),
        receivers=(('receiver-1', (2.0, 2.0, 2.0)),),
        sound_speed_m_s=343.2,
        configuration=_configuration(),
    )
    assert representation.region_id == 'room-air'
    assert representation.triangle_count == len(compiled.triangles)


def test_source_receiver_outside_or_boundary_fail_closed():
    semantic, compiled = _compile(*_rectangular_fixture())

    with pytest.raises(CandidateWaveExecutionError, match='source acoustic reference'):
        compile_r120b_polyhedral_to_pffdtd(
            semantic=semantic,
            compiled=compiled,
            source_position_m=(0.0, 2.0, 2.0),
            receivers=(('receiver-1', (2.0, 2.0, 2.0)),),
            sound_speed_m_s=343.2,
            configuration=_configuration(),
        )

    with pytest.raises(CandidateWaveExecutionError, match='receiver receiver-1'):
        compile_r120b_polyhedral_to_pffdtd(
            semantic=semantic,
            compiled=compiled,
            source_position_m=(1.0, 2.0, 2.0),
            receivers=(('receiver-1', (4.5, 2.0, 2.0)),),
            sound_speed_m_s=343.2,
            configuration=_configuration(),
        )


def test_deterministic_compilation_and_triangle_input_order_invariance():
    vertices, faces = _sloped_same_bbox_fixture()
    first_semantic, first_compiled = _compile(vertices, faces)

    order = tuple(reversed(range(len(vertices))))
    old_to_new = {old: new for new, old in enumerate(order)}
    second_vertices = tuple(vertices[old] for old in order)
    second_faces = []
    for key, loop in reversed(faces):
        rotated = loop[1:] + loop[:1]
        second_faces.append(
            (key, tuple(old_to_new[index] for index in rotated))
        )
    second_semantic, second_compiled = _compile(
        second_vertices,
        tuple(second_faces),
    )

    assert first_semantic.semantic_hash_sha256 == second_semantic.semantic_hash_sha256
    assert first_compiled.compiled_hash_sha256 == second_compiled.compiled_hash_sha256
    first_rep, first_model = _adapt(first_semantic, first_compiled)
    second_rep, second_model = _adapt(second_semantic, second_compiled)
    assert first_rep == second_rep
    assert first_model == second_model


def test_grid_or_containment_tolerance_changes_solver_geometry_identity():
    semantic, compiled = _compile(*_rectangular_fixture())

    baseline, _ = _adapt(semantic, compiled)
    denser, _ = _adapt(
        semantic,
        compiled,
        configuration=_configuration(points_per_wavelength=10.0),
    )
    looser_containment, _ = _adapt(
        semantic,
        compiled,
        tolerance=1.0e-8,
    )

    assert denser.grid_spacing_m != baseline.grid_spacing_m
    assert denser.representation_id != baseline.representation_id
    assert looser_containment.containment_tolerance_m != (
        baseline.containment_tolerance_m
    )
    assert looser_containment.representation_id != baseline.representation_id


def test_resource_ceiling_is_checked_before_backend_execution():
    semantic, compiled = _compile(*_rectangular_fixture())

    with pytest.raises(CandidateWaveExecutionError, match='resource ceiling'):
        _adapt(
            semantic,
            compiled,
            configuration=_configuration(max_grid_cells=1),
        )


def test_portal_geometry_is_explicitly_unsupported():
    vertices, faces = _rectangular_fixture()
    semantic = _geometry(
        vertices,
        faces,
        portals=(
            PolyhedralPortal(
                portal_id='fixture-portal',
                region_ids=('room-air',),
                surface_keys=('ceiling',),
            ),
        ),
    )
    compiled = compile_r120_polyhedral_geometry(semantic)

    with pytest.raises(CandidateWaveExecutionError, match='Portal-bearing'):
        _adapt(semantic, compiled)


def test_multiple_disjoint_acoustic_regions_are_explicitly_unsupported():
    vertices_a, faces_a = _rectangular_fixture()
    vertices_b = tuple((x + 10.0, y, z) for x, y, z in vertices_a)
    offset = len(vertices_a)
    faces_b = tuple(
        (f'b-{key}', tuple(index + offset for index in loop))
        for key, loop in faces_a
    )
    faces_a_named = tuple((f'a-{key}', loop) for key, loop in faces_a)
    all_faces = faces_a_named + faces_b
    semantic = _geometry(
        vertices_a + vertices_b,
        all_faces,
        volumes=(
            PolyhedralAirVolume(
                region_id='region-a',
                boundary_surface_keys=tuple(key for key, _ in faces_a_named),
            ),
            PolyhedralAirVolume(
                region_id='region-b',
                boundary_surface_keys=tuple(key for key, _ in faces_b),
            ),
        ),
    )
    compiled = compile_r120_polyhedral_geometry(semantic)

    with pytest.raises(CandidateWaveExecutionError, match='multiple AcousticRegions'):
        _adapt(semantic, compiled)


def test_invalid_open_topology_fails_before_solver_adapter():
    vertices = (
        (0.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
    )
    faces = (
        ('a', (0, 2, 1)),
        ('b', (0, 1, 3)),
        ('c', (0, 3, 2)),
    )
    semantic = _geometry(vertices, faces)
    with pytest.raises(R120PolyhedralGeometryError, match='failed closed'):
        compile_r120_polyhedral_geometry(semantic)


def test_stale_semantic_compiled_authority_pair_fails_closed(tmp_path: Path):
    first = _compile(*_rectangular_fixture(), source_identity='fixture:first')
    second = _compile(*_sloped_same_bbox_fixture(), source_identity='fixture:second')
    store = ExactJsonAuthorityStore(tmp_path)
    first_semantic_ref, _ = register_r120b_polyhedral_authorities(
        store,
        semantic=first[0],
        compiled=first[1],
    )
    _, second_compiled_ref = register_r120b_polyhedral_authorities(
        store,
        semantic=second[0],
        compiled=second[1],
    )

    with pytest.raises(
        CandidateWaveExecutionError,
        match='does not bind the exact semantic',
    ):
        load_r120b_polyhedral_authorities(
            store,
            semantic_ref=first_semantic_ref,
            compiled_ref=second_compiled_ref,
        )


def test_exact_authority_save_reopen_preserves_identity(tmp_path: Path):
    semantic, compiled = _compile(*_sloped_same_bbox_fixture())
    store = ExactJsonAuthorityStore(tmp_path)
    semantic_ref, compiled_ref = register_r120b_polyhedral_authorities(
        store,
        semantic=semantic,
        compiled=compiled,
    )

    reopened_semantic, reopened_compiled = load_r120b_polyhedral_authorities(
        store,
        semantic_ref=semantic_ref,
        compiled_ref=compiled_ref,
    )
    assert reopened_semantic == semantic
    assert reopened_compiled == compiled


def test_polygon_holes_remain_fail_closed_for_r130d():
    vertices, faces = _rectangular_fixture()
    # Add a coplanar inner loop to the ceiling. R120B may reject this during
    # topology validation or compile it as wave-UNSUPPORTED; either outcome is
    # fail-closed and must never reach a PFFDTD box fallback.
    vertices = vertices + (
        (1.0, 1.0, 4.0),
        (1.0, 2.0, 4.0),
        (2.0, 2.0, 4.0),
        (2.0, 1.0, 4.0),
    )
    specs = []
    for key, loop in faces:
        specs.append(
            PlanarPolygonSurfaceSpec(
                surface_key=key,
                semantic_class='room_boundary',
                outer_vertex_indices=loop,
                hole_vertex_indices=((8, 9, 10, 11),) if key == 'ceiling' else (),
                material_authority=_material('rigid'),
            )
        )
    semantic = make_r120_polyhedral_semantic_geometry(
        source_geometry_identity='fixture:hole',
        source_geometry_kind='explicit_polyhedral',
        vertices=vertices,
        surfaces=tuple(specs),
        air_volumes=(
            PolyhedralAirVolume(
                region_id='room-air',
                boundary_surface_keys=tuple(key for key, _ in faces),
            ),
        ),
    )
    try:
        compiled = compile_r120_polyhedral_geometry(semantic)
    except R120PolyhedralGeometryError:
        return

    with pytest.raises(CandidateWaveExecutionError, match='polygon holes|not READY'):
        _adapt(semantic, compiled)
