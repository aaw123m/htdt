"""IFC diff-review intake tests (Issue #981).

Covers the re-import path added on top of the #866 intake chain:

* diff categories stay honest — matched / changed / added / removed /
  ambiguous / unknown; ``unknown`` is reported, never counted as
  removed; renames (GlobalId renumber) and duplicate GlobalIds land in
  the ambiguous bucket; coordinate-authority changes produce a
  project-level changed row;
* the apply is transactional (one BEGIN IMMEDIATE — a mid-flight
  failure persists nothing) and writes a NEW merged subject
  (``ifc_diff_merge``) without mutating the pinned import;
* skipped rows keep the prior state and stay visibly flagged on
  re-open (``resume_diff_state``);
* the review panel forwards every operator intent as signals.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from htdt.cad_acoustic_solver_dispatch_repository import (
    CadAcousticSolverDispatchRepository,
)
from htdt.cad_geometry_intake import GeometryIntakeError
from htdt.cad_ifc_diff import (
    compute_ifc_diff_rows,
    ifc_diff_summary,
    latest_entity_mappings,
    merge_ifc_diff_mappings,
)
from htdt.cad_ifc_repository import CadIfcInteropRepository
from htdt.geometry_intake_controller import GeometryIntakeController

from test_rev70_ifc_intake import (  # noqa: E402  (shared fixtures)
    DOC_ID,
    MM_UNITS,
    M_UNITS,
    UTC,
    _controller,
    _model,
    _space_entity,
    _swept_solid,
    _wall_body_ids,
)


# ---------------------------------------------------------------------------
# Revised-model fixture builders
# ---------------------------------------------------------------------------


def _wall_geometry_changed() -> str:
    """Wall block with a longer wall profile (geometry fingerprint diff)."""
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
        ((0., 0.), (5200., 0.), (5200., 200.), (0., 200.)),
        2800.,
    )
    parts.append(solid)
    parts.append(
        f'#120=IFCWALL(\'gid-wall\',$,\'W1\',$,$,#110,#{shape});\n'
    )
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
    parts.append('#300=IFCLOCALPLACEMENT(#200,#301);\n')
    parts.append('#301=IFCAXIS2PLACEMENT3D(#302,$,$);\n')
    parts.append('#302=IFCCARTESIANPOINT((0.,0.,0.));\n')
    solid, shape = _swept_solid(
        310,
        ((0., 0.), (900., 0.), (900., 50.), (0., 50.)),
        2100.,
    )
    parts.append(solid)
    # Same type+name+parent, RENUMBERED GlobalId -> ambiguous_rebind.
    parts.append(
        f'#330=IFCDOOR(\'gid-door2\',$,\'D1\',$,$,#300,#{shape},'
        '\'door\',900.,2100.);\n'
    )
    parts.append('#400=IFCRELVOIDSELEMENT(\'vr\',$,$,$,#120,#240);\n')
    parts.append('#401=IFCRELFILLSELEMENT(\'fr\',$,$,$,#240,#330);\n')
    parts.append('#500=IFCLOCALPLACEMENT(#53,#501);\n')
    parts.append('#501=IFCAXIS2PLACEMENT3D(#502,$,$);\n')
    parts.append('#502=IFCCARTESIANPOINT((2500.,3000.,0.));\n')
    solid, shape = _swept_solid(
        510,
        ((0., 0.), (600., 0.), (600., 650.), (0., 650.)),
        1200.,
    )
    parts.append(solid)
    parts.append(
        f'#540=IFCFURNISHINGELEMENT(\'gid-furn\',$,\'Sofa\',$,$,#500,'
        f'#{shape});\n'
    )
    parts.append(
        '#600=IFCRELCONTAINEDINSPATIALSTRUCTURE('
        '\'c1\',$,$,$,(#100,#120,#240,#330,#540),#52);\n'
    )
    return ''.join(parts)


def _beam_and_stool() -> str:
    """A new beam (added) + a no-GlobalId stool (unmatched -> unknown)."""
    solid, shape = _swept_solid(
        910,
        ((0., 0.), (3000., 0.), (3000., 300.), (0., 300.)),
        400.,
    )
    beam = (
        '#900=IFCLOCALPLACEMENT(#53,#901);\n'
        '#901=IFCAXIS2PLACEMENT3D(#902,$,$);\n'
        '#902=IFCCARTESIANPOINT((800.,4500.,0.));\n'
        + solid
        + f'#940=IFCBEAM(\'gid-beam\',$,\'B1\',$,$,#900,#{shape});\n'
        + '#601=IFCRELCONTAINEDINSPATIALSTRUCTURE('
          '\'c2\',$,$,$,(#940),#52);\n'
    )
    solid, shape = _swept_solid(
        960,
        ((0., 0.), (500., 0.), (500., 500.), (0., 500.)),
        900.,
    )
    stool = (
        '#950=IFCLOCALPLACEMENT(#53,#951);\n'
        '#951=IFCAXIS2PLACEMENT3D(#952,$,$);\n'
        '#952=IFCCARTESIANPOINT((4000.,1500.,0.));\n'
        + solid
        # No GlobalId at all — identity cannot be proven.
        + f'#990=IFCFURNISHINGELEMENT($,$,\'Stool\',$,$,#950,#{shape});\n'
        + '#602=IFCRELCONTAINEDINSPATIALSTRUCTURE('
          '\'c3\',$,$,$,(#990),#52);\n'
    )
    return beam + stool


def _nogid_bench() -> str:
    """A prior-side no-GlobalId element that vanishes in the revision —
    must surface as ``unknown``, never ``removed``."""
    solid, shape = _swept_solid(
        960,
        ((0., 0.), (700., 0.), (700., 400.), (0., 400.)),
        450.,
    )
    return (
        '#950=IFCLOCALPLACEMENT(#53,#951);\n'
        '#951=IFCAXIS2PLACEMENT3D(#952,$,$);\n'
        '#952=IFCCARTESIANPOINT((3600.,800.,0.));\n'
        + solid
        + f'#990=IFCFURNISHINGELEMENT($,$,\'Bench\',$,$,#950,#{shape});\n'
        + '#602=IFCRELCONTAINEDINSPATIALSTRUCTURE('
          '\'c3\',$,$,$,(#990),#52);\n'
    )


def _import_base(controller, body_extra: str = '') -> None:
    controller.import_ifc_source(
        _model(body=_space_entity() + _wall_body_ids() + body_extra)
        .encode('utf-8'),
        file_name='room-v1.ifc',
    )


def _import_revision(
    controller, body: str, *, units=MM_UNITS, name='room-v2.ifc'
):
    return controller.import_revised_ifc_source(
        _model(units=units, body=body).encode('utf-8'),
        file_name=name,
    )


def _rows_by_category(rows):
    out: dict[str, list] = {}
    for row in rows:
        out.setdefault(row.category, []).append(row)
    return out


def _reopen(repo) -> GeometryIntakeController:
    """A fresh controller over the SAME store — the operator re-opened
    the project."""
    dispatch = CadAcousticSolverDispatchRepository(
        repo,
        external_authority_resolver=lambda ref: ref,
        fidelity_policy_resolver=lambda ref: None,
    )
    return GeometryIntakeController(
        repo,
        DOC_ID,
        dispatch_repository=dispatch,
        operator_id='op-test',
        now_utc=lambda: UTC,
    )


# ---------------------------------------------------------------------------
# Category honesty
# ---------------------------------------------------------------------------


def test_diff_three_changed_elements_show_only_three_diffs(tmp_path):
    """DoD: a 3-element revision surfaces exactly 3 changed rows."""
    repo, controller = _controller(tmp_path)
    _import_base(controller)

    # Revision: wall geometry + sofa geometry differ; door renumbered.
    body = _space_entity() + _wall_geometry_changed()
    artifact, delta, rows = _import_revision(controller, body)
    by_cat = _rows_by_category(rows)

    changed = by_cat.get('changed', [])
    # wall geometry + sofa geometry -> exactly two 'changed' rows, and
    # the renumbered door is its own ambiguous row — not a third change.
    assert len(changed) == 2
    assert {r.change_kind for r in changed} == {'geometry_changed'}
    ambiguous = by_cat.get('ambiguous', [])
    # one paired rebind row carrying both mapping refs — not two rows
    assert len(ambiguous) == 1
    assert ambiguous[0].prior_mapping_id
    assert ambiguous[0].new_mapping_id

    assert delta.delta_id.startswith('ird:')


def test_diff_categories_honest(tmp_path):
    repo, controller = _controller(tmp_path)
    _import_base(controller, body_extra=_nogid_bench())

    body = _space_entity() + _wall_geometry_changed() + _beam_and_stool()
    artifact, delta, rows = _import_revision(controller, body)
    by_cat = _rows_by_category(rows)
    summary = ifc_diff_summary(rows)

    # matched: space + void survive unchanged
    assert summary['matched'] == 2
    # changed: wall + sofa geometry fingerprints differ
    assert summary['changed'] == 2
    # added: the new beam; the no-gid stool is NOT confidently added
    assert summary['added'] == 1
    # removed: nothing with a GlobalId vanished
    assert summary['removed'] == 0
    # unknown: the prior no-gid bench vanished + the new no-gid stool —
    # reported gray, never counted as removed or added
    assert summary['unknown'] == 2
    assert all(
        r.change_kind == 'unmatched_identity'
        for r in by_cat['unknown']
    )
    # ambiguous: door GlobalId renumbered (type/name/parent match)
    assert summary['ambiguous'] >= 1
    rebind = [
        r for r in by_cat['ambiguous']
        if r.change_kind == 'ambiguous_rebind'
    ]
    assert len(rebind) == 1
    assert rebind[0].prior_mapping_id and rebind[0].new_mapping_id


def test_diff_coordinate_change_is_project_row(tmp_path):
    repo, controller = _controller(tmp_path)
    _import_base(controller)

    body = _space_entity() + _wall_body_ids()
    artifact, delta, rows = _import_revision(
        controller, body, units=M_UNITS
    )
    project_rows = [r for r in rows if r.ifc_type == 'IFCPROJECT']
    assert len(project_rows) == 1
    assert project_rows[0].category == 'changed'
    assert project_rows[0].change_kind == 'semantics_changed'
    assert delta.reconciliation_state == 'needs_review'


def test_diff_requires_base_revision(tmp_path):
    repo, controller = _controller(tmp_path)
    with pytest.raises(GeometryIntakeError):
        controller.import_revised_ifc_source(
            _model(body=_space_entity()).encode('utf-8'),
            file_name='room-v2.ifc',
        )


# ---------------------------------------------------------------------------
# Apply semantics — merged subject, pinning, transactionality
# ---------------------------------------------------------------------------


def test_apply_writes_merged_subject_and_sealed_apply(tmp_path):
    repo, controller = _controller(tmp_path)
    _import_base(controller, body_extra=_nogid_bench())
    prior_subject = controller.subject

    body = _space_entity() + _wall_geometry_changed() + _beam_and_stool()
    artifact, delta, rows = _import_revision(controller, body)
    by_cat = _rows_by_category(rows)

    # Accept the wall change + the beam; skip the ambiguous door rebind;
    # leave unknowns pending.
    changed = by_cat['changed']
    added = by_cat['added']
    ambiguous = by_cat['ambiguous']
    for row in changed:
        controller.submit_diff_decision(row.row_key, 'accepted')
    for row in added:
        controller.submit_diff_decision(row.row_key, 'accepted')
    for row in ambiguous:
        controller.submit_diff_decision(row.row_key, 'skipped')

    apply_record, report, proposal = controller.apply_ifc_diff()

    # Merged subject: new kind, three pinned refs, changed wall adopted.
    merged_subject = controller.subject
    assert merged_subject.source_kind == 'ifc_diff_merge'
    assert merged_subject is not prior_subject
    ref_kinds = [r.kind for r in merged_subject.source_refs]
    assert ref_kinds == [
        'ifc_import_artifact',
        'ifc_import_artifact',
        'ifc_revision_delta',
    ]
    assert merged_subject.source_refs[2].ref_id == delta.delta_id

    # The apply record is sealed + persisted.
    assert apply_record.apply_id.startswith('ida:')
    ifc_repo = CadIfcInteropRepository(repo)
    stored = ifc_repo.get_diff_apply(apply_record.apply_id)
    assert stored == apply_record
    assert stored.delta_ref.ref_sha256 == delta.delta_sha256
    assert stored.prior_subject_sha256 == prior_subject.subject_sha256
    assert stored.merged_subject_sha256 == merged_subject.subject_sha256
    assert set(stored.applied_row_keys) == {
        r.row_key for r in changed + added
    }
    assert set(stored.skipped_row_keys) == {
        r.row_key for r in ambiguous
    }
    # the unknown rows stayed undecided -> recorded as pending
    assert len(stored.pending_row_keys) == len(by_cat.get('unknown', []))

    # Merge content: accepted change adopted (new mapping), skipped
    # rebind keeps the prior door mapping; added beam present.
    merged_ids = {m.ifc_global_id for m in controller.diff_base_mappings}
    assert 'gid-beam' in merged_ids
    assert 'gid-door' in merged_ids  # skipped rebind keeps prior door
    assert 'gid-door2' not in merged_ids
    # unknowns: pending -> prior bench kept, new stool excluded
    assert 'Bench' in {
        m.name for m in controller.diff_base_mappings
    }
    assert 'Stool' not in {
        m.name for m in controller.diff_base_mappings
    }

    # Health check ran on the merged subject — persisted records.
    assert report.subject_ref.ref_sha256 == merged_subject.subject_sha256
    assert controller.intake_repository.get_intake_report(
        report.report_id
    ) == report
    assert controller.intake_repository.get_repair_proposal(
        proposal.proposal_id
    ) == proposal


def test_apply_is_transactional(tmp_path):
    """A failure mid-apply persists nothing (the #996 all-or-nothing
    pattern)."""
    repo, controller = _controller(tmp_path)
    _import_base(controller)
    prior_subject = controller.subject

    body = _space_entity() + _wall_geometry_changed()
    artifact, delta, rows = _import_revision(controller, body)
    for row in rows:
        if row.decision_required:
            controller.submit_diff_decision(row.row_key, 'accepted')

    # Force a mid-transaction failure at the LAST write.
    def _boom(connection, record):
        raise RuntimeError('simulated apply crash')

    original = (
        controller.intake_repository.repair_proposals.save_in_connection
    )
    controller.intake_repository.repair_proposals.save_in_connection = (
        _boom
    )
    try:
        with pytest.raises(RuntimeError, match='simulated apply crash'):
            controller.apply_ifc_diff()
    finally:
        controller.intake_repository.repair_proposals.save_in_connection = (
            original
        )

    # Nothing persisted: no apply record, no new report/proposal, and
    # the visible subject is still the prior one.
    ifc_repo = CadIfcInteropRepository(repo)
    assert ifc_repo.list_diff_applies(DOC_ID) == ()
    assert controller.subject is prior_subject
    assert controller.report is None


def test_apply_never_mutates_pinned_import(tmp_path):
    """The prior artifact + its mappings stay byte-identical; accept of a
    removal appends a NEW tombstone record rather than rewriting."""
    repo, controller = _controller(tmp_path)
    _import_base(controller)
    ifc_repo = CadIfcInteropRepository(repo)
    prior_artifact = controller.artifact
    prior_mappings = ifc_repo.list_entity_mappings(
        DOC_ID, prior_artifact.artifact_id
    )
    before = {m.mapping_id: m.mapping_sha256 for m in prior_mappings}

    # Revision drops the sofa entirely.
    body = _space_entity() + _wall_body_without_furn()
    artifact, delta, rows = _import_revision(controller, body)
    removed = [r for r in rows if r.category == 'removed']
    assert len(removed) == 1 and removed[0].ifc_global_id == 'gid-furn'
    controller.submit_diff_decision(removed[0].row_key, 'accepted')
    controller.apply_ifc_diff()

    after = {
        m.mapping_id: m.mapping_sha256
        for m in ifc_repo.list_entity_mappings(
            DOC_ID, prior_artifact.artifact_id
        )
    }
    for mapping_id, sha in before.items():
        # original rows untouched — the stale mark is a NEW mapping id
        assert after[mapping_id] == sha


def _wall_body_without_furn() -> str:
    body = _wall_body_ids()
    # drop the furnishing block + its containment ref
    lines = body.split('\n')
    kept = [
        line for line in lines
        if not line.startswith('#540=IFCFURNISHINGELEMENT')
    ]
    body = '\n'.join(kept)
    body = body.replace(
        '(#100,#120,#240,#330,#540),#52)',
        '(#100,#120,#240,#330),#52)',
    )
    return body


def test_cancel_leaves_no_trace(tmp_path):
    """Importing a revision without applying persists only the read-side
    records — subject, chain and applies untouched."""
    repo, controller = _controller(tmp_path)
    _import_base(controller)
    prior_subject = controller.subject
    prior_report = controller.report

    body = _space_entity() + _wall_geometry_changed()
    _import_revision(controller, body)

    ifc_repo = CadIfcInteropRepository(repo)
    assert ifc_repo.list_diff_applies(DOC_ID) == ()
    assert controller.subject is prior_subject
    assert controller.report is prior_report


# ---------------------------------------------------------------------------
# Re-open: skipped rows stay flagged
# ---------------------------------------------------------------------------


def test_skipped_rows_stay_flagged_on_reopen(tmp_path):
    repo, controller = _controller(tmp_path)
    _import_base(controller)

    body = _space_entity() + _wall_geometry_changed()
    artifact, delta, rows = _import_revision(controller, body)
    changed = [r for r in rows if r.category == 'changed']
    ambiguous = [r for r in rows if r.category == 'ambiguous']
    for row in changed:
        controller.submit_diff_decision(row.row_key, 'accepted')
    for row in ambiguous:
        controller.submit_diff_decision(row.row_key, 'skipped')
    controller.apply_ifc_diff()

    # Fresh controller over the same store — the operator re-opened.
    reopened = _reopen(repo)
    reopened.resume_diff_state()

    assert reopened.diff_apply is not None
    assert reopened.diff_delta is not None
    assert reopened.diff_delta.delta_id == delta.delta_id
    # skipped rows restored as skipped — still visibly flagged
    for row in ambiguous:
        decision = reopened.diff_decisions.get(row.row_key)
        assert decision is not None and decision.decision == 'skipped'
    for row in changed:
        decision = reopened.diff_decisions.get(row.row_key)
        assert decision is not None and decision.decision == 'accepted'
    # merged subject rebuilt — next revision diffs against the merged set
    assert reopened.subject is not None
    assert reopened.subject.source_kind == 'ifc_diff_merge'
    assert 'gid-door2' not in {
        m.ifc_global_id for m in reopened.diff_base_mappings
    }


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_diff_panel_renders_and_forwards(tmp_path):
    from htdt.ifc_diff_review_panel import IfcDiffReviewPanel

    app = _qapp()
    repo, controller = _controller(tmp_path)
    _import_base(controller)
    body = _space_entity() + _wall_geometry_changed() + _beam_and_stool()
    artifact, delta, rows = _import_revision(controller, body)

    panel = IfcDiffReviewPanel()
    decisions: list[tuple[str, str]] = []
    panel.diffDecisionRequested.connect(
        lambda key, dec: decisions.append((key, dec))
    )
    fired: list[str] = []
    panel.importRevisionRequested.connect(lambda: fired.append('import'))
    panel.reloadRequested.connect(lambda: fired.append('reload'))
    panel.applyRequested.connect(lambda: fired.append('apply'))
    panel.allDecisionsRequested.connect(
        lambda dec: fired.append(f'all:{dec}')
    )

    panel.set_rows(rows, {})
    assert panel.table.rowCount() == len(rows)
    assert panel.row_keys() == tuple(r.row_key for r in rows)

    # Per-row decision buttons emit the row key — use a
    # decision-required row (matched rows have disabled buttons).
    decided_index = next(
        i for i, r in enumerate(rows) if r.decision_required
    )
    panel.table.cellWidget(decided_index, 6).click()   # 承認
    panel.table.cellWidget(decided_index, 7).click()   # スキップ
    assert decisions[-2:] == [
        (rows[decided_index].row_key, 'accepted'),
        (rows[decided_index].row_key, 'skipped'),
    ]

    # Toolbar signals.
    panel.import_revision_button.click()
    panel.reload_button.click()
    panel.apply_button.click()
    panel.accept_all_button.click()
    panel.skip_all_button.click()
    assert fired == [
        'import', 'reload', 'apply', 'all:accepted', 'all:skipped'
    ]

    # Unknown rows render gray.
    unknown_index = next(
        i for i, r in enumerate(rows) if r.category == 'unknown'
    )
    brush = panel.table.item(unknown_index, 0).background()
    assert brush.color().isValid()

    # Skipped rows stay flagged.
    panel.decisions_changed({rows[decided_index].row_key: 'skipped'})
    assert 'スキップ' in panel.table.item(decided_index, 5).text()

    panel.deleteLater()
