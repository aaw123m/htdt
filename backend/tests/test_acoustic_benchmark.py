from __future__ import annotations

from pathlib import Path

import pytest

from htdt.acoustic_benchmark import (
    AcousticBenchmarkFixture,
    AcousticBenchmarkManifest,
    AcousticMaterial,
    load_acoustic_benchmark_manifest,
)


MANIFEST_PATH = Path(__file__).resolve().parents[2] / 'benchmarks' / 'acoustics' / 'r100a_manifest.json'


def _manifest() -> AcousticBenchmarkManifest:
    return load_acoustic_benchmark_manifest(MANIFEST_PATH)


def _fixture(manifest: AcousticBenchmarkManifest, fixture_id: str) -> AcousticBenchmarkFixture:
    return next(item for item in manifest.fixtures if item.fixture_id == fixture_id)


def test_r100a_manifest_loads_as_immutable_canonical_authority() -> None:
    manifest = _manifest()

    assert manifest.schema_version == 'r100a-4'
    assert manifest.manifest_id == 'htdt-issue-101-r100a-benchmark-authority'
    assert manifest.revision == 4
    assert len(manifest.fixtures) == 10
    assert len(manifest.hard_gates) == 6
    assert all(item.environment.density_kg_m3 == pytest.approx(1.2) for item in manifest.fixtures)
    assert len(manifest.semantic_hash()) == 64

    reparsed = AcousticBenchmarkManifest.model_validate_json(manifest.canonical_json())
    assert reparsed == manifest
    assert reparsed.semantic_hash() == manifest.semantic_hash()

    with pytest.raises(Exception):
        manifest.revision = 4  # type: ignore[misc]


def test_r100a_schema_version_and_revision_are_bound() -> None:
    manifest = _manifest()
    payload = manifest.model_dump(mode='python')

    payload['revision'] = 3
    with pytest.raises(ValueError, match='r100a-4 requires revision 4'):
        AcousticBenchmarkManifest.model_validate(payload)

    payload = manifest.model_dump(mode='python')
    payload['schema_version'] = 'r100a-3'
    with pytest.raises(ValueError, match='r100a-3 requires revision 3'):
        AcousticBenchmarkManifest.model_validate(payload)

    payload = manifest.model_dump(mode='python')
    payload['schema_version'] = 'r100a-2'
    with pytest.raises(ValueError, match='r100a-2 requires revision 2'):
        AcousticBenchmarkManifest.model_validate(payload)


def _remove_r100a4_finite_record_semantics(payload: dict[str, object]) -> None:
    for fixture in payload['fixtures']:
        fixture['comparison']['finite_record_transfer'] = None
        if fixture['fixture_id'] == 'wave-rectangular-convergence-v1':
            fixture['comparison']['time_step_s'] = 1.0 / 6000.0
            fixture['observables'][0]['kind'] = 'field_pressure_pa'
            fixture['observables'][0]['unit'] = 'Pa'
        if fixture['fixture_id'] == 'wave-concave-l-room-v1':
            for observable in fixture['observables']:
                if observable['observable_id'] == 'lroom-fr':
                    observable['unit'] = 'dB'
                    observable['reference_description'] = (
                        'Compare against an independently implemented/reference '
                        'discretization on the exact concave prism.'
                    )


def test_r100a2_rejects_r100a3_radiation_semantics() -> None:
    manifest = _manifest()

    capability_payload = manifest.model_dump(mode='python')
    _remove_r100a4_finite_record_semantics(capability_payload)
    capability_payload['schema_version'] = 'r100a-2'
    capability_payload['revision'] = 2
    with pytest.raises(ValueError, match='cannot declare wave_radiation_termination'):
        AcousticBenchmarkManifest.model_validate(capability_payload)

    semantics_payload = manifest.model_dump(mode='python')
    _remove_r100a4_finite_record_semantics(semantics_payload)
    semantics_payload['schema_version'] = 'r100a-2'
    semantics_payload['revision'] = 2
    radiation_fixture = next(
        item
        for item in semantics_payload['fixtures']
        if item['fixture_id'] == 'wave-explicit-radiation-termination-v1'
    )
    radiation_fixture['required_capabilities'] = ['wave_rigid']
    with pytest.raises(ValueError, match='cannot carry R100A-3 radiation semantics'):
        AcousticBenchmarkManifest.model_validate(semantics_payload)


