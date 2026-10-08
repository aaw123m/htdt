"""#946 — CommissioningPanel operator commands through the #868
closed-loop orchestrator.

Covers the controller/command port (``commissioning_operations``) and
the panel's wired command row: per-stage enablement with honest JA
disabled reasons, one-shot candidate-pinned approvals, device-mutation
previews, blocked/rejected outcomes surfaced verbatim, restart-restore
honesty, and reachable cancel/rollback — all against the real sealed
stores and a fixture CamillaDSP transport (no fabricated device).
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

import test_issue_868_commissioning_orchestrator as t868

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_calibration_deployment import EffectivenessMetricDelta
from htdt.cad_commissioning_orchestrator import (
    CommissioningAcceptanceCriterion,
    CommissioningOrchestrator,
    derive_evidence_strength,
)
from htdt.commissioning_operations import (
    CommissioningAcquisitionRequest,
    CommissioningBeforeAfterInput,
    CommissioningOperatorController,
    CommissioningOperatorServices,
)

DOC = t868.DOC
NOW = t868.NOW
MEAS_SHA = t868.MEAS_SHA


def _acq(tag: str, ref_sha: str) -> CommissioningAcquisitionRequest:
    return CommissioningAcquisitionRequest(
        request_identity_repr=f'GET /measurements/{tag}/response',
        request_sha256=sha256(tag.encode()).hexdigest(),
        acquire=lambda: (
            (AuthorityRef(
                kind='measurement', ref_id=f'm-{tag}',
                ref_sha256=ref_sha),),
            sha256(f'raw-{tag}'.encode()).hexdigest(),
            f'{tag} sweep'))


def _services(**kw) -> CommissioningOperatorServices:
    return CommissioningOperatorServices(**kw)


def _controller(
    tmp_path: Path, services=None,
) -> tuple[CommissioningOperatorController, CommissioningOrchestrator]:
    orch = t868._orchestrator(tmp_path)
    return (
        CommissioningOperatorController(
            orch, DOC, services=services or _services(),
            clock=lambda: NOW),
        orch)


def _seed_run(orch, **kw):
    return t868._create_run(orch, **kw)


def _drive_to_approval(
    tmp_path: Path,
    *,
    reject_config: str | None = None,
    transport=None,
):
    """A controller with a run parked at operator_approval."""
    transport = transport or t868._transport(
        reject_config=reject_config)
    adapter = t868._adapter(transport)
    orch = t868._orchestrator(tmp_path)
    manifest, binding, run = _seed_run(
        orch,
        acceptance_criteria=(CommissioningAcceptanceCriterion(
            metric_id='spl_dev', expected='improvement',
            required=True),))
    plan = t868._plan()
    export = t868._export()
    services = _services(
        adapter=adapter, binding=binding, manifest=manifest,
        quality_report=t868._quality_report(),
        analysis_refs=(t868._ref('analysis', 'an-1'),),
        plan=plan, export=export,
        declaration=t868._declaration(),
        baseline_request=_acq('b1', MEAS_SHA),
        post_request=_acq('p1', '7' * 64),
        before_after=CommissioningBeforeAfterInput(
            baseline_refs=(t868._ref('measurement', 'm-1', MEAS_SHA),),
            post_refs=(t868._ref('measurement', 'm-2', '7' * 64),),
            deltas=(EffectivenessMetricDelta(
                metric_id='spl_dev', before_value_repr='3.1',
                after_value_repr='1.2', outcome='improved'),)),
    )
    controller = CommissioningOperatorController(
        orch, DOC, services=services, clock=lambda: NOW)
    # precheck -> baseline -> quality -> analyze -> optimize
    for expected in (
            'baseline_measurement', 'ingest_quality_gate', 'analyze',
            'optimize', 'operator_approval'):
        result = controller.advance()
        assert result.ok, result.summary
        assert orch.derive_state(run).current_stage == expected
    return controller, orch, transport, manifest, binding, run, plan, export


def _op(controller, command: str):
    return {op.command: op for op in controller.operations()}[command]


class TestEnablement:
    def test_precheck_enabled_without_adapter_attempts_honestly(
            self, tmp_path: Path) -> None:
        controller, orch = _controller(tmp_path)
        _seed_run(orch)
        # advance is enabled: the machine itself seals the honest
        # 'no adapter capability report' blocked row.
        assert _op(controller, 'advance').enabled
        result = controller.advance()
        assert not result.ok
        assert result.outcome == 'blocked'
        assert 'capability' in result.transition.reason

    def test_no_run_disables_everything(self, tmp_path: Path) -> None:
        controller, _orch = _controller(tmp_path)
        assert not any(op.enabled for op in controller.operations())
        result = controller.advance()
        assert not result.ok and result.outcome == 'unavailable'

    def test_forward_buttons_disabled_at_dedicated_stages(
            self, tmp_path: Path) -> None:
        controller, orch, *_rest = _drive_to_approval(tmp_path)
        advance = _op(controller, 'advance')
        assert not advance.enabled
        assert '承認' in advance.disabled_reason
        # authorize is enabled; deploy/readback still cannot run.
        assert _op(controller, 'authorize_deploy').enabled
        assert not _op(controller, 'deploy').enabled
        assert not _op(controller, 'readback').enabled
        assert _op(controller, 'rollback').disabled_reason

    def test_device_ops_disabled_without_inputs(
            self, tmp_path: Path) -> None:
        """Missing hardware inputs render as honest JA reasons."""
        orch = t868._orchestrator(tmp_path)
        manifest, binding, run = _seed_run(orch)
        t868._drive_to_optimize(
            orch, manifest, binding, run, t868._transport())
        plan = t868._plan()
        orch.commit_optimization(run, plan=plan, at_utc=NOW)
        controller = CommissioningOperatorController(
            orch, DOC, services=_services(plan=plan),
            clock=lambda: NOW)
        authorize = _op(controller, 'authorize_deploy')
        assert authorize.enabled
        result = controller.authorize_deploy(operator_id='op-1')
        assert result.ok
        state = orch.derive_state(run)
        assert state.current_stage == 'compile_for_target'
        # No adapter/export bound: compile is honestly disabled.
        advance = _op(controller, 'advance')
        assert not advance.enabled
        assert 'アダプタ' in advance.disabled_reason

    def test_binding_mismatch_fails_closed(self, tmp_path: Path) -> None:
        controller, orch, transport, manifest, binding, run, plan, \
            export = _drive_to_approval(tmp_path)
        wrong = t868.build_device_binding(
            adapter_id=t868.CAMILLADSP_DEPLOY_ADAPTER_ID,
            binding_id='adb-other', device_family='camilladsp',
            device_model='CamillaDSP', device_serial='camilladsp://x',
            firmware_version='3.0.0', routing=(('ch-1', '0'),),
            bound_at_utc=NOW)
        controller._services = _services(
            adapter=t868._adapter(transport), binding=wrong,
            plan=plan, export=export,
            declaration=t868._declaration())
        controller.authorize_deploy(operator_id='op-1')
        result = controller.advance()
        assert not result.ok and result.outcome == 'unavailable'
        assert 'ピン' in result.summary
        assert orch.derive_state(run).current_stage == (
            'compile_for_target')


class TestFullLoop:
    def test_controller_drives_full_ladder(self, tmp_path: Path) -> None:
        controller, orch, transport, manifest, binding, run, plan, \
            export = _drive_to_approval(tmp_path)

        result = controller.authorize_deploy(operator_id='op-1')
        assert result.ok
        assert orch.derive_state(run).current_stage == (
            'compile_for_target')

        result = controller.advance()  # compile
        assert result.ok, result.summary
        assert orch.derive_state(run).current_stage == 'deploy'

        # The approval sealed at operator_approval is reused — the
        # deploy preview reports it.
        preview = controller.deploy_preview()
        assert preview is not None
        assert preview.reusable_authorization_id is not None
        assert preview.candidate_sha256 == (
            export.requested_plan_semantic_sha256)
        assert any('校正候補' in label for label, _ in preview.lines)

        result = controller.deploy(operator_id='op-1')
        assert result.ok, result.summary
        assert orch.derive_state(run).current_stage == 'readback_verify'

        result = controller.readback()
        assert result.ok, result.summary
        state = orch.derive_state(run)
        assert state.current_stage == 'post_measurement'
        assert state.readback_matched is True

        for expected in ('before_after', 'acceptance_decision',
                         'completed'):
            result = controller.advance()
            assert result.ok, result.summary
            assert orch.derive_state(run).current_stage == expected
        verdict = orch.repository.list_verdicts(DOC)[-1]
        assert verdict.verdict == 'accepted'
        assert derive_evidence_strength(
            orch.derive_state(run), verdict) == 'accepted'

        # Terminal: nothing is permitted any more.
        assert not any(op.enabled for op in controller.operations())

    def test_deploy_mints_authorization_when_missing(
            self, tmp_path: Path) -> None:
        """A deploy at the deploy stage with no valid sealed auth mints
        a one-shot, candidate-pinned one and consumes it."""
        controller, orch, transport, manifest, binding, run, plan, \
            export = _drive_to_approval(tmp_path)
        controller.authorize_deploy(operator_id='op-1')
        controller.advance()  # compile
        # Consume the sealed approval on a first deploy attempt that
        # fails at the transport — the authorization is NOT consumed
        # by a blocked attempt, so the retry reuses it.
        transport._reject_config = 'device rejected config'
        result = controller.deploy(operator_id='op-1')
        assert not result.ok and result.outcome == 'blocked'
        assert 'apply_error' in result.transition.reason
        assert orch.derive_state(run).current_stage == 'deploy'
        transport._reject_config = None
        result = controller.deploy(operator_id='op-1')
        assert result.ok, result.summary

    def test_authorization_is_one_shot(self, tmp_path: Path) -> None:
        """A consumed deploy_apply authorization is never reused."""
        controller, orch, transport, manifest, binding, run, plan, \
            export = _drive_to_approval(tmp_path)
        auth, _ = orch.authorize(
            run, operator_id='op-1', scope='deploy_apply',
            candidate_sha256=plan.plan_semantic_sha256, at_utc=NOW)
        controller.advance()
        assert controller.deploy_preview() \
            .reusable_authorization_id == auth.authorization_id
        result = controller.deploy(operator_id='op-1')
        assert result.ok
        # The sealed authorization is consumed — the next preview can
        # no longer offer it.
        assert controller._find_deploy_authorization(
            run, export.requested_plan_semantic_sha256, NOW) is None

    def test_wrong_candidate_authorization_not_reused(
            self, tmp_path: Path) -> None:
        controller, orch, transport, manifest, binding, run, plan, \
            export = _drive_to_approval(tmp_path)
        # A stale authorization pinned to a different candidate must
        # not satisfy this deploy — a fresh one is minted instead.
        orch.authorize(
            run, operator_id='op-9', scope='deploy_apply',
            candidate_sha256='9' * 64, at_utc=NOW)
        controller.advance()
        preview = controller.deploy_preview()
        assert preview.reusable_authorization_id is None
        result = controller.deploy(operator_id='op-1')
        assert result.ok, result.summary

    def test_expired_authorization_not_reused(
            self, tmp_path: Path) -> None:
        controller, orch, transport, manifest, binding, run, plan, \
            export = _drive_to_approval(tmp_path)
        # Sealed earlier than the controller clock, so it is already
        # expired when deploy runs (the record itself validates).
        orch.authorize(
            run, operator_id='op-9', scope='deploy_apply',
            candidate_sha256=export.requested_plan_semantic_sha256,
            at_utc='2026-10-06T00:00:00Z',
            expires_at_utc='2026-10-06T00:00:01Z')
        controller.advance()
        assert controller.deploy_preview() \
            .reusable_authorization_id is None
        result = controller.deploy(operator_id='op-1')
        assert result.ok, result.summary


class TestFailurePaths:
    def test_failed_apply_blocks_and_stays(self, tmp_path: Path) -> None:
        controller, orch, transport, manifest, binding, run, plan, \
            export = _drive_to_approval(
                tmp_path, transport=t868._transport(
                    reject_config='device rejected config'))
        controller.authorize_deploy(operator_id='op-1')
        controller.advance()
        result = controller.deploy(operator_id='op-1')
        assert not result.ok
        assert result.outcome == 'blocked'
        assert 'apply_error' in result.transition.reason
        assert orch.derive_state(run).current_stage == 'deploy'
        # The read-back button is still honestly disabled.
        assert not _op(controller, 'readback').enabled

    def test_readback_divergence_blocks(self, tmp_path: Path) -> None:
        controller, orch, transport, manifest, binding, run, plan, \
            export = _drive_to_approval(tmp_path)
        controller.authorize_deploy(operator_id='op-1')
        controller.advance()
        assert controller.deploy(operator_id='op-1').ok
        # Simulate device drift between apply and read-back: keep the
        # applied config's htdt deployment region (so read-back is
        # possible) but change one value — a true divergence.
        import copy
        drifted = copy.deepcopy(transport._config)
        region_names = [
            name for name in drifted['filters']
            if name.startswith('htdt_')]
        assert region_names
        params = drifted['filters'][region_names[0]]['parameters']
        for key, value in sorted(params.items()):
            if isinstance(value, (int, float)):
                params[key] = value + 0.137
                break
            if isinstance(value, list) and value and isinstance(
                    value[0], (int, float)):
                value[0] = value[0] + 0.137
                break
        transport._config = drifted
        result = controller.readback()
        assert not result.ok
        assert result.outcome == 'blocked'
        state = orch.derive_state(run)
        assert state.current_stage == 'readback_verify'
        assert state.readback_matched is False
        assert 'diverged' in result.transition.reason

    def test_rollback_requires_deployment(self, tmp_path: Path) -> None:
        controller, orch, *_rest = _drive_to_approval(tmp_path)
        # deploy not yet executed — no deployment record exists.
        op = _op(controller, 'rollback')
        assert not op.enabled

    def test_rollback_verified_round_trip(
            self, tmp_path: Path) -> None:
        controller, orch, transport, manifest, binding, run, plan, \
            export = _drive_to_approval(tmp_path)
        controller.authorize_deploy(operator_id='op-1')
        controller.advance()
        assert controller.deploy(operator_id='op-1').ok
        preview = controller.rollback_preview()
        assert preview is not None and preview.requires_reason
        assert preview.deployment_id is not None
        result = controller.rollback(
            operator_id='op-1', reason='operator veto')
        assert result.ok, result.summary
        state = orch.derive_state(run)
        assert state.current_stage == 'rolled_back'
        rollbacks = [
            r for r in orch.repository.list_rollbacks(DOC)
            if r.run_ref.ref_id == run.run_id]
        assert rollbacks[-1].outcome == 'restored_verified'

    def test_abort_reachable_anywhere(self, tmp_path: Path) -> None:
        controller, orch, *_rest = _drive_to_approval(tmp_path)
        result = controller.cancel(reason='operator stop')
        assert result.ok
        assert orch.derive_state(
            controller.current()[0]).current_stage == 'aborted'

    def test_disconnect_gates_then_restores(
            self, tmp_path: Path) -> None:
        controller, orch, transport, manifest, binding, run, *_ = (
            _drive_to_approval(tmp_path))
        orch.report_disconnect(
            run, scope='provider', reason='REW API timed out',
            at_utc=NOW)
        advance = _op(controller, 'advance')
        assert not advance.enabled
        assert _op(controller, 'restore_connectivity').enabled
        assert _op(controller, 'cancel').enabled
        result = controller.restore_connectivity(
            reason='provider reachable')
        assert result.ok
        # Permitted actions are un-collapsed: the approval-stage op is
        # reachable again (advance stays correctly disabled here).
        assert _op(controller, 'authorize_deploy').enabled
        assert '許可' in _op(controller, 'advance').disabled_reason \
            or '承認' in _op(controller, 'advance').disabled_reason

    def test_busy_guard_blocks_reentry(self, tmp_path: Path) -> None:
        controller, orch, *_rest = _drive_to_approval(tmp_path)
        controller._busy = True
        result = controller.advance()
        assert not result.ok and result.outcome == 'busy'

    def test_restart_loses_materialization_honestly(
            self, tmp_path: Path) -> None:
        """A fresh controller over the same store cannot fake the
        compile product — deploy degrades to a disabled reason."""
        controller, orch, transport, manifest, binding, run, plan, \
            export = _drive_to_approval(tmp_path)
        controller.authorize_deploy(operator_id='op-1')
        controller.advance()
        services = _services(
            adapter=t868._adapter(transport), binding=binding,
            manifest=manifest, plan=plan, export=export,
            declaration=t868._declaration())
        restarted = CommissioningOperatorController(
            orch, DOC, services=services, clock=lambda: NOW)
        op = _op(restarted, 'deploy')
        assert not op.enabled
        assert 'コンパイル' in op.disabled_reason
        # The sealed state itself is intact — same fold after reload.
        assert restarted.current()[1].current_stage == 'deploy'


# ----------------------------------------------------------------------
# panel — offscreen command-row wiring


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _panel(tmp_path: Path, services=None):
    from htdt.commissioning_panel import CommissioningPanel
    _app()
    orch = t868._orchestrator(tmp_path)
    panel = CommissioningPanel(orch, DOC, services=services)
    return panel, orch


class TestPanel:
    def test_empty_state_disables_commands(
            self, tmp_path: Path) -> None:
        panel, _orch = _panel(tmp_path)
        for button in panel._command_buttons():
            assert not button.isEnabled()

    def test_advance_button_drives_stage(self, tmp_path: Path) -> None:
        transport = t868._transport()
        panel, orch = _panel(tmp_path)
        manifest, binding, run = _seed_run(orch)
        panel._services = _services(
            adapter=t868._adapter(transport), binding=binding,
            manifest=manifest)
        panel._controller = CommissioningOperatorController(
            orch, DOC, services=panel._services, clock=lambda: NOW)
        panel.refresh()
        assert panel.advance_button.isEnabled()
        panel.advance_button.click()
        assert orch.derive_state(run).current_stage == (
            'baseline_measurement')
        assert '事前チェック' in panel.result_label.text()
        # The button re-derived its enablement for the new stage —
        # baseline needs a configured request, so it is disabled.
        assert not panel.advance_button.isEnabled()
        assert '測定リクエスト' in panel.advance_button.toolTip()

    def test_approve_dialog_drives_deploy_apply(
            self, tmp_path: Path, monkeypatch) -> None:
        from PySide6.QtWidgets import QDialog
        from htdt import commissioning_panel as panel_mod
        transport = t868._transport()
        panel, orch = _panel(tmp_path)
        manifest, binding, run = _seed_run(orch)
        t868._drive_to_optimize(
            orch, manifest, binding, run, transport)
        plan = t868._plan()
        panel._services = _services(
            adapter=t868._adapter(transport), binding=binding,
            manifest=manifest, plan=plan, export=t868._export(),
            declaration=t868._declaration())
        panel._controller = CommissioningOperatorController(
            orch, DOC, services=panel._services, clock=lambda: NOW)
        orch.commit_optimization(run, plan=plan, at_utc=NOW)
        panel.refresh()
        assert panel.authorize_button.isEnabled()
        assert not panel.advance_button.isEnabled()
        assert '承認' in panel.advance_button.toolTip()

        class _Approve:
            DialogCode = QDialog.DialogCode

            def __init__(self, preview, parent=None):
                self.preview = preview

            def exec(self):
                return QDialog.DialogCode.Accepted

            @property
            def operator_id(self):
                return 'op-7'

            @property
            def note(self):
                return 'looks good'

        monkeypatch.setattr(
            panel_mod, 'CommissioningApprovalDialog', _Approve)
        panel.authorize_button.click()
        assert orch.derive_state(run).current_stage == (
            'compile_for_target')
        # The authorization is sealed candidate-pinned.
        auth = orch.repository.list_authorizations(DOC)[-1]
        assert auth.scope == 'deploy_apply'
        assert auth.candidate_sha256 == plan.plan_semantic_sha256
        assert auth.operator_id == 'op-7'

    def test_deploy_dialog_mints_and_consumes(
            self, tmp_path: Path, monkeypatch) -> None:
        from PySide6.QtWidgets import QDialog
        from htdt import commissioning_panel as panel_mod
        controller, orch, transport, manifest, binding, run, plan, \
            export = _drive_to_approval(tmp_path)
        from htdt.commissioning_panel import CommissioningPanel
        services = _services(
            adapter=t868._adapter(transport), binding=binding,
            manifest=manifest, plan=plan, export=export,
            declaration=t868._declaration())
        panel = CommissioningPanel(orch, DOC, services=services,
                                 controller=controller)
        controller.authorize_deploy(operator_id='op-1')
        controller.advance()
        panel.refresh()
        assert panel.deploy_button.isEnabled()

        class _Approve:
            DialogCode = QDialog.DialogCode

            def __init__(self, preview, parent=None):
                self.preview = preview

            def exec(self):
                return QDialog.DialogCode.Accepted

            @property
            def operator_id(self):
                return 'op-8'

            @property
            def note(self):
                return ''

        monkeypatch.setattr(
            panel_mod, 'CommissioningApprovalDialog', _Approve)
        panel.deploy_button.click()
        assert orch.derive_state(run).current_stage == (
            'readback_verify')
        assert 'デプロイ' in panel.result_label.text()
        # Read-back is now the permitted forward op.
        assert panel.readback_button.isEnabled()
        panel.readback_button.click()
        assert orch.derive_state(run).current_stage == (
            'post_measurement')

    def test_rollback_and_cancel_reachable(
            self, tmp_path: Path, monkeypatch) -> None:
        from PySide6.QtWidgets import QDialog, QInputDialog
        from htdt import commissioning_panel as panel_mod
        controller, orch, transport, manifest, binding, run, plan, \
            export = _drive_to_approval(tmp_path)
        services = _services(
            adapter=t868._adapter(transport), binding=binding,
            manifest=manifest, plan=plan, export=export,
            declaration=t868._declaration())
        from htdt.commissioning_panel import CommissioningPanel
        panel = CommissioningPanel(orch, DOC, services=services,
                                 controller=controller)
        controller.authorize_deploy(operator_id='op-1')
        controller.advance()
        controller.deploy(operator_id='op-1')
        panel.refresh()
        assert panel.rollback_button.isEnabled()

        class _Approve:
            DialogCode = QDialog.DialogCode

            def __init__(self, preview, parent=None):
                self.preview = preview

            def exec(self):
                return QDialog.DialogCode.Accepted

            @property
            def operator_id(self):
                return 'op-9'

            @property
            def note(self):
                return 'revert it'

        monkeypatch.setattr(
            panel_mod, 'CommissioningApprovalDialog', _Approve)
        panel.rollback_button.click()
        assert orch.derive_state(run).current_stage == 'rolled_back'
        assert not panel.cancel_button.isEnabled()

    def test_cancel_aborts_via_input_dialog(
            self, tmp_path: Path, monkeypatch) -> None:
        from PySide6.QtWidgets import QInputDialog
        panel, orch = _panel(tmp_path)
        _seed_run(orch)
        panel.refresh()
        monkeypatch.setattr(
            QInputDialog, 'getText',
            staticmethod(lambda *a, **k: ('現場中止', True)))
        panel.cancel_button.click()
        assert orch.derive_state(
            orch.list_runs(DOC)[-1]).current_stage == 'aborted'

    def test_dialog_reject_emits_nothing(
            self, tmp_path: Path, monkeypatch) -> None:
        from PySide6.QtWidgets import QDialog
        from htdt import commissioning_panel as panel_mod
        controller, orch, transport, manifest, binding, run, plan, \
            export = _drive_to_approval(tmp_path)
        services = _services(
            adapter=t868._adapter(transport), binding=binding,
            manifest=manifest, plan=plan, export=export,
            declaration=t868._declaration())
        from htdt.commissioning_panel import CommissioningPanel
        panel = CommissioningPanel(orch, DOC, services=services,
                                 controller=controller)
        controller.authorize_deploy(operator_id='op-1')
        controller.advance()
        panel.refresh()
        before = len(orch.transitions(run))

        class _Reject:
            DialogCode = QDialog.DialogCode

            def __init__(self, preview, parent=None):
                self.preview = preview

            def exec(self):
                return QDialog.DialogCode.Rejected

            operator_id = ''
            note = ''

        monkeypatch.setattr(
            panel_mod, 'CommissioningApprovalDialog', _Reject)
        panel.deploy_button.click()
        assert len(orch.transitions(run)) == before

    def test_approval_dialog_gates_ok(self, tmp_path: Path) -> None:
        from htdt.commissioning_panel import CommissioningApprovalDialog
        from PySide6.QtWidgets import QDialogButtonBox
        controller, *_rest = _drive_to_approval(tmp_path)
        preview = controller.authorize_preview()
        _app()
        dialog = CommissioningApprovalDialog(preview)
        ok = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)
        assert not ok.isEnabled()
        dialog.operator_edit.setText('op-1')
        assert ok.isEnabled()
        # Deploy preview exists only once deploy is actually reachable
        # (fail-closed): approve + compile, then it exposes the pinned
        # candidate and one-shot scope.
        assert controller.deploy_preview() is None
        controller.authorize_deploy(operator_id='op-1')
        controller.advance()
        deploy_preview = controller.deploy_preview()
        assert deploy_preview is not None
        values = dict(deploy_preview.lines)
        assert '承認スコープ' in values
        assert 'deploy_apply' in values['承認スコープ']
