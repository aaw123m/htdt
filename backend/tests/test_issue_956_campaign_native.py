"""Issue #956 — campaign UI executes the native automated runner (#875).

One click on the campaign page materializes the runner plan into the
canonical ``mcplan-`` execution plan, runs the read-only PRECHECK,
requires one explicit arm approval, then drives every channel/repetition
entry automatically via the #869 sweep engine — pausing only when a
physical mic move is required. Cancel/pause/resume are journaled;
restart rebuilds from the sealed log; SIMULATED evidence is always
labelled and never promoted to a measured verdict.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pytest

from htdt.cad_campaign_execution import CampaignAutomationPolicy
from htdt.cad_campaign_execution_repository import (
    CadCampaignExecutionRepository,
)
from htdt.cad_campaign_native import (
    NativeCampaignDrive,
    NativePlanConversionError,
    build_native_preflight,
    cell_native_labels,
    channel_entity_id_for,
    materialize_native_campaign_plan,
    resolve_cell_assignments,
)
from htdt.cad_measurement_runner import (
    RunnerCellSpec,
    build_runner_plan_from_cells,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Position3,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_sweep_acquisition import (
    ArmConfirmation,
    FakeAudioBackend,
    MeasurementAcquisitionEngine,
    SweepStimulusSpec,
    WasapiAudioBackend,
    default_fake_scenario,
)
from htdt.cad_sweep_acquisition_repository import CadSweepAcquisitionRepository


# ---------------------------------------------------------------------------
# fixtures


def _scene() -> SceneDocument:
    return SceneDocument(
        document_id='doc-956',
        room=None,
        entities=(
            SceneEntity(
                entity_id='speaker-fl', kind='speaker', name='Front Left',
                speaker_role='FL',
                position=Position3(x_m=1.35, y_m=0.75, z_m=1.05),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
                aim_xyz=None),
            SceneEntity(
                entity_id='speaker-sub', kind='speaker', name='Sub',
                speaker_role='LFE',
                position=Position3(x_m=0.8, y_m=0.3, z_m=0.3),
                size_m=Size3(x_m=0.4, y_m=0.4, z_m=0.4),
                aim_xyz=None),
            SceneEntity(
                entity_id='point-mlp', kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1)),
            SceneEntity(
                entity_id='point-seat2', kind='measurement_point',
                name='Seat 2',
                position=Position3(x_m=3.6, y_m=3.0, z_m=1.1)),
        ),
    )


def _repos(tmp_path: Path):
    from htdt.cad_schema import ensure_native_schema

    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    scene = SceneRepository(db)
    sweep = CadSweepAcquisitionRepository(scene)
    campaign = CadCampaignExecutionRepository(
        scene, sweep_repository=sweep)
    return scene, sweep, campaign


def _controller(tmp_path: Path):
    from htdt.measurement_workflow import MeasurementWorkflowController

    scene, sweep, campaign = _repos(tmp_path)
    revision = scene.save(_scene(), parent_revision_id=None).revision
    controller = MeasurementWorkflowController(
        scene, revision.document_id)
    return controller, scene, sweep, campaign, revision


def _runner_plan(controller, *, targets=('point-mlp', 'point-seat2')):
    return controller.create_runner_plan(
        sources=(
            ('FL', ('speaker-fl',)),
            ('LFE', ('speaker-sub',)),
        ),
        target_entity_ids=tuple(targets),
        repeat_count=1,
        purposes=('measurement',),
    )


def _stimulus(**kw) -> SweepStimulusSpec:
    payload = dict(
        start_frequency_hz=100.0,
        end_frequency_hz=8000.0,
        duration_s=0.05,
        level_dbfs=-12.0,
        sample_rate_hz=48000,
        pre_roll_s=0.01,
        post_roll_s=0.01,
        fade_in_s=0.002,
        fade_out_s=0.002,
        repetitions=2,
        repetition_gap_s=0.02,
    )
    payload.update(kw)
    return SweepStimulusSpec(**payload)


def _routings(runner_plan):
    from htdt.cad_sweep_acquisition import ChannelRouting

    channels: list[str] = []
    for cell in runner_plan.cells:
        cid = channel_entity_id_for(
            cell.channel_role, cell.source_speaker_ids)
        if cid not in channels:
            channels.append(cid)
    return {
        cid: ChannelRouting(
            playback_device_id='fake-duplex-0',
            playback_channel=index,
            capture_device_id='fake-duplex-0',
            capture_channel=0,
            loopback_input_channel=1,
        )
        for index, cid in enumerate(channels)
    }


def _engine_factory(scenarios: Sequence[dict] | dict | None = None):
    """One fresh engine per attempt — a scenario sequence scripts each
    attempt's fake-backend outcome deterministically."""
    queue: list[dict] = []
    if isinstance(scenarios, dict):
        queue = [scenarios]
    elif scenarios:
        queue = list(scenarios)
    calls = {'n': 0}
    default = queue[-1] if queue else {}

    def factory() -> MeasurementAcquisitionEngine:
        idx = calls['n']
        calls['n'] += 1
        kw = queue[idx] if idx < len(queue) else default
        return MeasurementAcquisitionEngine(
            FakeAudioBackend(default_fake_scenario(**kw)))

    factory.calls = calls
    return factory


