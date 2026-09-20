from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_candidate_wave_execution import (
    CandidateBoundaryBinding,
    CandidateImpedanceBoundaryMapping,
    CandidateNumericalOutput,
    CandidateResourceConfiguration,
    ExactJsonAuthorityStore,
    build_pffdtd_candidate_configuration,
)
from htdt.cad_equipment import FrequencyDomain
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


def _resource(*, threads: int = 2) -> CandidateResourceConfiguration:
    return CandidateResourceConfiguration(
        solver_threads=threads,
        setup_processes=1,
        max_grid_cells=1000,
        max_time_steps=100,
        max_output_bytes=1024 * 1024,
        max_solver_wall_seconds=5.0,
    )


def test_content_addressed_authority_store_fails_closed_on_missing_or_tamper(
    tmp_path: Path,
) -> None:
    store = ExactJsonAuthorityStore(tmp_path / 'authorities')
    ref = store.put_json(
        'fixture-authority',
        '1',
        {'kind': 'fixture', 'value': 42},
    )

    assert store.resolve(ref) == ref
    assert store.read_payload(ref) == {'kind': 'fixture', 'value': 42}

    path = store.path_for(ref)
    original = path.read_text(encoding='utf-8')
    path.unlink()
    assert store.resolve(ref) is None
    with pytest.raises(ValueError, match='missing'):
        store.read_payload(ref)

    path.write_text(original, encoding='utf-8')
    document = json.loads(original)
    document['payload']['value'] = 43
    path.write_text(json.dumps(document) + '\n', encoding='utf-8')
    assert store.resolve(ref) is None
    with pytest.raises(ValueError, match='modified'):
        store.read_payload(ref)


def test_content_addressed_store_allows_same_semantics_in_distinct_authority_namespaces(
    tmp_path: Path,
) -> None:
    store = ExactJsonAuthorityStore(tmp_path / 'authorities')
    payload = {'declaration_mode': 'explicit_none', 'declarations': []}
    portal = store.put_json('portal-authority', '1', payload)
    termination = store.put_json('termination-authority', '1', payload)

    assert portal.semantic_hash_sha256 == termination.semantic_hash_sha256
    assert portal.authority_id != termination.authority_id
    assert store.path_for(portal) != store.path_for(termination)
    assert store.read_payload(portal) == payload
    assert store.read_payload(termination) == payload


def test_candidate_configuration_identity_is_deterministic_and_resource_sensitive(
    tmp_path: Path,
) -> None:
    store = ExactJsonAuthorityStore(tmp_path / 'authorities')
    density = store.put_json(
        'density',
        '1',
        {'quantity': 'air_density_kg_m3', 'value': 1.2},
    )
    humidity = store.put_json(
        'humidity',
        '1',
        {'quantity': 'relative_humidity_percent', 'value': 50.0},
    )
    kwargs = {
        'expected_pffdtd_commit_sha': 'a' * 40,
        'fmax_hz': 100.0,
        'points_per_wavelength': 8.0,
        'duration_s': 0.03,
        'frequency_samples_hz': (40.0, 80.0),
        'density_kg_m3': 1.2,
        'density_authority_ref': density,
        'relative_humidity_percent': 50.0,
        'humidity_authority_ref': humidity,
    }
    first = build_pffdtd_candidate_configuration(
        **kwargs,
        resource=_resource(threads=2),
    )
    second = build_pffdtd_candidate_configuration(
        **kwargs,
        resource=_resource(threads=2),
    )
    changed = build_pffdtd_candidate_configuration(
        **kwargs,
        resource=_resource(threads=3),
    )

    assert first == second
    assert first.semantic_sha256 == second.semantic_sha256
    assert first.semantic_sha256 != changed.semantic_sha256
    assert first.as_external_ref().semantic_hash_sha256 == first.semantic_sha256


def test_candidate_numerical_output_preserves_raw_complex_shape_and_rejects_bad_shape() -> None:
    valid = CandidateNumericalOutput(
        receiver_ids=('r1',),
        frequency_hz=(40.0, 80.0),
        pressure_real_pa=((1.0, 2.0),),
        pressure_imag_pa=((3.0, 4.0),),
        raw_solver_asset_sha256='b' * 64,
        raw_solver_asset_name='sim_outs.h5',
        time_step_s=1.0e-4,
        time_step_count=30,
        grid_shape=(5, 5, 5),
        sound_speed_m_s=343.2,
        compile_seconds=0.1,
        solve_seconds=0.2,
        postprocess_seconds=0.1,
        compatibility_patch={},
    )
    assert valid.pressure_real_pa[0][0] == 1.0
    assert valid.pressure_imag_pa[0][1] == 4.0

    with pytest.raises(ValidationError, match='frequency dimension mismatch'):
        CandidateNumericalOutput(
            receiver_ids=('r1',),
            frequency_hz=(40.0, 80.0),
            pressure_real_pa=((1.0,),),
            pressure_imag_pa=((3.0, 4.0),),
            raw_solver_asset_sha256='b' * 64,
            raw_solver_asset_name='sim_outs.h5',
            time_step_s=1.0e-4,
            time_step_count=30,
            grid_shape=(5, 5, 5),
            sound_speed_m_s=343.2,
            compile_seconds=0.1,
            solve_seconds=0.2,
            postprocess_seconds=0.1,
            compatibility_patch={},
        )



def _external_ref(seed: str, version: str = '1') -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=f'fixture:{seed}',
        authority_version=version,
        semantic_hash_sha256=seed * 64,
    )


def test_rigid_boundary_binding_keeps_pre_r130b_serialized_identity_shape() -> None:
    binding = CandidateBoundaryBinding(
        source_surface_id='surface-rigid',
        material_authority=_external_ref('a'),
        boundary_physics_authority=_external_ref('b'),
    )

    assert binding.model_dump(mode='json', exclude_none=True) == {
        'source_surface_id': 'surface-rigid',
        'material_authority': _external_ref('a').model_dump(mode='json'),
        'boundary_physics_authority': _external_ref('b').model_dump(mode='json'),
    }


def test_impedance_mapping_requires_exact_resistive_def_and_version() -> None:
    kwargs = {
        'material_id': 'z-2z0',
        'material_version': '1',
        'material_provenance': 'explicit analytic fixture',
        'boundary_provenance': {'basis': 'analytic_model'},
        'valid_frequency_domain': FrequencyDomain(
            minimum_hz=40.0,
            maximum_hz=80.0,
        ),
        'frequency_samples_hz': (40.0, 80.0),
        'physical_resistance_pa_s_m': 823.2,
        'physical_reactance_pa_s_m': 0.0,
        'density_kg_m3': 1.2,
        'density_authority_ref': _external_ref('c'),
        'sound_speed_m_s': 343.0,
        'sound_speed_authority_ref': _external_ref('d'),
        'characteristic_impedance_pa_s_m': 411.6,
        'normalized_impedance': 2.0,
        'normalized_admittance': 0.5,
        'def_coefficients': ((0.0, 2.0, 0.0),),
        'mapping_authority_ref': _external_ref('e'),
    }
    mapping = CandidateImpedanceBoundaryMapping(**kwargs)
    assert mapping.def_coefficients == ((0.0, 2.0, 0.0),)

    bad_def = dict(kwargs)
    bad_def['def_coefficients'] = ((0.0, 3.0, 0.0),)
    with pytest.raises(ValidationError, match='DEF'):
        CandidateImpedanceBoundaryMapping(**bad_def)

    bad_version = dict(kwargs)
    bad_version['mapping_version'] = '2'
    with pytest.raises(ValidationError):
        CandidateImpedanceBoundaryMapping(**bad_version)
