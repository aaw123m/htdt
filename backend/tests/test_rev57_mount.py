"""REV57-MOUNT regression tests — #620 AV mounting / structural-support
evidence authority. CAD placement alone never claims mountable; the
verdict ladder fails closed through structural approval, inspection and
UNKNOWN states.
"""

from __future__ import annotations

import sqlite3

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene

from htdt.cad_mounting_support_authority import (
    CadApproverIdentity,
    CadDeclaredCapacity,
    CadMountingAssembly,
    CadMountingComponent,
    CadMountPose,
    build_manufacturer_requirement,
    build_mounting_assembly,
    build_mounting_inspection,
    build_mount_load_evidence,
    build_structural_approval,
    build_support_element,
    evaluate_mounting_support,
)
from htdt.cad_mounting_support_repository import (
    CadMountingSupportRepository,
    MountingSupportIntegrityError,
)


DOC = 'doc-rev57-mount'
T0 = '2026-10-05T00:00:00+00:00'
T1 = '2026-10-05T01:00:00+00:00'
T2 = '2026-10-05T02:00:00+00:00'
SHA_A = 'a' * 64
SHA_B = 'b' * 64
SHA_C = 'c' * 64


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene(doc_id), parent_revision_id=None)
    return scene_repository


def _ref(kind: str, ref_id: str, sha: str = SHA_A) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=ref_id, ref_sha256=sha)


def _engineer() -> CadApproverIdentity:
    return CadApproverIdentity(
        name='T. Engineer', credential='PE (structural)',
        organization='Structure LLC',
    )


def _professional_approval(assembly, **overrides):
    params = dict(
        document_id=DOC,
        assembly=assembly,
        evidence_class='structural_engineer_design',
        approval_scope='assembly',
        duty_coverage='static_and_dynamic',
        approver=_engineer(),
        jurisdiction='Tokyo',
        code_edition='IBC 2021 / 建築基準法',
        document_ref='calc-2026-014',
        declared_rating_kg=500.0,
        declared_safety_factor=5.0,
        issued_at_utc=T1,
        declared_at_utc=T1,
    )
    params.update(overrides)
    return build_structural_approval(**params)


def _passing_inspection(assembly, **overrides):
    params = dict(
        document_id=DOC,
        assembly=assembly,
        inspection_kind='installation',
        inspector_class='qualified_inspector',
        support_point_observation='verified',
        secondary_retention_observation='verified',
        fastener_observation='verified',
        pose_observation='matches_assembly',
        findings='pass',
        inspected_at_utc=T2,
        declared_at_utc=T2,
    )
    params.update(overrides)
    return build_mounting_inspection(**params)


# ---------------------------------------------------------------------------
# MNT10 — approved projector mount: full evidence chain completes
# ---------------------------------------------------------------------------


def _mnt10_chain(tmp_path):
    """Return (assembly, kwargs) for a fully-evidenced ceiling projector."""
    assembly = build_mounting_assembly(
        document_id=DOC,
        equipment_ref=_ref('installed_instance', 'proj-1', SHA_B),
        placement_ref=_ref('placement', 'pl-1', SHA_C),
        equipment_class='projector',
        support_method='ceiling',
        overhead_suspension=True,
        components=(
            CadMountingComponent(
                role='equipment_interface', manufacturer='Peerless',
                model='PRG-UNV', identity_state='verified_observed',
            ),
            CadMountingComponent(
                role='pole_or_rig', manufacturer='Peerless',
                model='EXT-101', identity_state='verified_observed',
            ),
            CadMountingComponent(
                role='support_point', manufacturer='Unistrut',
                model='P1000', identity_state='verified_observed',
            ),
            CadMountingComponent(
                role='safety_retention', manufacturer='Peerless',
                model='ACC-safety-cable',
                identity_state='verified_observed',
            ),
        ),
        secondary_retention_state='required_installed',
        duty_state='static',
        interference_state='clear',
        cable_route_refs=(_ref('cable_run', 'cable-pj-1', SHA_C),),
        declared_at_utc=T0,
    )
    load = build_mount_load_evidence(
        document_id=DOC,
        assembly=assembly,
        mass_kg=12.4,
        center_of_gravity=CadMountPose(x_m=0.0, y_m=0.0, z_m=-0.08),
        duty_state='static',
        source_class='manufacturer_published',
        declared_at_utc=T0,
    )
    element = build_support_element(
        document_id=DOC,
        assembly=assembly,
        element_class='structural_steel',
        geometry_ref=_ref('geo_element_evidence', 'beam-w1', SHA_C),
        hidden_condition_state='verified',
        declared_capacity=CadDeclaredCapacity(
            value=500.0, unit='kg',
            source_class='approved_document',
            document_ref='calc-2026-014',
        ),
        declared_at_utc=T0,
    )
    requirement = build_manufacturer_requirement(
        document_id=DOC,
        subject_ref=_ref('installed_instance', 'proj-1', SHA_B),
        approved_mount_points=('PRG-UNV ceiling interface',),
        secondary_retention='required',
        vesa_pattern=None,
        source_ref=_ref('manufacturer_document', 'pj-manual', SHA_B),
        source_version='rev-3',
        declared_at_utc=T0,
    )
    approval = _professional_approval(assembly)
    inspection = _passing_inspection(assembly)
    return assembly, dict(
        requirement=requirement,
        load_evidence=load,
        support_elements=(element,),
        approvals=(approval,),
        inspections=(inspection,),
    )


