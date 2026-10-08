"""REV70 IFC source intake + panel wiring tests (Issue #866).

Covers the slice that was left device-only in REV66:

* real IFC file -> sealed artifact + entity mappings -> intake subject
  with honest ``source_refs`` (``ifc_import_artifact`` /
  ``ifc_entity_mapping`` AuthorityRefs, never fixture-shaped refs),
* unit-authority propagation (declared / custom / undeclared fail
  closed), openings resolved as declared IntakeOpenings from
  RELVOIDS/RELFILLS links, and fail-closed stubs for unresolvable
  geometry,
* the GeometryIntakeController chain (import -> diagnose -> propose ->
  decisions -> acceptance -> derived revision -> readiness verdict)
  with repository persistence at every step,
* panel workflow affordances (solver combo, action signals, decided-row
  disabling, decision progress).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from htdt.cad_acoustic_solver_adapter import (
    build_acoustic_solver_adapter_descriptor,
)
from htdt.cad_acoustic_solver_dispatch_repository import (
    CadAcousticSolverDispatchRepository,
)
from htdt.cad_equipment import FrequencyDomain
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef
from htdt.cad_geometry_intake import (
    AuthorityRef,
    GeometryIntakeError,
    GeometryRepairAction,
    GeometryRepairProposal,
    build_ifc_intake_subject,
    diagnose_geometry_intake,
    evaluate_solver_readiness,
    geometry_intake_label,
)
from htdt.cad_ifc_interop import build_ifc_import
from htdt.cad_ifc_repository import CadIfcInteropRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.cad_solver_capability_manifest import (
    SolverCapabilityRow,
    SOLVER_PATH_PHENOMENA,
    build_solver_capability_manifest,
)
from htdt.geometry_intake_controller import GeometryIntakeController

UTC = '2026-10-08T00:00:00+00:00'
DOC_ID = 'doc-rev70'


# ---------------------------------------------------------------------------
# IFC fixture builders (STEP Part 21 — same vocabulary as test_rev56_interop)
# ---------------------------------------------------------------------------


def _header(schema: str = 'IFC4X3_ADD2') -> str:
    return (
        'ISO-10303-21;\n'
        'HEADER;\n'
        "FILE_DESCRIPTION(('ViewDefinition [CoordinationView]'),'2;1');\n"
        "FILE_NAME('room.ifc','2026-10-08T10:00:00',('a'),('o'),'app','v','');\n"
        f"FILE_SCHEMA(('{schema}'));\n"
        'ENDSEC;\n'
        'DATA;\n'
    )


_TAIL = 'ENDSEC;\nEND-ISO-10303-21;\n'

MM_UNITS = "#30=IFCSIUNIT(*,.LENGTHUNIT.,.MILLI.,.METRE.);\n"
M_UNITS = "#30=IFCSIUNIT(*,.LENGTHUNIT.,$,.METRE.);\n"
DECIMETRE_UNITS = (
    "#30=IFCSIUNIT(*,.LENGTHUNIT.,.DECI.,.METRE.);\n"
)


def _context(units: str = MM_UNITS) -> str:
    return (
        '#1=IFCPROJECT(\'gid-proj\',$,\'P\',$,$,$,$,(#20),#40);\n'
        '#10=IFCAXIS2PLACEMENT3D(#11,$,$);\n'
        '#11=IFCCARTESIANPOINT((0.,0.,0.));\n'
        '#20=IFCGEOMETRICREPRESENTATIONCONTEXT($,\'Model\',3,1.E-05,#10);\n'
        + units
        + '#40=IFCUNITASSIGNMENT((#30));\n'
    )


_STOREY_CHAIN = (
    '#45=IFCAXIS2PLACEMENT3D(#46,$,$);\n'
    '#46=IFCCARTESIANPOINT((0.,0.,0.));\n'
    '#47=IFCLOCALPLACEMENT($,#45);\n'
    '#50=IFCSITE(\'gid-site\',$,\'Site\',$,$,#47,$,$,.ELEMENT.,$,$,$,$,$);\n'
    '#51=IFCBUILDING(\'gid-bldg\',$,\'B\',$,$,#47,$,$,.ELEMENT.,$,$,$);\n'
    '#52=IFCBUILDINGSTOREY(\'gid-storey\',$,\'L1\',$,$,#53,$,$,.ELEMENT.,$,$,3000.);\n'
    '#53=IFCLOCALPLACEMENT(#47,#54);\n'
    '#54=IFCAXIS2PLACEMENT3D(#55,$,$);\n'
    '#55=IFCCARTESIANPOINT((0.,0.,3000.));\n'
    '#60=IFCRELAGGREGATES(\'a1\',$,$,$,#1,(#50));\n'
    '#61=IFCRELAGGREGATES(\'a2\',$,$,$,#50,(#51));\n'
    '#62=IFCRELAGGREGATES(\'a3\',$,$,$,#51,(#52));\n'
)


def _swept_solid(
    first_id: int,
    profile: tuple[tuple[float, float], ...],
    depth: float,
    *,
    context_ref: int = 20,
) -> tuple[str, int]:
    """IFCARBITRARYCLOSEDPROFILEDEF -> IFCEXTRUDEDAREASOLID -> SweptSolid rep."""
    lines = [f'#{first_id}=IFCARBITRARYCLOSEDPROFILEDEF(.AREA.,\'p\',#{first_id + 1});\n']
    points = ','.join(f'#{first_id + 2 + i}' for i in range(len(profile)))
    lines.append(f'#{first_id + 1}=IFCPOLYLINE(({points}));\n')
    for i, (x, y) in enumerate(profile):
        lines.append(
            f'#{first_id + 2 + i}=IFCCARTESIANPOINT(({x},{y}));\n'
        )
    solid = first_id + 2 + len(profile)
    lines.append(
        f'#{solid}=IFCEXTRUDEDAREASOLID(#{first_id},$,#{solid + 1},{depth});\n'
    )
    lines.append(f'#{solid + 1}=IFCDIRECTION((0.,0.,1.));\n')
    lines.append(
        f'#{solid + 2}=IFCSHAPEREPRESENTATION(#{context_ref},'
        f"'Body','SweptSolid',(#{solid}));\n"
    )
    lines.append(f'#{solid + 3}=IFCPRODUCTDEFINITIONSHAPE($,$,(#{solid + 2}));\n')
    return ''.join(lines), solid + 3


_SPACE_BODY = (
    '#70=IFCLOCALPLACEMENT(#53,#71);\n'
    '#71=IFCAXIS2PLACEMENT3D(#72,$,$);\n'
    '#72=IFCCARTESIANPOINT((1000.,2000.,0.));\n'
)


def _wall_body_ids() -> str:
    """Wall + void + door block with swept-solid geometry everywhere."""
    parts = [
        '#110=IFCLOCALPLACEMENT(#53,#111);\n'
        '#111=IFCAXIS2PLACEMENT3D(#112,$,$);\n'
        '#112=IFCCARTESIANPOINT((0.,0.,0.));\n'
        '#113=IFCMATERIALLAYER(#114,19.,$,\'gypsum\');\n'
        '#114=IFCMATERIAL(\'Gypsum Board\');\n'
        '#115=IFCMATERIALLAYERSET((#113),\'stud wall\');\n'
        '#116=IFCMATERIALLAYERSETUSAGE(#115,.AXIS2.,.NEGATIVE.,0.,150.);\n'
        '#117=IFCRELASSOCIATESMATERIAL(\'mr\',$,$,$,(#120),#116);\n'
    ]
    solid, shape = _swept_solid(
        118,
        ((0., 0.), (5000., 0.), (5000., 200.), (0., 200.)),
        2800.,
    )
    parts.append(solid)
    parts.append(
        f'#120=IFCWALL(\'gid-wall\',$,\'W1\',$,$,#110,#{shape});\n'
    )
    # Void element at (1200,0,0) rel to wall; 900x2100 mm opening.
    parts.append('#200=IFCLOCALPLACEMENT(#110,#201);\n')
    parts.append('#201=IFCAXIS2PLACEMENT3D(#202,$,$);\n')
    parts.append('#202=IFCCARTESIANPOINT((1200.,0.,0.));\n')
    solid, shape = _swept_solid(
        210,
        ((0., 0.), (900., 0.), (900., 200.), (0., 200.)),
        2100.,
    )
    parts.append(solid)
    parts.append(
        f'#240=IFCOPENINGELEMENT(\'gid-open\',$,\'O1\',$,$,#200,#{shape});\n'
    )
    # Door fills the void (placement relative to the void's).
    parts.append('#300=IFCLOCALPLACEMENT(#200,#301);\n')
    parts.append('#301=IFCAXIS2PLACEMENT3D(#302,$,$);\n')
    parts.append('#302=IFCCARTESIANPOINT((0.,0.,0.));\n')
    solid, shape = _swept_solid(
        310,
        ((0., 0.), (900., 0.), (900., 50.), (0., 50.)),
        2100.,
    )
    parts.append(solid)
    parts.append(
        f'#330=IFCDOOR(\'gid-door\',$,\'D1\',$,$,#300,#{shape},\'door\',900.,2100.);\n'
    )
    parts.append('#400=IFCRELVOIDSELEMENT(\'vr\',$,$,$,#120,#240);\n')
    parts.append('#401=IFCRELFILLSELEMENT(\'fr\',$,$,$,#240,#330);\n')
    # Furniture (no material -> honest unassigned defect downstream).
    parts.append('#500=IFCLOCALPLACEMENT(#53,#501);\n')
    parts.append('#501=IFCAXIS2PLACEMENT3D(#502,$,$);\n')
    parts.append('#502=IFCCARTESIANPOINT((2500.,3000.,0.));\n')
    solid, shape = _swept_solid(
        510,
        ((0., 0.), (600., 0.), (600., 600.), (0., 600.)),
        1200.,
    )
    parts.append(solid)
    parts.append(
        f'#540=IFCFURNISHINGELEMENT(\'gid-furn\',$,\'Sofa\',$,$,#500,#{shape});\n'
    )
    parts.append(
        '#600=IFCRELCONTAINEDINSPATIALSTRUCTURE('
        '\'c1\',$,$,$,(#100,#120,#240,#330,#540),#52);\n'
    )
    return ''.join(parts)


def _space_entity() -> str:
    solid, shape = _swept_solid(
        700,
        ((0., 0.), (5000., 0.), (5000., 7000.),
         (3000., 7000.), (3000., 4000.), (0., 4000.)),
        2800.,
    )
    return (
        _SPACE_BODY
        + solid
        + f'#100=IFCSPACE(\'gid-space\',$,\'Theater\',$,$,#70,#{shape},$,.ELEMENT.,.INTERNAL.,$);\n'
    )


def _model(
    *,
    units: str = MM_UNITS,
    body: str = '',
    context_fn=_context,
) -> str:
    return _header() + context_fn(units) + _STOREY_CHAIN + body + _TAIL


_FULL_BODY = _space_entity() + _wall_body_ids()


def _import(tmp_path, *, units=MM_UNITS, body=_FULL_BODY):
    return build_ifc_import(
        document_id=DOC_ID,
        file_name='room.ifc',
        source=_model(units=units, body=body),
        imported_at_utc=UTC,
    )


def _mappings_dict(mappings):
    return {m.ifc_type: m for m in mappings}


# --- solver fixtures --------------------------------------------------------


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


def _manifest(descriptor):
    rows = [
        SolverCapabilityRow(
            phenomenon=phenomenon,
            state='SUPPORTED',
            valid_frequency_domain=FrequencyDomain(
                minimum_hz=20.0, maximum_hz=20000.0),
        )
        for phenomenon in SOLVER_PATH_PHENOMENA
    ]
    return build_solver_capability_manifest(descriptor=descriptor, rows=rows)


def _scene_repo(tmp_path: Path) -> SceneRepository:
    return SceneRepository(tmp_path / 'scene.sqlite3')


def _controller(tmp_path: Path) -> tuple[SceneRepository, GeometryIntakeController]:
    repo = _scene_repo(tmp_path)
    repo.save(
        make_f1_scene().model_copy(update={'document_id': DOC_ID}),
        parent_revision_id=None,
    )
    dispatch = CadAcousticSolverDispatchRepository(
        repo,
        external_authority_resolver=lambda ref: ref,
        fidelity_policy_resolver=lambda ref: None,
    )
    return repo, GeometryIntakeController(
        repo,
        DOC_ID,
        dispatch_repository=dispatch,
        operator_id='op-test',
        now_utc=lambda: UTC,
    )


# ---------------------------------------------------------------------------
# Subject construction
# ---------------------------------------------------------------------------


def test_ifc_subject_from_real_file(tmp_path) -> None:
    artifact, mappings = _import(tmp_path)
    subject = build_ifc_intake_subject(DOC_ID, artifact, mappings)

    assert subject.source_kind == 'ifc_import'
    assert len(subject.source_refs) == 1
    ref = subject.source_refs[0]
    assert ref.kind == 'ifc_import_artifact'
    assert ref.ref_id == artifact.artifact_id
    assert ref.ref_sha256 == artifact.artifact_sha256
    assert subject.unit_state == 'declared'
    assert 'millimetre' in subject.unit_details

    by_part = {p.part_id: p for p in subject.parts}
    assert len(by_part) == 4  # space + wall + door + furniture
    assert not subject.unresolved_parts

    space = by_part['ifc:100']
    assert space.owner_kind == 'room_boundary'
    assert space.role == 'room_boundary'
    assert space.mesh is not None
    assert space.import_authority.source_unit == 'millimeters'
    assert space.import_authority.unit_declared_by == 'format_specification'
    assert space.source_asset_ref.kind == 'ifc_entity_mapping'
    space_mapping = _mappings_dict(mappings)['IFCSPACE']
    assert space.source_asset_ref.ref_id == space_mapping.mapping_id
    assert space.source_asset_ref.ref_sha256 == space_mapping.mapping_sha256

    wall = by_part['ifc:120']
    assert wall.owner_kind == 'room_boundary'
    assert wall.material_state == 'assigned'
    assert wall.material_label == 'Gypsum Board'

    door = by_part['ifc:330']
    assert door.owner_kind == 'entity_body'
    assert door.role == 'object_surface'

    furniture = by_part['ifc:540']
    assert furniture.material_state == 'unassigned'

    assert len(subject.openings) == 1
    opening = subject.openings[0]
    assert opening.host_part_id == 'ifc:120'
    assert opening.is_open is False  # door fills the void
    # profile 900x200 mm -> 0.18 m^2 opening area proxy
    assert opening.area_m2 == pytest.approx(0.18, abs=0.01)
    assert opening.resolution == 'unresolved'

    # Determinism: same artifact + mappings -> identical subject.
    again = build_ifc_intake_subject(DOC_ID, artifact, mappings)
    assert again.subject_sha256 == subject.subject_sha256


def test_ifc_subject_world_transform_mm(tmp_path) -> None:
    artifact, mappings = _import(tmp_path)
    subject = build_ifc_intake_subject(DOC_ID, artifact, mappings)
    space = next(p for p in subject.parts if p.part_id == 'ifc:100')
    xs = [v.x for v in space.mesh.vertices]
    ys = [v.y for v in space.mesh.vertices]
    zs = [v.z for v in space.mesh.vertices]
    # Storey at z=3000mm, space at (1000,2000)mm, mm units -> meters.
    assert min(xs) == pytest.approx(1.0, abs=1e-4)
    assert max(xs) == pytest.approx(6.0, abs=1e-4)
    assert min(ys) == pytest.approx(2.0, abs=1e-4)
    assert max(ys) == pytest.approx(9.0, abs=1e-4)
    assert min(zs) == pytest.approx(3.0, abs=1e-4)
    assert max(zs) == pytest.approx(5.8, abs=1e-4)


def test_ifc_subject_unresolvable_geometry_is_stub(tmp_path) -> None:
    # A wall carrying only an Axis/curve representation has no meshable
    # descriptor — fail closed into a stub, never an invented mesh.
    body = (
        _space_entity()
        + '#110=IFCLOCALPLACEMENT(#53,#111);\n'
        + '#111=IFCAXIS2PLACEMENT3D(#112,$,$);\n'
        + '#112=IFCCARTESIANPOINT((0.,0.,0.));\n'
        + '#115=IFCSHAPEREPRESENTATION(#20,\'Axis\',\'Curve2D\',(#116));\n'
        + '#116=IFCPOLYLINE((#117,#118));\n'
        + '#117=IFCCARTESIANPOINT((0.,0.));\n'
        + '#118=IFCCARTESIANPOINT((5000.,0.));\n'
        + '#119=IFCPRODUCTDEFINITIONSHAPE($,$,(#115));\n'
        + '#120=IFCWALL(\'gid-wall\',$,\'W1\',$,$,#110,#119);\n'
    )
    artifact, mappings = _import(tmp_path, body=body)
    subject = build_ifc_intake_subject(DOC_ID, artifact, mappings)
    stub_ids = {s.part_id for s in subject.unresolved_parts}
    assert 'ifc:120' in stub_ids
    stub = next(s for s in subject.unresolved_parts if s.part_id == 'ifc:120')
    assert 'geometry' in stub.unavailability_reason
    report = diagnose_geometry_intake(subject, evaluated_at_utc=UTC)
    kinds = {d.kind for d in report.defects}
    assert 'geometry_unavailable' in kinds
    defect = report.defects_of_kind('geometry_unavailable')[0]
    assert 'ifc:120' in defect.part_refs


def test_ifc_undeclared_units_fail_closed(tmp_path) -> None:
    # No length unit declared -> transforms withheld, every mapped entity a stub.
    artifact, mappings = _import(tmp_path, units='')
    assert artifact.coordinate.length_unit_state == 'undeclared'
    subject = build_ifc_intake_subject(DOC_ID, artifact, mappings)
    assert subject.unit_state == 'undeclared'
    assert 'undeclared' in subject.unit_details
    assert not subject.parts
    assert subject.unresolved_parts
    # Stubs carry the withheld-transform reason (void stubs cite their
    # unresolvable host); with no mesh part there is no authority to repair —
    # fail closed with geometry_unavailable and a blocking readiness verdict
    # (operator must re-import with units).
    reasons = [s.unavailability_reason for s in subject.unresolved_parts]
    assert any('transform withheld' in r for r in reasons)
    assert any('host boundary unresolvable' in r for r in reasons)
    report = diagnose_geometry_intake(subject, evaluated_at_utc=UTC)
    kinds = {d.kind for d in report.defects}
    assert kinds == {'geometry_unavailable'}
    assert report.critical_count == len(report.defects) > 0
    verdict = evaluate_solver_readiness(
        subject=subject,
        report=report,
        adapter_descriptor=_adapter('geometric'),
        evaluated_at_utc=UTC,
    )
    assert verdict.verdict == 'unsupported'


def test_ifc_custom_unit_declared_as_custom(tmp_path) -> None:
    artifact, mappings = _import(tmp_path, units=DECIMETRE_UNITS)
    subject = build_ifc_intake_subject(DOC_ID, artifact, mappings)
    assert subject.unit_state == 'declared'
    space = next(p for p in subject.parts if p.part_id == 'ifc:100')
    assert space.import_authority.source_unit == 'custom'
    assert space.import_authority.custom_scale_to_meters == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# Controller chain
# ---------------------------------------------------------------------------


def test_controller_full_chain_persists_every_step(tmp_path) -> None:
    repo, controller = _controller(tmp_path)

    # solver registered in the dispatch repository first
    descriptor = _adapter('geometric')
    controller.dispatch_repository.save_descriptor(descriptor)
    options = controller.solver_options()
    assert len(options) == 1
    opt_descriptor, opt_manifest = options[0]
    assert opt_descriptor.descriptor_id == descriptor.descriptor_id

    artifact, subject = controller.import_ifc_source(
        _model(body=_FULL_BODY).encode('utf-8'), file_name='room.ifc'
    )
    assert controller.subject is subject
    # artifact + mappings persisted under the document
    assert repo is controller.repository
    ifc_repo = CadIfcInteropRepository(repo)
    assert (
        ifc_repo.get_import_artifact(artifact.artifact_id).artifact_sha256
        == artifact.artifact_sha256
    )
    assert len(ifc_repo.list_entity_mappings(DOC_ID, artifact.artifact_id)) == len(
        ifc_repo.list_entity_mappings(DOC_ID)
    )

    report, proposal = controller.run_health_check()
    assert controller.intake_repository.get_intake_report(report.report_id) == report
    assert controller.intake_repository.get_repair_proposal(
        proposal.proposal_id) == proposal
    assert report.subject_ref.ref_sha256 == subject.subject_sha256
    assert proposal.actions  # unassigned material on space/furniture

    # decisions accumulate; acceptance seals only on exact cover
    for action in proposal.actions[:-1]:
        assert controller.submit_decision(
            action.action_id, 'accepted',
            accepted_parameters=(
                {'material_label': 'acoustic plaster'}
                if action.kind == 'assign_material' else {}
            ),
        ) is None
    last = proposal.actions[-1]
    acceptance = controller.submit_decision(
        last.action_id, 'accepted',
        accepted_parameters=(
            {'material_label': 'acoustic plaster'}
            if last.kind == 'assign_material' else {}
        ),
    )
    assert acceptance is not None
    assert controller.intake_repository.get_repair_acceptance(
        acceptance.acceptance_id) == acceptance

    revision = controller.derive_revision()
    assert controller.intake_repository.get_derived_revision(
        revision.derived_revision_id) == revision
    assert revision.source_subject_ref.ref_sha256 == subject.subject_sha256

    verdict = controller.select_solver(opt_descriptor, opt_manifest)
    assert verdict is not None
    assert controller.intake_repository.get_solver_readiness(
        verdict.verdict_id) == verdict
    assert controller.verdict_evidence_state() == 'current'


def test_controller_decisions_require_exact_cover(tmp_path) -> None:
    _, controller = _controller(tmp_path)
    controller.import_ifc_source(
        _model(body=_FULL_BODY).encode('utf-8'), file_name='room.ifc'
    )
    _, proposal = controller.run_health_check()
    assert len(proposal.actions) >= 1
    # Decide all but the last action -> no acceptance may seal.
    for action in proposal.actions[:-1]:
        controller.submit_decision(
            action.action_id, 'rejected'
        )
    assert controller.acceptance is None
    with pytest.raises(GeometryIntakeError):
        controller.derive_revision()
    # Re-deciding an already-decided action replaces the pending decision.
    first = proposal.actions[0]
    controller.submit_decision(
        first.action_id, 'accepted',
        accepted_parameters=(
            {'material_label': 'oak'}
            if first.kind == 'assign_material' else {}
        ),
    )
    assert controller.pending_decisions[first.action_id].decision == 'accepted'
    # Unknown action ids are rejected outright.
    with pytest.raises(GeometryIntakeError):
        controller.submit_decision('ghost-action', 'accepted')


def test_controller_health_check_needs_subject(tmp_path) -> None:
    _, controller = _controller(tmp_path)
    with pytest.raises(GeometryIntakeError):
        controller.run_health_check()
    with pytest.raises(GeometryIntakeError):
        controller.evaluate_readiness(adapter_descriptor=_adapter())


def test_controller_scene_subject_path(tmp_path) -> None:
    repo, controller = _controller(tmp_path)
    subject = controller.adopt_current_subject(
        read_blob=repo.read_blob,
    )
    assert subject.source_kind == 'scene_document'
    assert subject.document_id == DOC_ID
    report, proposal = controller.run_health_check()
    assert report.subject_ref.ref_sha256 == subject.subject_sha256


def test_controller_evidence_invalidation(tmp_path) -> None:
    _, controller = _controller(tmp_path)
    descriptor = _adapter('geometric')
    manifest = _manifest(descriptor)
    controller.import_ifc_source(
        _model(body=_FULL_BODY).encode('utf-8'), file_name='room.ifc'
    )
    controller.run_health_check()
    controller.select_solver(descriptor, manifest)
    verdict = controller.verdict
    assert verdict is not None
    assert controller.verdict_evidence_state() == 'current'
    # Re-importing the same file changes nothing -> still current.
    controller.import_ifc_source(
        _model(body=_FULL_BODY).encode('utf-8'), file_name='room.ifc'
    )
    assert controller.verdict_evidence_state() == 'current'
    # A different file changes the geometry hash -> the old verdict retires.
    other = _model(units=M_UNITS, body=_FULL_BODY)
    controller.import_ifc_source(
        other.encode('utf-8'), file_name='room2.ifc'
    )
    assert controller.verdict_evidence_state() == 'stale_geometry'


# ---------------------------------------------------------------------------
# Panel wiring
# ---------------------------------------------------------------------------


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_panel_action_row_and_solver_combo(tmp_path) -> None:
    from htdt.geometry_intake_panel import GeometryIntakePanel

    app = _qapp()
    panel = GeometryIntakePanel()
    assert not panel.diagnose_button.isEnabled()
    assert not panel.derive_button.isEnabled()

    solver_events: list[object] = []
    panel.solverSelectionChanged.connect(solver_events.append)
    descriptor = _adapter('geometric')
    panel.set_solver_options([
        ('fixture-adapter v1 (geometric)', (descriptor, None)),
    ])
    assert panel.solver_combo.count() == 2
    # Placeholder row stays selected until the operator picks a solver —
    # no silent selection against the sealed chain.
    assert panel.solver_combo.currentIndex() == 0
    panel.solver_combo.setCurrentIndex(1)
    payload = panel.current_solver_payload()
    assert payload[0].descriptor_id == descriptor.descriptor_id
    assert solver_events and solver_events[-1][0] is descriptor

    fired: list[str] = []
    panel.ifcImportRequested.connect(lambda: fired.append('ifc'))
    panel.sceneSubjectRequested.connect(lambda: fired.append('scene'))
    panel.diagnoseRequested.connect(lambda: fired.append('diagnose'))
    panel.deriveRequested.connect(lambda: fired.append('derive'))
    panel.import_ifc_button.click()
    panel.adopt_scene_button.click()
    panel.deleteLater()
    assert fired == ['ifc', 'scene']


def test_panel_decision_progress_and_stage(tmp_path) -> None:
    from htdt.geometry_intake_panel import GeometryIntakePanel

    app = _qapp()
    _, controller = _controller(tmp_path)
    panel = GeometryIntakePanel()
    decisions: list[tuple] = []
    panel.decisionRequested.connect(
        lambda aid, dec, params: decisions.append((aid, dec, params)))

    controller.import_ifc_source(
        _model(body=_FULL_BODY).encode('utf-8'), file_name='room.ifc'
    )
    report, proposal = controller.run_health_check()
    panel.set_stage(has_subject=True)
    panel.set_report(report)
    panel.set_proposal(proposal)
    assert panel.diagnose_button.isEnabled()
    assert panel.proposal_table.rowCount() == len(proposal.actions)

    # Decide the first action from the UI -> pending grows, row disables.
    first = proposal.actions[0]
    params = (
        {'material_label': 'oak'}
        if first.kind == 'assign_material' else {}
    )
    controller.submit_decision(
        first.action_id, 'accepted', accepted_parameters=params
    )
    panel.mark_decided(first.action_id)
    assert not panel.proposal_table.cellWidget(0, 4).isEnabled()
    assert not panel.proposal_table.cellWidget(0, 5).isEnabled()
    total = len(proposal.actions)
    panel.set_decision_progress(
        f'決定 {len(controller.pending_decisions)}/{total}'
    )
    assert panel.decision_progress.text().startswith('決定 1/')
    panel.set_stage(has_subject=True, derive_enabled=False)
    panel.deleteLater()


def _ref_sha(char: str) -> str:
    return char * 64


def _declare_units_proposal(
    *, variant: str = 'a'
) -> GeometryRepairProposal:
    """Proposal carrying declare_units with operator-supplied parameters.

    Real declare_units actions ship ``scale_to_meters=None`` — the
    parameter editor must not crash on it (REV70 GUI regression).
    """
    action = GeometryRepairAction.create(
        kind='declare_units',
        automation='operator_required',
        target_part_id='ifc:1',
        target_defect_ids=('dgx-' + '0' * 24,),
        proposed_parameters={
            'source_unit': None,
            'scale_to_meters': None,
        },
        description='operator declares the source unit and scale',
    )
    return GeometryRepairProposal.create(
        document_id=DOC_ID,
        report_ref=AuthorityRef(
            kind='geometry_intake_report',
            ref_id='gdr-' + '0' * 24,
            ref_sha256=_ref_sha('1'),
        ),
        subject_ref=AuthorityRef(
            kind='geometry_intake_subject',
            ref_id='gis-' + '0' * 24,
            ref_sha256=_ref_sha('2'),
        ),
        actions=(action,),
        generated_by='op-test',
        generated_at_utc=UTC,
        proposal_reason=f'fixture-{variant}',
    )


def test_panel_declare_units_editor_no_crash() -> None:
    from htdt.geometry_intake_panel import GeometryIntakePanel

    app = _qapp()
    panel = GeometryIntakePanel()
    proposal = _declare_units_proposal()
    panel.set_proposal(proposal)
    assert panel.proposal_table.rowCount() == 1
    from PySide6.QtWidgets import QDoubleSpinBox

    scale = panel.proposal_table.cellWidget(0, 3).findChild(QDoubleSpinBox)
    assert scale is not None
    panel.deleteLater()


def test_panel_decided_rows_stay_disabled_on_resync() -> None:
    from htdt.geometry_intake_panel import GeometryIntakePanel

    app = _qapp()
    panel = GeometryIntakePanel()
    proposal = _declare_units_proposal()
    action_id = proposal.actions[0].action_id
    panel.set_proposal(proposal)
    panel.mark_decided(action_id)
    assert not panel.proposal_table.cellWidget(0, 4).isEnabled()
    # Same proposal re-pushed by the workspace sync: decided stays decided.
    panel.set_proposal(proposal)
    assert not panel.proposal_table.cellWidget(0, 4).isEnabled()
    assert not panel.proposal_table.cellWidget(0, 5).isEnabled()
    # A genuinely new proposal (different id) resets the decided set.
    other = _declare_units_proposal(variant='b')
    assert other.proposal_id != proposal.proposal_id
    panel.set_proposal(other)
    assert panel.proposal_table.cellWidget(0, 4).isEnabled()
    panel.deleteLater()


def test_controller_solver_options_empty_until_registered(tmp_path) -> None:
    _, controller = _controller(tmp_path)
    assert controller.solver_options() == ()
    controller.dispatch_repository.save_descriptor(_adapter('geometric'))
    options = controller.solver_options()
    assert len(options) == 1
    descriptor, manifest = options[0]
    assert manifest.adapter_descriptor_id == descriptor.descriptor_id


def test_label_coverage_for_new_ui_keys() -> None:
    assert geometry_intake_label('ui.import_ifc') == 'IFC を取り込む'
    assert geometry_intake_label('ui.adopt_scene') == '現在のシーンを採用'
    assert geometry_intake_label('ui.solver') == '対象ソルバー'