def test_r100a4_finite_record_transfer_authority_is_explicit() -> None:
    manifest = _manifest()
    finite_record = {
        fixture.fixture_id: fixture.comparison.finite_record_transfer
        for fixture in manifest.fixtures
        if fixture.comparison.finite_record_transfer is not None
    }

    assert set(finite_record) == {
        'wave-rectangular-convergence-v1',
        'wave-concave-l-room-v1',
    }
    for fixture_id, contract in finite_record.items():
        assert contract is not None
        fixture = _fixture(manifest, fixture_id)
        assert fixture.comparison.time_step_s is None
        assert fixture.comparison.observation_time_s == pytest.approx(2.0)
        assert contract.excitation_model == 'causal_discrete_unit_sample_volume_velocity'
        assert contract.sample_zero_reference == 'source_t0'
        assert contract.record_interval == 'half_open_0_T'
        assert contract.solver_time_step_policy == 'solver_native_recorded'
        assert contract.dtft_kernel == 'exp(+i*2*pi*f*n*dt)'
        assert contract.dtft_measure == 'dt_weighted_sum'
        assert contract.numerator_quantity == 'physical_pressure'
        assert (
            contract.numerator_record_policy
            == 'solver_pressure_or_declared_primary_field_conversion'
        )
        assert (
            contract.denominator_record
            == 'physical_volume_velocity_samples_on_solver_time_grid'
        )
        assert contract.transfer_definition == 'pressure_over_volume_velocity'
        assert contract.frequency_evaluation == 'direct_scored_frequency_dtft'
        assert contract.source_spectrum_requirement == 'finite_nonzero_on_scored_grid'
        assert contract.zero_padding == 'none'

    convergence = _fixture(manifest, 'wave-rectangular-convergence-v1')
    convergence_observable = convergence.observables[0]
    assert convergence_observable.kind == 'complex_pressure_transfer_pa_per_m3_s'
    assert convergence_observable.unit == 'Pa/(m3/s)'
    concave = _fixture(manifest, 'wave-concave-l-room-v1')
    magnitude = next(item for item in concave.observables if item.observable_id == 'lroom-fr')
    assert magnitude.unit == 'dB re 1 Pa/(m3/s)'


def test_r100a3_rejects_r100a4_finite_record_semantics() -> None:
    payload = _manifest().model_dump(mode='python')
    payload['schema_version'] = 'r100a-3'
    payload['revision'] = 3

    with pytest.raises(ValueError, match='cannot carry R100A-4 finite-record transfer semantics'):
        AcousticBenchmarkManifest.model_validate(payload)


def test_r100a3_rejects_r100a4_transfer_observable_kind() -> None:
    payload = _manifest().model_dump(mode='python')
    for fixture in payload['fixtures']:
        fixture['comparison']['finite_record_transfer'] = None
    payload['schema_version'] = 'r100a-3'
    payload['revision'] = 3

    with pytest.raises(ValueError, match='cannot carry R100A-4 complex pressure-transfer'):
        AcousticBenchmarkManifest.model_validate(payload)


def test_r100a4_finite_record_contract_fails_closed_on_fixture_or_dt_drift() -> None:
    manifest = _manifest()

    missing_payload = manifest.model_dump(mode='python')
    concave = next(
        item
        for item in missing_payload['fixtures']
        if item['fixture_id'] == 'wave-concave-l-room-v1'
    )
    concave['comparison']['finite_record_transfer'] = None
    with pytest.raises(ValueError, match='finite-record transfer fixtures must be exactly'):
        AcousticBenchmarkManifest.model_validate(missing_payload)

    dt_payload = manifest.model_dump(mode='python')
    convergence = next(
        item
        for item in dt_payload['fixtures']
        if item['fixture_id'] == 'wave-rectangular-convergence-v1'
    )
    convergence['comparison']['time_step_s'] = 1.0 / 6000.0
    with pytest.raises(ValueError, match='must leave time_step_s solver-native'):
        AcousticBenchmarkManifest.model_validate(dt_payload)

    explicit_payload = manifest.model_dump(mode='python')
    convergence = next(
        item
        for item in explicit_payload['fixtures']
        if item['fixture_id'] == 'wave-rectangular-convergence-v1'
    )
    del convergence['comparison']['finite_record_transfer']['numerator_quantity']
    with pytest.raises(ValueError, match='numerator_quantity'):
        AcousticBenchmarkManifest.model_validate(explicit_payload)

    phase_payload = manifest.model_dump(mode='python')
    concave = next(
        item
        for item in phase_payload['fixtures']
        if item['fixture_id'] == 'wave-concave-l-room-v1'
    )
    concave['sources'][0]['phase_deg'] = 5.0
    with pytest.raises(ValueError, match='requires the frozen unit volume-velocity source'):
        AcousticBenchmarkManifest.model_validate(phase_payload)