def test_mnt10_approved_projector_mount_completes(tmp_path):
    assembly, kwargs = _mnt10_chain(tmp_path)
    qualification = evaluate_mounting_support(
        document_id=DOC, assembly=assembly, **kwargs,
    )
    assert qualification.support_state == 'design_support_evidence_complete'
    assert qualification.demand_vs_rating_kg == pytest.approx(
        500.0 - 12.4
    )
    assert qualification.stale_flags == ()


# ---------------------------------------------------------------------------
# MNT20 — CAD-only ceiling speaker: structural approval required
# ---------------------------------------------------------------------------


def test_mnt20_cad_only_ceiling_speaker_fails_closed():
    assembly = build_mounting_assembly(
        document_id=DOC,
        equipment_class='loudspeaker',
        support_method='ceiling',
        overhead_suspension=True,
        duty_state='static',
        declared_at_utc=T0,
    )
    qualification = evaluate_mounting_support(
        document_id=DOC, assembly=assembly,
    )
    assert qualification.support_state == 'structural_approval_required'
    assert (
        qualification.suspension_layers.enclosure_capability == 'unknown'
    )
    assert (
        qualification.suspension_layers.building_support_point
        == 'unknown'
    )


# ---------------------------------------------------------------------------
# MNT30 — suspended loudspeaker with only E1.8 enclosure evidence
# ---------------------------------------------------------------------------


def test_mnt30_enclosure_evidence_does_not_approve_support_point():
    assembly = build_mounting_assembly(
        document_id=DOC,
        equipment_ref=_ref('installed_instance', 'spk-1', SHA_B),
        equipment_class='loudspeaker',
        support_method='suspended_rigging',
        overhead_suspension=True,
        secondary_retention_state='unknown',
        duty_state='static',
        interference_state='unknown',
        declared_at_utc=T0,
    )
    requirement = build_manufacturer_requirement(
        document_id=DOC,
        subject_ref=_ref('installed_instance', 'spk-1', SHA_B),
        enclosure_suspension='e1_8_rated',
        source_ref=_ref('manufacturer_document', 'spk-rig-manual', SHA_B),
        declared_at_utc=T0,
    )
    qualification = evaluate_mounting_support(
        document_id=DOC, assembly=assembly, requirement=requirement,
    )
    # E1.8 rates the enclosure only — the building support point is
    # still unknown, so the mount stays fail-closed.
    assert qualification.support_state == 'structural_approval_required'
    assert (
        qualification.suspension_layers.enclosure_capability
        == 'capable'
    )
    assert (
        qualification.suspension_layers.building_support_point
        == 'unknown'
    )


def test_mnt30_suspended_on_ceiling_grid_is_incompatible():
    assembly = build_mounting_assembly(
        document_id=DOC,
        equipment_ref=_ref('installed_instance', 'spk-1', SHA_B),
        equipment_class='loudspeaker',
        support_method='suspended_rigging',
        overhead_suspension=True,
        secondary_retention_state='required_installed',
        duty_state='static',
        declared_at_utc=T0,
    )
    element = build_support_element(
        document_id=DOC,
        assembly=assembly,
        element_class='suspended_ceiling_grid',
        declared_at_utc=T0,
    )
    qualification = evaluate_mounting_support(
        document_id=DOC, assembly=assembly,
        support_elements=(element,),
        approvals=(_professional_approval(assembly),),
        inspections=(_passing_inspection(assembly),),
    )
    # 'incompatible' layer never reaches a complete verdict.
    assert qualification.support_state != (
        'design_support_evidence_complete'
    )


