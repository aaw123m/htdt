from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
from pathlib import Path
import sys
import traceback

import numpy as np
from pydantic import ValidationError

from htdt.acoustic_benchmark import AcousticMaterial, SpecificImpedancePoint
from htdt.acoustic_pffdtd_impedance_adapter import (
    compile_frequency_independent_resistive_impedance_boundary,
)
from htdt.cad_candidate_wave_execution import (
    CandidateImpedanceBoundaryMapping,
    CandidateWaveExecutionError,
)

from run_r130a_candidate_wave_execution import (
    PFFDTD_SHA,
    _fixture,
    _hash_json,
    _verify_reopen_and_tamper,
)


R130B_FIXTURE_ID = 'r130b-candidate-explicit-impedance-v1'


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


def _preflight_impedance_authority(fixture: dict[str, object], authority) -> dict[str, bool]:
    store = fixture['store']
    impedance_bindings = [
        item
        for item in authority.boundary_bindings
        if item.impedance_mapping is not None
    ]
    assert len(impedance_bindings) == 1
    binding = impedance_bindings[0]
    mapping = binding.impedance_mapping
    assert mapping is not None

    # Exact material authority must exist and must retain its content hash.
    material_path = store.path_for(binding.material_authority)
    original_material = material_path.read_bytes()
    material_path.unlink()
    _expect_compile_failure(fixture, 'material')
    material_path.write_bytes(original_material)

    material_path.write_text('{}\n', encoding='utf-8')
    _expect_compile_failure(fixture, 'material')
    material_path.write_bytes(original_material)

    # Exact PFFDTD mapping authority/version is also part of the gate.
    mapping_path = store.path_for(mapping.mapping_authority_ref)
    original_mapping = mapping_path.read_bytes()
    mapping_document = json.loads(original_mapping.decode('utf-8'))
    mapping_document['authority_version'] = '2'
    mapping_path.write_text(
        json.dumps(mapping_document, sort_keys=True, separators=(',', ':')) + '\n',
        encoding='utf-8',
    )
    _expect_compile_failure(fixture, 'mapping')
    mapping_path.write_bytes(original_mapping)

    mismatch_payload = mapping.model_dump(mode='python')
    mismatch_payload['mapping_version'] = '2'
    try:
        CandidateImpedanceBoundaryMapping.model_validate(mismatch_payload)
    except ValidationError:
        pass
    else:
        raise AssertionError('mapping version mismatch was accepted')

    # R100B mapping remains fail-closed for non-impedance/scalar-style input.
    unsupported = AcousticMaterial(
        material_id='scalar-absorption-is-not-impedance',
        provenance='negative R130B capability fixture',
        version='1',
        wave_model='unsupported',
    )
    try:
        compile_frequency_independent_resistive_impedance_boundary(
            material=unsupported,
            frequencies_hz=(40.0, 80.0),
            density_kg_m3=1.2,
            sound_speed_m_s=343.0,
        )
    except ValueError:
        pass
    else:
        raise AssertionError('unsupported/scalar material was implicitly converted')

    reactive = AcousticMaterial(
        material_id='reactive-is-r130c',
        provenance='negative R130B capability fixture',
        version='1',
        wave_model='specific_impedance_table',
        specific_impedance=(
            SpecificImpedancePoint(
                frequency_hz=40.0,
                resistance_pa_s_m=823.2,
                reactance_pa_s_m=1.0,
            ),
            SpecificImpedancePoint(
                frequency_hz=80.0,
                resistance_pa_s_m=823.2,
                reactance_pa_s_m=1.0,
            ),
        ),
    )
    try:
        compile_frequency_independent_resistive_impedance_boundary(
            material=reactive,
            frequencies_hz=(40.0, 80.0),
            density_kg_m3=1.2,
            sound_speed_m_s=343.0,
        )
    except ValueError:
        pass
    else:
        raise AssertionError('reactive impedance was accepted by R130B')

    frequency_dependent = AcousticMaterial(
        material_id='frequency-dependent-is-r130c',
        provenance='negative R130B capability fixture',
        version='1',
        wave_model='specific_impedance_table',
        specific_impedance=(
            SpecificImpedancePoint(
                frequency_hz=40.0,
                resistance_pa_s_m=800.0,
                reactance_pa_s_m=0.0,
            ),
            SpecificImpedancePoint(
                frequency_hz=80.0,
                resistance_pa_s_m=900.0,
                reactance_pa_s_m=0.0,
            ),
        ),
    )
    try:
        compile_frequency_independent_resistive_impedance_boundary(
            material=frequency_dependent,
            frequencies_hz=(40.0, 80.0),
            density_kg_m3=1.2,
            sound_speed_m_s=343.0,
        )
    except ValueError:
        pass
    else:
        raise AssertionError('frequency-dependent impedance was accepted by R130B')

    # Execution identity is directly sensitive to the exact material hash and
    # compiled physical impedance mapping. No candidate result cache exists;
    # an old envelope remains bound to its old dispatch/execution identity.
    changed_payload = deepcopy(authority.semantic_payload())
    changed_binding = next(
        item
        for item in changed_payload['boundary_bindings']
        if item.get('impedance_mapping') is not None
    )
    changed_binding['material_authority']['semantic_hash_sha256'] = '0' * 64
    changed_binding['impedance_mapping']['physical_resistance_pa_s_m'] *= 1.01
    changed_identity = _hash_json(changed_payload)
    assert changed_identity != authority.semantic_sha256

    return {
        'missing_material_authority_rejected': True,
        'modified_material_authority_rejected': True,
        'mapping_authority_version_mismatch_rejected': True,
        'mapping_model_version_mismatch_rejected': True,
        'scalar_absorption_not_converted': True,
        'reactive_impedance_rejected': True,
        'frequency_dependent_impedance_rejected': True,
        'impedance_change_changes_execution_identity': True,
    }


