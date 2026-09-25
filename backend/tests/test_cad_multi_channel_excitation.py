from __future__ import annotations

import math
from hashlib import sha256

import pytest

from htdt.cad_equipment import FrequencyDomain
from htdt.cad_multi_channel_excitation import (
    BassManagementRoute,
    ExcitationChannelParticipant,
    ExcitationDriveState,
    ResolvedPlaybackTransfer,
    ScenarioSourceTransfer,
    bass_management_routes_for_scenario,
    build_multi_channel_excitation_scenario,
    compose_coherent_system_response,
)
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


def _hash(label: str) -> str:
    return sha256(label.encode('utf-8')).hexdigest()


def _authority(label: str) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=f'test:{label}',
        authority_version='1',
        semantic_hash_sha256=_hash(label),
    )


def _participant(
    entity: str,
    channel: str,
    *,
    gain_db: float = 0.0,
    delay_s: float = 0.0,
    polarity: int = 1,
) -> ExcitationChannelParticipant:
    return ExcitationChannelParticipant(
        logical_channel_id=channel,
        channel_role_id=f'role:{channel}',
        source_entity_id=entity,
        source_binding_sha256=_hash(f'binding:{entity}'),
        equipment_definition_id='equipment:generic',
        equipment_definition_version='1',
        equipment_definition_sha256=_hash(f'equipment:{entity}'),
        drive=ExcitationDriveState(
            gain_db=gain_db,
            delay_s=delay_s,
            polarity=polarity,
        ),
    )


def _scenario(**overrides):
    kwargs = dict(
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash=_hash('scene'),
        participants=(
            _participant('speaker-fl', 'FL'),
            _participant('speaker-fr', 'FR'),
        ),
        combination_semantics='coherent_system_sum',
    )
    kwargs.update(overrides)
    return build_multi_channel_excitation_scenario(**kwargs)


def _complex_transfer(
    entity: str,
    samples: tuple[complex, ...],
    *,
    timing: str = 'absolute_propagation_time',
    grid: tuple[float, ...] = (100.0, 200.0),
) -> ScenarioSourceTransfer:
    return ScenarioSourceTransfer(
        source_entity_id=entity,
        frequency_hz=grid[: len(samples)],
        pressure_real=tuple(s.real for s in samples),
        pressure_imag=tuple(s.imag for s in samples),
        phasor_convention='exp(+i*omega*t)',
        source_normalization_id='norm:1w1m',
        timing_authority=timing,
        result_authority_ref=_authority(f'result:{entity}'),
    )


def test_scenario_identity_and_unique_membership() -> None:
    scenario = _scenario()
    assert scenario.scenario_id.startswith('mc-excitation-scenario:')
    with pytest.raises(ValueError, match='twice'):
        _scenario(
            participants=(
                _participant('speaker-fl', 'FL'),
                _participant('speaker-fl', 'FL2'),
            )
        )


def test_bass_management_route_must_reference_scenario_members() -> None:
    route = BassManagementRoute(
        route_id='bm-1',
        from_logical_channel_id='FL',
        to_source_entity_id='sub-1',
        band=FrequencyDomain(minimum_hz=20.0, maximum_hz=80.0),
        method_authority_ref=_authority('bm-method'),
    )
    with pytest.raises(ValueError, match='outside the scenario'):
        _scenario(bass_management_routes=(route,))
    scenario = _scenario(
        participants=(
            _participant('speaker-fl', 'FL'),
            _participant('sub-1', 'SUB1'),
        ),
        bass_management_routes=(
            BassManagementRoute(
                route_id='bm-1',
                from_logical_channel_id='FL',
                to_source_entity_id='sub-1',
                band=FrequencyDomain(minimum_hz=20.0, maximum_hz=80.0),
                method_authority_ref=_authority('bm-method'),
            ),
        ),
        preset='mains_plus_subs_bass_managed',
    )
    assert scenario.preset == 'mains_plus_subs_bass_managed'


def test_coherent_sum_applies_gain_delay_polarity() -> None:
    scenario = _scenario(
        participants=(
            _participant('speaker-fl', 'FL', gain_db=0.0, delay_s=0.0),
            _participant(
                'speaker-fr', 'FR', gain_db=-6.0, delay_s=0.0, polarity=-1
            ),
        )
    )
    response = compose_coherent_system_response(
        scenario,
        (
            _complex_transfer('speaker-fl', (1.0 + 0j, 2.0 + 0j)),
            _complex_transfer('speaker-fr', (1.0 + 0j, 2.0 + 0j)),
        ),
    )
    assert response.state == 'READY'
    # factor_FR = -10^(-6/20) ≈ -0.5012; sum at 100 Hz ≈ 1 - 0.5012
    expected = 1.0 - 10.0 ** (-6.0 / 20.0)
    assert math.isclose(
        response.pressure_real[0], expected, rel_tol=1e-9
    ), response.pressure_real


