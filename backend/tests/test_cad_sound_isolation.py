from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_equipment import EquipmentDataProvenance, FrequencyDomain
from htdt.cad_sound_isolation import (
    IsolationGoal,
    IsolationMeasurement,
    IsolationPath,
    IsolationRating,
    IsolationScenario,
    TransmissionLossBand,
    UNMODELED_ISOLATION_MECHANISMS,
    build_isolation_assembly,
    estimate_isolation,
    evaluate_isolation_goal,
)


def _prov() -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='manufacturer',
        source_name='Fixture Lab',
        source_version='1',
        source_reference='report-1',
        source_sha256='a' * 64,
    )


def _band(lo: float, hi: float, band_id: str = 'b') -> FrequencyDomain:
    return FrequencyDomain(minimum_hz=lo, maximum_hz=hi)


def _wall() -> object:
    return build_isolation_assembly(
        assembly_id='asm-wall',
        name='Double stud wall',
        evidence_tier='lab_tl_spectrum',
        provenance=(_prov(),),
        tl_bands=(
            TransmissionLossBand(
                band_id='125',
                frequency=_band(100.0, 160.0),
                tl_db=30.0,
            ),
            TransmissionLossBand(
                band_id='500',
                frequency=_band(400.0, 630.0),
                tl_db=55.0,
            ),
        ),
        ratings=(IsolationRating(method='stc', value=59.0),),
        valid_frequency=_band(100.0, 630.0),
    )


def _door() -> object:
    return build_isolation_assembly(
        assembly_id='asm-door',
        name='Solid door',
        evidence_tier='lab_rating_only',
        provenance=(_prov(),),
        tl_bands=(
            TransmissionLossBand(
                band_id='125',
                frequency=_band(100.0, 160.0),
                tl_db=18.0,
            ),
        ),
        valid_frequency=_band(100.0, 160.0),
    )


def _scenario(paths) -> IsolationScenario:
    return IsolationScenario(
        scenario_id='sc-1',
        source_region_id='theater',
        receiving_region_id='bedroom',
        paths=paths,
        evaluation_bands=(_band(100.0, 160.0), _band(400.0, 630.0)),
    )


def test_estimate_combines_paths_energetically_not_db_average():
    wall = _wall()
    door = _door()
    scenario = _scenario((
        IsolationPath(
            path_id='wall',
            kind='partition',
            source_region_id='theater',
            receiving_region_id='bedroom',
            assembly_id='asm-wall',
            area_m2=9.0,
        ),
        IsolationPath(
            path_id='door',
            kind='door',
            source_region_id='theater',
            receiving_region_id='bedroom',
            assembly_id='asm-door',
            area_m2=1.0,
        ),
    ))
    estimate = estimate_isolation(scenario=scenario, assemblies=(wall, door))
    band0 = estimate.band_estimates[0]
    assert band0.combined_tl_db is not None
    # τ composite: (9*10^-3 + 1*10^-1.8)/10 -> 26.05 dB, pulled toward the
    # weak door but never the naive dB mean of 30 and 18 (24 dB).
    assert band0.combined_tl_db == pytest.approx(26.0469, abs=1e-3)
    assert 18.0 < band0.combined_tl_db < 30.0
    assert band0.combined_tl_db != pytest.approx(24.0)
    assert set(band0.modeled_path_ids) == {'wall', 'door'}
    # second band has no door coverage -> door stays UNKNOWN, not dropped
    band1 = estimate.band_estimates[1]
    assert band1.combined_tl_db is not None
    assert 'door' in band1.unknown_path_ids


def test_missing_assembly_stays_unknown_never_default_wall():
    scenario = _scenario((
        IsolationPath(
            path_id='mystery-wall',
            kind='partition',
            source_region_id='theater',
            receiving_region_id='bedroom',
            assembly_id=None,
            area_m2=10.0,
        ),
    ))
    estimate = estimate_isolation(scenario=scenario, assemblies=())
    assert all(
        item.status == 'UNKNOWN' for item in estimate.path_results
    )
    assert all(
        item.combined_tl_db is None for item in estimate.band_estimates
    )


