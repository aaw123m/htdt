"""Connected-space acoustic portal coupling tests (#1029)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_acoustic_portal_coupling import (
    AcousticPortalCoupling,
    build_acoustic_portal_coupling,
    build_connected_acoustic_region,
    evaluate_portal_readiness,
)
from htdt.cad_equipment import EquipmentDataProvenance


def _provenance(digit: str = '2'):
    return (
        EquipmentDataProvenance(
            evidence_kind='user_defined',
            source_name='room survey',
            source_version='1',
            source_reference='site measurements',
            source_sha256=digit * 64,
        ),
    )


def _coupling(**overrides):
    kwargs = dict(
        portal_coupling_id='apc-1',
        version='1',
        physical_portal_ref='door-west',
        from_region_id='theater',
        to_region_id='hallway',
        kind='doorway',
        state='open',
        termination='connected_region',
        effective_aperture_m2=1.8,
        provenance=_provenance(),
    )
    kwargs.update(overrides)
    return build_acoustic_portal_coupling(**kwargs)


def test_coupling_hash():
    coupling = _coupling()
    payload = coupling.model_dump(mode='python')
    payload['state'] = 'closed'
    with pytest.raises(ValidationError, match='semantic hash mismatch'):
        AcousticPortalCoupling(**payload)


def test_open_fraction_state_consistency():
    with pytest.raises(ValidationError, match='open_fraction'):
        _coupling(state='open', open_fraction=0.5)
    with pytest.raises(ValidationError, match='open_fraction'):
        _coupling(state='partially_open', open_fraction=None)
    ok = _coupling(state='custom', open_fraction=0.3)
    assert ok.open_fraction == 0.3


def test_connected_region_requires_target():
    with pytest.raises(ValidationError, match='to_region_id'):
        _coupling(to_region_id=None)
    with pytest.raises(ValidationError, match='itself'):
        _coupling(to_region_id='theater')


def test_resolved_region_portal_readiness():
    region = build_connected_acoustic_region(
        region_id='hallway',
        label='Hallway',
        volume_m3=12.0,
        provenance=_provenance('3'),
    )
    assert region.volume_m3 == 12.0
    report = evaluate_portal_readiness(
        coupling=_coupling(),
        known_region_ids=('theater', 'hallway'),
        solver_capabilities=('portal_coupling',),
    )
    checks = {c.check: c.status for c in report.checks}
    assert checks['topology_recorded'] == 'PASS'
    assert checks['aperture_recorded'] == 'PASS'
    assert checks['state_recorded'] == 'PASS'
    assert checks['solver_support'] == 'PASS'


def test_unresolved_region_never_absorbing_wall():
    report = evaluate_portal_readiness(
        coupling=_coupling(),
        known_region_ids=('theater',),
        solver_capabilities=('portal_coupling',),
    )
    checks = {c.check: c.status for c in report.checks}
    assert checks['topology_recorded'] == 'FAIL'
    reason = {c.check: c.reason for c in report.checks}[
        'topology_recorded'
    ]
    assert 'absorbing wall' in reason


def test_closed_portal_is_valid_state():
    report = evaluate_portal_readiness(
        coupling=_coupling(state='closed', effective_aperture_m2=None),
        known_region_ids=('theater', 'hallway'),
        solver_capabilities=('portal_coupling',),
    )
    checks = {c.check: c.status for c in report.checks}
    assert checks['aperture_recorded'] == 'PASS'
    assert checks['state_recorded'] == 'PASS'


def test_unknown_state_and_no_solver_support():
    report = evaluate_portal_readiness(
        coupling=_coupling(state='unknown', effective_aperture_m2=None),
        known_region_ids=('theater', 'hallway'),
        solver_capabilities=(),
    )
    checks = {c.check: c.status for c in report.checks}
    assert checks['state_recorded'] == 'UNKNOWN'
    assert checks['aperture_recorded'] == 'UNKNOWN'
    assert checks['solver_support'] == 'UNKNOWN'


def test_exterior_termination_no_region_geometry():
    report = evaluate_portal_readiness(
        coupling=_coupling(
            kind='window',
            to_region_id=None,
            termination='exterior_radiation',
        ),
        solver_capabilities=('portal_coupling',),
    )
    checks = {c.check: c.status for c in report.checks}
    assert checks['topology_recorded'] == 'PASS'
    assert checks['adjacent_geometry'] == 'NOT_APPLICABLE'