def test_delay_applies_phase_factor_under_exp_plus_iwt() -> None:
    # delay of 5 ms at 100 Hz: factor = exp(-i*2π*f*τ) = exp(-iπ) = -1
    scenario = _scenario(
        participants=(
            _participant('speaker-fl', 'FL'),
            _participant('speaker-fr', 'FR', delay_s=0.005),
        )
    )
    response = compose_coherent_system_response(
        scenario,
        (
            _complex_transfer('speaker-fl', (1.0 + 0j,)),
            _complex_transfer('speaker-fr', (1.0 + 0j,)),
        ),
    )
    assert response.state == 'READY'
    assert math.isclose(response.pressure_real[0], 0.0, abs_tol=1e-9)
    assert math.isclose(response.pressure_imag[0], 0.0, abs_tol=1e-9)


def test_magnitude_only_transfer_fails_closed() -> None:
    scenario = _scenario()
    response = compose_coherent_system_response(
        scenario,
        (
            _complex_transfer('speaker-fl', (1.0 + 0j, 1.0 + 0j)),
            ScenarioSourceTransfer(
                source_entity_id='speaker-fr',
                frequency_hz=(100.0, 200.0),
                pressure_magnitude_pa=(1.0, 1.0),
                source_normalization_id='norm:1w1m',
                timing_authority='unavailable',
            ),
        ),
    )
    assert response.state == 'UNSUPPORTED'
    assert any('magnitude-only' in r for r in response.unsupported_reasons)


def test_independent_transfer_set_is_not_coherent_sum() -> None:
    scenario = _scenario(combination_semantics='independent_transfer_set')
    response = compose_coherent_system_response(
        scenario,
        (
            _complex_transfer('speaker-fl', (1.0 + 0j, 1.0 + 0j)),
            _complex_transfer('speaker-fr', (1.0 + 0j, 1.0 + 0j)),
        ),
    )
    assert response.state == 'UNSUPPORTED'
    assert any('independent_transfer_set' in r for r in response.unsupported_reasons)


def test_missing_or_extra_transfers_fail_closed() -> None:
    scenario = _scenario()
    missing = compose_coherent_system_response(
        scenario, (_complex_transfer('speaker-fl', (1.0 + 0j, 1.0 + 0j)),)
    )
    assert missing.state == 'UNSUPPORTED'
    assert any('speaker-fr' in r for r in missing.unsupported_reasons)

    extra = compose_coherent_system_response(
        scenario,
        (
            _complex_transfer('speaker-fl', (1.0 + 0j, 1.0 + 0j)),
            _complex_transfer('speaker-fr', (1.0 + 0j, 1.0 + 0j)),
            _complex_transfer('ghost', (1.0 + 0j, 1.0 + 0j)),
        ),
    )
    assert extra.state == 'UNSUPPORTED'
    assert any('outside the scenario' in r for r in extra.unsupported_reasons)


def test_mismatched_grids_and_conventions_fail_closed() -> None:
    scenario = _scenario()
    bad_grid = ScenarioSourceTransfer(
        source_entity_id='speaker-fr',
        frequency_hz=(110.0, 220.0),
        pressure_real=(1.0, 1.0),
        pressure_imag=(0.0, 0.0),
        phasor_convention='exp(+i*omega*t)',
        source_normalization_id='norm:1w1m',
        timing_authority='absolute_propagation_time',
    )
    response = compose_coherent_system_response(
        scenario,
        (_complex_transfer('speaker-fl', (1.0 + 0j, 1.0 + 0j)), bad_grid),
    )
    assert response.state == 'UNSUPPORTED'
    assert any('frequency grid' in r for r in response.unsupported_reasons)


def test_delay_without_timing_authority_is_flagged() -> None:
    scenario = _scenario(
        participants=(
            _participant('speaker-fl', 'FL'),
            _participant('speaker-fr', 'FR', delay_s=0.001),
        )
    )
    response = compose_coherent_system_response(
        scenario,
        (
            _complex_transfer('speaker-fl', (1.0 + 0j, 1.0 + 0j)),
            _complex_transfer(
                'speaker-fr', (1.0 + 0j, 1.0 + 0j), timing='unavailable'
            ),
        ),
    )
    assert response.state == 'UNSUPPORTED'
    assert any('timing authority' in r for r in response.unsupported_reasons)


def _resolved(
    label: str,
    real: tuple[float, ...],
    imag: tuple[float, ...],
    *,
    grid: tuple[float, ...] = (50.0, 150.0),
    residual: tuple[tuple[float, ...], tuple[float, ...]] | None = None,
) -> ResolvedPlaybackTransfer:
    return ResolvedPlaybackTransfer(
        authority=_authority(label),
        frequency_hz=grid,
        transfer_real=real,
        transfer_imag=imag,
        phasor_convention='exp(+i*omega*t)',
        residual_real=residual[0] if residual else None,
        residual_imag=residual[1] if residual else None,
    )