# ---------------------------------------------------------------------------
# MNT40 — substitution staleness
# ---------------------------------------------------------------------------


def test_mnt40_substitution_invalidates_qualification():
    assembly, kwargs = _mnt10_chain(None)
    qualification = evaluate_mounting_support(
        document_id=DOC, assembly=assembly,
        substitution_flags=('equipment_mass_or_cg', 'support_point'),
        **kwargs,
    )
    assert qualification.support_state == 'stale_after_change'
    assert qualification.stale_flags == (
        'equipment_mass_or_cg', 'support_point',
    )


# ---------------------------------------------------------------------------
# MNT50 — scan cannot see blocking: support stays unknown
# ---------------------------------------------------------------------------


def test_mnt50_hidden_support_unknown_not_mountable():
    assembly = build_mounting_assembly(
        document_id=DOC,
        equipment_ref=_ref('installed_instance', 'spk-w1', SHA_B),
        equipment_class='loudspeaker',
        support_method='wall',
        duty_state='static',
        declared_at_utc=T0,
    )
    element = build_support_element(
        document_id=DOC,
        assembly=assembly,
        element_class='unknown',
        hidden_condition_state='inaccessible',
        declared_at_utc=T0,
    )
    qualification = evaluate_mounting_support(
        document_id=DOC, assembly=assembly,
        support_elements=(element,),
    )
    assert qualification.support_state == 'support_unknown'


# ---------------------------------------------------------------------------
# MNT60 — isolation mount needs both structural and mechanical evidence
# ---------------------------------------------------------------------------


def test_mnt60_isolation_mount_requires_professional_evidence():
    assembly = build_mounting_assembly(
        document_id=DOC,
        equipment_ref=_ref('installed_instance', 'sub-1', SHA_B),
        equipment_class='subwoofer',
        support_method='floor',
        isolation_mount=True,
        duty_state='static',
        declared_at_utc=T0,
    )
    element = build_support_element(
        document_id=DOC,
        assembly=assembly,
        element_class='concrete',
        hidden_condition_state='verified',
        declared_at_utc=T0,
    )
    installer = build_structural_approval(
        document_id=DOC,
        assembly=assembly,
        evidence_class='installer_declaration',
        approval_scope='assembly',
        declared_at_utc=T0,
    )
    qualification = evaluate_mounting_support(
        document_id=DOC, assembly=assembly,
        support_elements=(element,),
        approvals=(installer,),
    )
    # An installer declaration is not professional isolation design.
    assert qualification.support_state == 'structural_approval_required'


# ---------------------------------------------------------------------------
# MNT70 — motorized screen: static-only evidence never covers moving duty
# ---------------------------------------------------------------------------


def test_mnt70_motorized_screen_static_qualification_is_limited():
    assembly = build_mounting_assembly(
        document_id=DOC,
        equipment_ref=_ref('installed_instance', 'screen-1', SHA_B),
        equipment_class='screen_or_masking',
        support_method='ceiling',
        overhead_suspension=True,
        secondary_retention_state='not_required',
        duty_state='moving_motorized',
        interference_state='clear',
        declared_at_utc=T0,
    )
    element = build_support_element(
        document_id=DOC,
        assembly=assembly,
        element_class='structural_steel',
        hidden_condition_state='verified',
        declared_capacity=CadDeclaredCapacity(
            value=200.0, unit='kg',
            source_class='approved_document',
            document_ref='calc-2026-015',
        ),
        declared_at_utc=T0,
    )
    approval = _professional_approval(
        assembly, duty_coverage='static_only',
    )
    qualification = evaluate_mounting_support(
        document_id=DOC, assembly=assembly,
        support_elements=(element,), approvals=(approval,),
        inspections=(_passing_inspection(assembly),),
    )
    assert qualification.support_state == 'approved_with_limitations'
    assert any('moving' in reason for reason in qualification.reasons)


# ---------------------------------------------------------------------------
# Additional evaluator ladder cases
# ---------------------------------------------------------------------------


def test_declared_capacity_below_demand_reports_insufficient():
    assembly = build_mounting_assembly(
        document_id=DOC,
        equipment_ref=_ref('installed_instance', 'dsp-1', SHA_B),
        equipment_class='display',
        support_method='wall',
        duty_state='static',
        declared_at_utc=T0,
    )
    load = build_mount_load_evidence(
        document_id=DOC, assembly=assembly, mass_kg=120.0,
        source_class='manufacturer_published', declared_at_utc=T0,
    )
    element = build_support_element(
        document_id=DOC, assembly=assembly,
        element_class='wall_framing', hidden_condition_state='verified',
        declared_capacity=CadDeclaredCapacity(
            value=45.0, unit='kg',
            source_class='approved_document',
            document_ref='calc-x',
        ),
        declared_at_utc=T0,
    )
    qualification = evaluate_mounting_support(
        document_id=DOC, assembly=assembly, load_evidence=load,
        support_elements=(element,),
    )
    assert qualification.support_state == 'support_capacity_insufficient'
    assert qualification.demand_vs_rating_kg == pytest.approx(
        45.0 - 120.0
    )


