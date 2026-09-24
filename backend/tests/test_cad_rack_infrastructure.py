from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_equipment import EquipmentDataProvenance
from htdt.cad_rack_infrastructure import (
    CircuitAssignment,
    CircuitEndpoint,
    ElectricalScenario,
    EquipmentPowerProfile,
    PowerValue,
    RackLayout,
    RackPlacement,
    build_rack_definition,
    evaluate_rack_fit,
    summarize_endpoint_loads,
    summarize_heat,
    summarize_load,
)


def _prov() -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='manufacturer',
        source_name='Yamaha RX-A4A specs',
        source_version='2026',
        source_reference='specs-page',
        source_sha256='b' * 64,
    )


def _rack(**overrides) -> object:
    payload = dict(
        rack_id='rack-a',
        name='Rack A',
        width_m=0.6,
        height_m=1.0,
        usable_depth_m=0.5,
        ru_capacity=12,
        front_clearance_m=0.1,
        rear_clearance_m=0.15,
        provenance=(_prov(),),
    )
    payload.update(overrides)
    return build_rack_definition(**payload)


def _avr() -> EquipmentPowerProfile:
    """RX-A4A-like declared evidence: dims + input power, no invented RU."""
    return EquipmentPowerProfile(
        device_id='avr-1',
        power_values=(
            PowerValue(
                state='standby_off', power_w=0.4,
                kind='manufacturer_declared', provenance=_prov(),
            ),
            PowerValue(
                state='standby_passthrough', power_w=3.0,
                kind='manufacturer_declared', provenance=_prov(),
            ),
            PowerValue(
                state='rated_max', power_w=410.0,
                kind='manufacturer_declared', provenance=_prov(),
            ),
        ),
        chassis_depth_m=0.442,
        ru_height=5,
        requires_rear_clearance_m=0.1,
        provenance=(_prov(),),
    )


def test_rack_fit_pass_fail_unknown():
    rack = _rack()
    fit = evaluate_rack_fit(
        layout=RackLayout(
            rack_id='rack-a',
            placements=(RackPlacement(device_id='avr-1', ru_position=2),),
        ),
        rack=rack,
        devices=(_avr(),),
    )[0]
    assert fit.ru_occupancy == 'PASS'
    assert fit.depth_fit == 'PASS'
    assert fit.clearance_fit == 'PASS'

    # 0.6 m deep device does not fit 0.5 m usable depth
    deep = EquipmentPowerProfile(
        device_id='amp-1',
        chassis_depth_m=0.6,
        ru_height=2,
        provenance=(_prov(),),
    )
    fit2 = evaluate_rack_fit(
        layout=RackLayout(
            rack_id='rack-a',
            placements=(RackPlacement(device_id='amp-1', ru_position=7),),
        ),
        rack=rack,
        devices=(deep,),
    )[0]
    assert fit2.depth_fit == 'FAIL'

    # undeclared dimensions stay UNKNOWN, never silently pass
    unknown = evaluate_rack_fit(
        layout=RackLayout(
            rack_id='rack-a',
            placements=(RackPlacement(device_id='mystery', shelf_label='top'),),
        ),
        rack=rack,
        devices=(),
    )[0]
    assert unknown.ru_occupancy == 'UNKNOWN'
    assert unknown.depth_fit == 'UNKNOWN'


def test_ru_conflict_and_capacity_fail():
    rack = _rack(ru_capacity=6)
    two = (
        EquipmentPowerProfile(device_id='a', ru_height=4, provenance=(_prov(),)),
        EquipmentPowerProfile(device_id='b', ru_height=4, provenance=(_prov(),)),
    )
    results = evaluate_rack_fit(
        layout=RackLayout(
            rack_id='rack-a',
            placements=(
                RackPlacement(device_id='a', ru_position=1),
                RackPlacement(device_id='b', ru_position=3),
            ),
        ),
        rack=rack,
        devices=two,
    )
    assert results[0].ru_occupancy == 'PASS'
    assert results[1].ru_occupancy == 'FAIL'
    assert 'a' in results[1].conflicts


def test_load_summary_never_counts_unknown_as_zero():
    scenario = ElectricalScenario(
        scenario_id='sc-max',
        state='rated_max',
        device_ids=('avr-1', 'amp-1', 'unprofiled'),
    )
    devices = (
        _avr(),
        EquipmentPowerProfile(device_id='amp-1', provenance=(_prov(),)),
    )
    summary = summarize_load(scenario, devices)
    assert summary.known_w == 410.0
    assert summary.known_device_ids == ('avr-1',)
    assert set(summary.unknown_device_ids) == {'amp-1', 'unprofiled'}


def test_standby_scenario_uses_only_matching_state():
    scenario = ElectricalScenario(
        scenario_id='sc-standby',
        state='standby_off',
        device_ids=('avr-1',),
    )
    summary = summarize_load(scenario, (_avr(),))
    assert summary.known_w == 0.4


def test_endpoint_loads_are_arithmetic_not_compliance():
    endpoints = (
        CircuitEndpoint(
            endpoint_id='pdu-1', kind='pdu_branch', label='PDU 1',
            declared_rating_w=500.0,
        ),
        CircuitEndpoint(
            endpoint_id='wall-1', kind='outlet', label='Wall',
        ),
    )
    assignments = (
        CircuitAssignment(device_id='avr-1', endpoint_id='pdu-1'),
        CircuitAssignment(device_id='amp-1', endpoint_id='pdu-1'),
        CircuitAssignment(device_id='avr-1', endpoint_id='wall-1'),
    )
    devices = (
        _avr(),
        EquipmentPowerProfile(device_id='amp-1', provenance=(_prov(),)),
    )
    summaries = {item.endpoint_id: item for item in summarize_endpoint_loads(
        assignments, endpoints, devices, state='rated_max',
    )}
    pdu = summaries['pdu-1']
    assert pdu.known_w == 410.0
    assert pdu.unknown_device_ids == ('amp-1',)
    assert pdu.exceeds_declared_rating is None  # unknowns suppress the check
    wall = summaries['wall-1']
    assert wall.known_w == 410.0
    assert wall.exceeds_declared_rating is None  # no declared rating


def test_heat_summary_keeps_documented_separate_from_derived():
    amp = EquipmentPowerProfile(
        device_id='amp-1',
        power_values=(
            PowerValue(
                state='typical', power_w=120.0,
                kind='user_measured', provenance=_prov(),
            ),
        ),
        heat_dissipation_w=95.0,
        heat_provenance=_prov(),
        provenance=(_prov(),),
    )
    summary = summarize_heat((_avr(), amp))
    assert summary.documented_heat_w == 95.0
    assert summary.documented_device_ids == ('amp-1',)
    assert summary.undocumented_device_ids == ('avr-1',)
    assert summary.derived_heat_w is None

    derived = summarize_heat(
        (amp,), derived_from_state='typical',
        derivation_note='all measured input becomes room heat (assumption)',
    )
    assert derived.derived_heat_w == 120.0
    assert 'assumption' in (derived.derivation_note or '')


def test_derived_power_requires_derivation_record():
    with pytest.raises(ValidationError):
        PowerValue(
            state='typical', power_w=10.0,
            kind='derived', provenance=_prov(),
        )


def test_heat_value_requires_provenance():
    with pytest.raises(ValidationError):
        EquipmentPowerProfile(
            device_id='x',
            heat_dissipation_w=50.0,
            provenance=(_prov(),),
        )
