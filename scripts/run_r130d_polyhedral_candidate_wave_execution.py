from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import traceback

import numpy as np

from htdt.acoustic_pffdtd_polyhedral_geometry import (
    compile_r120b_polyhedral_to_pffdtd,
    r130d_snapshot_geometry_identity,
    register_r120b_polyhedral_authorities,
)
from htdt.cad_candidate_wave_execution import (
    CandidateResourceConfiguration,
    CandidateWaveExecutionError,
    build_pffdtd_candidate_configuration,
)
from htdt.r120_polyhedral_geometry import (
    PlanarPolygonSurfaceSpec,
    PolyhedralAirVolume,
    PolyhedralAuthorityRef,
    compile_r120_polyhedral_geometry,
    make_r120_polyhedral_semantic_geometry,
)
from run_r130a_candidate_wave_execution import (
    PFFDTD_SHA,
    _fixture as build_r130a_fixture,
)
from htdt.acoustics.services.acoustic_pffdtd_polyhedral_executor import PffdtdPolyhedralCandidateWaveExecutor


FIXTURE_ID = 'r130d-polyhedral-candidate-wave-v1'


def _poly_material(ref) -> PolyhedralAuthorityRef:
    return PolyhedralAuthorityRef(
        authority_id=ref.authority_id,
        authority_version=ref.authority_version,
        semantic_hash_sha256=ref.semantic_hash_sha256,
    )