def test_inspection_mismatch_is_as_built_mismatch():
    assembly = build_mounting_assembly(
        document_id=DOC,
        equipment_ref=_ref('installed_instance', 'dsp-2', SHA_B),
        equipment_class='display', support_method='wall',
        duty_state='static', interference_state='clear',
        declared_at_utc=T0,
    )
    inspection = _passing_inspection(
        assembly, pose_observation='differs',
        findings='failed',
    )
    qualification = evaluate_mounting_support(
        document_id=DOC, assembly=assembly,
        inspections=(inspection,),
        approvals=(_professional_approval(assembly),),
    )
    assert qualification.support_state == 'as_built_mismatch'


def test_interference_conflict_blocks_approval():
    assembly, kwargs = _mnt10_chain(None)
    assembly = build_mounting_assembly(
        document_id=DOC,
        equipment_ref=_ref('installed_instance', 'proj-1', SHA_B),
        equipment_class='projector', support_method='ceiling',
        overhead_suspension=True, duty_state='static',
        interference_state='conflict_observed',
        declared_at_utc=T0,
    )
    qualification = evaluate_mounting_support(
        document_id=DOC, assembly=assembly,
    )
    assert qualification.support_state != (
        'design_support_evidence_complete'
    )


def test_installation_inspection_required_when_none_recorded():
    assembly = build_mounting_assembly(
        document_id=DOC,
        equipment_ref=_ref('installed_instance', 'proj-2', SHA_B),
        equipment_class='projector', support_method='ceiling',
        overhead_suspension=True, duty_state='static',
        secondary_retention_state='not_required',
        interference_state='clear',
        declared_at_utc=T0,
    )
    element = build_support_element(
        document_id=DOC, assembly=assembly,
        element_class='structural_steel',
        hidden_condition_state='verified',
        declared_at_utc=T0,
    )
    qualification = evaluate_mounting_support(
        document_id=DOC, assembly=assembly,
        support_elements=(element,),
        approvals=(_professional_approval(assembly),),
    )
    assert qualification.support_state == (
        'installation_inspection_required'
    )


def test_documentary_only_approval_stays_limited():
    assembly = build_mounting_assembly(
        document_id=DOC,
        equipment_ref=_ref('installed_instance', 'dsp-3', SHA_B),
        equipment_class='display', support_method='wall',
        duty_state='static', interference_state='clear',
        declared_at_utc=T0,
    )
    element = build_support_element(
        document_id=DOC, assembly=assembly,
        element_class='wall_framing', hidden_condition_state='verified',
        declared_at_utc=T0,
    )
    approval = build_structural_approval(
        document_id=DOC, assembly=assembly,
        evidence_class='manufacturer_installation_requirement',
        approval_scope='assembly', duty_coverage='static_only',
        document_ref='install-guide-9',
        declared_at_utc=T0,
    )
    inspection = _passing_inspection(assembly)
    qualification = evaluate_mounting_support(
        document_id=DOC, assembly=assembly,
        support_elements=(element,), approvals=(approval,),
        inspections=(inspection,),
    )
    assert qualification.support_state == 'approved_with_limitations'


# ---------------------------------------------------------------------------
# Validator / sealing discipline
# ---------------------------------------------------------------------------


def test_assembly_rejects_wrong_ref_kind():
    with pytest.raises(Exception):
        build_mounting_assembly(
            document_id=DOC,
            equipment_ref=_ref('cable_run', 'cable-9'),
            equipment_class='display', support_method='wall',
            declared_at_utc=T0,
        )


def test_load_evidence_requires_some_demand():
    assembly = build_mounting_assembly(
        document_id=DOC, equipment_class='display',
        support_method='wall', declared_at_utc=T0,
    )
    with pytest.raises(Exception):
        build_mount_load_evidence(
            document_id=DOC, assembly=assembly, declared_at_utc=T0,
        )