def _bass_managed_scenario(**overrides):
    kwargs = dict(
        participants=(
            _participant('speaker-fl', 'FL'),
            _participant('sub-1', 'SUB1'),
        ),
        bass_management_routes=(
            BassManagementRoute(
                route_id='bm-1',
                from_logical_channel_id='FL',
                to_source_entity_id='sub-1',
                band=FrequencyDomain(minimum_hz=20.0, maximum_hz=80.0),
                method_authority_ref=_authority('crossover'),
            ),
        ),
        preset='mains_plus_subs_bass_managed',
    )
    kwargs.update(overrides)
    return _scenario(**kwargs)


def test_bass_management_route_resolves_and_splits_bands() -> None:
    scenario = _bass_managed_scenario()
    # Idealized crossover on a (50, 150) Hz grid: the low band at 50 Hz
    # moves to the sub; the residual at 150 Hz stays on the main.
    crossover = _resolved(
        'crossover',
        real=(1.0, 0.0),
        imag=(0.0, 0.0),
        residual=((0.0, 1.0), (0.0, 0.0)),
    )
    response = compose_coherent_system_response(
        scenario,
        (
            _complex_transfer('speaker-fl', (1.0 + 0j, 1.0 + 0j), grid=(50.0, 150.0)),
            _complex_transfer('sub-1', (0.5 + 0j, 0.5 + 0j), grid=(50.0, 150.0)),
        ),
        resolved_transfers=(crossover,),
    )
    assert response.state == 'READY', response.unsupported_reasons
    # 50 Hz: FL keeps residual 0; SUB reproduces own band + FL's redirected
    # band: 0.5 * (1 + 1) = 1.0. Naive full-band sum would be 1.5.
    # 150 Hz: FL keeps residual 1.0; SUB gets nothing extra: 1.0 + 0.5 = 1.5.
    assert math.isclose(response.pressure_real[0], 1.0, abs_tol=1e-9)
    assert math.isclose(response.pressure_real[1], 1.5, abs_tol=1e-9)
    assert crossover.authority in response.applied_authority_refs


def test_bass_management_route_requires_resolved_method() -> None:
    scenario = _bass_managed_scenario()
    response = compose_coherent_system_response(
        scenario,
        (
            _complex_transfer('speaker-fl', (1.0 + 0j, 1.0 + 0j), grid=(50.0, 150.0)),
            _complex_transfer('sub-1', (1.0 + 0j, 1.0 + 0j), grid=(50.0, 150.0)),
        ),
    )
    assert response.state == 'UNSUPPORTED'
    assert any('bm-1' in r for r in response.unsupported_reasons)

    # A hash-colliding but non-matching authority id must not resolve.
    wrong_id = _resolved(
        'crossover', (1.0, 0.0), (0.0, 0.0), residual=((0.0, 1.0), (0.0, 0.0))
    )
    wrong_id = ResolvedPlaybackTransfer(
        authority=ExactExternalAuthorityRef(
            authority_id='test:different',
            authority_version='1',
            semantic_hash_sha256=_hash('crossover'),
        ),
        frequency_hz=wrong_id.frequency_hz,
        transfer_real=wrong_id.transfer_real,
        transfer_imag=wrong_id.transfer_imag,
        phasor_convention=wrong_id.phasor_convention,
        residual_real=wrong_id.residual_real,
        residual_imag=wrong_id.residual_imag,
    )
    response = compose_coherent_system_response(
        scenario,
        (
            _complex_transfer('speaker-fl', (1.0 + 0j, 1.0 + 0j), grid=(50.0, 150.0)),
            _complex_transfer('sub-1', (1.0 + 0j, 1.0 + 0j), grid=(50.0, 150.0)),
        ),
        resolved_transfers=(wrong_id,),
    )
    assert response.state == 'UNSUPPORTED'


def test_route_method_without_residual_fails_closed() -> None:
    scenario = _bass_managed_scenario()
    no_residual = _resolved('crossover', (1.0, 1.0), (0.0, 0.0))
    response = compose_coherent_system_response(
        scenario,
        (
            _complex_transfer('speaker-fl', (1.0 + 0j, 1.0 + 0j), grid=(50.0, 150.0)),
            _complex_transfer('sub-1', (1.0 + 0j, 1.0 + 0j), grid=(50.0, 150.0)),
        ),
        resolved_transfers=(no_residual,),
    )
    assert response.state == 'UNSUPPORTED'
    assert any('residual' in r for r in response.unsupported_reasons)