def test_estimate_always_lists_unmodeled_mechanisms():
    estimate = estimate_isolation(
        scenario=_scenario((
            IsolationPath(
                path_id='wall',
                kind='partition',
                source_region_id='theater',
                receiving_region_id='bedroom',
                assembly_id='asm-wall',
                area_m2=10.0,
            ),
        )),
        assemblies=(_wall(),),
    )
    assert estimate.unmodeled_mechanisms == UNMODELED_ISOLATION_MECHANISMS
    assert 'flanking_paths' in estimate.unmodeled_mechanisms
    assert estimate.dominant_path_ids == ('wall', 'wall')


def test_opening_state_semantics():
    scenario = _scenario((
        IsolationPath(
            path_id='hatch',
            kind='opening',
            source_region_id='theater',
            receiving_region_id='bedroom',
            area_m2=0.5,
            opening_state='open',
        ),
        IsolationPath(
            path_id='unstated',
            kind='penetration',
            source_region_id='theater',
            receiving_region_id='bedroom',
            area_m2=0.1,
            opening_state='unknown',
        ),
    ))
    estimate = estimate_isolation(scenario=scenario, assemblies=())
    statuses = {item.path_id: item.status for item in estimate.path_results}
    assert statuses['hatch'] == 'AVAILABLE'
    assert statuses['unstated'] == 'UNKNOWN'
    # an open path still combines at tau=1 -> combined estimate ≈ 0 dB-ish
    assert estimate.band_estimates[0].combined_tl_db == pytest.approx(0.0)


def test_rating_never_fills_missing_band_tl():
    assembly = build_isolation_assembly(
        assembly_id='asm-rw',
        name='Rated only',
        evidence_tier='lab_rating_only',
        provenance=(_prov(),),
        tl_bands=(),
        ratings=(IsolationRating(method='rw', value=52.0),),
        valid_frequency=_band(50.0, 5000.0),
    )
    scenario = _scenario((
        IsolationPath(
            path_id='wall',
            kind='partition',
            source_region_id='theater',
            receiving_region_id='bedroom',
            assembly_id='asm-rw',
            area_m2=8.0,
        ),
    ))
    estimate = estimate_isolation(scenario=scenario, assemblies=(assembly,))
    assert all(item.status == 'UNKNOWN' for item in estimate.path_results)


def test_measurement_method_profile_separates_informal_from_standard():
    with pytest.raises(ValidationError):
        IsolationMeasurement(
            measurement_id='m-1',
            method_profile='informal',
            source_region_id='theater',
            receiving_region_id='bedroom',
            single_number_method='iso_717_1',
            single_number_value=45.0,
            measured_at_utc='2026-09-24T00:00:00+00:00',
            provenance=(_prov(),),
        )
    informal = IsolationMeasurement(
        measurement_id='m-2',
        method_profile='informal',
        source_region_id='theater',
        receiving_region_id='bedroom',
        band_attenuation_db=(
            TransmissionLossBand(
                band_id='125',
                frequency=_band(100.0, 160.0),
                tl_db=22.0,
            ),
        ),
        measured_at_utc='2026-09-24T00:00:00+00:00',
        provenance=(_prov(),),
    )
    assert not informal.is_standardized


def test_isolation_goal_evaluation():
    estimate = estimate_isolation(
        scenario=_scenario((
            IsolationPath(
                path_id='wall',
                kind='partition',
                source_region_id='theater',
                receiving_region_id='bedroom',
                assembly_id='asm-wall',
                area_m2=10.0,
            ),
        )),
        assemblies=(_wall(),),
    )
    goal = IsolationGoal(
        goal_id='g-1',
        kind='minimum_isolation',
        frequency=_band(400.0, 630.0),
        value_db=50.0,
    )
    assert evaluate_isolation_goal(goal, estimate).status == 'PASS'
    strict = IsolationGoal(
        goal_id='g-2',
        kind='minimum_isolation',
        frequency=_band(400.0, 630.0),
        value_db=60.0,
    )
    assert evaluate_isolation_goal(strict, estimate).status == 'FAIL'
    level = IsolationGoal(
        goal_id='g-3',
        kind='maximum_received_level',
        frequency=_band(400.0, 630.0),
        value_db=35.0,
    )
    assert evaluate_isolation_goal(level, estimate).status == 'UNSUPPORTED'
    outside = IsolationGoal(
        goal_id='g-4',
        kind='minimum_isolation',
        frequency=_band(20.0, 40.0),
        value_db=10.0,
    )
    assert evaluate_isolation_goal(outside, estimate).status == 'UNKNOWN'
