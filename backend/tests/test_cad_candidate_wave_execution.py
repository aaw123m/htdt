from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_candidate_wave_execution import (
    CandidateNumericalOutput,
    CandidateResourceConfiguration,
    ExactJsonAuthorityStore,
    build_pffdtd_candidate_configuration,
)


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