def _arm(entry, request) -> ArmConfirmation:
    r = request.routing
    return ArmConfirmation(
        acknowledged_playback_device_id=r.playback_device_id,
        acknowledged_playback_channel=r.playback_channel,
        acknowledged_capture_device_id=r.capture_device_id,
        acknowledged_capture_channel=r.capture_channel,
        acknowledged_level_dbfs=request.stimulus.level_dbfs,
    )


def _drive(drive: NativeCampaignDrive):
    """Drive to blocked/terminal, auto-confirming position gates."""
    while True:
        result = drive.advance()
        if result.action == 'awaiting_position':
            state = drive.state()
            drive.confirm_position(state.awaiting_position_id)
            continue
        return result


def _materialize(controller, runner_plan, **kw):
    kw.setdefault('routings', _routings(runner_plan))
    return materialize_native_campaign_plan(
        runner_plan=runner_plan,
        revision=controller.scene_repository.get(
            runner_plan.scene_revision_id),
        stimulus_template=_stimulus(),
        generated_at_utc='2026-10-08T00:00:00+00:00',
        **kw,
    )


# ---------------------------------------------------------------------------
# materialization — runner plan -> canonical mcplan-
# ---------------------------------------------------------------------------


class TestMaterialization:
    def test_exact_queue_cell_mapping(self, tmp_path: Path) -> None:
        controller, *_ = _controller(tmp_path)
        runner_plan = _runner_plan(controller)
        materialization = _materialize(controller, runner_plan)
        plan = materialization.plan
        # 2 positions x 2 channels x 1 repetition = 4 queue entries.
        assert plan.entry_count == 4
        assert {e.position_id for e in plan.queue} == {
            'point-mlp', 'point-seat2'}
        # First entry per position is the physical-move gate.
        gated = [e for e in plan.queue
                 if e.requires_position_confirmation]
        assert len(gated) == 2
        # Exact binding: every queue entry lands on exactly one cell.
        assert materialization.assignments.exact
        assert set(materialization.assignments.entry_to_cell.values()) == {
            0, 1, 2, 3}
        # Lineage: the runner plan + its scene revision are pinned.
        assert plan.campaign_ref.ref_id == runner_plan.plan_id
        assert plan.scene_ref is not None
        assert plan.scene_ref.ref_id == runner_plan.scene_revision_id

    def test_deterministic_plan_id(self, tmp_path: Path) -> None:
        controller, *_ = _controller(tmp_path)
        runner_plan = _runner_plan(controller)
        first = _materialize(controller, runner_plan)
        second = _materialize(controller, runner_plan)
        assert first.plan.plan_id == second.plan.plan_id
        assert first.plan.plan_sha256 == second.plan.plan_sha256

    def test_missing_routing_fails_closed(self, tmp_path: Path) -> None:
        controller, *_ = _controller(tmp_path)
        runner_plan = _runner_plan(controller)
        with pytest.raises(NativePlanConversionError):
            _materialize(controller, runner_plan, routings={})

    def test_unknown_target_fails_closed(self, tmp_path: Path) -> None:
        controller, scene, *_ = _controller(tmp_path)
        runner_plan = _runner_plan(controller)
        revision = scene.get(runner_plan.scene_revision_id)
        # Rewrite the scene without one of the targets.
        reduced = SceneDocument(
            document_id=revision.document_id,
            room=revision.document.room,
            entities=tuple(
                e for e in revision.document.entities
                if e.entity_id != 'point-seat2'),
        )
        saved = scene.save(
            reduced, parent_revision_id=revision.revision_id).revision
        bogus = build_runner_plan_from_cells(
            document_id=runner_plan.document_id,
            scene_revision_id=saved.revision_id,
            scene_content_hash=saved.content_hash,
            cells=runner_plan.cells,
        )
        with pytest.raises(NativePlanConversionError):
            materialize_native_campaign_plan(
                runner_plan=bogus, revision=saved,
                routings=_routings(runner_plan),
                stimulus_template=_stimulus(),
                generated_at_utc='2026-10-08T00:00:00+00:00')

    def test_ambiguous_cells_park_for_review(self, tmp_path: Path) -> None:
        """A cell identity shared by two cells can never auto-bind —
        it stays parked for manual review, never guessed."""
        controller, *_ = _controller(tmp_path)
        runner_plan = _runner_plan(controller)
        materialization = _materialize(controller, runner_plan)
        plan = materialization.plan
        # Craft a runner plan with a duplicate cell key — resolve against
        # the valid exec plan exercises the ambiguity path directly.
        cells = list(runner_plan.cells)
        cells.append(RunnerCellSpec(
            cell_index=len(cells),
            channel_role=cells[0].channel_role,
            source_speaker_ids=cells[0].source_speaker_ids,
            target_entity_id=cells[0].target_entity_id,
            repeat_index=cells[0].repeat_index,
            purpose=cells[0].purpose,
        ))
        dup_plan = build_runner_plan_from_cells(
            document_id=runner_plan.document_id,
            scene_revision_id=runner_plan.scene_revision_id,
            scene_content_hash=runner_plan.scene_content_hash,
            cells=cells,
        )
        assignments = resolve_cell_assignments(dup_plan, plan)
        assert not assignments.exact
        assert assignments.ambiguous
        assert assignments.unbound_cells
        # The ambiguous entry still ran — its evidence stays reviewable
        # rather than bound to a guessed cell.
        labels = cell_native_labels(
            runner_plan=dup_plan,
            assignments=assignments,
            state=type('S', (), {'entries': {}})(),
            run_records=(),
        )
        assert '要レビュー' in set(labels.values())