def test_r100a_required_fixture_roles_are_present() -> None:
    manifest = _manifest()
    fixture_ids = {item.fixture_id for item in manifest.fixtures}

    assert {
        'wave-rigid-rectangular-modes-v1',
        'wave-rectangular-convergence-v1',
        'wave-normal-incidence-impedance-v1',
        'wave-concave-l-room-v1',
        'wave-portal-split-room-v1',
        'wave-explicit-radiation-termination-v1',
        'geometric-direct-first-reflection-v1',
        'geometric-reflecting-counter-v1',
        'geometric-seed-repeatability-v1',
        'hybrid-overlap-continuity-v1',
    } == fixture_ids


def test_opening_authority_is_explicit_portal_or_termination() -> None:
    manifest = _manifest()
    portal_fixture = _fixture(manifest, 'wave-portal-split-room-v1')
    termination_fixture = _fixture(manifest, 'wave-explicit-radiation-termination-v1')

    assert len(portal_fixture.regions) == 2
    assert len(portal_fixture.portals) == 1
    assert portal_fixture.portals[0].region_a_id == 'left'
    assert portal_fixture.portals[0].region_b_id == 'right'
    assert not portal_fixture.terminations

    assert len(termination_fixture.regions) == 1
    assert not termination_fixture.portals
    assert len(termination_fixture.terminations) == 1
    termination = termination_fixture.terminations[0]
    assert termination.kind == 'radiation'
    assert termination.boundary_id == 'b-interface'
    assert termination.radiation_model == 'local_first_order_outgoing'
    assert termination.normal_convention == 'outward_from_region'
    assert termination.characteristic_impedance_model == 'rho_c_from_environment'
    assert termination.pressure_velocity_equation == 'p_eq_rho_c_u_n'
    assert termination.wavenumber_equation == 'k_eq_omega_over_c'
    assert termination.helmholtz_robin_equation == 'dp_dn_minus_i_k_p_eq_0'
    assert tuple(termination_fixture.required_capabilities) == (
        'wave_rigid',
        'wave_radiation_termination',
    )
    observable_by_id = {
        item.observable_id: item for item in termination_fixture.observables
    }
    assert observable_by_id['termination-fr'].unit == 'dB re 1 Pa/(m3/s)'
    assert observable_by_id['termination-complex-pressure'].unit == 'Pa'
    assert len(observable_by_id['termination-fr'].samples) == 281
    assert len(observable_by_id['termination-complex-pressure'].samples) == 281


def test_radiation_termination_rejects_implicit_or_mismatched_model() -> None:
    manifest = _manifest()
    fixture = _fixture(manifest, 'wave-explicit-radiation-termination-v1')
    termination = fixture.terminations[0]

    legacy_termination_payload = termination.model_dump(mode='python')
    legacy_termination_payload['boundary_id'] = None
    for key in (
        'radiation_model',
        'normal_convention',
        'characteristic_impedance_model',
        'pressure_velocity_equation',
        'wavenumber_equation',
        'helmholtz_robin_equation',
    ):
        legacy_termination_payload[key] = None
    legacy_termination = type(termination).model_validate(legacy_termination_payload)
    assert legacy_termination.kind == 'radiation'

    current_payload = manifest.model_dump(mode='python')
    current_fixture = next(
        item
        for item in current_payload['fixtures']
        if item['fixture_id'] == 'wave-explicit-radiation-termination-v1'
    )
    current_fixture['terminations'] = list(current_fixture['terminations'])
    current_fixture['terminations'][0] = legacy_termination_payload
    with pytest.raises(ValueError, match='requires explicit boundary/model/sign/normal authority'):
        AcousticBenchmarkManifest.model_validate(current_payload)

    legacy_manifest_payload = manifest.model_dump(mode='python')
    _remove_r100a4_finite_record_semantics(legacy_manifest_payload)
    legacy_manifest_payload['schema_version'] = 'r100a-2'
    legacy_manifest_payload['revision'] = 2
    legacy_fixture = next(
        item
        for item in legacy_manifest_payload['fixtures']
        if item['fixture_id'] == 'wave-explicit-radiation-termination-v1'
    )
    legacy_fixture['required_capabilities'] = ['wave_rigid']
    legacy_fixture['terminations'] = list(legacy_fixture['terminations'])
    legacy_fixture['terminations'][0] = legacy_termination_payload
    reparsed_legacy = AcousticBenchmarkManifest.model_validate(legacy_manifest_payload)
    assert reparsed_legacy.schema_version == 'r100a-2'

    non_radiation = termination.model_copy(
        update={'kind': 'rigid'}
    ).model_dump(mode='python')
    with pytest.raises(ValueError, match='only valid for radiation termination'):
        type(termination).model_validate(non_radiation)


