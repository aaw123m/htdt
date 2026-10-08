"""Issue #886 — first-run guided wizard.

Covers the pure stage derivation, the file-backed resume record, and the
dialog's rendering/navigation behavior (offscreen).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from htdt.first_run_wizard_state import (
    FirstRunWizardFacts,
    WizardStage,
    WizardStageStatus,
    derive_wizard_progress,
    first_incomplete_stage,
)
from htdt.first_run_wizard_store import (
    FirstRunWizardRecord,
    load_wizard_state,
    save_wizard_state,
    wizard_state_path,
)
from htdt.workflow_navigation import WorkspaceId


# ---------------------------------------------------------------------------
# stage derivation
# ---------------------------------------------------------------------------


def _facts(**overrides) -> FirstRunWizardFacts:
    base = dict(
        project_exists=True,
        room_saved=True,
        speakers_present=True,
        speaker_roles_ok=True,
        equipment_unresolved_count=0,
        audio_backend_available=True,
        audio_backend_reason=None,
        calibration_complete=True,
        channel_verified=True,
        baseline_measured=True,
        has_candidates=True,
        deploy_applied=True,
        deploy_readback_verified=True,
        verify_measured=True,
    )
    base.update(overrides)
    return FirstRunWizardFacts(**base)


class TestDerivation:
    def test_empty_project_first_stage_current(self):
        views = derive_wizard_progress(FirstRunWizardFacts())
        assert views[0].status is WizardStageStatus.CURRENT
        assert views[0].stage is WizardStage.PROJECT_ROOM
        # no backend on a fresh build — readiness reports blocked rather
        # than hiding that the hardware path is missing
        assert views[2].status is WizardStageStatus.BLOCKED
        assert all(
            v.status is WizardStageStatus.PENDING
            for i, v in enumerate(views[1:], start=1)
            if i != 2
        )
        assert first_incomplete_stage(views) is WizardStage.PROJECT_ROOM
        assert views[0].reason_ja == 'まだプロジェクトがありません'

    def test_project_without_room_explains_reason(self):
        views = derive_wizard_progress(
            FirstRunWizardFacts(project_exists=True)
        )
        assert views[0].status is WizardStageStatus.CURRENT
        assert '部屋' in (views[0].reason_ja or '')

    def test_exactly_one_current(self):
        facts = _facts(
            calibration_complete=False,
            channel_verified=False,
            baseline_measured=False,
            has_candidates=False,
            deploy_applied=False,
            deploy_readback_verified=False,
            verify_measured=False,
        )
        views = derive_wizard_progress(facts)
        currents = [
            v for v in views if v.status is WizardStageStatus.CURRENT
        ]
        assert len(currents) == 1
        assert currents[0].stage is WizardStage.MEASUREMENT_READINESS

    def test_missing_speakers_reason(self):
        views = derive_wizard_progress(
            _facts(speakers_present=False, speaker_roles_ok=False,
                   calibration_complete=False, channel_verified=False,
                   baseline_measured=False, has_candidates=False,
                   deploy_applied=False, verify_measured=False)
        )
        stage = views[1]
        assert stage.stage is WizardStage.SYSTEM_DEFINITION
        assert stage.status is WizardStageStatus.CURRENT
        assert 'スピーカー' in (stage.reason_ja or '')

    def test_unresolved_equipment_reason_count(self):
        views = derive_wizard_progress(
            _facts(equipment_unresolved_count=2,
                   calibration_complete=False, channel_verified=False,
                   baseline_measured=False, verify_measured=False)
        )
        stage = views[1]
        assert stage.status is WizardStageStatus.CURRENT
        assert '2 件' in (stage.reason_ja or '')

    def test_backend_unavailable_blocks_readiness(self):
        views = derive_wizard_progress(
            _facts(audio_backend_available=False,
                   audio_backend_reason='wasapi stub',
                   calibration_complete=False, channel_verified=False,
                   baseline_measured=False, verify_measured=False)
        )
        stage = views[2]
        assert stage.stage is WizardStage.MEASUREMENT_READINESS
        assert stage.status is WizardStageStatus.BLOCKED
        assert 'オーディオバックエンド' in (stage.reason_ja or '')
        assert 'wasapi stub' in (stage.reason_ja or '')
        # a blocked stage never claims the current slot — stage order
        # keeps stage 3 as the resume target
        assert first_incomplete_stage(views) is stage.stage

    def test_readiness_missing_pieces_listed(self):
        views = derive_wizard_progress(
            _facts(calibration_complete=False, channel_verified=False,
                   baseline_measured=False, verify_measured=False)
        )
        reason = views[2].reason_ja or ''
        assert '校正' in reason and 'チャンネル検証' in reason

    def test_deploy_applied_needs_readback_with_candidates(self):
        views = derive_wizard_progress(
            _facts(deploy_readback_verified=False, verify_measured=False)
        )
        stage = views[4]
        assert stage.status is WizardStageStatus.CURRENT
        assert '適用' in (stage.reason_ja or '')

    def test_no_candidates_deploy_pending_reason(self):
        views = derive_wizard_progress(
            _facts(has_candidates=False, deploy_applied=False,
                   deploy_readback_verified=False, verify_measured=False)
        )
        stage = views[4]
        assert '候補' in (stage.reason_ja or '')

    def test_all_complete(self):
        views = derive_wizard_progress(_facts())
        assert all(
            v.status is WizardStageStatus.COMPLETE for v in views
        )
        assert first_incomplete_stage(views) is None

    def test_stage_order_and_targets(self):
        views = derive_wizard_progress(FirstRunWizardFacts())
        assert [v.stage for v in views] == list(WizardStage)
        assert views[0].target is not None
        assert views[0].target.workspace is WorkspaceId.ROOM
        assert views[2].target is not None
        assert views[2].target.workspace is WorkspaceId.MEASUREMENT

    def test_expert_progress_skips_middle_stages(self):
        """An operator who did everything outside the wizard sees all
        stages complete — progress is derived, never stored."""
        views = derive_wizard_progress(_facts())
        assert first_incomplete_stage(views) is None

    def test_gapped_progress_resumes_at_first_gap(self):
        """Baseline measured without calibration (expert path) — the
        wizard still reports readiness as the gap."""
        views = derive_wizard_progress(
            _facts(calibration_complete=False, verify_measured=False)
        )
        assert views[2].status is WizardStageStatus.CURRENT
        # baseline was done through the expert path — it does not
        # become 'current' again
        assert views[3].status is WizardStageStatus.COMPLETE
        assert first_incomplete_stage(views) is (
            WizardStage.MEASUREMENT_READINESS
        )


# ---------------------------------------------------------------------------
# resume record
# ---------------------------------------------------------------------------


class TestResumeState:
    def test_round_trip(self, tmp_path: Path):
        record = FirstRunWizardRecord(
            started_utc='2026-10-06T00:00:00+00:00',
            last_shown_stage='measurement_readiness',
            status='active',
            updated_utc='2026-10-06T00:00:00+00:00',
        )
        path = save_wizard_state(tmp_path, record)
        assert path == wizard_state_path(tmp_path)
        loaded = load_wizard_state(tmp_path)
        assert loaded == record

    def test_missing_file_returns_none(self, tmp_path: Path):
        assert load_wizard_state(tmp_path) is None

    def test_corrupt_file_returns_none(self, tmp_path: Path):
        path = wizard_state_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text('{not json', encoding='utf-8')
        assert load_wizard_state(tmp_path) is None

    def test_extra_fields_rejected(self, tmp_path: Path):
        path = wizard_state_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({
                'schema_version': 1,
                'started_utc': '2026-10-06T00:00:00+00:00',
                'status': 'active',
                'updated_utc': '2026-10-06T00:00:00+00:00',
                'surprise': True,
            }),
            encoding='utf-8',
        )
        assert load_wizard_state(tmp_path) is None


# ---------------------------------------------------------------------------
# dialog (offscreen)
# ---------------------------------------------------------------------------


class TestDialog:
    @pytest.fixture
    def app(self):
        from PySide6.QtWidgets import QApplication
        yield QApplication.instance() or QApplication([])

    def _dialog(self, app, facts, navigations):
        from htdt.first_run_wizard import FirstRunWizardDialog
        dialog = FirstRunWizardDialog(
            lambda: facts,
            navigate=navigations.append,
        )
        return dialog

    def test_marks_current_stage(self, app):
        dialog = self._dialog(app, FirstRunWizardFacts(), [])
        try:
            assert dialog.current_stage is WizardStage.PROJECT_ROOM
            rail = dialog._stage_list
            assert rail.count() == len(WizardStage)
            first = rail.item(0).text()
            assert '●' in first and 'プロジェクトと部屋' in first
        finally:
            dialog.deleteLater()

    def test_action_navigates_to_target(self, app):
        navigations = []
        dialog = self._dialog(app, FirstRunWizardFacts(), navigations)
        try:
            dialog._action.click()
            assert len(navigations) == 1
            assert navigations[0].workspace is WorkspaceId.ROOM
        finally:
            dialog.deleteLater()

    def test_refresh_rederives_after_expert_progress(self, app):
        facts = FirstRunWizardFacts()
        navigations = []
        dialog = self._dialog(app, facts, navigations)
        try:
            # "expert" finishes the room outside the wizard
            complete = _facts()
            dialog._facts_provider = lambda: complete
            dialog.refresh()
            assert all(
                '✓' in dialog._stage_list.item(i).text()
                for i in range(dialog._stage_list.count())
            )
        finally:
            dialog.deleteLater()

    def test_blocked_stage_shows_reason(self, app):
        facts = _facts(
            audio_backend_available=False,
            audio_backend_reason='wasapi stub',
            calibration_complete=False,
            channel_verified=False,
            baseline_measured=False,
            verify_measured=False,
        )
        dialog = self._dialog(app, facts, [])
        try:
            assert dialog.current_stage is (
                WizardStage.MEASUREMENT_READINESS
            )
            assert '理由:' in dialog._reason.text()
            assert not dialog._reason.isHidden()
        finally:
            dialog.deleteLater()

    def test_defer_sets_flag_and_accepts(self, app):
        dialog = self._dialog(app, FirstRunWizardFacts(), [])
        try:
            assert not dialog.deferred
            dialog._defer_button.click()
            assert dialog.deferred
        finally:
            dialog.deleteLater()

    def test_completed_stage_has_no_action(self, app):
        facts = _facts()
        dialog = self._dialog(app, facts, [])
        try:
            # every stage complete — selecting one shows no action button
            dialog._select_stage(0)
            assert dialog._action.isHidden()
        finally:
            dialog.deleteLater()