# ---------------------------------------------------------------------------
# preflight — read-only, fail-closed
# ---------------------------------------------------------------------------


class TestPreflight:
    def test_preflight_lists_everything(self, tmp_path: Path) -> None:
        controller, *_ = _controller(tmp_path)
        runner_plan = _runner_plan(controller)
        plan = _materialize(controller, runner_plan).plan
        report = build_native_preflight(
            plan=plan, backend=FakeAudioBackend(default_fake_scenario()))
        assert report.ok
        assert report.simulated_backend
        assert report.playback_devices == ('fake-duplex-0',)
        assert len(report.routing_lines) == 2
        assert len(report.position_lines) == 2
        assert report.blocked_reasons == ()

    def test_wasapi_stub_blocks_arm(self, tmp_path: Path) -> None:
        """PRECHECK fail-closed: no devices -> nothing can be armed."""
        controller, *_ = _controller(tmp_path)
        runner_plan = _runner_plan(controller)
        plan = _materialize(controller, runner_plan).plan
        report = build_native_preflight(
            plan=plan, backend=WasapiAudioBackend())
        assert not report.ok
        assert not report.simulated_backend
        assert report.playback_devices == ()
        assert report.blocked_reasons


# ---------------------------------------------------------------------------
# drive — the automated runner end to end
# ---------------------------------------------------------------------------