def test_drive_filter_authority_multiplies_source() -> None:
    participant_fl = _participant('speaker-fl', 'FL')
    participant_fr = _participant('speaker-fr', 'FR')
    participant_fr = ExcitationChannelParticipant(
        logical_channel_id=participant_fr.logical_channel_id,
        channel_role_id=participant_fr.channel_role_id,
        source_entity_id=participant_fr.source_entity_id,
        source_binding_sha256=participant_fr.source_binding_sha256,
        equipment_definition_id=participant_fr.equipment_definition_id,
        equipment_definition_version=participant_fr.equipment_definition_version,
        equipment_definition_sha256=participant_fr.equipment_definition_sha256,
        drive=ExcitationDriveState(
            gain_db=0.0,
            delay_s=0.0,
            polarity=1,
            filter_authority_ref=_authority('fr-filter'),
        ),
    )
    scenario = _scenario(participants=(participant_fl, participant_fr))
    # Two different filters must produce different responses.
    half = _resolved('fr-filter', (0.5, 0.5), (0.0, 0.0))
    quarter = ResolvedPlaybackTransfer(
        authority=_authority('fr-filter'),
        frequency_hz=half.frequency_hz,
        transfer_real=(0.25, 0.25),
        transfer_imag=(0.0, 0.0),
        phasor_convention=half.phasor_convention,
    )
    transfers = (
        _complex_transfer('speaker-fl', (1.0 + 0j, 1.0 + 0j), grid=(50.0, 150.0)),
        _complex_transfer('speaker-fr', (1.0 + 0j, 1.0 + 0j), grid=(50.0, 150.0)),
    )
    with_half = compose_coherent_system_response(
        scenario, transfers, resolved_transfers=(half,)
    )
    with_quarter = compose_coherent_system_response(
        scenario, transfers, resolved_transfers=(quarter,)
    )
    assert with_half.state == 'READY'
    assert math.isclose(with_half.pressure_real[0], 1.5, abs_tol=1e-9)
    assert math.isclose(with_quarter.pressure_real[0], 1.25, abs_tol=1e-9)
    assert half.authority in with_half.applied_authority_refs

    unresolved = compose_coherent_system_response(scenario, transfers)
    assert unresolved.state == 'UNSUPPORTED'
    assert any('fr-filter' in r for r in unresolved.unsupported_reasons)


def test_profile_compiles_into_scenario_routes() -> None:
    from htdt.cad_bass_management import (
        CrossoverSpec,
        FrequencyBand,
        MainChannelBassRule,
        build_bass_management_profile,
    )

    profile = build_bass_management_profile(
        profile_id='bm-profile',
        version='1',
        main_rules=(
            MainChannelBassRule(
                logical_role_id='role:FL',
                handling='high_pass',
                high_pass=CrossoverSpec(
                    frequency_hz=80.0,
                    slope_db_per_octave=24.0,
                    filter_family='linkwitz_riley',
                ),
                redirected_low_band=FrequencyBand(low_hz=20.0, high_hz=80.0),
                redirected_destinations=('sub-group-1',),
            ),
        ),
    )
    scenario = _scenario(
        participants=(
            _participant('speaker-fl', 'FL'),
            _participant('sub-1', 'SUB1'),
        ),
        preset='mains_plus_subs_bass_managed',
    )
    routes = bass_management_routes_for_scenario(
        scenario,
        profile,
        method_authority_ref=_authority('crossover'),
        destination_source_ids={'sub-group-1': 'sub-1'},
    )
    assert len(routes) == 1
    assert routes[0].from_logical_channel_id == 'FL'
    assert routes[0].to_source_entity_id == 'sub-1'

    crossover = _resolved(
        'crossover',
        real=(1.0, 0.0),
        imag=(0.0, 0.0),
        residual=((0.0, 1.0), (0.0, 0.0)),
    )
    managed = _scenario(
        participants=(
            _participant('speaker-fl', 'FL'),
            _participant('sub-1', 'SUB1'),
        ),
        bass_management_routes=routes,
        preset='mains_plus_subs_bass_managed',
    )
    response = compose_coherent_system_response(
        managed,
        (
            _complex_transfer('speaker-fl', (1.0 + 0j, 1.0 + 0j), grid=(50.0, 150.0)),
            _complex_transfer('sub-1', (0.5 + 0j, 0.5 + 0j), grid=(50.0, 150.0)),
        ),
        resolved_transfers=(crossover,),
    )
    assert response.state == 'READY'
    assert math.isclose(response.pressure_real[0], 1.0, abs_tol=1e-9)
    assert math.isclose(response.pressure_real[1], 1.5, abs_tol=1e-9)

    with pytest.raises(ValueError, match='not bound'):
        bass_management_routes_for_scenario(
            scenario,
            profile,
            method_authority_ref=_authority('crossover'),
            destination_source_ids={},
        )