def _independent_normal_incidence_reference(
    upstream_root: Path,
    authority,
) -> dict[str, object]:
    impedance_bindings = [
        item
        for item in authority.boundary_bindings
        if item.impedance_mapping is not None
    ]
    assert len(impedance_bindings) == 1
    mapping = impedance_bindings[0].impedance_mapping
    assert mapping is not None

    upstream_python = upstream_root / 'python'
    sys.path.insert(0, str(upstream_python))
    try:
        from materials.adm_funcs import compute_Rf_from_DEF
    finally:
        pass

    frequencies = np.asarray(mapping.frequency_samples_hz, dtype=np.float64)
    coefficients = np.asarray(mapping.def_coefficients, dtype=np.float64)
    D, E, F = coefficients.T
    candidate_reflection, _, _, _ = compute_Rf_from_DEF(
        1j * 2.0 * np.pi * frequencies,
        D,
        E,
        F,
    )

    z = complex(
        mapping.physical_resistance_pa_s_m,
        mapping.physical_reactance_pa_s_m,
    )
    rho_c = float(mapping.characteristic_impedance_pa_s_m)
    # Independent closed-form normal-incidence reference. This does not call
    # PFFDTD and does not use compute_Rf_from_DEF.
    expected = (z - rho_c) / (z + rho_c)
    expected_values = np.full(frequencies.shape, expected, dtype=np.complex128)

    magnitude_error = np.abs(
        np.abs(candidate_reflection) - np.abs(expected_values)
    )
    phase_error_deg = np.abs(
        np.degrees(
            np.angle(candidate_reflection / expected_values)
        )
    )
    complex_error = np.abs(candidate_reflection - expected_values)

    # Frozen R100B gate tolerances for the canonical explicit-impedance fixture.
    magnitude_abs_tolerance = 0.01
    phase_tolerance_deg = 1.0
    if not np.all(magnitude_error <= magnitude_abs_tolerance):
        raise AssertionError(
            f'candidate reflection magnitude mismatch: {magnitude_error.tolist()}'
        )
    if not np.all(phase_error_deg <= phase_tolerance_deg):
        raise AssertionError(
            f'candidate reflection phase mismatch: {phase_error_deg.tolist()}'
        )

    return {
        'reference_kind': 'independent_closed_form_normal_incidence',
        'candidate_kind': 'pinned_pffdtd_compute_Rf_from_DEF',
        'frequency_hz': frequencies.tolist(),
        'physical_impedance_pa_s_m': {
            'real': float(z.real),
            'imag': float(z.imag),
        },
        'density_kg_m3': float(mapping.density_kg_m3),
        'sound_speed_m_s': float(mapping.sound_speed_m_s),
        'rho_c_pa_s_m': rho_c,
        'expected_reflection': {
            'real': float(expected.real),
            'imag': float(expected.imag),
            'magnitude': float(abs(expected)),
            'phase_deg': float(math.degrees(math.atan2(expected.imag, expected.real))),
        },
        'candidate_reflection': [
            {
                'real': float(value.real),
                'imag': float(value.imag),
                'magnitude': float(abs(value)),
                'phase_deg': float(math.degrees(math.atan2(value.imag, value.real))),
            }
            for value in candidate_reflection
        ],
        'max_magnitude_error': float(np.max(magnitude_error)),
        'max_phase_error_deg': float(np.max(phase_error_deg)),
        'max_complex_error': float(np.max(complex_error)),
        'magnitude_abs_tolerance': magnitude_abs_tolerance,
        'phase_tolerance_deg': phase_tolerance_deg,
        'status': 'PASS',
        'spatial_fdtd_reflection_decomposition_claim': False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Run bounded R130B explicit-impedance PFFDTD candidate execution'
    )
    parser.add_argument('--upstream-root', required=True, type=Path)
    parser.add_argument('--work-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.work_root.exists():
        import shutil
        shutil.rmtree(args.work_root)
    args.work_root.mkdir(parents=True)

    payload: dict[str, object] = {
        'schema_version': 'htdt.r130b.candidate-impedance-execution-evidence-1',
        'fixture_id': R130B_FIXTURE_ID,
        'candidate_backend': 'PFFDTD Python/Numba CPU',
        'candidate_source_commit_sha': PFFDTD_SHA,
        'production_solver_selected': False,
        'r130b_numerical_acceptance_completed': False,
        'owned_room_evidence': False,
        'rdc_calls': 0,
    }
    status = 'PASS'
    try:
        fixture = _fixture(
            args.work_root,
            args.upstream_root,
            boundary_mode='impedance',
            fixture_id=R130B_FIXTURE_ID,
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

        preflight = _preflight_impedance_authority(fixture, authority)
        reference = _independent_normal_incidence_reference(
            args.upstream_root,
            authority,
        )

        result = fixture['executor'].execute(
            dispatch_binding_id=fixture['dispatch'].binding_id,
            configuration=fixture['configuration'],
        )
        artifact_payload = fixture['store'].read_payload(
            result.artifacts[0].artifact_authority
        )
        provenance_payload = fixture['store'].read_payload(
            result.execution_provenance_ref
        )
        assert 'boundary_authority' in artifact_payload
        assert provenance_payload['production_solver_selected'] is False
        assert provenance_payload['r130b_numerical_acceptance_completed'] is False
        assert provenance_payload['owned_room_evidence'] is False
        boundary_assets = provenance_payload['boundary_execution'][
            'pffdtd_material_assets'
        ]
        assert len(boundary_assets) == 1
        assert boundary_assets[0]['active_boundary_node_count'] > 0
        assert np.allclose(
            np.asarray(boundary_assets[0]['def_coefficients'], dtype=np.float64),
            np.asarray([[0.0, 2.0, 0.0]], dtype=np.float64),
            rtol=0.0,
            atol=1.0e-15,
        )

        reopen = _verify_reopen_and_tamper(fixture, result)
        payload.update(
            {
                'status': status,
                'preflight': preflight,
                'normal_incidence_reference': reference,
                'reopen': reopen,
                'dispatch_state': fixture['dispatch'].state,
                'snapshot_id': fixture['snapshot'].snapshot_id,
                'snapshot_sha256': fixture['snapshot'].semantic_sha256,
                'candidate_execution_input_id': authority.execution_input_id,
                'candidate_execution_input_sha256': authority.semantic_sha256,
                'material_boundary_configuration_sha256': (
                    authority.material_boundary_configuration_sha256
                ),
                'boundary_bindings': [
                    item.model_dump(mode='json', exclude_none=True)
                    for item in authority.boundary_bindings
                ],
                'solver_model_sha256': authority.solver_model_sha256,
                'result_id': result.result_id,
                'result_sha256': result.semantic_sha256,
                'result_artifact_ref': (
                    result.artifacts[0].artifact_authority.model_dump(mode='json')
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
                'actual_pffdtd_impedance_execution': True,
                'spatial_reflection_decomposition_available': False,
                'non_claim': (
                    'PASS establishes bounded candidate execution plus independently '
                    'checked exact normal-incidence mapping; it does not establish '
                    'spatial incident/reflected decomposition or production adoption.'
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
