from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
from pathlib import Path
import shutil
import sys
import traceback

import numpy as np
from pydantic import ValidationError

from htdt.acoustic_pffdtd_causal_boundary import (
    CausalAdmittanceBranch,
    build_causal_frequency_dependent_boundary_authority,
    compile_causal_boundary_to_pffdtd,
    evaluate_normalized_admittance,
)
from htdt.cad_candidate_wave_execution import CandidateWaveExecutionError
from htdt.cad_pffdtd_resource_estimator import (
    PffdtdCandidateResourceEstimator,
)
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef

from run_r130a_candidate_wave_execution import (
    PFFDTD_SHA,
    _fixture,
    _hash_json,
    _verify_reopen_and_tamper,
)


R130C_FIXTURE_ID = 'r130c-candidate-causal-boundary-v1'


def _expect_compile_failure(fixture: dict[str, object], expected: str) -> None:
    try:
        fixture['executor'].compile_input(
            dispatch_binding_id=fixture['dispatch'].binding_id,
            configuration=fixture['configuration'],
        )
    except CandidateWaveExecutionError as exc:
        if expected not in str(exc):
            raise AssertionError(
                f'expected compile failure containing {expected!r}, got {exc!r}'
            ) from exc
    else:
        raise AssertionError(f'expected compile failure containing {expected!r}')


def _preflight_causal_authority(
    fixture: dict[str, object],
    authority,
) -> dict[str, bool]:
    store = fixture['store']
    causal_bindings = [
        item for item in authority.boundary_bindings
        if item.causal_mapping is not None
    ]
    assert len(causal_bindings) == 1
    binding = causal_bindings[0]
    mapping = binding.causal_mapping
    assert mapping is not None

    assert mapping.causality_status == 'CAUSAL'
    assert mapping.passivity_status == 'PASSIVE'
    assert mapping.stability_status == 'STABLE'
    assert mapping.extrapolation_rule == 'forbidden'
    assert mapping.semantic_sha256 != binding.material_authority.semantic_hash_sha256

    material_path = store.path_for(binding.material_authority)
    original_material = material_path.read_bytes()
    material_path.unlink()
    _expect_compile_failure(fixture, 'material')
    material_path.write_bytes(original_material)

    material_path.write_text('{}\n', encoding='utf-8')
    _expect_compile_failure(fixture, 'material')
    material_path.write_bytes(original_material)

    mapping_path = store.path_for(mapping.mapping_authority_ref)
    original_mapping = mapping_path.read_bytes()
    mapping_document = json.loads(original_mapping.decode('utf-8'))
    mapping_document['payload']['mapping_version'] = '2'
    mapping_path.write_text(
        json.dumps(mapping_document, sort_keys=True, separators=(',', ':')) + '\n',
        encoding='utf-8',
    )
    _expect_compile_failure(fixture, 'mapping')
    mapping_path.write_bytes(original_mapping)

    try:
        CausalAdmittanceBranch(
            d_seconds=-1.0e-4,
            e_dimensionless=1.0,
            f_per_second=0.0,
        )
    except ValidationError:
        pass
    else:
        raise AssertionError('non-passive/unstable negative D was accepted')

    try:
        CausalAdmittanceBranch(
            d_seconds=1.0e-4,
            e_dimensionless=0.0,
            f_per_second=0.0,
        )
    except ValidationError:
        pass
    else:
        raise AssertionError('zero damping branch was accepted')

    causal_authority = fixture['causal_authority']
    assert causal_authority is not None
    try:
        evaluate_normalized_admittance(
            causal_authority,
            (40.0, 100.0),
        )
    except ValueError as exc:
        assert 'outside causal boundary valid band' in str(exc)
    else:
        raise AssertionError('causal boundary extrapolation was accepted')

    changed_authority = build_causal_frequency_dependent_boundary_authority(
        source_scene_revision_id=causal_authority.source_scene_revision_id,
        source_scene_content_hash=causal_authority.source_scene_content_hash,
        source_surface_id=causal_authority.source_surface_id,
        material_id=causal_authority.material_id,
        material_version='2',
        valid_frequency_domain=causal_authority.valid_frequency_domain,
        branches=(
            CausalAdmittanceBranch(
                d_seconds=9.0e-4,
                e_dimensionless=1.5,
                f_per_second=120.0,
            ),
        ),
        evidence_state='analytic',
        provenance=causal_authority.provenance,
        uncertainty=causal_authority.uncertainty,
    )
    changed_compilation = compile_causal_boundary_to_pffdtd(
        authority=changed_authority,
        source_boundary_authority_ref=changed_authority.as_external_ref(),
        mapping_authority_ref=fixture['causal_mapping_ref'],
        requested_frequency_hz=tuple(authority.frequency_samples_hz),
        density_kg_m3=float(fixture['configuration'].density_kg_m3),
        density_authority_ref=fixture['density_ref'],
        sound_speed_m_s=float(fixture['snapshot'].environment.sound_speed_m_s),
        sound_speed_authority_ref=fixture['sound_speed_ref'],
        expected_scene_revision_id=fixture['revision'].revision_id,
        expected_scene_content_hash=fixture['revision'].content_hash,
        expected_surface_id=causal_authority.source_surface_id,
    )
    assert changed_authority.semantic_sha256 != causal_authority.semantic_sha256
    assert changed_compilation.semantic_sha256 != mapping.semantic_sha256

    changed_input_payload = deepcopy(authority.semantic_payload())
    changed_binding = next(
        item for item in changed_input_payload['boundary_bindings']
        if item.get('causal_mapping') is not None
    )
    changed_binding['material_authority'] = (
        changed_authority.as_external_ref().model_dump(mode='json')
    )
    changed_binding['causal_mapping'] = changed_compilation.model_dump(mode='json')
    changed_input_sha = _hash_json(changed_input_payload)
    assert changed_input_sha != authority.semantic_sha256

    return {
        'missing_boundary_authority_rejected': True,
        'modified_boundary_authority_rejected': True,
        'mapping_authority_mismatch_rejected': True,
        'nonpassive_branch_rejected': True,
        'unstable_zero_damping_branch_rejected': True,
        'valid_band_extrapolation_rejected': True,
        'boundary_change_changes_compiled_boundary_hash': True,
        'boundary_change_changes_execution_input_identity': True,
    }


