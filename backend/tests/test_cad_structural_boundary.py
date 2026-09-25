"""Compliant boundary / vibroacoustic coupling tests (#1008)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_equipment import EquipmentDataProvenance, FrequencyDomain
from htdt.cad_structural_boundary import (
    DerivedEffectiveImpedance,
    StructuralBoundaryModel,
    build_derived_effective_impedance,
    build_structural_boundary_model,
    evaluate_boundary_capability,
)


def _provenance(digit: str = '2'):
    return (
        EquipmentDataProvenance(
            evidence_kind='manufacturer',
            source_name='Materials DB',
            source_version='1',
            source_reference='gypsum datasheet',
            source_sha256=digit * 64,
        ),
    )


def _model(**overrides):
    kwargs = dict(
        boundary_model_id='wall-n-1',
        version='1',
        boundary_ref='wall-north',
        kind='thin_plate',
        areal_mass_kg_m2=8.5,
        bending_stiffness_nm=1.2e5,
        damping_loss_factor=0.05,
        edge_condition='simply_supported',
        cavity_backing='sealed_air_cavity',
        cavity_depth_m=0.09,
        coupling_sides='both',
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=20.0, maximum_hz=5000.0
        ),
        construction_assembly_ref='assembly-gyp-1',
        provenance=_provenance(),
    )
    kwargs.update(overrides)
    return build_structural_boundary_model(**kwargs)


def test_structural_model_hash_and_class():
    model = _model()
    assert model.capability_class == 'structurally_compliant'
    payload = model.model_dump(mode='python')
    payload['kind'] = 'shell'
    with pytest.raises(ValidationError, match='semantic hash mismatch'):
        StructuralBoundaryModel(**payload)


def test_plate_kind_requires_structural_constant():
    with pytest.raises(ValidationError, match='structural constant'):
        _model(areal_mass_kg_m2=None, bending_stiffness_nm=None)


def test_construction_ref_is_lineage_not_constants():
    # an assembly ref alone does not fabricate areal mass / stiffness
    model = build_structural_boundary_model(
        boundary_model_id='m2',
        boundary_ref='wall-east',
        kind='opaque',
        construction_assembly_ref='assembly-gyp-1',
        provenance=_provenance(),
    )
    assert model.areal_mass_kg_m2 is None
    assert model.bending_stiffness_nm is None


def test_derived_impedance_keeps_lineage():
    model = _model()
    derivation = build_derived_effective_impedance(
        structural_model=model,
        provider_id='tmm-struct-1',
        parameters={'incidence': 'normal'},
        provenance=_provenance('3'),
    )
    assert derivation.structural_model_sha256 == model.semantic_sha256
    payload = derivation.model_dump(mode='python')
    payload['provider_id'] = 'other'
    with pytest.raises(ValidationError, match='hash mismatch'):
        DerivedEffectiveImpedance(**payload)


def test_structural_boundary_supported_by_plate_coupling():
    report = evaluate_boundary_capability(
        boundary_ref='wall-north',
        capability_class='structurally_compliant',
        structural_model=_model(),
        provider_capabilities=(
            'local_impedance_boundary',
            'structural_plate_coupling',
        ),
    )
    checks = {c.check: c.status for c in report.checks}
    assert checks['capability_class_consistent'] == 'PASS'
    assert checks['provider_support'] == 'PASS'


def test_missing_structural_support_is_fail_not_downgrade():
    report = evaluate_boundary_capability(
        boundary_ref='wall-north',
        capability_class='structurally_compliant',
        structural_model=_model(),
        provider_capabilities=('rigid_boundary', 'local_impedance_boundary'),
    )
    checks = {c.check: c.status for c in report.checks}
    assert checks['provider_support'] == 'FAIL'
    reason = {c.check: c.reason for c in report.checks}['provider_support']
    assert 'no downgrade' in reason


def test_rigid_boundary_cannot_carry_structural_model():
    report = evaluate_boundary_capability(
        boundary_ref='wall-north',
        capability_class='rigid',
        structural_model=_model(),
    )
    checks = {c.check: c.status for c in report.checks}
    assert checks['capability_class_consistent'] == 'FAIL'


def test_compliant_without_model_is_unknown():
    report = evaluate_boundary_capability(
        boundary_ref='wall-north',
        capability_class='structurally_compliant',
        structural_model=None,
        provider_capabilities=('structural_plate_coupling',),
    )
    checks = {c.check: c.status for c in report.checks}
    assert checks['capability_class_consistent'] == 'UNKNOWN'


def test_external_fe_requires_external_fsi():
    model = _model(
        kind='external_FE',
        areal_mass_kg_m2=None,
        bending_stiffness_nm=None,
    )
    assert model.required_provider_capability == 'external_FSI'
    report = evaluate_boundary_capability(
        boundary_ref='wall-north',
        capability_class='structurally_compliant',
        structural_model=model,
        provider_capabilities=('structural_plate_coupling',),
    )
    checks = {c.check: c.status for c in report.checks}
    assert checks['provider_support'] == 'FAIL'