class TestNativeDrive:
    def test_full_campaign_one_arm_two_gates(
            self, tmp_path: Path) -> None:
        """One arm approval -> all same-position entries run -> stop only
        at position moves -> resume -> completed."""
        controller, scene, sweep, campaign, _ = _controller(tmp_path)
        runner_plan = _runner_plan(controller)
        plan = _materialize(controller, runner_plan).plan
        campaign.save_plan(plan)
        drive = controller.native_campaign_drive(
            plan,
            engine_factory=_engine_factory(),
            arm_confirmation_provider=_arm,
        )
        # Gate 1: position-mlp.
        result = drive.advance()
        assert result.action == 'awaiting_position'
        assert drive.state().awaiting_position_id == 'point-mlp'
        drive.confirm_position('point-mlp')
        # Both channels at position 1 run with zero further input.
        result = drive.advance()
        assert result.action == 'awaiting_position'
        assert drive.state().awaiting_position_id == 'point-seat2'
        state = drive.state()
        assert state.progress.completed == 2
        drive.confirm_position('point-seat2')
        result = drive.advance()
        assert result.action == 'terminal'
        state = drive.state()
        assert state.outcome == 'completed'
        assert state.progress.completed == 4
        # Every entry bound a real swrun- acquisition record.
        records = campaign.list_run_records(plan.plan_id)
        assert len(records) == 4
        assert all(r.outcome == 'completed' for r in records)
        assert all(r.acquisition_ref is not None for r in records)
        assert all(r.backend_is_simulated for r in records)
        # Honest: cell labels carry the SIMULATED tag.
        labels = cell_native_labels(
            runner_plan=runner_plan,
            assignments=resolve_cell_assignments(runner_plan, plan),
            state=state,
            run_records=records,
        )
        assert all('SIMULATED' in v for v in labels.values())
        # And the raw assets are sealed + reopenable.
        for record in records:
            assert sweep.get_acquisition_run(
                record.acquisition_ref.ref_id) is not None

    def test_restart_resumes_without_double_recording(
            self, tmp_path: Path) -> None:
        """A rebuilt runner resumes from the journal — completed entries
        are never re-run (no double recording / double commit)."""
        controller, scene, sweep, campaign, _ = _controller(tmp_path)
        runner_plan = _runner_plan(controller)
        plan = _materialize(controller, runner_plan).plan
        campaign.save_plan(plan)
        factory = _engine_factory()
        drive = controller.native_campaign_drive(
            plan,
            engine_factory=factory,
            arm_confirmation_provider=_arm,
        )
        drive.advance()
        drive.confirm_position('point-mlp')
        drive.advance()  # position 1 complete; awaiting seat2
        assert factory.calls['n'] == 2
        # Abandon the drive object; rebuild from the sealed journal.
        rebuilt = controller.native_campaign_drive(
            plan,
            engine_factory=factory,
            arm_confirmation_provider=_arm,
        )
        assert rebuilt.state().progress.completed == 2
        rebuilt.confirm_position('point-seat2')
        result = rebuilt.advance()
        assert result.action == 'terminal'
        assert rebuilt.state().outcome == 'completed'
        # Only the remaining two entries ran — no repeats.
        assert factory.calls['n'] == 4
        assert len(campaign.list_run_records(plan.plan_id)) == 4

    def test_clipping_retry_at_reduced_level(
            self, tmp_path: Path) -> None:
        """In-policy retry: a clipped capture retries once at reduced
        level; the journal keeps both attempts honestly."""
        controller, scene, sweep, campaign, _ = _controller(tmp_path)
        runner_plan = _runner_plan(controller, targets=('point-mlp',))
        plan = _materialize(
            controller, runner_plan,
            policy=CampaignAutomationPolicy(
                retry_clipping_once_at_reduced_level=True),
        ).plan
        campaign.save_plan(plan)
        # First attempt clips; the retried level is reduced by policy.
        drive = controller.native_campaign_drive(
            plan,
            engine_factory=_engine_factory(
                [{'clip_at_dbfs': -20.0}, {}]),
            arm_confirmation_provider=_arm,
        )
        result = _drive(drive)
        assert result.action == 'terminal'
        state = drive.state()
        records = campaign.list_run_records(plan.plan_id)
        first_entry = min(
            records, key=lambda r: (r.entry_ordinal, r.attempt))
        assert first_entry.attempt == 1
        clipped = [r for r in records
                   if r.level_dbfs < plan.stimulus_template.level_dbfs]
        assert clipped  # at least one reduced-level attempt recorded
        assert state.progress.completed == 2

    def test_timeout_failure_is_terminal_honest(
            self, tmp_path: Path) -> None:
        """A capture failure is journaled as failed — never invented."""
        controller, scene, sweep, campaign, _ = _controller(tmp_path)
        runner_plan = _runner_plan(controller, targets=('point-mlp',))
        plan = _materialize(controller, runner_plan).plan
        campaign.save_plan(plan)
        drive = controller.native_campaign_drive(
            plan,
            engine_factory=_engine_factory({'truncate_at_frame': 8}),
            arm_confirmation_provider=_arm,
        )
        result = _drive(drive)
        assert result.action == 'terminal'
        state = drive.state()
        assert state.outcome in ('completed_with_failures', 'failed')
        assert state.progress.failed >= 1
        labels = cell_native_labels(
            runner_plan=runner_plan,
            assignments=resolve_cell_assignments(runner_plan, plan),
            state=state,
            run_records=campaign.list_run_records(plan.plan_id),
        )
        assert any('失敗' in v for v in labels.values())

    def test_routing_change_blocks_until_operator_resumes(
            self, tmp_path: Path) -> None:
        """Unexpected routing drift holds the campaign — operator resume
        is the only way through."""
        controller, scene, sweep, campaign, _ = _controller(tmp_path)
        runner_plan = _runner_plan(controller)
        plan = _materialize(controller, runner_plan).plan
        campaign.save_plan(plan)
        drive = controller.native_campaign_drive(
            plan,
            engine_factory=_engine_factory(),
            arm_confirmation_provider=_arm,
        )
        drive.advance()
        drive.confirm_position('point-mlp')
        drive.report_routing_change('device map changed')
        state = drive.state()
        assert state.routing_drift
        assert state.blocked_reason or state.outcome == 'blocked'
        # Drive is refused until the operator resumes.
        result = drive.advance()
        assert result.action != 'terminal'
        drive.resume(reason='operator verified wiring')
        result = _drive(drive)
        assert result.action == 'terminal'

    def test_pause_cancel_journal(self, tmp_path: Path) -> None:
        """Pause and cancel are journaled; a cancelled campaign is
        terminal and cannot be resumed."""
        controller, scene, sweep, campaign, _ = _controller(tmp_path)
        runner_plan = _runner_plan(controller)
        plan = _materialize(controller, runner_plan).plan
        campaign.save_plan(plan)
        drive = controller.native_campaign_drive(
            plan,
            engine_factory=_engine_factory(),
            arm_confirmation_provider=_arm,
        )
        drive.advance()
        drive.pause('operator stepped away')
        assert drive.state().paused
        drive.resume(reason='back')
        result = drive.advance()
        assert result.action == 'awaiting_position'
        drive.cancel('abandon')
        assert drive.state().outcome == 'cancelled'
        # Partial acquisition remains recorded — no rollback pretense.
        assert campaign.list_events(plan.plan_id)

    def test_unarmed_run_never_emits(self, tmp_path: Path) -> None:
        """Without an arm provider every entry blocks — nothing runs on
        spec alone."""
        controller, scene, sweep, campaign, _ = _controller(tmp_path)
        runner_plan = _runner_plan(controller, targets=('point-mlp',))
        plan = _materialize(controller, runner_plan).plan
        campaign.save_plan(plan)
        drive = controller.native_campaign_drive(
            plan,
            engine_factory=_engine_factory(),
            arm_confirmation_provider=lambda entry, request: None,
        )
        result = _drive(drive)
        assert result.action in ('blocked', 'terminal')
        state = drive.state()
        assert state.progress.completed == 0
        records = campaign.list_run_records(plan.plan_id)
        assert not any(r.outcome == 'completed' for r in records)


