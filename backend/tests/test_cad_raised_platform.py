"""Raised seating-platform acoustic assembly tests (#1024)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_equipment import EquipmentDataProvenance
from htdt.cad_raised_platform import (
    PlatformCavity,
    PlatformOpening,
    PlatformPorousFill,
    RaisedPlatformAcousticAssembly,
    build_raised_platform_assembly,
    evaluate_platform_readiness,
)


def _provenance(digit: str = '2'):
    return (
        EquipmentDataProvenance(
            evidence_kind='user_defined',
            source_name='owner survey',
            source_version='1',
            source_reference='platform drawings',
            source_sha256=digit * 64,
        ),
    )


def _cavity(cavity_id='cav-1', **overrides):
    kwargs = dict(
        volume_m3=1.8,
        interior_state='recorded',
        porous_fill=(
            PlatformPorousFill(
                material_ref='insulation-r30',
                coverage_fraction=0.7,
                provenance=_provenance('3'),
            ),
        ),
        provenance=_provenance(),
    )
    kwargs.update(overrides)
    return PlatformCavity(cavity_id=cavity_id, **kwargs)


def _assembly(**overrides):
    kwargs = dict(
        assembly_id='platform-1',
        version='1',
        platform_entity_id='riser-main',
        declared_purposes=('seating_elevation_only', 'tactile_platform'),
        cavities=(_cavity(),),
        openings=(
            PlatformOpening(
                opening_id='vent-1',
                cavity_id='cav-1',
                kind='slot',
                area_m2=0.04,
                neck_length_m=0.12,
                termination='unflanged',
                blockage_state='clear',
                provenance=_provenance('5'),
            ),
        ),
        provenance=_provenance('6'),
    )
    kwargs.update(overrides)
    return build_raised_platform_assembly(**kwargs)


def test_assembly_hash_and_unique_ids():
    assembly = _assembly()
    assert assembly.semantic_sha256
    payload = assembly.model_dump(mode='python')
    payload['declared_purposes'] = ('vented_low_frequency_absorber',)
    with pytest.raises(ValidationError, match='semantic hash mismatch'):
        RaisedPlatformAcousticAssembly(**payload)
    with pytest.raises(ValidationError, match='duplicate'):
        _assembly(cavities=(_cavity(), _cavity()))


def test_opening_must_bind_known_cavity():
    with pytest.raises(ValidationError, match='unknown cavity'):
        _assembly(
            openings=(
                PlatformOpening(
                    opening_id='o', cavity_id='ghost', kind='slot'
                ),
            )
        )


def test_purpose_is_declared_not_inferred():
    bare = build_raised_platform_assembly(
        assembly_id='bare',
        platform_entity_id='riser-2',
        provenance=_provenance(),
    )
    report = evaluate_platform_readiness(assembly=bare)
    checks = {c.check: c.status for c in report.checks}
    # no declared purpose → not assumed inert or absorptive
    assert checks['purpose_declared'] == 'UNKNOWN'
    assert checks['cavities_recorded'] == 'UNKNOWN'


def test_vented_purpose_requires_openings():
    sealed = _assembly(
        declared_purposes=('vented_low_frequency_absorber',),
        openings=(),
    )
    report = evaluate_platform_readiness(assembly=sealed)
    checks = {c.check: c.status for c in report.checks}
    assert checks['openings_bound'] == 'UNKNOWN'


def test_unknown_interior_stays_unknown():
    assembly = _assembly(
        cavities=(
            _cavity(
                cavity_id='cav-1',
                volume_m3=None,
                interior_state='unknown',
                porous_fill=(),
            ),
        )
    )
    report = evaluate_platform_readiness(assembly=assembly)
    checks = {c.check: c.status for c in report.checks}
    assert checks['cavities_recorded'] == 'UNKNOWN'


def test_full_recorded_assembly_passes():
    report = evaluate_platform_readiness(assembly=_assembly())
    checks = {c.check: c.status for c in report.checks}
    assert checks['purpose_declared'] == 'PASS'
    assert checks['cavities_recorded'] == 'PASS'
    assert checks['openings_bound'] == 'PASS'
    assert checks['openings_unblocked'] == 'PASS'


def test_blocked_opening_not_an_absorber():
    assembly = _assembly(
        openings=(
            PlatformOpening(
                opening_id='vent-1',
                cavity_id='cav-1',
                kind='slot',
                blockage_state='blocked',
                provenance=_provenance('5'),
            ),
        )
    )
    report = evaluate_platform_readiness(assembly=assembly)
    checks = {c.check: c.status for c in report.checks}
    assert checks['openings_unblocked'] == 'UNKNOWN'
    with pytest.raises(ValidationError, match='blocked opening'):
        PlatformOpening(
            opening_id='bad',
            cavity_id='cav-1',
            kind='slot',
            area_m2=0.04,
            blockage_state='blocked',
        )


def test_multi_cell_cavities_not_merged():
    assembly = _assembly(
        cavities=(
            _cavity(),
            _cavity(
                cavity_id='cav-2',
                volume_m3=0.9,
                interior_state='recorded',
                porous_fill=(),
            ),
        )
    )
    assert len(assembly.cavities) == 2
    report = evaluate_platform_readiness(assembly=assembly)
    checks = {c.check: c.status for c in report.checks}
    assert checks['cavities_recorded'] == 'PASS'