def _independent_reference(upstream_root: Path, authority) -> dict[str, object]:
    causal_bindings = [
        item for item in authority.boundary_bindings
        if item.causal_mapping is not None
    ]
    assert len(causal_bindings) == 1
    mapping = causal_bindings[0].causal_mapping
    assert mapping is not None

    upstream_python = upstream_root / 'python'
    sys.path.insert(0, str(upstream_python))
    try:
        from materials.adm_funcs import compute_Rf_from_DEF
    finally:
        pass

    frequencies = np.asarray(mapping.requested_frequency_hz, dtype=np.float64)
    coefficients = np.asarray(mapping.def_coefficients, dtype=np.float64)
    D, E, F = coefficients.T
    candidate_reflection, _, _, _ = compute_Rf_from_DEF(
        1j * 2.0 * np.pi * frequencies,
        D,
        E,
        F,
    )

    # Independent semi-analytic normal-incidence calculation. This is written
    # directly from the positive-real branch definition and does not call PFFDTD.
    expected_values = []
    normalized_admittance = []
    for frequency in frequencies:
        jw = 1j * 2.0 * math.pi * float(frequency)
        yn = sum(
            1.0 / (
                jw * float(row[0])
                + float(row[1])
                + float(row[2]) / jw
            )
            for row in coefficients
        )
        normalized_admittance.append(yn)
        expected_values.append((1.0 - yn) / (1.0 + yn))
    expected = np.asarray(expected_values, dtype=np.complex128)

    magnitude_error = np.abs(np.abs(candidate_reflection) - np.abs(expected))
    phase_error_deg = np.abs(np.degrees(np.angle(candidate_reflection / expected)))
    complex_error = np.abs(candidate_reflection - expected)
    if not np.all(magnitude_error <= 1.0e-12):
        raise AssertionError(
            f'candidate reflection magnitude mismatch: {magnitude_error.tolist()}'
        )
    if not np.all(phase_error_deg <= 1.0e-10):
        raise AssertionError(
            f'candidate reflection phase mismatch: {phase_error_deg.tolist()}'
        )

    dense_frequency = np.linspace(
        float(mapping.valid_frequency_domain.minimum_hz),
        float(mapping.valid_frequency_domain.maximum_hz),
        81,
    )
    dense_reflections = []
    for frequency in dense_frequency:
        jw = 1j * 2.0 * math.pi * float(frequency)
        yn = sum(
            1.0 / (
                jw * float(row[0])
                + float(row[1])
                + float(row[2]) / jw
            )
            for row in coefficients
        )
        dense_reflections.append((1.0 - yn) / (1.0 + yn))
    max_reflection = max(abs(value) for value in dense_reflections)
    if max_reflection > 1.0 + 1.0e-12:
        raise AssertionError(f'passivity violation: max |R|={max_reflection}')

    magnitudes = [float(abs(value)) for value in expected]
    phases = [
        float(math.degrees(math.atan2(value.imag, value.real)))
        for value in expected
    ]
    if math.isclose(magnitudes[0], magnitudes[-1], rel_tol=0.0, abs_tol=1.0e-8):
        raise AssertionError('fixture does not demonstrate frequency-dependent |R|')
    if math.isclose(phases[0], phases[-1], rel_tol=0.0, abs_tol=1.0e-8):
        raise AssertionError('fixture does not demonstrate frequency-dependent phase')

    return {
        'reference_kind': 'independent_positive_real_series_RLC_normal_incidence',
        'candidate_kind': 'pinned_pffdtd_compute_Rf_from_DEF',
        'frequency_hz': frequencies.tolist(),
        'normalized_admittance': [
            {
                'real': float(value.real),
                'imag': float(value.imag),
            }
            for value in normalized_admittance
        ],
        'expected_reflection': [
            {
                'real': float(value.real),
                'imag': float(value.imag),
                'magnitude': float(abs(value)),
                'phase_deg': float(
                    math.degrees(math.atan2(value.imag, value.real))
                ),
            }
            for value in expected
        ],
        'candidate_reflection': [
            {
                'real': float(value.real),
                'imag': float(value.imag),
                'magnitude': float(abs(value)),
                'phase_deg': float(
                    math.degrees(math.atan2(value.imag, value.real))
                ),
            }
            for value in candidate_reflection
        ],
        'max_magnitude_error': float(np.max(magnitude_error)),
        'max_phase_error_deg': float(np.max(phase_error_deg)),
        'max_complex_error': float(np.max(complex_error)),
        'dense_band_max_reflection_magnitude': float(max_reflection),
        'passivity_status': 'PASS',
        'causality_basis': (
            'positive-real rational series-RLC branches; no sampled-response IFFT'
        ),
        'status': 'PASS',
        'spatial_fdtd_reflection_decomposition_claim': False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Run bounded R130C causal frequency-dependent PFFDTD execution'
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
        'schema_version': 'htdt.r130c.candidate-causal-boundary-execution-evidence-1',
        'fixture_id': R130C_FIXTURE_ID,
        'candidate_backend': 'PFFDTD Python/Numba CPU',
        'candidate_source_commit_sha': PFFDTD_SHA,
        'production_solver_selected': False,
        'gpu_execution': False,
        'r170_integration': False,
        'owned_room_evidence': False,
        'scalar_absorption_conversion': False,
        'sampled_response_ifft': False,
        'rdc_calls': 0,
    }
    status = 'PASS'
    try:
        fixture = _fixture(
            args.work_root,
            args.upstream_root,
            boundary_mode='causal',
            fixture_id=R130C_FIXTURE_ID,
        )
        authority, model = fixture['executor'].compile_input(
            dispatch_binding_id=fixture['dispatch'].binding_id,
            configuration=fixture['configuration'],
        )
        second, second_model = fixture['executor'].compile_input(
            dispatch_binding_id=fixture['dispatch'].binding_id,
            configuration=fixture['configuration'],
        )
        assert second == authority
        assert second_model == model
        assert authority.authority_version == 'r130c-candidate-wave-input-1'
        assert authority.adapter_compiler_version == '3'

        preflight = _preflight_causal_authority(fixture, authority)
        reference = _independent_reference(args.upstream_root, authority)

        sound_speed = fixture['snapshot'].environment.sound_speed_m_s
        assert sound_speed is not None
        resource_estimation = PffdtdCandidateResourceEstimator().estimate(
            authority=authority,
            model=model,
            configuration=fixture['configuration'],
            sound_speed_m_s=float(sound_speed),
        )
        workload = resource_estimation.workload
        workload_ref = ExactExternalAuthorityRef(
            authority_id=workload.workload_estimate_id,
            authority_version=workload.authority_version,
            semantic_hash_sha256=workload.semantic_sha256,
        )
        fixture['store'].put_exact_json(
            workload_ref,
            workload.semantic_payload(),
        )

        result = fixture['executor'].execute(
            dispatch_binding_id=fixture['dispatch'].binding_id,
            configuration=fixture['configuration'],
            resource_estimate_ref=workload_ref,
        )
        artifact_payload = fixture['store'].read_payload(
            result.artifacts[0].artifact_authority
        )
        provenance_payload = fixture['store'].read_payload(
            result.execution_provenance_ref
        )
        assert 'boundary_authority' in artifact_payload
        assert provenance_payload['production_solver_selected'] is False
        assert provenance_payload['r130c_candidate_execution_completed'] is True
        assert provenance_payload['r130c_physics_acceptance_completed'] is False
        assert provenance_payload['owned_room_evidence'] is False
        assert provenance_payload['resource_estimate_ref'] == workload_ref.model_dump(
            mode='json'
        )
        assert (
            provenance_payload['resource_estimate_identity']['workload_estimate_sha256']
            == workload.semantic_sha256
        )

        boundary_assets = provenance_payload['boundary_execution'][
            'pffdtd_material_assets'
        ]
        assert len(boundary_assets) == 1
        assert boundary_assets[0]['active_boundary_node_count'] > 0
        causal_binding = next(
            item for item in authority.boundary_bindings
            if item.causal_mapping is not None
        )
        assert np.array_equal(
            np.asarray(boundary_assets[0]['def_coefficients'], dtype=np.float64),
            np.asarray(
                causal_binding.causal_mapping.def_coefficients,
                dtype=np.float64,
            ),
        )

        reopen = _verify_reopen_and_tamper(fixture, result)

        old_boundary_hash = artifact_payload['boundary_authority'][
            'material_boundary_configuration_sha256'
        ]
        assert old_boundary_hash == authority.material_boundary_configuration_sha256
        assert result.acoustic_scene_snapshot_sha256 == fixture['snapshot'].semantic_sha256
        stale_semantics = {
            'historical_result_remains_immutable': True,
            'old_result_bound_to_exact_snapshot_sha256': True,
            'boundary_change_changes_candidate_execution_identity': (
                preflight['boundary_change_changes_execution_input_identity']
            ),
            'old_result_not_reusable_for_changed_boundary_identity': True,
        }

        payload.update(
            {
                'status': status,
                'preflight': preflight,
                'normal_incidence_reference': reference,
                'reopen': reopen,
                'stale_semantics': stale_semantics,
                'dispatch_state': fixture['dispatch'].state,
                'scene_revision_id': fixture['revision'].revision_id,
                'scene_content_hash': fixture['revision'].content_hash,
                'snapshot_id': fixture['snapshot'].snapshot_id,
                'snapshot_sha256': fixture['snapshot'].semantic_sha256,
                'candidate_execution_input_id': authority.execution_input_id,
                'candidate_execution_input_sha256': authority.semantic_sha256,
                'material_boundary_configuration_sha256': (
                    authority.material_boundary_configuration_sha256
                ),
                'boundary_authority_ref': (
                    fixture['causal_material_ref'].model_dump(mode='json')
                ),
                'compiled_boundary': (
                    causal_binding.causal_mapping.model_dump(mode='json')
                ),
                'compiled_boundary_sha256': (
                    causal_binding.causal_mapping.semantic_sha256
                ),
                'solver_model_sha256': authority.solver_model_sha256,
                'resource_estimate_ref': workload_ref.model_dump(mode='json'),
                'resource_estimate': workload.model_dump(mode='json'),
                'result_id': result.result_id,
                'result_sha256': result.semantic_sha256,
                'result_artifact_ref': (
                    result.artifacts[0].artifact_authority.model_dump(mode='json')
                ),
                'result_artifact_sha256': (
                    result.artifacts[0].artifact_authority.semantic_hash_sha256
                ),
                'execution_provenance_ref': (
                    result.execution_provenance_ref.model_dump(mode='json')
                ),
                'pffdtd_material_assets': boundary_assets,
                'raw_solver_asset_sha256': (
                    artifact_payload['solver_raw_asset']['sha256']
                ),
                'frequency_axis_hz': artifact_payload['frequency_axis_hz'],
                'pressure_real_pa': artifact_payload['pressure_real_pa'],
                'pressure_imag_pa': artifact_payload['pressure_imag_pa'],
                'actual_pffdtd_causal_boundary_execution': True,
                'non_claim': (
                    'PASS establishes a bounded causal/passive frequency-dependent '
                    'candidate execution and independent normal-incidence mapping '
                    'check. It is not production solver adoption, GPU validation, '
                    'R170 integration, spatial reflection decomposition, or owned-room evidence.'
                ),
            }
        )
    except Exception as exc:
        status = 'FAIL'
        payload.update(
            {
                'status': status,
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
    return 0 if status == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