def test_ceiling_grid_cannot_be_verified_support():
    assembly = build_mounting_assembly(
        document_id=DOC, equipment_class='loudspeaker',
        support_method='ceiling', declared_at_utc=T0,
    )
    with pytest.raises(Exception):
        build_support_element(
            document_id=DOC, assembly=assembly,
            element_class='suspended_ceiling_grid',
            hidden_condition_state='verified', declared_at_utc=T0,
        )


def test_professional_approval_requires_approver_identity():
    assembly = build_mounting_assembly(
        document_id=DOC, equipment_class='display',
        support_method='wall', declared_at_utc=T0,
    )
    with pytest.raises(Exception):
        build_structural_approval(
            document_id=DOC, assembly=assembly,
            evidence_class='structural_engineer_design',
            approval_scope='assembly', declared_at_utc=T0,
        )


def test_user_assumed_approval_cannot_carry_ratings():
    with pytest.raises(Exception):
        build_structural_approval(
            document_id=DOC,
            evidence_class='user_assumed',
            declared_rating_kg=100.0,
            declared_at_utc=T0,
        )


def test_e1_8_rating_requires_pinned_source():
    with pytest.raises(Exception):
        build_manufacturer_requirement(
            document_id=DOC,
            subject_ref=_ref('installed_instance', 'spk-1', SHA_B),
            enclosure_suspension='e1_8_rated',
            declared_at_utc=T0,
        )


def test_inspection_pass_rejects_mismatch_observation():
    assembly = build_mounting_assembly(
        document_id=DOC, equipment_class='display',
        support_method='wall', declared_at_utc=T0,
    )
    with pytest.raises(Exception):
        build_mounting_inspection(
            document_id=DOC, assembly=assembly,
            inspection_kind='installation',
            inspector_class='qualified_inspector',
            support_point_observation='mismatch',
            findings='pass', inspected_at_utc=T1,
            declared_at_utc=T1,
        )


def test_records_are_sealed_and_tamper_evident():
    assembly = build_mounting_assembly(
        document_id=DOC, equipment_class='display',
        support_method='wall', declared_at_utc=T0,
    )
    payload = assembly.model_dump()
    payload['support_method'] = 'ceiling'
    with pytest.raises(Exception):
        CadMountingAssembly.model_validate(payload)


# ---------------------------------------------------------------------------
# Repository round-trip + tamper detection
# ---------------------------------------------------------------------------


def test_repository_round_trip(tmp_path):
    scene_repository = _scene_repo(tmp_path)
    repository = CadMountingSupportRepository(scene_repository)
    assembly, kwargs = _mnt10_chain(tmp_path)

    repository.save_assembly(assembly)
    repository.save_load_evidence(kwargs['load_evidence'])
    repository.save_support_element(kwargs['support_elements'][0])
    repository.save_manufacturer_requirement(kwargs['requirement'])
    repository.save_approval(kwargs['approvals'][0])
    repository.save_inspection(kwargs['inspections'][0])

    qualification = evaluate_mounting_support(
        document_id=DOC, assembly=assembly, **kwargs,
    )
    repository.save_qualification(qualification)

    assert repository.get_assembly(assembly.assembly_id) == assembly
    assert repository.get_load_evidence(
        kwargs['load_evidence'].evidence_id
    ) == kwargs['load_evidence']
    assert repository.get_support_element(
        kwargs['support_elements'][0].element_id
    ) == kwargs['support_elements'][0]
    assert repository.get_manufacturer_requirement(
        kwargs['requirement'].requirement_id
    ) == kwargs['requirement']
    assert repository.get_approval(
        kwargs['approvals'][0].approval_id
    ) == kwargs['approvals'][0]
    assert repository.get_inspection(
        kwargs['inspections'][0].inspection_id
    ) == kwargs['inspections'][0]
    assert repository.get_qualification(
        qualification.qualification_id
    ) == qualification

    # Idempotent append-only saves.
    repository.save_assembly(assembly)
    assert len(repository.list_assemblies(DOC)) == 1
    assert len(repository.list_qualifications(DOC)) == 1


def test_repository_detects_payload_tamper(tmp_path):
    scene_repository = _scene_repo(tmp_path)
    repository = CadMountingSupportRepository(scene_repository)
    assembly, _ = _mnt10_chain(tmp_path)
    repository.save_assembly(assembly)

    with sqlite3.connect(scene_repository.path) as connection:
        connection.execute(
            "UPDATE cad_mount_assemblies SET support_method='floor' "
            'WHERE assembly_id=?',
            (assembly.assembly_id,),
        )
        connection.commit()

    with pytest.raises(MountingSupportIntegrityError):
        repository.get_assembly(assembly.assembly_id)
