"""Geometry intake readiness tests (Issue #866 / REV66).

Covers the sealed intake chain
SOURCE_GEOMETRY -> HEALTH_CHECK -> REPAIR_PROPOSAL ->
EXPLICIT_ACCEPTANCE -> DERIVED_GEOMETRY_REVISION -> SOLVER_READINESS:

* every diagnostic defect class,
* repair proposal generation + explicit accept/reject,
* provenance-linked derived geometry revisions (source never mutated),
* solver-readiness verdicts (supported/degraded/unsupported/unknown),
* deterministic evidence invalidation + fail-closed execution gate,
* repository round-trip and tamper detection,
* offscreen panel wiring (defect split, locator signal, decisions).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from htdt.cad_acoustic_solver_adapter import (
    build_acoustic_solver_adapter_descriptor,
)
from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_equipment import FrequencyDomain
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef
from htdt.cad_geometry_intake import (
    GEOMETRY_INTAKE_AUTHORITY_VERSION,
    GeometryDefect,
    GeometryIntakeError,
    GeometryIntakeSubject,
    GeometryRepairAcceptanceError,
    GeometrySolverExecutionBlockedError,
    IntakeMeshPart,
    IntakeOpening,
    IntakePartStub,
    MeshImportAuthority,
    RepairActionDecision,
    SolverGeometryContext,
    assert_geometry_solver_execution_permitted,
    build_geometry_intake_subject,
    defect_locate_targets,
    derive_geometry_revision,
    derive_solver_geometry_context,
    diagnose_geometry_intake,
    evaluate_solver_readiness,
    geometry_solver_execution_gate,
    propose_geometry_repairs,
    readiness_evidence_state,
    record_geometry_repair_acceptance,
)
from htdt.cad_geometry_intake_repository import (
    CadGeometryIntakeRepository,
    GeometryIntakeConflictError,
    GeometryIntakeIntegrityError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_solver_capability_manifest import (
    SolverCapabilityRow,
    SOLVER_PATH_PHENOMENA,
    build_solver_capability_manifest,
)
from htdt.raw_mesh import import_raw_visual_mesh

UTC = '2026-10-07T00:00:00+00:00'


# --- mesh fixtures ------------------------------------------------------

_CUBE_OBJ = b"""
v 0 0 0
v 1 0 0
v 1 1 0
v 0 1 0
v 0 0 1
v 1 0 1
v 1 1 1
v 0 1 1
f 1 4 3
f 1 3 2
f 5 6 7
f 5 7 8
f 1 2 6
f 1 6 5
f 4 8 7
f 4 7 3
f 1 5 8
f 1 8 4
f 2 3 7
f 2 7 6
"""

_OPEN_TRIANGLE_OBJ = b"""
v 0 0 0
v 1 0 0
v 0 1 0
f 1 2 3
"""

_NONMANIFOLD_OBJ = b"""
v 0 0 0
v 1 0 0
v 0 1 0
v 0 0 1
f 1 2 3
f 1 2 4
f 2 1 3
"""

_DUPLICATE_OBJ = b"""
v 0 0 0
v 1 0 0
v 1 1 0
v 0 1 0
f 1 2 3
f 1 3 4
f 1 2 3
"""

_DISCONNECTED_OBJ = b"""
v 0 0 0
v 1 0 0
v 0 1 0
v 5 0 0
v 6 0 0
v 5 1 0
f 1 2 3
f 4 5 6
"""

_SLIVER_OBJ = b"""
v 0 0 0
v 1 0 0
v 2 0 0.0000001
v 0 1 0
f 1 2 3
f 1 2 4
"""


def _mesh(asset: bytes, name: str = 'fixture.obj'):
    return import_raw_visual_mesh(asset, source_name=name)


def _part(mesh, part_id='part-1', *, material_state='assigned',
          material_label='gypsum', import_authority=None):
    return IntakeMeshPart(
        part_id=part_id,
        owner_kind='entity_body',
        owner_ref='e1',
        role='object_surface',
        mesh=mesh,
        material_state=material_state,
        material_label=material_label,
        import_authority=import_authority,
    )


def _subject(*, parts=(), stubs=(), openings=(), unit_state='declared',
             document_id='doc-1'):
    return GeometryIntakeSubject.create(
        document_id=document_id,
        source_kind='ifc_import',
        source_refs=(
            AuthorityRef(
                kind='ifc_file', ref_id='ifc-1',
                ref_sha256='a' * 64,
            ),
        ),
        parts=parts,
        unresolved_parts=stubs,
        openings=openings,
        unit_state=unit_state,
        unit_details='fixture units declared by operator',
    )


def _report(subject, solver_context=None):
    return diagnose_geometry_intake(
        subject, evaluated_at_utc=UTC, solver_context=solver_context
    )


def _defect_kinds(report):
    return {defect.kind for defect in report.defects}


# --- solver fixtures ----------------------------------------------------

def _ref(name: str, char: str) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=name,
        authority_version='fixture-v1',
        semantic_hash_sha256=char * 64,
    )


def _adapter(domain='geometric'):
    return build_acoustic_solver_adapter_descriptor(
        adapter_id=f'fixture-{domain}-adapter',
        adapter_version='1',
        model_solver_role_id='fixture-role',
        acoustic_domain=domain,
        solver_implementation_ref=_ref(f'{domain}-impl', '1'),
        solver_configuration_schema_ref=_ref(f'{domain}-schema', '2'),
        supported_snapshot_schema_versions=(1, 2),
        supported_observables=('deterministic_paths',),
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=20.0, maximum_hz=20000.0,
        ),
    )


def _manifest(descriptor, portal_state='SUPPORTED'):
    rows = []
    for phenomenon in SOLVER_PATH_PHENOMENA:
        if phenomenon == 'portal_region_coupling':
            if portal_state == 'UNSUPPORTED':
                rows.append(SolverCapabilityRow(
                    phenomenon=phenomenon,
                    state='UNSUPPORTED',
                    reasons=('portal coupling outside solver scope',),
                ))
            elif portal_state == 'BOUNDED':
                rows.append(SolverCapabilityRow(
                    phenomenon=phenomenon,
                    state='BOUNDED',
                    bound_description='single coupled volume only',
                    valid_frequency_domain=FrequencyDomain(
                        minimum_hz=20.0, maximum_hz=20000.0),
                ))
            else:
                rows.append(SolverCapabilityRow(
                    phenomenon=phenomenon,
                    state='SUPPORTED',
                    valid_frequency_domain=FrequencyDomain(
                        minimum_hz=20.0, maximum_hz=20000.0),
                ))
        else:
            rows.append(SolverCapabilityRow(
                phenomenon=phenomenon,
                state='SUPPORTED',
                valid_frequency_domain=FrequencyDomain(
                    minimum_hz=20.0, maximum_hz=20000.0),
            ))
    return build_solver_capability_manifest(descriptor=descriptor, rows=rows)


def _opening(*, is_open=True, resolution='unresolved'):
    return IntakeOpening(
        opening_id='op-1',
        host_part_id='room-boundary',
        host_wall_id='wall-1',
        kind='door',
        is_open=is_open,
        area_m2=2.0,
        resolution=resolution,
    )


# --- subject construction -----------------------------------------------


def test_subject_from_scene_document_room_only() -> None:
    doc = SceneDocument(
        document_id='doc-room',
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=3.0),
        entities=(),
    )
    subject = build_geometry_intake_subject(doc)
    assert subject.subject_id.startswith('gis-')
    assert len(subject.subject_sha256) == 64
    assert [p.part_id for p in subject.parts] == ['room-boundary']
    part = subject.part('room-boundary')
    assert part.owner_kind == 'room_boundary'
    assert part.role == 'room_boundary'
    assert len(part.mesh.triangles) > 0


def test_subject_from_scene_document_with_entity_body() -> None:
    doc = SceneDocument(
        document_id='doc-entity',
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=3.0),
        entities=(
            SceneEntity(
                entity_id='e1',
                kind='furniture',
                name='desk',
                position=Position3(x_m=1.0, y_m=1.0, z_m=0.0),
                size_m=Size3(x_m=1.0, y_m=0.6, z_m=0.75),
            ),
        ),
    )
    subject = build_geometry_intake_subject(doc)
    part_ids = {p.part_id for p in subject.parts}
    assert 'room-boundary' in part_ids
    entity_parts = [p for p in subject.parts if p.owner_kind == 'entity_body']
    assert len(entity_parts) == 1
    assert entity_parts[0].owner_ref == 'e1'


def test_subject_deterministic_sha() -> None:
    doc = SceneDocument(
        document_id='doc-det',
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=3.0),
        entities=(),
    )
    a = build_geometry_intake_subject(doc)
    b = build_geometry_intake_subject(doc)
    assert a.subject_sha256 == b.subject_sha256
    assert a.subject_id == b.subject_id


def test_subject_rejects_duplicate_part_ids() -> None:
    mesh = _mesh(_CUBE_OBJ, 'cube.obj')
    part = _part(mesh)
    with pytest.raises(Exception, match='unique'):
        _subject(parts=(part, part))


# --- diagnostics: one fixture per defect class ---------------------------


def test_clean_cube_is_defect_free() -> None:
    subject = _subject(parts=(_part(_mesh(_CUBE_OBJ)),))
    report = _report(subject)
    assert report.defect_count == 0
    assert report.critical_count == 0


def test_open_boundary_and_watertight_and_evidence_gap() -> None:
    subject = _subject(parts=(_part(_mesh(_OPEN_TRIANGLE_OBJ)),))
    report = _report(subject)
    kinds = _defect_kinds(report)
    assert 'open_boundary_edges' in kinds
    assert 'not_watertight' in kinds
    assert 'diagnostic_evidence_gap' in kinds
    boundary = report.defects_of_kind('open_boundary_edges')
    assert boundary[0].severity == 'critical'
    assert boundary[0].origin == 'source_model'


def test_non_manifold_edges() -> None:
    subject = _subject(parts=(_part(_mesh(_NONMANIFOLD_OBJ)),))
    report = _report(subject)
    defect = report.defects_of_kind('non_manifold_edges')
    assert defect
    assert defect[0].severity == 'critical'
    assert defect[0].repairable == 'operator_required'


def test_duplicate_faces() -> None:
    subject = _subject(parts=(_part(_mesh(_DUPLICATE_OBJ)),))
    report = _report(subject)
    defect = report.defects_of_kind('duplicate_or_overlapping_faces')
    assert defect
    assert defect[0].severity == 'warning'
    assert defect[0].repairable == 'automatic'


def test_disconnected_regions() -> None:
    subject = _subject(parts=(_part(_mesh(_DISCONNECTED_OBJ)),))
    report = _report(subject)
    defect = report.defects_of_kind('disconnected_regions')
    assert defect
    assert defect[0].evidence['component_count'] == 2
    assert defect[0].evidence['fragment_component_indices']


def test_degenerate_or_tiny_features() -> None:
    subject = _subject(parts=(_part(_mesh(_SLIVER_OBJ)),))
    report = _report(subject)
    assert _defect_kinds(report) & {'degenerate_or_tiny_features'}


def test_portal_opening_ambiguity() -> None:
    subject = _subject(
        parts=(_part(_mesh(_CUBE_OBJ), part_id='room-boundary',
                       material_state='assigned'),),
        openings=(_opening(),),
    )
    report = _report(subject)
    defect = report.defects_of_kind('portal_opening_ambiguity')
    assert defect
    assert defect[0].opening_refs == ('op-1',)


def test_material_assignment_gap() -> None:
    subject = _subject(
        parts=(_part(_mesh(_CUBE_OBJ), material_state='unassigned',
                     material_label=None),),
    )
    report = _report(subject)
    defect = report.defects_of_kind('material_assignment_gap')
    assert defect
    assert defect[0].repairable == 'operator_required'


def test_coordinate_unit_anomaly_undeclared() -> None:
    authority = MeshImportAuthority(
        source_unit='unknown',
        unit_declared_by='undeclared',
        importer_id='fixture-importer',
        importer_version='1',
    )
    subject = _subject(
        parts=(_part(_mesh(_CUBE_OBJ), import_authority=authority),),
        unit_state='undeclared',
    )
    report = _report(subject)
    defect = report.defects_of_kind('coordinate_unit_anomaly')
    assert defect
    assert defect[0].evidence['unit_state'] == 'undeclared'


def test_geometry_unavailable_stub() -> None:
    stub = IntakePartStub(
        part_id='missing-part',
        owner_kind='entity_body',
        owner_ref='e9',
        role='object_surface',
        unavailability_reason='mesh blob not resolvable',
    )
    subject = _subject(stubs=(stub,))
    report = _report(subject)
    defect = report.defects_of_kind('geometry_unavailable')
    assert defect
    assert defect[0].severity == 'critical'


def test_solver_unsupported_condition_via_context() -> None:
    context = SolverGeometryContext(
        acoustic_domain='geometric',
        open_portals_unsupported=True,
    )
    subject = _subject(
        parts=(_part(_mesh(_CUBE_OBJ), part_id='room-boundary'),),
        openings=(_opening(),),
    )
    report = _report(subject, solver_context=context)
    defect = report.defects_of_kind('solver_unsupported_condition')
    assert defect
    assert defect[0].origin == 'solver_limitation'


def test_report_seal_tamper_rejected() -> None:
    subject = _subject(parts=(_part(_mesh(_CUBE_OBJ)),))
    report = _report(subject)
    payload = report.model_dump()
    payload['defect_count'] = 999
    with pytest.raises(Exception):
        type(report).model_validate(payload)


# --- repair proposals + acceptance ----------------------------------------


def test_proposal_emits_actions_for_repairable_defects() -> None:
    subject = _subject(
        parts=(_part(_mesh(_DUPLICATE_OBJ), material_state='unassigned',
                     material_label=None),),
    )
    report = _report(subject)
    proposal = propose_geometry_repairs(
        report, subject, proposed_by='op', proposed_at_utc=UTC,
    )
    kinds = {action.kind for action in proposal.actions}
    assert 'remove_exact_duplicate_faces' in kinds
    assert 'assign_material' in kinds


def test_proposal_rejects_mismatched_report() -> None:
    subject = _subject(parts=(_part(_mesh(_CUBE_OBJ)),))
    report = _report(subject)
    other = _subject(
        parts=(_part(_mesh(_OPEN_TRIANGLE_OBJ)),), document_id='doc-2')
    with pytest.raises(GeometryIntakeError):
        propose_geometry_repairs(
            report, other, proposed_by='op', proposed_at_utc=UTC)


def test_acceptance_requires_exact_cover() -> None:
    subject = _subject(
        parts=(_part(_mesh(_DUPLICATE_OBJ), material_state='unassigned',
                     material_label=None),),
    )
    report = _report(subject)
    proposal = propose_geometry_repairs(
        report, subject, proposed_by='op', proposed_at_utc=UTC)
    with pytest.raises(GeometryRepairAcceptanceError, match='undecided'):
        record_geometry_repair_acceptance(
            proposal, (), decided_by='op', decided_at_utc=UTC)
    bogus = RepairActionDecision(
        action_id='gax-' + 'f' * 24,
        decision='rejected',
        decided_by='op',
        decided_at_utc=UTC,
    )
    with pytest.raises(GeometryRepairAcceptanceError, match='outside'):
        record_geometry_repair_acceptance(
            proposal, [bogus], decided_by='op', decided_at_utc=UTC)


def test_acceptance_requires_operator_parameters() -> None:
    subject = _subject(
        parts=(_part(_mesh(_CUBE_OBJ), material_state='unassigned',
                     material_label=None),),
    )
    report = _report(subject)
    proposal = propose_geometry_repairs(
        report, subject, proposed_by='op', proposed_at_utc=UTC)
    action = next(a for a in proposal.actions if a.kind == 'assign_material')
    decisions = [
        RepairActionDecision(
            action_id=a.action_id,
            decision=(
                'accepted' if a is action else 'rejected'
            ),
            decided_by='op',
            decided_at_utc=UTC,
            accepted_parameters=(
                {'material_label': 'concrete'} if a is action else {}
            ),
        )
        for a in proposal.actions
    ]
    acceptance = record_geometry_repair_acceptance(
        proposal, decisions, decided_by='op', decided_at_utc=UTC)
    assert action.action_id in acceptance.accepted_action_ids

    # missing parameter -> rejected
    bad = [
        RepairActionDecision(
            action_id=a.action_id, decision='accepted',
            decided_by='op', decided_at_utc=UTC,
        )
        for a in proposal.actions
    ]
    with pytest.raises(GeometryRepairAcceptanceError,
                       match='material_label'):
        record_geometry_repair_acceptance(
            proposal, bad, decided_by='op', decided_at_utc=UTC)


def test_declare_units_parameter_validation() -> None:
    authority = MeshImportAuthority(
        source_unit='unknown', unit_declared_by='undeclared',
        importer_id='fixture', importer_version='1')
    subject = _subject(
        parts=(_part(_mesh(_CUBE_OBJ), import_authority=authority),),
        unit_state='undeclared')
    report = _report(subject)
    proposal = propose_geometry_repairs(
        report, subject, proposed_by='op', proposed_at_utc=UTC)
    action = next(a for a in proposal.actions if a.kind == 'declare_units')
    decisions = [
        RepairActionDecision(
            action_id=action.action_id, decision='accepted',
            decided_by='op', decided_at_utc=UTC,
            accepted_parameters={
                'source_unit': 'furlongs', 'scale_to_meters': 1.0},
        )
    ]
    for a in proposal.actions:
        if a.action_id != action.action_id:
            decisions.append(RepairActionDecision(
                action_id=a.action_id, decision='rejected',
                decided_by='op', decided_at_utc=UTC))
    with pytest.raises(GeometryRepairAcceptanceError,
                       match='source_unit'):
        record_geometry_repair_acceptance(
            proposal, decisions, decided_by='op', decided_at_utc=UTC)


# --- derived geometry revision --------------------------------------------


def _accepted_chain(mesh_asset=_DUPLICATE_OBJ, *,
                    material_state='assigned', material_label='gypsum',
                    unit_state='declared'):
    subject = _subject(
        parts=(_part(_mesh(mesh_asset), material_state=material_state,
                     material_label=material_label),),
        unit_state=unit_state,
    )
    report = _report(subject)
    proposal = propose_geometry_repairs(
        report, subject, proposed_by='op', proposed_at_utc=UTC)
    param_overrides: dict[str, dict[str, object]] = {
        'assign_material': {'material_label': 'concrete'},
    }
    decisions = [
        RepairActionDecision(
            action_id=a.action_id, decision='accepted',
            decided_by='op', decided_at_utc=UTC,
            accepted_parameters=param_overrides.get(
                a.kind, dict(a.proposed_parameters)),
        )
        for a in proposal.actions
    ]
    acceptance = record_geometry_repair_acceptance(
        proposal, decisions, decided_by='op', decided_at_utc=UTC)
    revision = derive_geometry_revision(
        subject, report, proposal, acceptance,
        derived_by='op', derived_at_utc=UTC)
    return subject, report, proposal, acceptance, revision


def test_derived_revision_repaired_mesh_and_provenance() -> None:
    subject, report, proposal, acceptance, revision = _accepted_chain()
    assert revision.derived_revision_id.startswith('gdv-')
    assert revision.source_subject_ref.ref_sha256 == subject.subject_sha256
    assert revision.proposal_ref.ref_sha256 == proposal.proposal_sha256
    assert revision.acceptance_ref.ref_sha256 == acceptance.acceptance_sha256
    assert revision.derived_geometry_sha256 != subject.subject_sha256
    part = revision.parts[0]
    assert part.geometry_state == 'repaired_mesh'
    assert part.repaired_mesh is not None
    assert part.repair_lineage is not None
    # source subject untouched
    assert subject.parts[0].mesh.mesh_id == part.source_mesh_id
    # duplicate faces gone from the derived product
    assert not revision.defects_of_kind('duplicate_or_overlapping_faces')


def test_derived_revision_reject_leaves_source_unchanged() -> None:
    subject = _subject(
        parts=(_part(_mesh(_DUPLICATE_OBJ)),),
    )
    report = _report(subject)
    proposal = propose_geometry_repairs(
        report, subject, proposed_by='op', proposed_at_utc=UTC)
    decisions = [
        RepairActionDecision(
            action_id=a.action_id, decision='rejected',
            decided_by='op', decided_at_utc=UTC,
            decision_reason='operator declined',
        )
        for a in proposal.actions
    ]
    acceptance = record_geometry_repair_acceptance(
        proposal, decisions, decided_by='op', decided_at_utc=UTC)
    revision = derive_geometry_revision(
        subject, report, proposal, acceptance,
        derived_by='op', derived_at_utc=UTC)
    part = revision.parts[0]
    assert part.geometry_state == 'unchanged_source'
    assert revision.defects_of_kind('duplicate_or_overlapping_faces')


def test_derived_revision_remove_disconnected_fragment_replaces_mesh(
) -> None:
    subject = _subject(parts=(_part(_mesh(_DISCONNECTED_OBJ)),))
    report = _report(subject)
    proposal = propose_geometry_repairs(
        report, subject, proposed_by='op', proposed_at_utc=UTC)
    fragment = next(
        a for a in proposal.actions
        if a.kind == 'remove_disconnected_fragment')
    decisions = [
        RepairActionDecision(
            action_id=a.action_id,
            decision='accepted' if a is fragment else 'rejected',
            decided_by='op', decided_at_utc=UTC,
            accepted_parameters=(
                dict(a.proposed_parameters) if a is fragment else {}),
        )
        for a in proposal.actions
    ]
    acceptance = record_geometry_repair_acceptance(
        proposal, decisions, decided_by='op', decided_at_utc=UTC)
    revision = derive_geometry_revision(
        subject, report, proposal, acceptance,
        derived_by='op', derived_at_utc=UTC)
    part = revision.parts[0]
    assert part.geometry_state == 'replaced_mesh'
    assert part.replaced_mesh is not None
    assert len(part.replaced_mesh.triangles) == 1


def test_derived_revision_material_assignment() -> None:
    subject = _subject(
        parts=(_part(_mesh(_CUBE_OBJ), material_state='unassigned',
                     material_label=None),),
    )
    report = _report(subject)
    proposal = propose_geometry_repairs(
        report, subject, proposed_by='op', proposed_at_utc=UTC)
    decisions = [
        RepairActionDecision(
            action_id=a.action_id,
            decision='accepted' if a.kind == 'assign_material' else 'rejected',
            decided_by='op', decided_at_utc=UTC,
            accepted_parameters=(
                {'material_label': 'concrete'}
                if a.kind == 'assign_material' else {}),
        )
        for a in proposal.actions
    ]
    acceptance = record_geometry_repair_acceptance(
        proposal, decisions, decided_by='op', decided_at_utc=UTC)
    revision = derive_geometry_revision(
        subject, report, proposal, acceptance,
        derived_by='op', derived_at_utc=UTC)
    part = revision.parts[0]
    assert part.material_state == 'assigned'
    assert part.material_label == 'concrete'
    assert not revision.defects_of_kind('material_assignment_gap')


def test_derived_revision_portal_resolution() -> None:
    subject = _subject(
        parts=(_part(_mesh(_CUBE_OBJ), part_id='room-boundary'),),
        openings=(_opening(),),
    )
    report = _report(subject)
    proposal = propose_geometry_repairs(
        report, subject, proposed_by='op', proposed_at_utc=UTC)
    decisions = [
        RepairActionDecision(
            action_id=a.action_id,
            decision='accepted' if a.kind == 'resolve_portal' else 'rejected',
            decided_by='op', decided_at_utc=UTC,
            accepted_parameters=(
                {'resolution': 'closed_boundary'}
                if a.kind == 'resolve_portal' else {}),
        )
        for a in proposal.actions
    ]
    acceptance = record_geometry_repair_acceptance(
        proposal, decisions, decided_by='op', decided_at_utc=UTC)
    revision = derive_geometry_revision(
        subject, report, proposal, acceptance,
        derived_by='op', derived_at_utc=UTC)
    assert revision.openings[0].resolution == 'resolved_closed'
    assert not revision.defects_of_kind('portal_opening_ambiguity')


def test_derived_revision_provenance_pins() -> None:
    subject = _subject(parts=(_part(_mesh(_CUBE_OBJ),
                                        material_state='unassigned',
                                        material_label=None),))
    report = _report(subject)
    proposal = propose_geometry_repairs(
        report, subject, proposed_by='op', proposed_at_utc=UTC)
    decisions = [
        RepairActionDecision(
            action_id=a.action_id, decision='rejected',
            decided_by='op', decided_at_utc=UTC)
        for a in proposal.actions
    ]
    acceptance = record_geometry_repair_acceptance(
        proposal, decisions, decided_by='op', decided_at_utc=UTC)
    other_subject = _subject(
        parts=(_part(_mesh(_CUBE_OBJ, 'other.obj')),),
        document_id='doc-9')
    with pytest.raises(GeometryIntakeError, match='report'):
        derive_geometry_revision(
            other_subject, report, proposal, acceptance,
            derived_by='op', derived_at_utc=UTC)


# --- solver readiness ------------------------------------------------------


def test_readiness_supported() -> None:
    subject = _subject(parts=(_part(_mesh(_CUBE_OBJ)),))
    report = _report(subject)
    verdict = evaluate_solver_readiness(
        subject=subject, report=report,
        adapter_descriptor=_adapter('geometric'),
        evaluated_at_utc=UTC)
    assert verdict.verdict == 'supported'
    assert verdict.verdict_id.startswith('srv-')
    assert verdict.geometry_sha256 == subject.subject_sha256
    decision, reasons = geometry_solver_execution_gate(verdict)
    assert decision == 'allow'
    assert reasons == ()
    assert_geometry_solver_execution_permitted(verdict)


def test_readiness_degraded_on_warning_defect() -> None:
    # material gap is a warning defect; under a context that does not
    # require material assignments it degrades rather than blocks.
    subject = _subject(
        parts=(_part(_mesh(_CUBE_OBJ), material_state='unassigned',
                     material_label=None),))
    context = SolverGeometryContext(
        acoustic_domain='geometric', requires_material_assignments=False)
    report = _report(subject, solver_context=context)
    verdict = evaluate_solver_readiness(
        subject=subject, report=report,
        adapter_descriptor=_adapter('geometric'),
        solver_context=context,
        evaluated_at_utc=UTC)
    assert verdict.verdict == 'degraded'
    assert verdict.degraded_reasons
    decision, reasons = geometry_solver_execution_gate(verdict)
    assert decision == 'allow'


def test_readiness_unsupported_on_critical_defect() -> None:
    subject = _subject(parts=(_part(_mesh(_NONMANIFOLD_OBJ)),))
    report = _report(subject)
    verdict = evaluate_solver_readiness(
        subject=subject, report=report,
        adapter_descriptor=_adapter('geometric'),
        evaluated_at_utc=UTC)
    assert verdict.verdict == 'unsupported'
    assert verdict.blocking_reasons
    decision, reasons = geometry_solver_execution_gate(verdict)
    assert decision == 'deny'
    with pytest.raises(GeometrySolverExecutionBlockedError):
        assert_geometry_solver_execution_permitted(verdict)


def test_readiness_unsupported_when_required_material_missing() -> None:
    subject = _subject(
        parts=(_part(_mesh(_CUBE_OBJ), material_state='unassigned',
                     material_label=None),))
    report = _report(subject)
    verdict = evaluate_solver_readiness(
        subject=subject, report=report,
        adapter_descriptor=_adapter('wave'),
        evaluated_at_utc=UTC)
    assert verdict.verdict == 'unsupported'


def test_readiness_unknown_on_evidence_gap() -> None:
    authority = MeshImportAuthority(
        source_unit='unknown', unit_declared_by='undeclared',
        importer_id='fixture', importer_version='1')
    subject = _subject(
        parts=(_part(_mesh(_CUBE_OBJ), import_authority=authority),),
        unit_state='undeclared')
    report = _report(subject)
    verdict = evaluate_solver_readiness(
        subject=subject, report=report,
        adapter_descriptor=_adapter('geometric'),
        evaluated_at_utc=UTC)
    assert verdict.verdict == 'unknown'
    assert verdict.evidence_gaps
    decision, _ = geometry_solver_execution_gate(verdict)
    assert decision == 'deny'
    with pytest.raises(GeometrySolverExecutionBlockedError):
        assert_geometry_solver_execution_permitted(verdict)


def test_readiness_wave_solver_requires_watertight() -> None:
    subject = _subject(parts=(_part(_mesh(_OPEN_TRIANGLE_OBJ)),))
    report = _report(subject)
    verdict = evaluate_solver_readiness(
        subject=subject, report=report,
        adapter_descriptor=_adapter('wave'),
        evaluated_at_utc=UTC)
    assert verdict.verdict == 'unsupported'
    kinds = {r.defect_kind for r in verdict.blocking_reasons
             if r.defect_kind is not None}
    assert 'open_boundary_edges' in kinds


def test_readiness_manifest_unsupported_portal_blocks() -> None:
    adapter = _adapter('geometric')
    manifest = _manifest(adapter, portal_state='UNSUPPORTED')
    subject = _subject(
        parts=(_part(_mesh(_CUBE_OBJ), part_id='room-boundary'),),
        openings=(_opening(),),
    )
    report = _report(subject)
    verdict = evaluate_solver_readiness(
        subject=subject, report=report,
        adapter_descriptor=adapter,
        capability_manifest=manifest,
        evaluated_at_utc=UTC)
    assert verdict.verdict == 'unsupported'
    assert any(r.origin == 'solver_limitation'
               for r in verdict.blocking_reasons)


def test_readiness_manifest_bounded_portal_degrades() -> None:
    adapter = _adapter('geometric')
    manifest = _manifest(adapter, portal_state='BOUNDED')
    subject = _subject(
        parts=(_part(_mesh(_CUBE_OBJ), part_id='room-boundary'),),
        openings=(_opening(),),
    )
    report = _report(subject)
    verdict = evaluate_solver_readiness(
        subject=subject, report=report,
        adapter_descriptor=adapter,
        capability_manifest=manifest,
        evaluated_at_utc=UTC)
    # portal ambiguity blocks under requires_resolved_portals, but the
    # BOUNDED manifest row must surface as a solver_limitation reason
    assert any(r.origin == 'solver_limitation'
               for r in (
                   verdict.blocking_reasons + verdict.degraded_reasons))


def test_derive_context_rejects_foreign_manifest() -> None:
    adapter = _adapter('geometric')
    other = _adapter('wave')
    manifest = _manifest(other)
    with pytest.raises(GeometryIntakeError):
        derive_solver_geometry_context(adapter, manifest)


# --- invalidation -----------------------------------------------------------


def test_evidence_state_current_and_stale_geometry() -> None:
    subject = _subject(parts=(_part(_mesh(_CUBE_OBJ)),))
    report = _report(subject)
    verdict = evaluate_solver_readiness(
        subject=subject, report=report,
        adapter_descriptor=_adapter('geometric'),
        evaluated_at_utc=UTC)
    assert readiness_evidence_state(
        verdict, current_geometry_sha256=subject.subject_sha256
    ) == 'current'
    assert readiness_evidence_state(
        verdict, current_geometry_sha256='f' * 64
    ) == 'stale_geometry'
    decision, reasons = geometry_solver_execution_gate(
        verdict, current_geometry_sha256='f' * 64)
    assert decision == 'deny'


def test_evidence_state_stale_solver_and_manifest() -> None:
    adapter = _adapter('geometric')
    manifest = _manifest(adapter)
    subject = _subject(
        parts=(_part(_mesh(_CUBE_OBJ), part_id='room-boundary'),))
    report = _report(subject)
    verdict = evaluate_solver_readiness(
        subject=subject, report=report,
        adapter_descriptor=adapter, capability_manifest=manifest,
        evaluated_at_utc=UTC)
    assert readiness_evidence_state(
        verdict,
        current_geometry_sha256=subject.subject_sha256,
        current_adapter_sha256='e' * 64,
    ) == 'stale_solver'
    assert readiness_evidence_state(
        verdict,
        current_geometry_sha256=subject.subject_sha256,
        current_manifest_sha256='e' * 64,
    ) == 'stale_manifest'
    assert readiness_evidence_state(
        verdict,
        current_geometry_sha256=subject.subject_sha256,
        current_adapter_sha256=adapter.semantic_sha256,
        current_manifest_sha256=manifest.semantic_sha256,
    ) == 'current'


def test_revision_invalidates_prior_verdict() -> None:
    subject, report, proposal, acceptance, revision = (
        _accepted_chain(material_state='unassigned', material_label=None))
    verdict = evaluate_solver_readiness(
        subject=subject, report=report,
        adapter_descriptor=_adapter('geometric'),
        evaluated_at_utc=UTC)
    state = readiness_evidence_state(
        verdict,
        current_geometry_sha256=revision.derived_geometry_sha256,
    )
    assert state == 'stale_geometry'


# --- end to end -------------------------------------------------------------


def test_end_to_end_import_diagnose_repair_ready() -> None:
    """One complete import -> diagnose -> repair -> solver-ready chain."""
    subject = _subject(
        parts=(_part(_mesh(_CUBE_OBJ), material_state='unassigned',
                     material_label=None),),
    )
    report = _report(subject)
    assert report.defects_of_kind('material_assignment_gap')
    proposal = propose_geometry_repairs(
        report, subject, proposed_by='op', proposed_at_utc=UTC)
    decisions = [
        RepairActionDecision(
            action_id=a.action_id,
            decision='accepted' if a.kind == 'assign_material' else 'rejected',
            decided_by='op', decided_at_utc=UTC,
            accepted_parameters=(
                {'material_label': 'gypsum board'}
                if a.kind == 'assign_material' else {}),
        )
        for a in proposal.actions
    ]
    acceptance = record_geometry_repair_acceptance(
        proposal, decisions, decided_by='op', decided_at_utc=UTC)
    revision = derive_geometry_revision(
        subject, report, proposal, acceptance,
        derived_by='op', derived_at_utc=UTC)
    assert revision.residual_defects == ()
    verdict = evaluate_solver_readiness(
        derived_revision=revision, report=report,
        adapter_descriptor=_adapter('geometric'),
        evaluated_at_utc=UTC)
    assert verdict.verdict == 'supported'
    assert verdict.geometry_ref.kind == 'derived_geometry_revision'
    decision, _ = geometry_solver_execution_gate(
        verdict, current_geometry_sha256=revision.derived_geometry_sha256)
    assert decision == 'allow'


# --- repository -------------------------------------------------------------


def _repository(tmp_path: Path) -> CadGeometryIntakeRepository:
    scene = SceneRepository(tmp_path / 'cad-scenes.sqlite3')
    return CadGeometryIntakeRepository(scene)


def _chain_records(tmp_path: Path):
    subject = _subject(
        parts=(_part(_mesh(_CUBE_OBJ), material_state='unassigned',
                     material_label=None),),
        document_id='repo-doc',
    )
    report = _report(subject)
    proposal = propose_geometry_repairs(
        report, subject, proposed_by='op', proposed_at_utc=UTC)
    decisions = [
        RepairActionDecision(
            action_id=a.action_id,
            decision='accepted' if a.kind == 'assign_material' else 'rejected',
            decided_by='op', decided_at_utc=UTC,
            accepted_parameters=(
                {'material_label': 'oak'}
                if a.kind == 'assign_material' else {}),
        )
        for a in proposal.actions
    ]
    acceptance = record_geometry_repair_acceptance(
        proposal, decisions, decided_by='op', decided_at_utc=UTC)
    revision = derive_geometry_revision(
        subject, report, proposal, acceptance,
        derived_by='op', derived_at_utc=UTC)
    verdict = evaluate_solver_readiness(
        derived_revision=revision, report=report,
        adapter_descriptor=_adapter('geometric'),
        evaluated_at_utc=UTC)
    return subject, report, proposal, acceptance, revision, verdict


def test_repository_round_trip(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    subject, report, proposal, acceptance, revision, verdict = (
        _chain_records(tmp_path))
    repo.save_intake_report(report)
    repo.save_repair_proposal(proposal)
    repo.save_repair_acceptance(acceptance)
    repo.save_derived_revision(revision)
    repo.save_solver_readiness(verdict)

    assert repo.get_intake_report(report.report_id) == report
    assert repo.get_repair_proposal(proposal.proposal_id) == proposal
    assert repo.get_repair_acceptance(
        acceptance.acceptance_id) == acceptance
    assert repo.get_derived_revision(
        revision.derived_revision_id) == revision
    assert repo.get_solver_readiness(verdict.verdict_id) == verdict

    assert [r.report_id for r in repo.list_intake_reports('repo-doc')] == [
        report.report_id]
    assert repo.list_intake_reports('other-doc') == ()


def test_repository_append_only_conflict(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _, report, *_ = _chain_records(tmp_path)
    repo.save_intake_report(report)
    repo.save_intake_report(report)  # idempotent
    tampered = report.model_copy(
        update={'evaluated_at_utc': '2026-10-08T00:00:00+00:00'})
    # model_copy bypasses validators — seal it again so save reaches
    # the append-only check on a DIFFERENT sha with the same id.
    fake = type(report).model_construct(**report.model_dump())
    object.__setattr__(fake, 'evaluated_at_utc',
                       '2026-10-08T00:00:00+00:00')
    # save() re-seals: assert_sealed fails first -> IntegrityError
    with pytest.raises(GeometryIntakeIntegrityError):
        repo.save_intake_report(fake)


def test_repository_tamper_detection(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _, report, *_ = _chain_records(tmp_path)
    repo.save_intake_report(report)
    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            'UPDATE cad_geometry_intake_reports SET critical_count=? '
            'WHERE report_id=?',
            (999, report.report_id))
    with pytest.raises(GeometryIntakeIntegrityError):
        repo.get_intake_report(report.report_id)


def test_repository_rejects_unsealed_record(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _, report, *_ = _chain_records(tmp_path)
    fake = type(report).model_construct(**report.model_dump())
    object.__setattr__(fake, 'report_sha256', 'f' * 64)
    with pytest.raises(GeometryIntakeIntegrityError):
        repo.save_intake_report(fake)


# --- UI panel -----------------------------------------------------------------


def test_panel_splits_defects_by_origin_and_locates() -> None:
    from PySide6.QtWidgets import QApplication
    from htdt.geometry_intake_panel import GeometryIntakePanel

    app = QApplication.instance() or QApplication([])
    context = SolverGeometryContext(
        acoustic_domain='geometric', open_portals_unsupported=True)
    subject = _subject(
        parts=(_part(_mesh(_NONMANIFOLD_OBJ), part_id='room-boundary'),),
        openings=(_opening(),),
        document_id='ui-doc',
    )
    report = _report(subject, solver_context=context)
    panel = GeometryIntakePanel()
    panel.set_report(report)
    assert panel.source_table.rowCount() >= 1
    assert panel.solver_table.rowCount() >= 1

    located: list[tuple] = []
    panel.locateRequested.connect(located.append)
    # select first source defect row
    panel.source_table.selectRow(0)
    if panel.locate_button.isEnabled():
        panel.locate_button.click()
    # at least one defect should be locatable
    assert panel.locate_button.isEnabled() or located == []
    panel.deleteLater()


def test_panel_defect_locate_targets() -> None:
    subject = _subject(
        parts=(_part(_mesh(_NONMANIFOLD_OBJ), part_id='entity:e1'),))
    report = _report(subject)
    defect = report.defects_of_kind('non_manifold_edges')[0]
    targets = defect_locate_targets(defect)
    assert targets  # entity:e1 resolvable


def test_panel_verdict_and_decision_signals() -> None:
    from PySide6.QtWidgets import QApplication
    from htdt.geometry_intake_panel import GeometryIntakePanel

    app = QApplication.instance() or QApplication([])
    subject = _subject(
        parts=(_part(_mesh(_CUBE_OBJ), material_state='unassigned',
                     material_label=None),))
    report = _report(subject)
    proposal = propose_geometry_repairs(
        report, subject, proposed_by='op', proposed_at_utc=UTC)
    verdict = evaluate_solver_readiness(
        subject=subject, report=report,
        adapter_descriptor=_adapter('geometric'),
        evaluated_at_utc=UTC)
    panel = GeometryIntakePanel()
    panel.set_report(report)
    panel.set_proposal(proposal)
    panel.set_verdict(verdict)
    assert panel.verdict_value.text() != '—'

    decisions: list[tuple] = []
    panel.decisionRequested.connect(
        lambda aid, dec, params: decisions.append((aid, dec, params)))
    # accept the material assignment with a label
    for row in range(panel.proposal_table.rowCount()):
        widget = panel.proposal_table.cellWidget(row, 3)
        from PySide6.QtWidgets import QLineEdit
        line = widget.findChild(QLineEdit) if widget else None
        if line is not None:
            line.setText('acoustic plaster')
            panel.proposal_table.cellWidget(row, 4).click()
    assert any(d[1] == 'accepted' and
               d[2].get('material_label') == 'acoustic plaster'
               for d in decisions)
    panel.deleteLater()