def test_wave_radiation_capability_requires_radiation_termination() -> None:
    manifest = _manifest()
    fixture = _fixture(manifest, 'wave-explicit-radiation-termination-v1')
    payload = fixture.model_dump(mode='python')
    payload['terminations'] = []

    with pytest.raises(ValueError, match='requires an explicit radiation termination'):
        AcousticBenchmarkFixture.model_validate(payload)


def test_wave_impedance_capability_rejects_geometric_only_material() -> None:
    manifest = _manifest()
    fixture = _fixture(manifest, 'wave-normal-incidence-impedance-v1')
    payload = fixture.model_dump(mode='python')

    payload['materials'] = [
        item
        if item['material_id'] != 'z-2z0'
        else AcousticMaterial(
            material_id='z-2z0',
            provenance='deliberately insufficient geometric-only fixture',
            version='1',
            wave_model='unsupported',
            geometric_model='banded',
            geometric_bands=(
                {
                    'center_hz': 250.0,
                    'absorption': 0.5,
                    'scattering': 0.0,
                },
            ),
        ).model_dump(mode='python')
        for item in payload['materials']
    ]

    with pytest.raises(ValueError, match='phase-bearing impedance'):
        AcousticBenchmarkFixture.model_validate(payload)


def test_counter_fixture_has_explicit_acoustic_obstacle_and_peer_delta_contract() -> None:
    manifest = _manifest()
    counter = _fixture(manifest, 'geometric-reflecting-counter-v1')

    assert len(counter.obstacles) == 1
    obstacle = counter.obstacles[0]
    assert obstacle.obstacle_id == 'counter'
    assert obstacle.host_region_id == 'room'
    assert obstacle.representation == 'solid_volume'

    observable = counter.observables[0]
    assert observable.acceptance_relation == 'must_differ_from_peer'
    assert observable.peer_fixture_id == 'geometric-direct-first-reflection-v1'
    assert observable.tolerance.minimum_difference == pytest.approx(0.001)


def test_unknown_peer_fixture_is_rejected_fail_closed() -> None:
    manifest = _manifest()
    payload = manifest.model_dump(mode='python')
    counter = next(
        item for item in payload['fixtures'] if item['fixture_id'] == 'geometric-reflecting-counter-v1'
    )
    counter['observables'][0]['peer_fixture_id'] = 'missing-reference-fixture'

    with pytest.raises(ValueError, match='unknown peer fixtures'):
        AcousticBenchmarkManifest.model_validate(payload)


def test_missing_required_hard_gate_is_rejected_fail_closed() -> None:
    manifest = _manifest()
    payload = manifest.model_dump(mode='python')
    payload['hard_gates'] = [
        item for item in payload['hard_gates'] if item['category'] != 'cpu_baseline'
    ]

    with pytest.raises(ValueError, match='missing required hard gate categories'):
        AcousticBenchmarkManifest.model_validate(payload)


def test_pressure_convergence_authority_has_density_and_complex_error_contract() -> None:
    manifest = _manifest()
    fixture = _fixture(manifest, 'wave-rectangular-convergence-v1')
    observable = fixture.observables[0]

    assert fixture.environment.density_kg_m3 == pytest.approx(1.2)
    assert observable.kind == 'complex_pressure_transfer_pa_per_m3_s'
    assert observable.unit == 'Pa/(m3/s)'
    assert observable.acceptance_relation == 'monotonic_convergence'
    assert observable.tolerance.absolute == pytest.approx(0.02)
    assert observable.tolerance.relative == pytest.approx(0.02)
    assert observable.tolerance.phase_deg is None
