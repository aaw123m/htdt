from __future__ import annotations

from math import isclose, pi, radians, sqrt
from pathlib import Path
import random

import pytest

from htdt.cad_geometric_acoustics_adapter import (
    CadDeterministicPathArtifactRepository,
    DeterministicGaUnsupportedError,
)
from htdt.occluder_grid_index import _IndexedOccluderRows
from htdt.cad_late_field_energy import (
    LATE_ENERGY_DECAY_OBSERVABLE,
    LATE_FIELD_ARTIFACT_SCHEMA_REF,
    LATE_FIELD_ENGINE_ID,
    LATE_FIELD_ENGINE_VERSION,
    LATE_FIELD_IMPLEMENTATION_REF,
    CadLateFieldEnergyArtifactRepository,
    DeclaredDiffractingEdge,
    HtdtLateFieldEnergyEngine,
    LateFieldConfiguration,
    _segment_blocked_ignoring_triangles,
    build_late_field_configuration,
    build_late_field_result_envelope,
    execute_late_field_energy,
    late_energy_observable_manifest,
)
from htdt.cad_scene import Position3
from htdt.clock import utc_now_iso

from test_cad_geometric_acoustics_adapter import (  # noqa: E402  (shared fixtures)
    _fixture,
    _generated_occluder_rows,
    _material,
    _portal_fixture,
)


def _late_configuration(**kwargs):
    return build_late_field_configuration(**kwargs)


def _execute_late(fx, configuration=None):
    return execute_late_field_energy(
        execution_input=fx['execution_input'],
        late_field_configuration=(
            configuration if configuration is not None else _late_configuration()
        ),
        compiled_geometry=fx['compiled'],
        region_authority=fx['region'],
        portal_authority=fx['portals'],
        boundary_termination_authority=fx['terminations'],
        directivity_datasets=(fx['dataset'],),
        material_resolver=fx['material_resolver'],
    )


def _vertex_index(fx, x_m, y_m, z_m) -> int:
    for index, vertex in enumerate(fx['compiled'].vertices):
        if (
            abs(vertex.x_m - x_m) < 1e-9
            and abs(vertex.y_m - y_m) < 1e-9
            and abs(vertex.z_m - z_m) < 1e-9
        ):
            return index
    raise AssertionError(f'compiled vertex {(x_m, y_m, z_m)} not found')


def _contributions(artifact, kind):
    return [item for item in artifact.contributions if item.kind == kind]


def _rejections(artifact, decision):
    return [
        item
        for item in artifact.rejected_candidates
        if item.decision == decision
    ]


