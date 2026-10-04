"""REV44-HEALTHSYNC — record-entry surfaces for AV sync and system health.

rev43-producers finding 7: the AV-sync condition → measurement chain and the
health baseline → plan → run families shipped complete models + repositories
+ consumers but had ZERO production writers — the 稼働状況 overview domain
could never leave 未チェック and the ``av_sync_recorded`` /
``health_baseline_created`` activity kinds were permanently dead.

These tests drive the new record-entry dialogs (``measurement_record_surfaces``,
hosted on the measurement workspace 品質 context) against the real
persisted stack: register → readiness domain lights; cancel → nothing;
invalid input → typed error, nothing saved.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest  # noqa: E402

from htdt.application_preferences import (  # noqa: E402
    ApplicationPreferenceStore,
)
from htdt.cad_av_sync_repository import CadAVSyncRepository  # noqa: E402
from htdt.cad_project_activity import CadProjectActivityService  # noqa: E402
from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.cad_system_health_repository import (  # noqa: E402
    CadSystemHealthRepository,
)
from htdt.measurement_record_surfaces import (  # noqa: E402
    AVSyncRecordDialog,
    HealthCheckDialog,
)
from htdt.navigation_target import (  # noqa: E402
    NavigationResolver,
    NavigationTarget,
    NavigationTargetKind,
    navigation_target_from_uri,
)
from htdt.workflow_application import (  # noqa: E402
    WorkflowApplicationComposition,
)
from htdt.workflow_navigation import WorkspaceId  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402

from test_cad_system_health import _scene  # noqa: E402
from test_overview_secondary_domains import _domain, _service  # noqa: E402


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _repository(tmp_path: Path):
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(_scene(), parent_revision_id=None).revision
    return repository, revision


def _health_dialog(repository, document_id: str) -> HealthCheckDialog:
    _app()
    return HealthCheckDialog(
        scene_repository=repository, document_id=document_id
    )


def _av_dialog(repository, document_id: str) -> AVSyncRecordDialog:
    _app()
    return AVSyncRecordDialog(
        scene_repository=repository, document_id=document_id
    )


def _register_baseline(dialog: HealthCheckDialog) -> None:
    """Drive the baseline form exactly as the surface presents it."""
    dialog.pin_key_edit.setText('mlp_level_db')
    dialog.pin_expected_edit.setText('82.0')
    dialog.pin_tolerance_edit.setText('0.5')
    dialog._add_pin()
    dialog.baseline_name_edit.setText('試験ベースライン')
    dialog._register_baseline()


def _build_plan(dialog: HealthCheckDialog) -> None:
    dialog.plan_baseline_combo.setCurrentIndex(0)
    dialog.check_description_edit.setText('FLレベルを確認')
    dialog._add_check_item()
    dialog._save_plan()


def _pick_first_evidence(combo) -> None:
    """Pick the first persisted-authority option (index 0 is the skip row)."""
    for index in range(combo.count()):
        if combo.itemData(index) is not None:
            combo.setCurrentIndex(index)
            return
    raise AssertionError('no persisted authority option offered')


def _record_run(dialog: HealthCheckDialog, observed: str = '82.0') -> None:
    dialog.run_plan_combo.setCurrentIndex(0)
    row = dialog._run_rows[0]
    _pick_first_evidence(row['evidence'])
    assert row['pins'], 'check has no related pin field'
    row['pins'][0][1].setText(observed)
    dialog.run_repeatability_check.setChecked(True)
    dialog._record_run()


# ---------------------------------------------------------------------------
# Health baseline → plan → run → overview domain
# ---------------------------------------------------------------------------


def test_health_surface_registers_and_readiness_lights(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    document_id = revision.document_id
    dialog = _health_dialog(repository, document_id)
    try:
        _register_baseline(dialog)
        baselines = dialog.health_repository.list_baselines(document_id)
        assert len(baselines) == 1
        assert baselines[0].name == '試験ベースライン'
        assert baselines[0].scene_revision_id == revision.revision_id
        assert baselines[0].metric_pins[0].expected_repr == (
            '{"value_db":82.0}'
        )

        _build_plan(dialog)
        plans = dialog.health_repository.list_plans(baselines[0].baseline_id)
        assert len(plans) == 1
        assert plans[0].checks[0].related_pin_ids == (
            baselines[0].metric_pins[0].pin_id,
        )

        _record_run(dialog)
        runs = dialog.health_repository.list_document_runs(document_id)
        assert len(runs) == 1
        assert runs[0].assessments[0].state == 'within_baseline'
        # The evidence pick is a real persisted authority — the scene
        # revision this project is currently pinned to.
        evidence = runs[0].observations[0].evidence_ref
        assert evidence.kind == 'scene_revision'
        assert evidence.ref_id == revision.revision_id

        # The overview domain lights up for real.
        view = _service(revision, health_source=dialog.health_repository).read(
            document_id
        )
        domain = _domain(view, 'health')
        assert domain is not None
        assert domain.state_label == 'ベースライン内'
        assert domain.action is not None
        assert domain.action.target.workspace == WorkspaceId.MEASUREMENT
        assert domain.action.target.section == 'quality'
    finally:
        dialog.deleteLater()


def test_health_domain_lights_changed_on_drift(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    document_id = revision.document_id
    dialog = _health_dialog(repository, document_id)
    try:
        _register_baseline(dialog)
        _build_plan(dialog)
        _record_run(dialog, observed='84.0')
        runs = dialog.health_repository.list_document_runs(document_id)
        assert runs[-1].assessments[0].state == 'changed'
        view = _service(revision, health_source=dialog.health_repository).read(
            document_id
        )
        domain = _domain(view, 'health')
        assert domain.state_label == '変化あり'
        assert '変化 1件' in domain.detail
    finally:
        dialog.deleteLater()


def test_health_cancel_persists_nothing(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    document_id = revision.document_id
    dialog = _health_dialog(repository, document_id)
    try:
        dialog.pin_key_edit.setText('mlp_level_db')
        dialog.pin_expected_edit.setText('82.0')
        dialog._add_pin()
        dialog.baseline_name_edit.setText('破棄するベースライン')
        dialog.reject()
        health = CadSystemHealthRepository(repository)
        assert health.list_baselines(document_id) == ()
        assert health.list_document_runs(document_id) == ()
        view = _service(revision, health_source=health).read(document_id)
        assert _domain(view, 'health').state_label == '未チェック'
    finally:
        dialog.deleteLater()


def test_health_invalid_inputs_fail_closed(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    document_id = revision.document_id
    dialog = _health_dialog(repository, document_id)
    try:
        # Missing metric key → typed status, nothing staged.
        dialog.pin_expected_edit.setText('82.0')
        dialog._add_pin()
        assert 'メトリクスキー' in dialog.status_label.text()
        assert dialog._pins == []

        # Non-numeric expected value → typed status, nothing staged.
        dialog.pin_key_edit.setText('mlp_level_db')
        dialog.pin_expected_edit.setText('abc')
        dialog._add_pin()
        assert '数値' in dialog.status_label.text()
        assert dialog._pins == []

        # Missing name → typed status, nothing persisted.
        dialog.pin_expected_edit.setText('82.0')
        dialog._add_pin()
        dialog._register_baseline()
        assert 'ベースライン名' in dialog.status_label.text()
        assert dialog.health_repository.list_baselines(document_id) == ()

        # A bad observed value fails closed — the run never lands.
        dialog.baseline_name_edit.setText('試験')
        dialog._register_baseline()
        _build_plan(dialog)
        dialog.run_plan_combo.setCurrentIndex(0)
        row = dialog._run_rows[0]
        _pick_first_evidence(row['evidence'])
        row['pins'][0][1].setText('not-a-number')
        dialog._record_run()
        assert '数値' in dialog.status_label.text()
        assert dialog.health_repository.list_document_runs(document_id) == ()
    finally:
        dialog.deleteLater()


def test_health_activity_events_from_real_saves(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    document_id = revision.document_id
    dialog = _health_dialog(repository, document_id)
    try:
        _register_baseline(dialog)
        _build_plan(dialog)
        _record_run(dialog)
        service = CadProjectActivityService(
            scene_repository=repository,
            health_repository=dialog.health_repository,
        )
        kinds = {event.kind for event in service.events(document_id)}
        assert 'health_baseline_created' in kinds
        assert 'health_check_completed' in kinds
        baseline_event = next(
            event
            for event in service.events(document_id)
            if event.kind == 'health_baseline_created'
        )
        # The deep link resolves through the activity surface and routes to
        # the measurement quality context where the record lives.
        target = navigation_target_from_uri(baseline_event.deep_link)
        resolution = NavigationResolver().resolve(
            target,
            registered={WorkspaceId.MEASUREMENT},
            capabilities={
                WorkspaceId.MEASUREMENT: frozenset(
                    {NavigationTargetKind.HEALTH_BASELINE}
                )
            },
        )
        assert resolution.link.workspace == WorkspaceId.MEASUREMENT
        assert resolution.link.section == 'quality'
        assert resolution.status == 'focused'
    finally:
        dialog.deleteLater()


# ---------------------------------------------------------------------------
# AV sync condition → measurement chain
# ---------------------------------------------------------------------------


def test_av_sync_condition_and_chain(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    document_id = revision.document_id
    dialog = _av_dialog(repository, document_id)
    try:
        # Auto-derived pins: current head revision; no operating state.
        assert dialog._head.revision_id == revision.revision_id
        assert dialog._operating_state is None

        dialog.display_device_edit.setText('projector-x')
        dialog.video_mode_edit.setText('game')
        dialog.refresh_rate_edit.setText('120')
        dialog.audio_path_edit.setText('earc')
        dialog.lip_sync_edit.setText('15')
        dialog._register_condition()

        conditions = dialog.av_sync_repository.list_conditions(document_id)
        assert len(conditions) == 1
        condition = conditions[0]
        assert condition.scene_revision_id == revision.revision_id
        assert condition.scene_revision_sha256 == revision.content_hash
        assert condition.display_device_id == 'projector-x'
        assert condition.source_device_id == 'unknown'  # honest unknown

        # First record must be the chain head at 'measured'.
        dialog.conditions.topLevelItem(0).setSelected(True)
        assert dialog.chain_combo.currentData() is None
        assert dialog.stage_combo.currentData() == 'measured'
        dialog.measure_value_edit.setText('42')
        dialog.uncertainty_edit.setText('8')
        dialog._record_measurement()

        stages = dialog.av_sync_repository.list_measurements(
            condition.condition_id
        )
        assert len(stages) == 1
        head = stages[0]
        assert head.status == 'measured'
        assert head.measured_offset_ms == pytest.approx(42.0)
        assert head.predecessor_measurement_id is None

        # Next record pins the current head — the honest chain transition.
        dialog.measure_value_edit.setText('40')
        assert dialog.chain_combo.currentData().measurement_id == (
            head.measurement_id
        )
        assert dialog.stage_combo.currentData() == 'correction_requested'
        dialog._record_measurement()

        stages = dialog.av_sync_repository.list_measurements(
            condition.condition_id
        )
        assert len(stages) == 2
        successor = stages[1]
        assert successor.status == 'correction_requested'
        assert successor.predecessor_measurement_id == head.measurement_id
        assert successor.predecessor_sha256 == head.measurement_sha256
        assert successor.requested_correction_ms == pytest.approx(40.0)
        # The next stage is the only one offered — one head, no branching.
        assert dialog.stage_combo.currentData() == 'setting_applied'
    finally:
        dialog.deleteLater()


def test_av_sync_cancel_persists_nothing(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    document_id = revision.document_id
    dialog = _av_dialog(repository, document_id)
    try:
        dialog.display_device_edit.setText('projector-x')
        dialog.reject()
        av_sync = CadAVSyncRepository(repository)
        assert av_sync.list_conditions(document_id) == ()
    finally:
        dialog.deleteLater()


def test_av_sync_invalid_inputs_fail_closed(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    document_id = revision.document_id
    dialog = _av_dialog(repository, document_id)
    try:
        # Missing display device → typed status, nothing persisted.
        dialog._register_condition()
        assert '表示機器' in dialog.status_label.text()
        assert dialog.av_sync_repository.list_conditions(document_id) == ()

        # Non-numeric Hz field → typed status, nothing persisted.
        dialog.display_device_edit.setText('projector-x')
        dialog.refresh_rate_edit.setText('fast')
        dialog._register_condition()
        assert '数値' in dialog.status_label.text()
        assert dialog.av_sync_repository.list_conditions(document_id) == ()

        # Register for real, then a bad measurement value fails closed.
        dialog.refresh_rate_edit.setText('120')
        dialog._register_condition()
        dialog.conditions.topLevelItem(0).setSelected(True)
        dialog.measure_value_edit.setText('junk')
        dialog._record_measurement()
        assert '数値' in dialog.status_label.text()
        condition = dialog.av_sync_repository.list_conditions(document_id)[0]
        assert dialog.av_sync_repository.list_measurements(
            condition.condition_id
        ) == ()
    finally:
        dialog.deleteLater()


def test_av_sync_activity_events_from_real_saves(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    document_id = revision.document_id
    dialog = _av_dialog(repository, document_id)
    try:
        dialog.display_device_edit.setText('projector-x')
        dialog._register_condition()
        dialog.conditions.topLevelItem(0).setSelected(True)
        dialog.measure_value_edit.setText('42')
        dialog._record_measurement()
        service = CadProjectActivityService(
            scene_repository=repository,
            av_sync_repository=dialog.av_sync_repository,
        )
        events = [
            event
            for event in service.events(document_id)
            if event.kind == 'av_sync_recorded'
        ]
        assert len(events) == 1
        target = navigation_target_from_uri(events[0].deep_link)
        resolution = NavigationResolver().resolve(
            target,
            registered={WorkspaceId.MEASUREMENT},
            capabilities={
                WorkspaceId.MEASUREMENT: frozenset(
                    {NavigationTargetKind.AV_SYNC_CONDITION}
                )
            },
        )
        assert resolution.link.workspace == WorkspaceId.MEASUREMENT
        assert resolution.link.section == 'quality'
        assert resolution.status == 'focused'
    finally:
        dialog.deleteLater()


# ---------------------------------------------------------------------------
# Deep-link focus through the real measurement mount
# ---------------------------------------------------------------------------


def test_measurement_mount_focuses_record_targets(tmp_path: Path) -> None:
    _app()
    repository, revision = _repository(tmp_path)
    document_id = revision.document_id
    preferences = ApplicationPreferenceStore.for_data_dir(tmp_path / 'data')
    composition = WorkflowApplicationComposition(
        repository, document_id, preferences=preferences
    )
    try:
        mount = composition._make_measurement()
        workspace = mount.widget
        try:
            assert NavigationTargetKind.HEALTH_BASELINE in mount.focus_kinds
            assert NavigationTargetKind.AV_SYNC_CONDITION in mount.focus_kinds

            # A persisted record focuses the quality context…
            health = CadSystemHealthRepository(repository)
            dialog = _health_dialog(repository, document_id)
            try:
                _register_baseline(dialog)
            finally:
                dialog.deleteLater()
            baseline = health.list_baselines(document_id)[0]
            target = NavigationTarget(
                kind=NavigationTargetKind.HEALTH_BASELINE,
                object_ids=(baseline.baseline_id,),
            )
            result = mount.focus_target(target)
            assert result.focused
            assert workspace.current_context_id == 'quality'

            # …and a ghost record fails closed instead of navigating blind.
            ghost = NavigationTarget(
                kind=NavigationTargetKind.HEALTH_BASELINE,
                object_ids=('ghost-baseline',),
            )
            result = mount.focus_target(ghost)
            assert not result.focused
        finally:
            workspace.deleteLater()
    finally:
        composition.shell.close()
        composition.shell.deleteLater()


def test_quality_page_hosts_record_cards(tmp_path: Path) -> None:
    """The record-entry cards live on the measurement quality page."""
    _app()
    repository, revision = _repository(tmp_path)
    document_id = revision.document_id
    preferences = ApplicationPreferenceStore.for_data_dir(tmp_path / 'data')
    composition = WorkflowApplicationComposition(
        repository, document_id, preferences=preferences
    )
    try:
        mount = composition._make_measurement()
        workspace = mount.widget
        try:
            workspace.set_context('quality')
            assert workspace.av_sync_summary.text() == '条件なし'
            assert workspace.health_summary.text() == 'ベースラインなし'
            assert workspace.av_sync_button.text() == 'AV同期を記録…'
            assert workspace.health_button.text() == '健全性チェックを記録…'
        finally:
            workspace.deleteLater()
    finally:
        composition.shell.close()
        composition.shell.deleteLater()
