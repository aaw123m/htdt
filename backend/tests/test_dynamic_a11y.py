"""#975 — dynamic-UI accessibility: focus retention, announcements,
and on-screen disabled reasons for the new panels.

The fake screen-reader sink records every ``QAccessibleAnnouncementEvent``
a surface emits — required notices must arrive exactly once per
meaningful change (no polling/cursor-move spam), and a disabled primary
action must explain why + how to resolve it on the same screen (the
button itself leaves the Tab order, so the reason can never live on it
alone).
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.usefixtures('qapp')


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def announcement_sink(monkeypatch):
    """Capture every accessible announcement a widget emits."""
    from PySide6.QtGui import QAccessible, QAccessibleAnnouncementEvent

    events = []
    monkeypatch.setattr(
        QAccessible,
        'updateAccessibility',
        lambda event: (
            events.append(event)
            if isinstance(event, QAccessibleAnnouncementEvent)
            else None
        ),
    )
    return events


def _announcements(events):
    return [event.message() for event in events]


# ---------------------------------------------------------------------
# Contract layer
# ---------------------------------------------------------------------


def test_disabled_reason_requires_readable_resolution() -> None:
    from htdt.native_accessibility import DisabledControlReason

    reason = DisabledControlReason(
        control_id='c1',
        reason='診断を実行できません: 対象がありません',
        resolution='IFCをインポートしてください',
    )
    assert reason.reason in reason.display_text()
    assert reason.resolution in reason.display_text()
    assert '解消' in reason.display_text()

    with pytest.raises(Exception):
        DisabledControlReason(
            control_id='c2', reason='ErrObj', resolution=None
        )
    with pytest.raises(Exception):
        DisabledControlReason(
            control_id='c3',
            reason='できません: 対象なし',
            resolution='fix',
        )


def test_block_events_are_in_the_required_vocabulary() -> None:
    from htdt.native_accessibility import (
        REQUIRED_ANNOUNCEMENTS,
        StatusAnnouncement,
        requires_announcement,
    )

    for event in ('operation_blocked', 'operation_unblocked'):
        assert requires_announcement(event)
        StatusAnnouncement(event=event, text='テスト')
    assert 'operation_blocked' in REQUIRED_ANNOUNCEMENTS


def test_announcer_fires_once_per_state_transition(qapp, announcement_sink) -> None:
    from PySide6.QtWidgets import QWidget

    from htdt.dynamic_a11y import DynamicAnnouncer

    widget = QWidget()
    announcer = DynamicAnnouncer(widget)
    for _ in range(3):
        announcer.announce_state(
            'verdict', 'srv-1|current', 'operation_completed', '判定: 対応'
        )
    assert _announcements(announcement_sink) == ['判定: 対応']
    assert [a.event for a in announcer.events] == ['operation_completed']

    # a state transition announces again — same aspect, new state
    announcer.announce_state(
        'verdict', 'srv-1|stale', 'result_stale', '証拠が最新ではありません'
    )
    # clearing the aspect stays silent and re-arms the transition
    announcer.announce_state('verdict', None, 'operation_completed', '')
    announcer.announce_state(
        'verdict', 'srv-1|current', 'operation_completed', '判定: 対応'
    )
    assert _announcements(announcement_sink) == [
        '判定: 対応',
        '証拠が最新ではありません',
        '判定: 対応',
    ]
    widget.deleteLater()


def test_announcer_urgent_maps_to_assertive(qapp, announcement_sink) -> None:
    from PySide6.QtGui import QAccessible
    from PySide6.QtWidgets import QWidget

    from htdt.dynamic_a11y import DynamicAnnouncer

    widget = QWidget()
    announcer = DynamicAnnouncer(widget)
    announcer.announce('operation_completed', '完了')
    announcer.announce('operation_failed', '失敗', urgent=True)
    assert announcement_sink[0].politeness() == (
        QAccessible.AnnouncementPoliteness.Polite
    )
    assert announcement_sink[1].politeness() == (
        QAccessible.AnnouncementPoliteness.Assertive
    )
    widget.deleteLater()


def _activate(widget) -> None:
    """Offscreen QPA never activates a shown window on its own —
    ``QApplication.setActiveWindow`` is what makes setFocus() land."""
    from PySide6.QtWidgets import QApplication

    widget.show()
    QApplication.setActiveWindow(widget)
    (QApplication.instance() or QApplication([])).processEvents()


def test_focus_token_restores_same_item_on_table(qapp) -> None:
    from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QWidget, QVBoxLayout

    from htdt.dynamic_a11y import capture_focus, restore_focus

    host = QWidget()
    QVBoxLayout(host)
    table = QTableWidget(0, 1, host)
    table.setObjectName('probe-table')
    host.layout().addWidget(table)
    for row in range(3):
        table.insertRow(row)
        item = QTableWidgetItem(f'row {row}')
        item.setData(0x0100, f'id-{row}')  # Qt.UserRole
        table.setItem(row, 0, item)
    _activate(host)
    table.setFocus()
    table.setCurrentCell(1, 0)
    assert qapp.focusWidget() is table

    token = capture_focus(host)
    assert token is not None and token.is_view and token.item_id == 'id-1'

    # simulate a rebuild: same rows, fresh items
    table.setRowCount(0)
    for row in range(3):
        table.insertRow(row)
        item = QTableWidgetItem(f'row {row}')
        item.setData(0x0100, f'id-{row}')
        table.setItem(row, 0, item)

    restored = restore_focus(host, token)
    assert restored is table
    assert table.currentRow() == 1
    assert table.currentItem().data(0x0100) == 'id-1'
    host.deleteLater()


def test_restore_focus_keeps_keyboard_on_view_when_item_vanished(qapp) -> None:
    from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QWidget, QVBoxLayout

    from htdt.dynamic_a11y import capture_focus, restore_focus

    host = QWidget()
    QVBoxLayout(host)
    table = QTableWidget(0, 1, host)
    table.setObjectName('probe-table')
    host.layout().addWidget(table)
    table.insertRow(0)
    item = QTableWidgetItem('only')
    item.setData(0x0100, 'gone-id')
    table.setItem(0, 0, item)
    _activate(host)
    table.setFocus()
    table.setCurrentCell(0, 0)

    token = capture_focus(host)
    table.setRowCount(0)
    table.insertRow(0)
    item = QTableWidgetItem('replacement')
    item.setData(0x0100, 'other-id')
    table.setItem(0, 0, item)

    restored = restore_focus(host, token)
    # the vanished row is not re-pointed at a different record —
    # the keyboard anchor stays on the view so arrows re-anchor.
    assert restored is table
    assert qapp.focusWidget() is table
    host.deleteLater()


def test_restore_focus_resolves_rebuilt_widget_by_name(qapp) -> None:
    from PySide6.QtWidgets import QPushButton, QVBoxLayout, QWidget

    from htdt.dynamic_a11y import capture_focus, restore_focus

    host = QWidget()
    layout = QVBoxLayout(host)
    first = QPushButton('one', host)
    first.setObjectName('act:thing-1')
    layout.addWidget(first)
    _activate(host)
    first.setFocus()
    token = capture_focus(host)

    layout.removeWidget(first)
    first.deleteLater()
    from PySide6.QtCore import QEvent

    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    second = QPushButton('one', host)
    second.setObjectName('act:thing-1')
    layout.addWidget(second)
    second.show()

    assert restore_focus(host, token) is second
    assert qapp.focusWidget() is second
    host.deleteLater()


def test_capture_focus_ignores_focus_outside_the_surface(qapp) -> None:
    from PySide6.QtWidgets import QPushButton, QVBoxLayout, QWidget

    from htdt.dynamic_a11y import capture_focus

    inside = QWidget()
    QVBoxLayout(inside)
    outside = QWidget()
    outer_layout = QVBoxLayout(outside)
    outer_button = QPushButton('out', outside)
    outer_layout.addWidget(outer_button)
    _activate(outside)
    outer_button.setFocus()
    assert capture_focus(inside) is None
    inside.deleteLater()
    outside.deleteLater()


# ---------------------------------------------------------------------
# GeometryIntakePanel
# ---------------------------------------------------------------------


def _intake_report(document_id='a11y-doc'):
    from test_issue_866_geometry_intake import (
        _mesh,
        _NONMANIFOLD_OBJ,
        _part,
        _report,
        _subject,
    )

    subject = _subject(
        parts=(_part(_mesh(_NONMANIFOLD_OBJ), part_id='room-boundary'),),
        document_id=document_id,
    )
    return subject, _report(subject)


def test_geometry_stage_hint_shows_reason_and_resolution(qapp) -> None:
    from PySide6.QtCore import Qt

    from htdt.geometry_intake_panel import GeometryIntakePanel

    panel = GeometryIntakePanel()
    panel.set_stage(has_subject=False, derive_enabled=False)
    assert not panel.diagnose_button.isEnabled()
    assert not panel.derive_button.isEnabled()
    text = panel.stage_hint.text()
    assert panel.stage_hint.isVisibleTo(panel) or not panel.isVisible()
    assert '診断を実行できません' in text
    assert '派生リビジョンを生成できません' in text
    assert '解消' in text
    # keyboard users can reach the reason — the label takes Tab focus
    assert panel.stage_hint.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert panel.diagnose_button.toolTip()
    assert panel.derive_button.toolTip()

    panel.set_stage(has_subject=True, derive_enabled=True)
    assert panel.diagnose_button.isEnabled()
    assert panel.derive_button.isEnabled()
    assert panel.stage_hint.text() == ''
    assert not panel.stage_hint.isVisibleTo(panel)
    panel.deleteLater()


def test_geometry_report_announces_once(qapp, announcement_sink) -> None:
    from htdt.geometry_intake_panel import GeometryIntakePanel

    _subject, report = _intake_report()
    panel = GeometryIntakePanel()
    panel.set_report(report)
    panel.set_report(report)  # routine re-sync — silent
    panel.set_report(report)
    assert _announcements(announcement_sink) == [
        f'診断が完了しました — 欠陥 {len(report.defects)} 件'
    ]
    panel.deleteLater()


def test_geometry_verdict_stale_transition_announces(qapp, announcement_sink) -> None:
    from test_issue_866_geometry_intake import (
        _adapter,
        evaluate_solver_readiness,
        UTC,
    )

    from htdt.geometry_intake_panel import GeometryIntakePanel

    subject, report = _intake_report()
    verdict = evaluate_solver_readiness(
        subject=subject,
        report=report,
        adapter_descriptor=_adapter('geometric'),
        evaluated_at_utc=UTC,
    )
    panel = GeometryIntakePanel()
    panel.set_verdict(verdict, evidence_state='current')
    panel.set_verdict(verdict, evidence_state='current')
    assert len(_announcements(announcement_sink)) == 1
    panel.set_verdict(verdict, evidence_state='stale_geometry')
    messages = _announcements(announcement_sink)
    assert len(messages) == 2
    assert '最新ではありません' in messages[-1]
    assert panel._announcer.events[-1].event == 'result_stale'
    panel.deleteLater()


def test_geometry_focus_survives_defect_rebuild(qapp) -> None:
    from htdt.geometry_intake_panel import GeometryIntakePanel

    _subject, report = _intake_report()
    panel = GeometryIntakePanel()
    _activate(panel)
    panel.set_report(report)
    assert panel.source_table.rowCount() >= 1
    panel.source_table.setFocus()
    panel.source_table.setCurrentCell(0, 0)
    defect_id = (
        panel.source_table.item(0, 0).data(
            __import__('htdt.dynamic_a11y', fromlist=['STABLE_ID_ROLE'])
            .STABLE_ID_ROLE
        )
    )

    panel.set_report(report)  # full rebuild of both tables
    current = panel.source_table.currentItem()
    assert current is not None
    assert current.data(
        __import__('htdt.dynamic_a11y', fromlist=['STABLE_ID_ROLE'])
        .STABLE_ID_ROLE
    ) == defect_id
    assert qapp.focusWidget() is panel.source_table
    panel.deleteLater()


def test_geometry_mark_decided_announces_save_state(qapp, announcement_sink) -> None:
    from test_issue_866_geometry_intake import propose_geometry_repairs, UTC

    from htdt.geometry_intake_panel import GeometryIntakePanel

    subject, report = _intake_report()
    proposal = propose_geometry_repairs(
        report, subject, proposed_by='op', proposed_at_utc=UTC
    )
    panel = GeometryIntakePanel()
    panel.set_report(report)
    panel.set_proposal(proposal)
    before = len(announcement_sink)
    action = proposal.actions[0]
    panel.mark_decided(action.action_id)
    panel.mark_decided(action.action_id)  # idempotent — no second notice
    messages = _announcements(announcement_sink[before:])
    assert messages == ['修復提案への決定を記録しました']
    assert panel._announcer.events[-1].event == 'save_state'
    panel.deleteLater()


def test_geometry_proposal_focus_returns_to_same_action_button(qapp) -> None:
    from test_issue_866_geometry_intake import propose_geometry_repairs, UTC

    from htdt.geometry_intake_panel import GeometryIntakePanel

    subject, report = _intake_report()
    proposal = propose_geometry_repairs(
        report, subject, proposed_by='op', proposed_at_utc=UTC
    )
    panel = GeometryIntakePanel()
    _activate(panel)
    panel.set_report(report)
    panel.set_proposal(proposal)
    qapp.processEvents()

    action = proposal.actions[0]
    from PySide6.QtWidgets import QPushButton

    accept = panel.findChild(
        QPushButton, f'intake-accept:{action.action_id}'
    )
    assert accept is not None
    accept.setFocus()
    assert qapp.focusWidget() is accept

    # rebuild with the same proposal — the old button is destroyed and
    # focus must re-resolve to the NEW button carrying the stable name.
    from shiboken6 import isValid

    panel.set_proposal(proposal)
    qapp.processEvents()
    assert not isValid(accept)
    rebuilt = panel.findChild(
        QPushButton, f'intake-accept:{action.action_id}'
    )
    assert rebuilt is not None and isValid(rebuilt)
    assert qapp.focusWidget() is rebuilt
    panel.deleteLater()


# ---------------------------------------------------------------------
# DecisionBriefPanel
# ---------------------------------------------------------------------


def _brief_env(tmp_path):
    from test_issue_950_decision_brief_evidence import _Env

    env = _Env(tmp_path)
    env.add_all()
    env.comparison()
    return env


def _brief_panel(env, on_navigate=None):
    from test_issue_950_decision_brief_evidence import DOC, _compose
    from htdt.cad_decision_brief_repository import CadDecisionBriefRepository
    from htdt.decision_brief_panel import DecisionBriefPanel

    env.briefs.save_brief(_compose(env, env.gates()))
    brief_repository = CadDecisionBriefRepository(
        env.scene_repository,
        system_variant_repository=env.variants,
        kind_resolvers=env.resolver.kind_resolvers(),
    )
    return DecisionBriefPanel(
        env.scene_repository,
        DOC,
        brief_repository=brief_repository,
        comparison_repository=env.comparisons,
        on_navigate=on_navigate,
    )


def test_brief_unroutable_action_explains_reason_on_screen(
    tmp_path, qapp,
) -> None:
    from PySide6.QtCore import Qt

    env = _brief_env(tmp_path)
    panel = _brief_panel(env, on_navigate=None)
    from test_issue_950_decision_brief_evidence import DOC

    action = env.briefs.latest_brief(DOC).actions[0]
    from PySide6.QtWidgets import QPushButton

    button = panel.findChild(
        QPushButton, f'brief-next:{action.action_id}'
    )
    assert button is not None and not button.isEnabled()
    # a disabled button leaves the Tab order — the reason must live in
    # a focusable label on the same card.
    hint = panel.findChild(
        __import__('PySide6.QtWidgets', fromlist=['QLabel']).QLabel,
        f'brief-hint:{action.action_id}',
    )
    assert hint is not None
    assert 'まだ接続されていません' in hint.text()
    assert '解消' in hint.text()
    assert hint.focusPolicy() == Qt.FocusPolicy.StrongFocus
    panel.deleteLater()


def test_brief_refresh_keeps_focus_on_same_action_card(
    tmp_path, qapp, announcement_sink,
) -> None:
    from PySide6.QtWidgets import QPushButton

    from test_issue_950_decision_brief_evidence import DOC

    env = _brief_env(tmp_path)
    panel = _brief_panel(env, on_navigate=lambda link: True)
    _activate(panel)
    action = env.briefs.latest_brief(DOC).actions[0]
    button = panel.findChild(
        QPushButton, f'brief-next:{action.action_id}'
    )
    assert button is not None and button.isEnabled()
    button.setFocus()

    before = len(announcement_sink)
    panel.refresh()  # destroys and rebuilds every card
    from shiboken6 import isValid

    assert not isValid(button)
    rebuilt = panel.findChild(
        QPushButton, f'brief-next:{action.action_id}'
    )
    assert rebuilt is not None and isValid(rebuilt)
    assert qapp.focusWidget() is rebuilt
    # re-rendering the same brief is not a meaningful change — silent
    assert _announcements(announcement_sink[before:]) == []
    panel.deleteLater()


def test_brief_stale_transition_announces_result_stale(
    tmp_path, qapp, announcement_sink,
) -> None:
    from test_issue_950_decision_brief_evidence import DOC

    from test_issue_950_decision_brief_evidence import _scene

    env = _brief_env(tmp_path)
    panel = _brief_panel(env)
    panel._announcer.events.clear()
    announcement_sink.clear()

    # a fresh scene head (different content) supersedes the brief's
    # pinned revision — saving identical content is a no-op on a
    # content-addressed repository.
    head = env.scene_repository.current_head(DOC)
    env.scene_repository.save(
        _scene(speakers=('spk-fl', 'spk-fr', 'spk-extra')),
        parent_revision_id=head.revision_id,
    )
    panel.refresh()
    events = [a.event for a in panel._announcer.events]
    assert 'result_stale' in events
    assert '最新のシーンに対応しません' in _announcements(
        announcement_sink
    )[-1]
    assert '再計算' in panel.freshness_label.text()
    panel.deleteLater()


# ---------------------------------------------------------------------
# StandardsCriterionPanel
# ---------------------------------------------------------------------


def _standards_panel(tmp_path):
    from htdt.cad_repository import SceneRepository
    from htdt.cad_scene import make_f1_scene
    from htdt.standards_workspace import StandardsCriterionPanel

    scene_repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    return StandardsCriterionPanel(
        scene_repository, revision.document_id
    )


def test_standards_tree_focus_restored_by_criterion_id(
    tmp_path, qapp,
) -> None:
    from PySide6.QtCore import Qt

    panel = _standards_panel(tmp_path)
    _activate(panel)
    assert panel.tree.topLevelItemCount() >= 2
    target = panel.tree.topLevelItem(1)
    criterion_id = target.data(0, Qt.ItemDataRole.UserRole)
    panel.tree.setFocus()
    panel.tree.setCurrentItem(target)
    qapp.processEvents()

    panel.refresh()  # tree.clear() + full rebuild
    current = panel.tree.currentItem()
    assert current is not None
    assert current.data(0, Qt.ItemDataRole.UserRole) == criterion_id
    assert qapp.focusWidget() is panel.tree
    panel.deleteLater()


def test_standards_evaluate_disabled_reason_when_no_profile(
    tmp_path, qapp, monkeypatch,
) -> None:
    panel = _standards_panel(tmp_path)
    # the honest-disabled path: no selectable profile → the button is
    # disabled AND its reason + resolution are on screen.
    monkeypatch.setattr(panel, 'selected_profile', lambda: None)
    panel._sync_evaluate_enabled()
    assert not panel.evaluate_button.isEnabled()
    assert '基準プロファイルがありません' in panel.evaluate_hint.text()
    assert '解消' in panel.evaluate_hint.text()
    assert panel.evaluate_button.toolTip() == panel.evaluate_hint.text()
    panel.deleteLater()


def test_standards_gate_announces_block_and_unblock(
    tmp_path, qapp, announcement_sink,
) -> None:
    panel = _standards_panel(tmp_path)
    panel.evaluate_selected()
    events = [a.event for a in panel._announcer.events]
    assert 'operation_completed' in events
    announcement_sink.clear()

    from PySide6.QtCore import Qt

    # check every FAIL/UNKNOWN criterion as a hard constraint → blocked
    for index in range(panel.tree.topLevelItemCount()):
        item = panel.tree.topLevelItem(index)
        item.setCheckState(0, Qt.CheckState.Checked)
    blocked = [a for a in panel._announcer.events
               if a.event == 'operation_blocked']
    assert blocked, 'a checked failing constraint must announce blocked'

    # uncheck all → unblocked
    for index in range(panel.tree.topLevelItemCount()):
        item = panel.tree.topLevelItem(index)
        item.setCheckState(0, Qt.CheckState.Unchecked)
    unblocked = [a for a in panel._announcer.events
                 if a.event == 'operation_unblocked']
    assert unblocked, 'clearing the block must announce unblocked'
    panel.deleteLater()


# ---------------------------------------------------------------------
# MeasurementPageWorkspace — quality surface
# ---------------------------------------------------------------------


def _quality_workspace(tmp_path, specs=()):
    from test_measurement_rev72_quality_table import _workspace

    workspace, _saved = _workspace(tmp_path, specs)
    return workspace


def test_quality_actions_disabled_with_reason_when_no_selection(
    tmp_path, qapp,
) -> None:
    workspace = _quality_workspace(tmp_path)
    workspace.refresh()
    assert workspace.quality_table.rowCount() == 0
    assert workspace._quality_announcer is not None
    for button in (
        workspace.retake_button,
        workspace.disposition_apply_button,
        workspace.correct_button,
        workspace.attach_button,
    ):
        assert not button.isEnabled()
        assert button.toolTip()
    assert '測定' in workspace.disposition_label.text()
    workspace.close()
    workspace.deleteLater()


def test_quality_focus_stays_on_table_when_selection_vanishes(
    tmp_path, qapp,
) -> None:
    workspace = _quality_workspace(
        tmp_path,
        (
            {'measurement_id': 'm-a'},
            {'measurement_id': 'm-b', 'channel_role': 'center'},
        ),
    )
    _activate(workspace)
    workspace.refresh()
    table = workspace.quality_table
    assert table.rowCount() >= 2
    table.selectRow(0)
    table.setFocus()

    # filter to a channel that hides the selected row → it vanishes
    from test_measurement_rev72_quality_table import _set_combo_data

    _set_combo_data(workspace.quality_channel_filter, 'center')
    assert qapp.focusWidget() is table
    assert workspace.quality_selection_note.text()
    workspace.quality_filter_clear.click()
    qapp.processEvents()
    assert qapp.focusWidget() is table
    workspace.close()
    workspace.deleteLater()


def test_quality_retake_announces_retake_required(
    tmp_path, qapp, announcement_sink,
) -> None:
    workspace = _quality_workspace(
        tmp_path, ({'measurement_id': 'm-retake'},)
    )
    workspace.refresh()
    workspace.quality_table.selectRow(0)
    qapp.processEvents()
    before = len(announcement_sink)
    workspace._start_retake()
    messages = _announcements(announcement_sink[before:])
    assert messages and '再測定' in messages[-1]
    assert workspace._quality_announcer.events[-1].event == (
        'retake_required'
    )
    workspace.close()
    workspace.deleteLater()