def _rectangular_triangle_fixture():
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
    triangles = (
        (0, 3, 2),
        (0, 2, 1),
        (4, 5, 6),
        (4, 6, 7),
        (0, 1, 5),
        (0, 5, 4),
        (3, 7, 6),
        (3, 6, 2),
        (0, 4, 7),
        (0, 7, 3),
        (1, 2, 6),
        (1, 6, 5),
    )
    faces = tuple(
        (f'triangle-{index:02d}', triangle)
        for index, triangle in enumerate(triangles)
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


def _concave_same_bbox_fixture():
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


def _make_polyhedron(
    *,
    snapshot,
    source_key: str,
    vertices,
    faces,
    material_ref,
):
    semantic = make_r120_polyhedral_semantic_geometry(
        source_geometry_identity=r130d_snapshot_geometry_identity(
            snapshot.snapshot_id,
            snapshot.semantic_sha256,
            source_key,
        ),
        source_geometry_kind='explicit_polyhedral',
        vertices=tuple(vertices),
        surfaces=tuple(
            PlanarPolygonSurfaceSpec(
                surface_key=key,
                semantic_class='room_boundary',
                outer_vertex_indices=loop,
                material_authority=_poly_material(material_ref),
            )
            for key, loop in faces
        ),
        air_volumes=(
            PolyhedralAirVolume(
                region_id='room-air',
                boundary_surface_keys=tuple(key for key, _ in faces),
            ),
        ),
    )
    compiled = compile_r120_polyhedral_geometry(
        semantic,
        topology_tolerance_m=1.0e-9,
    )
    return semantic, compiled


def _pressure(store, result) -> np.ndarray:
    payload = store.read_payload(result.artifacts[0].artifact_authority)
    return np.asarray(payload['pressure_real_pa'], dtype=np.float64) + 1j * np.asarray(
        payload['pressure_imag_pa'],
        dtype=np.float64,
    )


def _result_evidence(fixture, result) -> dict[str, object]:
    store = fixture['store']
    artifact = store.read_payload(result.artifacts[0].artifact_authority)
    provenance = store.read_payload(result.execution_provenance_ref)
    solver_geometry_ref = artifact['solver_geometry_ref']
    executed_grid_ref = artifact['executed_grid_ref']
    solver_geometry = store.read_payload(
        type(result.artifacts[0].artifact_authority).model_validate(
            solver_geometry_ref
        )
    )
    executed_grid = store.read_payload(
        type(result.artifacts[0].artifact_authority).model_validate(
            executed_grid_ref
        )
    )
    return {
        'result_id': result.result_id,
        'result_sha256': result.semantic_sha256,
        'artifact_ref': result.artifacts[0].artifact_authority.model_dump(
            mode='json'
        ),
        'provenance_ref': result.execution_provenance_ref.model_dump(mode='json'),
        'candidate_execution_input_id': artifact['candidate_execution_input_id'],
        'candidate_execution_input_sha256': (
            artifact['candidate_execution_input_sha256']
        ),
        'solver_geometry_ref': solver_geometry_ref,
        'solver_geometry_sha256': solver_geometry['semantic_sha256']
        if 'semantic_sha256' in solver_geometry
        else solver_geometry_ref['semantic_hash_sha256'],
        'generated_geometry_sha256': solver_geometry[
            'generated_geometry_sha256'
        ],
        'grid_origin_m': solver_geometry['grid_origin_m'],
        'grid_spacing_m': solver_geometry['grid_spacing_m'],
        'grid_dimensions': solver_geometry['grid_dimensions'],
        'executed_grid_ref': executed_grid_ref,
        'boundary_mask_logical_sha256': executed_grid[
            'boundary_mask_logical_sha256'
        ],
        'cart_grid_logical_sha256': executed_grid[
            'cart_grid_logical_sha256'
        ],
        'boundary_node_count': executed_grid['boundary_node_count'],
        'resource_estimate_ref': provenance['resource_estimate_ref'],
        'resource_estimate': provenance['resource_estimate'],
        'timings_s': provenance['timings_s'],
        'raw_solver_asset_sha256': provenance['raw_solver_asset_sha256'],
        'pressure_real_pa': artifact['pressure_real_pa'],
        'pressure_imag_pa': artifact['pressure_imag_pa'],
    }


def _tiny_resource_configuration(fixture):
    cfg = fixture['configuration']
    return build_pffdtd_candidate_configuration(
        expected_pffdtd_commit_sha=cfg.expected_pffdtd_commit_sha,
        fmax_hz=float(cfg.fmax_hz),
        points_per_wavelength=float(cfg.points_per_wavelength),
        duration_s=float(cfg.duration_s),
        frequency_samples_hz=tuple(float(x) for x in cfg.frequency_samples_hz),
        density_kg_m3=float(cfg.density_kg_m3),
        density_authority_ref=cfg.density_authority_ref,
        relative_humidity_percent=float(cfg.relative_humidity_percent),
        humidity_authority_ref=cfg.humidity_authority_ref,
        resource=CandidateResourceConfiguration(
            solver_threads=1,
            setup_processes=1,
            max_grid_cells=1,
            max_time_steps=cfg.resource.max_time_steps,
            max_output_bytes=cfg.resource.max_output_bytes,
            max_solver_wall_seconds=cfg.resource.max_solver_wall_seconds,
        ),
    )


def _restore_pinned_pffdtd_checkout(executor) -> None:
    root = Path(executor.upstream_root)
    subprocess.run(
        ['git', '-C', str(root), 'reset', '--hard', PFFDTD_SHA],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ['git', '-C', str(root), 'clean', '-fd'],
        check=True,
        capture_output=True,
        text=True,
    )
    actual = subprocess.run(
        ['git', '-C', str(root), 'rev-parse', 'HEAD'],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip().lower()
    if actual != PFFDTD_SHA:
        raise AssertionError(
            f'PFFDTD restore did not reproduce pinned commit: {actual}'
        )


def _tamper_reopen_rejection(fixture, result) -> bool:
    store = fixture['store']
    artifact_ref = result.artifacts[0].artifact_authority
    path = store.path_for(artifact_ref)
    original = path.read_text(encoding='utf-8')
    document = json.loads(original)
    document['payload']['units'] = 'tampered-Pa'
    path.write_text(
        json.dumps(document, sort_keys=True, separators=(',', ':')) + '\n',
        encoding='utf-8',
    )
    rejected = False
    try:
        fixture['result_repository'].get(result.result_id)
    except ValueError:
        rejected = True
    finally:
        path.write_text(original, encoding='utf-8')
    return rejected


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Run bounded R130D exact-polyhedral PFFDTD CPU evidence'
    )
    parser.add_argument('--upstream-root', required=True, type=Path)
    parser.add_argument('--work-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.work_root.exists():
        shutil.rmtree(args.work_root)
    args.work_root.mkdir(parents=True)

    payload: dict[str, object] = {
        'schema_version': 'htdt.r130d.polyhedral-candidate-wave-evidence-1',
        'fixture_id': FIXTURE_ID,
        'candidate_backend': 'PFFDTD Python/Numba CPU',
        'candidate_source_commit_sha': PFFDTD_SHA,
        'software_execution_status': 'FAIL',
        'general_3d_physics_validation_status': 'NOT_VALIDATED',
        'production_solver_selected': False,
        'gpu_execution': False,
        'portal_execution': False,
        'multi_region_execution': False,
        'owned_room_evidence': False,
        'rdc_calls': 0,
        'htdt_capture_changed': False,
    }
    try:
        fixture = build_r130a_fixture(
            args.work_root / 'fixture',
            args.upstream_root,
            boundary_mode='rigid',
            fixture_id=FIXTURE_ID,
        )
        snapshot = fixture['snapshot']
        boundary_binding = snapshot.surface_boundary_configuration[0]
        material_ref = boundary_binding.material_authority
        rigid_boundary_ref = boundary_binding.boundary_physics_authority
        if material_ref is None or rigid_boundary_ref is None:
            raise AssertionError('rigid fixture is missing exact boundary authorities')

        variants = {}
        for name, factory in (
            ('rectangular', _rectangular_triangle_fixture),
            ('sloped', _sloped_same_bbox_fixture),
            ('concave', _concave_same_bbox_fixture),
        ):
            semantic, compiled = _make_polyhedron(
                snapshot=snapshot,
                source_key=name,
                vertices=factory()[0],
                faces=factory()[1],
                material_ref=material_ref,
            )
            semantic_ref, compiled_ref = register_r120b_polyhedral_authorities(
                fixture['store'],
                semantic=semantic,
                compiled=compiled,
            )
            variants[name] = {
                'semantic': semantic,
                'compiled': compiled,
                'semantic_ref': semantic_ref,
                'compiled_ref': compiled_ref,
            }

        executor = PffdtdPolyhedralCandidateWaveExecutor(
            base_executor=fixture['executor'],
            containment_tolerance_m=1.0e-9,
        )

        first_authority, first_model, first_representation = (
            executor.compile_input(
                dispatch_binding_id=fixture['dispatch'].binding_id,
                configuration=fixture['configuration'],
                semantic_geometry_ref=variants['rectangular']['semantic_ref'],
                compiled_geometry_ref=variants['rectangular']['compiled_ref'],
                rigid_boundary_physics_ref=rigid_boundary_ref,
            )
        )
        second_authority, second_model, second_representation = (
            executor.compile_input(
                dispatch_binding_id=fixture['dispatch'].binding_id,
                configuration=fixture['configuration'],
                semantic_geometry_ref=variants['rectangular']['semantic_ref'],
                compiled_geometry_ref=variants['rectangular']['compiled_ref'],
                rigid_boundary_physics_ref=rigid_boundary_ref,
            )
        )
        if (
            second_authority != first_authority
            or second_model != first_model
            or second_representation != first_representation
        ):
            raise AssertionError('R130D compilation is not deterministic')

        stale_semantic, stale_compiled = _make_polyhedron(
            snapshot=snapshot,
            source_key='stale-source',
            vertices=_sloped_same_bbox_fixture()[0],
            faces=_sloped_same_bbox_fixture()[1],
            material_ref=material_ref,
        )
        stale_semantic = stale_semantic.model_copy(
            update={'source_geometry_identity': 'fixture:deliberately-stale'}
        )
        # Rebuild after changing source identity so its content identity remains valid.
        stale_semantic = make_r120_polyhedral_semantic_geometry(
            source_geometry_identity='fixture:deliberately-stale',
            source_geometry_kind='explicit_polyhedral',
            vertices=tuple(vertex.point() for vertex in stale_compiled.vertices),
            surfaces=tuple(
                PlanarPolygonSurfaceSpec(
                    surface_key=surface.surface_key,
                    semantic_class=surface.semantic_class,
                    outer_vertex_indices=surface.outer_vertex_indices,
                    material_authority=surface.material_authority,
                )
                for surface in stale_semantic.surfaces
            ),
            air_volumes=stale_semantic.air_volumes,
        )
        stale_compiled = compile_r120_polyhedral_geometry(stale_semantic)
        stale_refs = register_r120b_polyhedral_authorities(
            fixture['store'],
            semantic=stale_semantic,
            compiled=stale_compiled,
        )
        stale_snapshot_rejected = False
        try:
            executor.compile_input(
                dispatch_binding_id=fixture['dispatch'].binding_id,
                configuration=fixture['configuration'],
                semantic_geometry_ref=stale_refs[0],
                compiled_geometry_ref=stale_refs[1],
                rigid_boundary_physics_ref=rigid_boundary_ref,
            )
        except CandidateWaveExecutionError as exc:
            stale_snapshot_rejected = 'stale/unbound' in str(exc)
        if not stale_snapshot_rejected:
            raise AssertionError('stale R120B snapshot binding was not rejected')

        resource_ceiling_rejected = False
        try:
            compile_r120b_polyhedral_to_pffdtd(
                semantic=variants['rectangular']['semantic'],
                compiled=variants['rectangular']['compiled'],
                source_position_m=(1.5, 2.0, 2.0),
                receivers=(('receiver-1', (2.5, 2.0, 2.0)),),
                sound_speed_m_s=float(snapshot.environment.sound_speed_m_s),
                configuration=_tiny_resource_configuration(fixture),
                source_name='speaker-source',
            )
        except CandidateWaveExecutionError as exc:
            resource_ceiling_rejected = 'resource ceiling' in str(exc)
        if not resource_ceiling_rejected:
            raise AssertionError('R130D grid resource ceiling did not fail closed')

        _restore_pinned_pffdtd_checkout(fixture['executor'])
        legacy_result = fixture['executor'].execute(
            dispatch_binding_id=fixture['dispatch'].binding_id,
            configuration=fixture['configuration'],
        )
        r130d_results = {}
        r130d_evidence = {}
        for name in ('rectangular', 'sloped', 'concave'):
            _restore_pinned_pffdtd_checkout(fixture['executor'])
            result = executor.execute(
                dispatch_binding_id=fixture['dispatch'].binding_id,
                configuration=fixture['configuration'],
                semantic_geometry_ref=variants[name]['semantic_ref'],
                compiled_geometry_ref=variants[name]['compiled_ref'],
                rigid_boundary_physics_ref=rigid_boundary_ref,
            )
            r130d_results[name] = result
            r130d_evidence[name] = _result_evidence(fixture, result)

        legacy_pressure = _pressure(fixture['store'], legacy_result)
        rectangular_pressure = _pressure(
            fixture['store'],
            r130d_results['rectangular'],
        )
        absolute_error = np.abs(rectangular_pressure - legacy_pressure)
        denominator = np.maximum(np.abs(legacy_pressure), 1.0e-12)
        relative_error = absolute_error / denominator
        rectangular_equivalent = bool(
            np.allclose(
                rectangular_pressure,
                legacy_pressure,
                rtol=1.0e-9,
                atol=1.0e-10,
            )
        )
        if not rectangular_equivalent:
            raise AssertionError(
                'R120B rectangular PFFDTD response differs from existing RoomPrism/'
                f'R120 path: max_abs={float(np.max(absolute_error))}, '
                f'max_rel={float(np.max(relative_error))}'
            )

        rectangular_ev = r130d_evidence['rectangular']
        sloped_ev = r130d_evidence['sloped']
        concave_ev = r130d_evidence['concave']
        same_bbox_sloped = (
            rectangular_ev['grid_origin_m'] == sloped_ev['grid_origin_m']
            and rectangular_ev['grid_dimensions'] == sloped_ev['grid_dimensions']
        )
        if not same_bbox_sloped:
            raise AssertionError(
                'sloped fixture no longer shares the rectangular bounding/grid envelope'
            )

        for name, evidence in (
            ('sloped', sloped_ev),
            ('concave', concave_ev),
        ):
            if (
                evidence['generated_geometry_sha256']
                == rectangular_ev['generated_geometry_sha256']
                or evidence['candidate_execution_input_id']
                == rectangular_ev['candidate_execution_input_id']
                or evidence['boundary_mask_logical_sha256']
                == rectangular_ev['boundary_mask_logical_sha256']
                or evidence['artifact_ref']['semantic_hash_sha256']
                == rectangular_ev['artifact_ref']['semantic_hash_sha256']
            ):
                raise AssertionError(
                    f'{name} geometry collapsed onto rectangular solver identity'
                )

        sloped_pressure = _pressure(fixture['store'], r130d_results['sloped'])
        concave_pressure = _pressure(fixture['store'], r130d_results['concave'])
        if np.allclose(
            sloped_pressure,
            rectangular_pressure,
            rtol=1.0e-8,
            atol=1.0e-10,
        ):
            raise AssertionError(
                'sloped geometry did not change the bounded numerical response'
            )
        if np.allclose(
            concave_pressure,
            rectangular_pressure,
            rtol=1.0e-8,
            atol=1.0e-10,
        ):
            raise AssertionError(
                'concave geometry did not change the bounded numerical response'
            )

        reopen = fixture['result_repository'].get(
            r130d_results['concave'].result_id
        )
        exact_reopen = reopen == r130d_results['concave']
        if not exact_reopen:
            raise AssertionError('R130D result did not reopen exactly')
        tamper_rejected = _tamper_reopen_rejection(
            fixture,
            r130d_results['rectangular'],
        )
        if not tamper_rejected:
            raise AssertionError('tampered R130D result authority was not rejected')

        payload.update(
            {
                'status': 'PASS',
                'software_execution_status': 'PASS',
                'rectangular_equivalence': {
                    'status': 'PASS',
                    'reference_lane': 'existing R130A rigid R120/PFFDTD path',
                    'candidate_lane': 'R120B exact polyhedron R130D/PFFDTD path',
                    'same_configuration_ref': (
                        fixture['configuration'].as_external_ref().model_dump(
                            mode='json'
                        )
                    ),
                    'max_absolute_complex_pressure_error_pa': float(
                        np.max(absolute_error)
                    ),
                    'max_relative_complex_pressure_error': float(
                        np.max(relative_error)
                    ),
                    'rtol': 1.0e-9,
                    'atol_pa': 1.0e-10,
                    'legacy_result_id': legacy_result.result_id,
                    'polyhedral_result_id': r130d_results[
                        'rectangular'
                    ].result_id,
                },
                'general_3d_sensitivity': {
                    'status': 'PASS',
                    'physics_validation_status': 'NOT_VALIDATED',
                    'sloped_same_bounding_grid_as_rectangular': same_bbox_sloped,
                    'sloped_response_differs_from_rectangular': True,
                    'concave_response_differs_from_rectangular': True,
                    'mechanical_box_collapse_gate': 'PASS',
                },
                'preflight': {
                    'deterministic_compilation': True,
                    'stale_snapshot_geometry_rejected': stale_snapshot_rejected,
                    'resource_ceiling_rejected_before_backend': (
                        resource_ceiling_rejected
                    ),
                    'source_receiver_exact_polyhedral_containment': True,
                },
                'reopen': {
                    'save_reopen_exact_reresolution': exact_reopen,
                    'tampered_artifact_rejected': tamper_rejected,
                },
                'frequency_axis_hz': list(
                    fixture['configuration'].frequency_samples_hz
                ),
                'bounded_frequency_scope_hz': {
                    'minimum_supported_scope_hz': 20.0,
                    'maximum_supported_scope_hz': 300.0,
                    'actual_evidence_minimum_hz': float(
                        min(fixture['configuration'].frequency_samples_hz)
                    ),
                    'actual_evidence_maximum_hz': float(
                        max(fixture['configuration'].frequency_samples_hz)
                    ),
                },
                'rectangular': rectangular_ev,
                'sloped': sloped_ev,
                'concave': concave_ev,
                'non_claim': (
                    'PASS establishes deterministic software execution, exact '
                    'polyhedral-to-PFFDTD provenance, rectangular regression '
                    'equivalence, and general-3D sensitivity. Sloped/concave '
                    'results have no independent physics reference and remain '
                    'NOT_VALIDATED. This does not select a production solver.'
                ),
            }
        )
    except Exception as exc:
        payload.update(
            {
                'status': 'FAIL',
                'software_execution_status': 'FAIL',
                'error': f'{type(exc).__name__}: {exc}',
                'traceback': traceback.format_exc(),
            }
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get('status') == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