# ---------------------------------------------------------------------------
# workspace surface — the one-click UI path
# ---------------------------------------------------------------------------


class TestWorkspaceNativeCampaign:
    def _workspace(self, tmp_path: Path, backend=None, controller=None):
        from PySide6.QtWidgets import QApplication
        from htdt.measurement_page_workspace import (
            MeasurementPageWorkspace,
        )

        QApplication.instance() or QApplication([])
        if controller is None:
            controller, *_ = _controller(tmp_path)
        workspace = MeasurementPageWorkspace(
            controller,
            acquisition_backend=backend or FakeAudioBackend(
                default_fake_scenario()),
        )
        return controller, workspace

    def _armed_exec(self):
        """Patch QDialog.exec to click the real arm button — the approval
        path the operator takes, not a bypass."""
        from PySide6.QtWidgets import QDialog
        from htdt.measurement_page_workspace import (
            _NativeCampaignPreflightDialog,
        )

        def armed(dialog, *a, **k):
            if isinstance(dialog, _NativeCampaignPreflightDialog):
                assert dialog.arm_button.isEnabled()
                dialog.arm_button.click()
                return QDialog.DialogCode.Accepted
            return QDialog.DialogCode.Accepted

        return armed

    def test_preflight_dialog_blocks_on_failed_precheck(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With the WASAPI stub the arm button stays disabled — nothing
        emits audio on a failed PRECHECK."""
        controller, workspace = self._workspace(
            tmp_path, backend=WasapiAudioBackend())
        try:
            runner_plan = _runner_plan(controller)
            workspace._refresh_campaign()
            workspace.campaign_plan_combo.setCurrentIndex(
                workspace.campaign_plan_combo.findData(
                    runner_plan.plan_id))
            seen = {}
            from PySide6.QtWidgets import QDialog
            from htdt.measurement_page_workspace import (
                _NativeCampaignPreflightDialog,
            )

            def spy_exec(dialog, *a, **k):
                if isinstance(dialog, _NativeCampaignPreflightDialog):
                    seen['armed_enabled'] = dialog.arm_button.isEnabled()
                    seen['blocked'] = dialog.report.blocked_reasons
                return QDialog.DialogCode.Rejected

            monkeypatch.setattr(QDialog, 'exec', spy_exec)
            # Routings name devices the backend no longer enumerates —
            # conversion succeeds (declared, non-empty) and PRECHECK
            # blocks arming honestly.
            monkeypatch.setattr(
                workspace, '_native_default_routings',
                lambda plan: _routings(runner_plan))
            workspace._native_campaign_prepare()
            assert seen['armed_enabled'] is False
            assert seen['blocked']
            assert workspace._native_drive is None
        finally:
            workspace.deleteLater()

    def test_one_click_drives_whole_campaign(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Arm once -> same-position cells all run -> stops only at the
        position move -> confirm -> completed. Per-cell clicks: zero."""
        from PySide6.QtWidgets import QDialog

        controller, workspace = self._workspace(tmp_path)
        try:
            runner_plan = _runner_plan(controller)
            workspace._refresh_campaign()
            index = workspace.campaign_plan_combo.findData(
                runner_plan.plan_id)
            workspace.campaign_plan_combo.setCurrentIndex(index)
            monkeypatch.setattr(QDialog, 'exec', self._armed_exec())

            # Zero per-cell interaction is the contract — any call fails.
            monkeypatch.setattr(
                workspace, '_commit_campaign_cell',
                lambda *a, **k: pytest.fail('per-cell commit clicked'))
            monkeypatch.setattr(
                workspace, '_skip_campaign_cell',
                lambda *a, **k: pytest.fail('per-cell skip clicked'))

            workspace._native_campaign_prepare()
            # Position gate 1 shown; nothing ran past it unconfirmed.
            assert workspace._native_drive is not None
            state = workspace._native_drive.state()
            assert state.awaiting_position_id == 'point-mlp'
            assert 'マイクを' in (
                workspace.campaign_native_position_label.text())
            assert workspace.campaign_native_confirm_button.isEnabled()

            # Confirm -> both position-1 entries run unattended.
            workspace._native_confirm_position()
            state = workspace._native_drive.state()
            assert state.awaiting_position_id == 'point-seat2'
            assert state.progress.completed == 2

            workspace._native_confirm_position()
            state = workspace._native_drive.state()
            assert state.outcome == 'completed'
            assert state.progress.completed == 4
            assert '完了' in workspace.campaign_native_status_label.text()
            # SIMULATED honesty: banner visible, cells carry the tag.
            assert 'SIMULATED' in (
                workspace.campaign_native_simulated_label.text())
            assert 'SIMULATED' in workspace.campaign_table.item(0, 5).text()
            # Runner cells stay honest — never promoted by fake evidence.
            states = controller.runner_cell_states(
                workspace._campaign_run_id)
            assert all(
                s.status == 'not_started' for s in states.values())
        finally:
            workspace.deleteLater()

    def test_reopen_recovers_journal(self, tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
        """A fresh workspace re-materializes to the SAME sealed plan and
        rebuilds the runner from the persisted journal — resume."""
        from PySide6.QtWidgets import QDialog

        controller, workspace = self._workspace(tmp_path)
        try:
            runner_plan = _runner_plan(controller)
            workspace._refresh_campaign()
            workspace.campaign_plan_combo.setCurrentIndex(
                workspace.campaign_plan_combo.findData(
                    runner_plan.plan_id))
            monkeypatch.setattr(QDialog, 'exec', self._armed_exec())
            workspace._native_campaign_prepare()
            workspace._native_confirm_position()  # pos 1 done
            plan_id = workspace._native_plan.plan_id
            run_records = len(
                controller._campaign_execution_repo().list_run_records(
                    plan_id))
            assert run_records == 2
        finally:
            workspace.deleteLater()

        # Fresh workspace — a restarted app's controller over the same
        # sealed repository.
        controller2, workspace2 = self._workspace(
            tmp_path, controller=controller)
        try:
            workspace2._refresh_campaign()
            workspace2.campaign_plan_combo.setCurrentIndex(
                workspace2.campaign_plan_combo.findData(
                    runner_plan.plan_id))
            workspace2._native_campaign_prepare()
            # Same deterministic plan id; journal resumed.
            assert workspace2._native_plan.plan_id == plan_id
            state = workspace2._native_drive.state()
            assert state.progress.completed == 2
            workspace2._native_confirm_position()
            assert workspace2._native_drive.state().outcome == 'completed'
            # Total sealed records: still exactly 4 — no double commit.
            assert len(controller2._campaign_execution_repo()
                       .list_run_records(plan_id)) == 4
        finally:
            workspace2.deleteLater()