def test_surface_scattering_emits_bounded_contributions_with_analytic_bounds(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    artifact = _execute_late(fx)

    contributions = _contributions(artifact, 'surface_scattering')
    assert len(artifact.contributions) == 6
    assert len(contributions) == 6
    assert not artifact.rejected_candidates
    assert {
        item.interaction_surface_id for item in contributions
    } == {
        plane.source_surface_id
        for plane in fx['execution_input'].boundary_planes
    }

    floor_id = fx['surface_by_key']['floor-z-min']
    floor = next(
        item for item in contributions if item.interaction_surface_id == floor_id
    )
    assert floor.source_entity_id == 'speaker-fl'
    assert floor.kind == 'surface_scattering'
    assert floor.diffracting_edge is None
    assert floor.apex_clamped_to_segment_endpoint is None
    assert floor.energy_semantics == 'upper_bound_not_point_estimate'
    assert floor.coherent_phase == 'NOT_APPLICABLE_ENERGY_DOMAIN'
    assert floor.direction_semantics == (
        'world_propagation_direction_source_out_and_receiver_in'
    )
    assert floor.late_field_implementation_ref == LATE_FIELD_IMPLEMENTATION_REF
    centroid = floor.interaction_point
    assert (centroid.x_m, centroid.y_m, centroid.z_m) == (2.0, 1.5, 0.0)

    # Analytic bound: D*A/(4*pi*d1_min^2) * (1-a)*s * 1/(2*pi*d2_min^2)
    # (interior half-space re-emission — a boundary patch cannot emit
    # through its own surface).
    # floor patch area 12 m2, d1_min = d2_min = sqrt(3) m.
    assert len(floor.bands) == 2
    band = next(item for item in floor.bands if item.center_hz == 500.0)
    assert band.patch_area_m2 == 12.0
    assert band.band_definition == 'exact_center_frequency_sample'
    assert band.quantity == 'late_energy_upper_bound_per_m2'
    assert band.coherent_phase == 'NOT_APPLICABLE_ENERGY_DOMAIN'
    assert isclose(band.incident_distance_bound_m, sqrt(3.0), abs_tol=1e-9)
    assert isclose(band.emergent_distance_bound_m, sqrt(3.0), abs_tol=1e-9)
    assert isclose(band.redirected_fraction, (1.0 - 0.2) * 0.1, abs_tol=1e-12)
    expected = (
        band.source_directivity.energy_factor
        * 12.0
        / (4.0 * pi * 3.0)
        * 0.08
        / (2.0 * pi * 3.0)
    )
    assert isclose(
        band.late_energy_upper_bound_per_m2,
        expected,
        rel_tol=1e-9,
    )
    assert band.boundary_material is not None
    assert band.boundary_material.absorption == 0.2
    assert band.boundary_material.scattering == 0.1
    assert isclose(
        band.boundary_material.specular_energy_factor,
        (1.0 - 0.2) * (1.0 - 0.1),
        abs_tol=1e-12,
    )
    band_1000 = next(
        item for item in floor.bands if item.center_hz == 1000.0
    )
    assert isclose(band_1000.redirected_fraction, (1.0 - 0.3) * 0.2, abs_tol=1e-12)

    # Delay equals the exact source-centroid-receiver path length over c.
    expected_length = sqrt(
        (2.0 - 1.0) ** 2 + (1.5 - 1.0) ** 2 + (0.0 - 1.0) ** 2
    ) + sqrt(
        (2.0 - 3.0) ** 2 + (1.5 - 2.0) ** 2 + (0.0 - 1.0) ** 2
    )
    assert isclose(
        floor.geometric_path_length_m, expected_length, abs_tol=1e-9
    )
    assert isclose(
        floor.propagation_delay_s,
        expected_length / fx['execution_input'].sound_speed_m_s,
        abs_tol=1e-9,
    )

    ordering = [
        (
            item.source_entity_id,
            item.receiver_id,
            item.kind,
            item.interaction_surface_id,
            item.contribution_id,
        )
        for item in artifact.contributions
    ]
    assert ordering == sorted(ordering)


def test_late_field_execution_is_deterministic_and_engine_bound(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    first = _execute_late(fx)
    second = _execute_late(fx)
    assert first == second
    assert first.artifact_id == second.artifact_id
    assert first.engine_id == LATE_FIELD_ENGINE_ID
    assert first.engine_version == LATE_FIELD_ENGINE_VERSION
    assert first.late_field_implementation_ref == LATE_FIELD_IMPLEMENTATION_REF
    # The artifact records the dispatch's deterministic-GA solver ref; the
    # late-field kernel's own ref is separate and explicitly declared.
    assert first.solver_implementation_ref == (
        fx['execution_input'].solver_implementation_ref
    )
    assert (
        first.solver_implementation_ref
        != first.late_field_implementation_ref
    )
    capability = first.capability_record
    assert capability.supported_geometry_policies == (
        'exact_axis_aligned_closed_shoebox_v1',
        'general_planar_closed_polyhedral_v1',
    )
    assert 'general_planar_multi_region_portal_v1' not in (
        capability.supported_geometry_policies
    )
    assert capability.maximum_scattering_order == 1
    assert capability.maximum_diffraction_order == 1
    assert capability.unsupported_capabilities


def test_wedge_diffraction_resolves_apex_and_rejects_occluded_edges(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path, occluder=True)
    v9 = _vertex_index(fx, 1.8, 1.3, 0.5)
    v10 = _vertex_index(fx, 2.2, 1.3, 0.5)
    v11 = _vertex_index(fx, 2.0, 1.7, 0.5)
    v12 = _vertex_index(fx, 2.0, 1.5, 1.5)
    edges = tuple(
        DeclaredDiffractingEdge(
            vertex_a=min(a, b),
            vertex_b=max(a, b),
            edge_semantics='wedge',
        )
        for a, b in ((v9, v10), (v9, v11), (v10, v11), (v9, v12), (v10, v12), (v11, v12))
    )
    artifact = _execute_late(
        fx, _late_configuration(declared_diffracting_edges=edges)
    )

    wedge = _contributions(artifact, 'edge_diffraction')
    assert len(wedge) == 2
    by_key = {item.diffracting_edge.edge_key: item for item in wedge}
    assert set(by_key) == {
        f'wedge:{min(v10, v12)}-{max(v10, v12)}',
        f'wedge:{min(v11, v12)}-{max(v11, v12)}',
    }

    back = by_key[f'wedge:{min(v11, v12)}-{max(v11, v12)}']
    edge = back.diffracting_edge
    assert edge.edge_semantics == 'wedge'
    assert len(edge.incident_triangle_indices) == 2
    assert len(set(edge.incident_surface_ids)) == 1
    assert isclose(
        edge.wedge_face_separation_angle_deg, 126.0, abs_tol=0.2
    )
    assert back.apex_clamped_to_segment_endpoint is False
    apex = back.interaction_point
    assert isclose(apex.x_m, 2.0, abs_tol=1e-9)
    assert isclose(apex.y_m, 1.5969, abs_tol=1e-3)
    assert isclose(apex.z_m, 1.0157, abs_tol=1e-3)
    expected_length = sqrt(
        (apex.x_m - 1.0) ** 2 + (apex.y_m - 1.0) ** 2 + (apex.z_m - 1.0) ** 2
    ) + sqrt(
        (apex.x_m - 3.0) ** 2 + (apex.y_m - 2.0) ** 2 + (apex.z_m - 1.0) ** 2
    )
    assert isclose(
        back.geometric_path_length_m, expected_length, abs_tol=1e-9
    )
    band = next(item for item in back.bands if item.center_hz == 500.0)
    assert band.boundary_material is None
    assert band.patch_area_m2 is None
    assert band.redirected_fraction == 0.1
    d1 = min(
        sqrt(
            (1.0 - 2.0) ** 2 + (1.0 - 1.7) ** 2 + (1.0 - 0.5) ** 2
        ),
        sqrt(
            (1.0 - 2.0) ** 2 + (1.0 - 1.5) ** 2 + (1.0 - 1.5) ** 2
        ),
    )
    d2 = min(
        sqrt(
            (3.0 - 2.0) ** 2 + (2.0 - 1.7) ** 2 + (1.0 - 0.5) ** 2
        ),
        sqrt(
            (3.0 - 2.0) ** 2 + (2.0 - 1.5) ** 2 + (1.0 - 1.5) ** 2
        ),
    )
    assert isclose(band.incident_distance_bound_m, d1, abs_tol=1e-9)
    assert isclose(band.emergent_distance_bound_m, d2, abs_tol=1e-9)
    # The apex re-emits into the smallest air-side dihedral the edge can
    # open into, 2*(pi - sigma) steradians — never the full 4*pi sphere.
    sigma_rad = radians(edge.wedge_face_separation_angle_deg)
    assert isclose(
        band.late_energy_upper_bound_per_m2,
        band.source_directivity.energy_factor
        * 0.1
        / (4.0 * pi * d1 * d1)
        / (2.0 * (pi - sigma_rad) * d2 * d2),
        rel_tol=1e-9,
    )

    occluded = _rejections(artifact, 'EDGE_REQUIRES_HIGHER_ORDER_DIFFRACTION')
    assert {
        item.interaction_key for item in occluded
    } == {
        f'wedge:{min(a, b)}-{max(a, b)}'
        for a, b in ((v9, v10), (v9, v11), (v10, v11), (v9, v12))
    }
    assert all(item.kind == 'edge_diffraction' for item in occluded)


def test_aperture_rim_diffraction_at_two_surface_junction(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    # Floor/front junction edge (0,0,0)-(4,0,0): two incident non-coplanar
    # triangles belonging to two different room-boundary surfaces.
    edge = DeclaredDiffractingEdge(
        vertex_a=_vertex_index(fx, 0.0, 0.0, 0.0),
        vertex_b=_vertex_index(fx, 4.0, 0.0, 0.0),
        edge_semantics='aperture_rim',
    )
    artifact = _execute_late(
        fx, _late_configuration(declared_diffracting_edges=(edge,))
    )
    rim = _contributions(artifact, 'aperture_diffraction')
    assert len(rim) == 1
    contribution = rim[0]
    assert contribution.diffracting_edge.edge_semantics == 'aperture_rim'
    assert len(contribution.diffracting_edge.incident_surface_ids) == 2
    assert isclose(
        contribution.diffracting_edge.wedge_face_separation_angle_deg,
        90.0,
        abs_tol=1e-6,
    )
    apex = contribution.interaction_point
    assert isclose(apex.y_m, 0.0, abs_tol=1e-9)
    assert isclose(apex.z_m, 0.0, abs_tol=1e-9)
    assert isclose(apex.x_m, 1.7749, abs_tol=1e-3)
    assert contribution.apex_clamped_to_segment_endpoint is False
    band = contribution.bands[0]
    d1 = sqrt(3.0)
    d2 = sqrt(6.0)
    assert isclose(band.incident_distance_bound_m, d1, abs_tol=1e-9)
    assert isclose(band.emergent_distance_bound_m, d2, abs_tol=1e-9)


def test_undiffracting_edge_declarations_fail_closed(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, occluder=True)
    edges = (
        # Coplanar floor diagonal — two coplanar incident triangles.
        DeclaredDiffractingEdge(
            vertex_a=_vertex_index(fx, 0.0, 0.0, 0.0),
            vertex_b=_vertex_index(fx, 4.0, 3.0, 0.0),
            edge_semantics='wedge',
        ),
        # Two-surface junction misdeclared as a single-surface wedge.
        DeclaredDiffractingEdge(
            vertex_a=_vertex_index(fx, 0.0, 0.0, 0.0),
            vertex_b=_vertex_index(fx, 4.0, 0.0, 0.0),
            edge_semantics='wedge',
        ),
        # Vertex pair that is not a mesh edge.
        DeclaredDiffractingEdge(
            vertex_a=_vertex_index(fx, 0.0, 0.0, 0.0),
            vertex_b=_vertex_index(fx, 4.0, 3.0, 2.0),
            edge_semantics='wedge',
        ),
        # Solid tetra edge misdeclared as a two-surface aperture rim.
        DeclaredDiffractingEdge(
            vertex_a=_vertex_index(fx, 1.8, 1.3, 0.5),
            vertex_b=_vertex_index(fx, 2.2, 1.3, 0.5),
            edge_semantics='aperture_rim',
        ),
    )
    artifact = _execute_late(
        fx, _late_configuration(declared_diffracting_edges=edges)
    )
    undiffracting = _rejections(artifact, 'UNDIFRACTING_EDGE')
    assert len(undiffracting) == 4
    assert not _contributions(artifact, 'edge_diffraction')
    assert not _contributions(artifact, 'aperture_diffraction')

    with pytest.raises(ValueError, match='outside the compiled mesh'):
        _execute_late(
            fx,
            _late_configuration(
                declared_diffracting_edges=(
                    DeclaredDiffractingEdge(
                        vertex_a=0,
                        vertex_b=len(fx['compiled'].vertices) + 2,
                        edge_semantics='wedge',
                    ),
                )
            ),
        )
    with pytest.raises(ValueError, match='vertex_a < vertex_b'):
        DeclaredDiffractingEdge(
            vertex_a=2,
            vertex_b=2,
            edge_semantics='wedge',
        )


def test_scattering_occlusion_and_capability_fail_closed(tmp_path: Path) -> None:
    # Receiver raised above the tetra occluder: the floor patch centroid's
    # emergent segment crosses the occluder body.
    fx = _fixture(
        tmp_path,
        occluder=True,
        receiver_position=Position3(x_m=2.0, y_m=1.5, z_m=1.8),
    )
    artifact = _execute_late(fx)
    blocked = _rejections(artifact, 'BLOCKED_VISIBILITY')
    assert {item.interaction_key for item in blocked} == {
        fx['surface_by_key']['floor-z-min']
    }
    assert not any(
        item.interaction_surface_id == fx['surface_by_key']['floor-z-min']
        for item in artifact.contributions
    )


def test_unsupported_boundary_quantity_rejects_scattering(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path, supported_material=False)
    artifact = _execute_late(fx)
    assert not artifact.contributions
    unsupported = _rejections(artifact, 'UNSUPPORTED_BOUNDARY_QUANTITY')
    assert len(unsupported) == 6


def test_narrow_directivity_coverage_rejects_uncovered_departures(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path, narrow_directivity=True)
    artifact = _execute_late(fx)
    uncovered = _rejections(artifact, 'UNSUPPORTED_DIRECTIVITY')
    assert uncovered
    covered_surfaces = {
        item.interaction_surface_id for item in artifact.contributions
    }
    assert covered_surfaces.isdisjoint(
        {item.interaction_key for item in uncovered}
    )


def test_portal_geometry_policy_fails_closed(tmp_path: Path) -> None:
    fx = _portal_fixture(tmp_path)
    with pytest.raises(DeterministicGaUnsupportedError) as error:
        _execute_late(fx)
    assert error.value.reason_code == 'UNSUPPORTED_GEOMETRY'


def test_execution_input_exact_authority_mismatch_fails_closed(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path / 'a')
    other = _fixture(tmp_path / 'b', occluder=True)
    with pytest.raises(ValueError, match='compiled geometry exact identity'):
        execute_late_field_energy(
            execution_input=fx['execution_input'],
            late_field_configuration=_late_configuration(),
            compiled_geometry=other['compiled'],
            region_authority=fx['region'],
            portal_authority=fx['portals'],
            boundary_termination_authority=fx['terminations'],
            directivity_datasets=(fx['dataset'],),
            material_resolver=fx['material_resolver'],
        )
    with pytest.raises(ValueError, match='exact geometry authority'):
        execute_late_field_energy(
            execution_input=fx['execution_input'],
            late_field_configuration=_late_configuration(),
            compiled_geometry=fx['compiled'],
            region_authority=fx['portals'],
            portal_authority=fx['portals'],
            boundary_termination_authority=fx['terminations'],
            directivity_datasets=(fx['dataset'],),
            material_resolver=fx['material_resolver'],
        )


def test_configuration_policy_is_canonical_and_bounded() -> None:
    edge = DeclaredDiffractingEdge(
        vertex_a=0, vertex_b=1, edge_semantics='wedge'
    )
    configuration = build_late_field_configuration(
        enabled_kinds=('edge_diffraction', 'surface_scattering'),
        declared_diffracting_edges=(edge,),
    )
    assert configuration.enabled_kinds == (
        'edge_diffraction',
        'surface_scattering',
    )
    assert configuration.configuration_id.startswith(
        'r150-late-field-configuration:'
    )
    assert configuration.authority_version == 'r150-late-field-energy-1'

    with pytest.raises(ValueError, match='enabled diffraction kind'):
        build_late_field_configuration(
            enabled_kinds=('surface_scattering',),
            declared_diffracting_edges=(edge,),
        )
    with pytest.raises(ValueError):
        DeclaredDiffractingEdge(
            vertex_a=0, vertex_b=1, edge_semantics='rim'
        )
    with pytest.raises(Exception):
        build_late_field_configuration(diffraction_energy_bound_factor=0.0)
    with pytest.raises(Exception):
        build_late_field_configuration(diffraction_energy_bound_factor=1.5)

    dumped = LateFieldConfiguration.model_validate(
        configuration.model_dump(mode='python')
    )
    assert dumped == configuration


def test_observable_manifest_and_envelope_fail_closed_binding(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    artifact = _execute_late(fx)
    observable = late_energy_observable_manifest(artifact)
    assert observable.observable == LATE_ENERGY_DECAY_OBSERVABLE
    assert observable.artifact_authority == artifact.as_external_ref()
    assert observable.encoding_schema_ref == LATE_FIELD_ARTIFACT_SCHEMA_REF
    assert observable.valid_frequency_domain == artifact.frequency_domain

    # The consuming request must declare 'late_energy_decay' — the deterministic
    # fixture request asks only for 'deterministic_paths', so envelope build
    # fails closed on the exact observable binding.
    with pytest.raises(ValueError):
        build_late_field_result_envelope(
            dispatch=fx['dispatch'],
            request=fx['request'],
            artifact=artifact,
            completed_at_utc=utc_now_iso(),
        )


def test_repository_persistence_revalidates_every_exact_authority(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    configuration = _late_configuration()
    artifact = _execute_late(fx, configuration)

    deterministic_repository = CadDeterministicPathArtifactRepository(
        fx['scene_repository'],
        snapshot_repository=fx['snapshot_repository'],
        dispatch_repository=fx['dispatch_repository'],
        configuration_resolver=fx['configuration_resolver'],
        material_resolver=fx['material_resolver'],
        geometry_authority_resolver=fx['geometry_resolver'],
    )
    deterministic_repository.save_execution_input(fx['execution_input'])

    repository = CadLateFieldEnergyArtifactRepository(
        fx['scene_repository'],
        snapshot_repository=fx['snapshot_repository'],
        dispatch_repository=fx['dispatch_repository'],
        configuration_resolver=fx['configuration_resolver'],
        late_field_configuration_resolver=lambda ref: (
            configuration
            if configuration.as_external_ref() == ref
            else None
        ),
        geometry_authority_resolver=fx['geometry_resolver'],
        material_resolver=fx['material_resolver'],
    )
    repository.save(artifact)
    assert repository.get(artifact.artifact_id) == artifact
    assert repository.save(artifact) == artifact

    ref = artifact.as_external_ref()
    assert repository.resolve_external_authority(ref) == ref
    provenance = artifact.execution_provenance_ref()
    assert repository.resolve_external_authority(provenance) == provenance
    manifest = repository.resolve_artifact_manifest(ref)
    assert manifest is not None
    assert manifest.observable == 'late_energy_decay'
    assert manifest.encoding_schema_ref == LATE_FIELD_ARTIFACT_SCHEMA_REF
    assert manifest.solver_lineage['execution_input_id'] == (
        artifact.execution_input_id
    )
    assert manifest.solver_lineage['late_field_configuration_id'] == (
        configuration.configuration_id
    )

    # Stale authority: the recorded material authority no longer reproduces
    # the artifact — reopening fails closed.
    fx['material_box']['value'] = _material(supported=False)
    with pytest.raises(ValueError):
        repository.get(artifact.artifact_id)


def test_persisted_late_field_read_loads_each_authority_once(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    configuration = _late_configuration()
    artifact = _execute_late(fx, configuration)

    deterministic_repository = CadDeterministicPathArtifactRepository(
        fx['scene_repository'],
        snapshot_repository=fx['snapshot_repository'],
        dispatch_repository=fx['dispatch_repository'],
        configuration_resolver=fx['configuration_resolver'],
        material_resolver=fx['material_resolver'],
        geometry_authority_resolver=fx['geometry_resolver'],
    )
    deterministic_repository.save_execution_input(fx['execution_input'])

    repository = CadLateFieldEnergyArtifactRepository(
        fx['scene_repository'],
        snapshot_repository=fx['snapshot_repository'],
        dispatch_repository=fx['dispatch_repository'],
        configuration_resolver=fx['configuration_resolver'],
        late_field_configuration_resolver=lambda ref: (
            configuration
            if configuration.as_external_ref() == ref
            else None
        ),
        geometry_authority_resolver=fx['geometry_resolver'],
        material_resolver=fx['material_resolver'],
    )
    repository.save(artifact)

    snapshot_loads: list[str] = []
    original_get_snapshot = fx['snapshot_repository'].get_snapshot

    def spy(snapshot_id):
        snapshot_loads.append(snapshot_id)
        return original_get_snapshot(snapshot_id)

    fx['snapshot_repository'].get_snapshot = spy
    try:
        assert repository.get(artifact.artifact_id) == artifact
    finally:
        fx['snapshot_repository'].get_snapshot = original_get_snapshot
    assert snapshot_loads == [artifact.snapshot_id]


def test_segment_blocked_ignoring_triangles_grid_matches_brute_force() -> None:
    """Indexed occluder records must yield identical pass/block verdicts."""
    rng = random.Random(20240924)
    base_rows = _generated_occluder_rows(rng, 400)
    records = tuple((index,) + row[1:] for index, row in enumerate(base_rows))
    indexed = _IndexedOccluderRows(records)
    plain = tuple(records)
    ignored = frozenset({3, 97, 250, 399})
    mismatches = []
    for _ in range(1500):
        start = tuple(rng.uniform(-1.0, 10.0) for _ in range(3))
        end = tuple(rng.uniform(-1.0, 10.0) for _ in range(3))
        for tolerance in (1.0e-9, 1.0e-6):
            expected = _segment_blocked_ignoring_triangles(
                None,
                start,
                end,
                tolerance=tolerance,
                ignored_triangle_indices=ignored,
                occluder_records=plain,
            )
            actual = _segment_blocked_ignoring_triangles(
                None,
                start,
                end,
                tolerance=tolerance,
                ignored_triangle_indices=ignored,
                occluder_records=indexed,
            )
            if actual != expected:
                mismatches.append((start, end, tolerance))
    assert mismatches == []
