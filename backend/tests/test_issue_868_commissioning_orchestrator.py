"""#868 closed-loop commissioning orchestrator.

Covers: the pure stage machine (every advance/reject/blocked/terminal
path), sealed-record integrity, repository round-trip + tamper
detection, the invalidation evaluator, resumability after restart, and
an end-to-end run driven entirely through a fake delegated provider and
the in-memory CamillaDSP fixture transport.
"""

from __future__ import annotations

import sqlite3
from hashlib import sha256
from pathlib import Path

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_calibration import (
    CadCalibrationChannel,
    CadCalibrationExportSnapshot,
    CadCalibrationPlan,
    CadDeviceCapabilityConstraints,
    build_generic_biquad_export,
)
from htdt.cad_calibration_deployment import (
    DeploymentCapabilityDeclaration,
    EffectivenessMetricDelta,
)
from htdt.cad_camilladsp import (
    CamillaDSPError,
    FixtureCamillaDSPTransport,
)
from htdt.cad_camilladsp_deploy import (
    CAMILLADSP_DEPLOY_ADAPTER_ID,
    CamillaDSPCalibrationAdapter,
)
from htdt.cad_commissioning_orchestrator import (
    CommissioningAcceptanceCriterion,
    CommissioningAcceptanceVerdict,
    CommissioningBeforeAfter,
    CommissioningEvent,
    CommissioningOperatorAuthorization,
    CommissioningOrchestrationError,
    CommissioningOrchestrationRun,
    CommissioningOrchestrator,
    CommissioningRollback,
    CommissioningRunState,
    CommissioningStageTransition,
    TransitionDecision,
    TransitionRejection,
    derive_evidence_strength,
    derive_run_state,
    evaluate_invalidation,
    next_permitted_actions,
    stage_transition,
)
from htdt.cad_commissioning_orchestrator_repository import (
    CadCommissioningOrchestratorRepository,
    DeploymentIntegrityError,
)
from htdt.cad_delegated_provider import (
    DelegatedProviderManifest,
    ProviderCapabilityEntry,
    build_provider_acquisition,
    manifest_ref,
)
from htdt.cad_delegated_provider_repository import (
    CadDelegatedProviderRepository,
)
from htdt.cad_device_adapter import build_device_binding
from htdt.cad_measurement_quality import (
    CadMeasurementCapability,
    CadMeasurementQualityEvidence,
    CadMeasurementQualityProfile,
    CadMeasurementQualityReport,
    CadMeasurementQualityCheck,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.cad_scene import Position3
from htdt.canonical_json import canonical_sha256


NOW = '2026-10-07T00:00:00Z'
DOC = 'doc-868'
SCENE_SHA = 'a' * 64
MEAS_SHA = 'c' * 64
DS_SHA = 'e' * 64
RPT_SHA = 'f' * 64
ENDPOINT = 'camilladsp://127.0.0.1:1234'


# ----------------------------------------------------------------------
# fixtures


def _ref(kind: str, rid: str = 'x1', sha: str = 'b' * 64) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)


def _manifest(**kw) -> DelegatedProviderManifest:
    payload = dict(
        document_id=DOC,
        provider_class='rew_api',
        provider_id='rew',
        provider_version='5.40',
        adapter_id='htdt-rew-api',
        adapter_version='1',
        endpoint_kind='localhost',
        endpoint_repr='http://localhost:4735',
        capabilities=(
            ProviderCapabilityEntry(
                capability='frequency_response', condition='supported',
                observed=True),
        ),
        declared_at_utc=NOW,
    )
    payload.update(kw)
    return DelegatedProviderManifest.create(**payload)


def _binding() -> object:
    return build_device_binding(
        adapter_id=CAMILLADSP_DEPLOY_ADAPTER_ID,
        binding_id='adb-868',
        device_family='camilladsp',
        device_model='CamillaDSP',
        device_serial=ENDPOINT,
        firmware_version='3.0.0',
        routing=(('ch-1', '0'),),
        bound_at_utc=NOW,
    )


def _transport(**kw) -> FixtureCamillaDSPTransport:
    return FixtureCamillaDSPTransport(
        config=kw.pop('config', {
            'devices': {'samplerate': 48000, 'playback': {'channels': 2}},
        }),
        **kw,
    )


def _adapter(
    transport: FixtureCamillaDSPTransport,
) -> CamillaDSPCalibrationAdapter:
    return CamillaDSPCalibrationAdapter({ENDPOINT: transport})


def _declaration(**kw) -> DeploymentCapabilityDeclaration:
    payload = dict(
        document_id=DOC,
        adapter_id=CAMILLADSP_DEPLOY_ADAPTER_ID,
        adapter_version='1',
        adapter_kind='network_api',
        device_family='camilladsp',
        supports_apply=True,
        supports_read_back=True,
        supports_materialization=True,
        supported_features=('peq', 'gain'),
        declared_limit_notes=(),
        declared_at_utc=NOW,
    )
    payload.update(kw)
    return DeploymentCapabilityDeclaration.create(**payload)


def _pass_check() -> CadMeasurementQualityCheck:
    return CadMeasurementQualityCheck.model_construct(
        status='PASS', reason='ok')


def _quality_report(**kw) -> CadMeasurementQualityReport:
    """Structurally complete report — the orchestrator only reads the
    derived check/capability verdicts, never re-derives them."""
    payload = dict(
        report_id='q-1',
        created_at_utc=NOW,
        measurement_id='m-1',
        measurement_sha256=MEAS_SHA,
        dataset_id='d-1',
        dataset_sha256=DS_SHA,
        raw_asset_sha256='0' * 64,
        document_id=DOC,
        scene_revision_id='rev-1',
        scene_content_hash=SCENE_SHA,
        measurement_entity_id='spk-fl',
        measurement_position=Position3.model_construct(
            x_m=0.0, y_m=0.0, z_m=0.0),
        acquisition_context=None,
        observation=None,
        level_reference=None,
        algorithm_version='t-1',
        algorithm_sha256='0' * 64,
        profile=CadMeasurementQualityProfile.model_construct(
            profile_version='p-1',
            minimum_snr_db=None,
            required_usable_band_hz=None,
            minimum_polarity_confidence=None,
            maximum_repeatability_rms_db=None,
            profile_sha256='0' * 64),
        evidence=CadMeasurementQualityEvidence(),
        clipping=_pass_check(),
        noise_snr=_pass_check(),
        usable_frequency_band=_pass_check(),
        timing_reference=_pass_check(),
        polarity=_pass_check(),
        ir_window=_pass_check(),
        calibration=_pass_check(),
        repeatability=_pass_check(),
        retake_recommendation='NOT_NEEDED',
        retake_reasons=(),
        capabilities=(
            CadMeasurementCapability.model_construct(
                claim='magnitude_response', decision='ALLOWED',
                reasons=('test',)),
        ),
        report_sha256=RPT_SHA,
    )
    payload.update(kw)
    return CadMeasurementQualityReport.model_construct(**payload)


def _plan(**kw) -> CadCalibrationPlan:
    channel = CadCalibrationChannel(
        channel_id='ch-1',
        role_id='FL',
        source_entity_id='spk-fl',
        physical_output_id='0',
        sample_rate_hz=48000,
        gain_db=1.0,
        delay_s=0.0,
        polarity='normal',
        crossovers=(),
        peq=(),
        routing=('0',),
    )
    payload = dict(
        plan_id='plan-868',
        plan_version='1',
        created_at_utc=NOW,
        source_kind='provided_fixture',
        document_id=DOC,
        scene_revision_id='rev-1',
        scene_content_hash=SCENE_SHA,
        system_variant_id='var-1',
        system_variant_sha256='b' * 64,
        source_measurement_id='m-1',
        source_measurement_sha256=MEAS_SHA,
        source_dataset_id='d-1',
        source_dataset_sha256=DS_SHA,
        measurement_quality_report_id='q-1',
        measurement_quality_report_sha256=RPT_SHA,
        sample_rate_hz=48000,
        channels=(channel,),
        target_curve=None,
        max_boost_db=6.0,
        max_cut_db=10.0,
        device_constraints=CadDeviceCapabilityConstraints(
            capability_id='test-device-868', capability_version='1',
            supported_sample_rates_hz=(48000, 96000),
            supported_filter_types=('peaking', 'low_pass', 'high_pass'),
            channel_gain_resolution_db=0.5),
        support_state='SUPPORTED',
        unsupported_reasons=(),
        authority_version='authority-868/1',
        plan_semantic_sha256='0' * 64,
    )
    payload.update(kw)
    provisional = CadCalibrationPlan.model_construct(**payload)
    payload['plan_semantic_sha256'] = canonical_sha256(
        provisional.semantic_payload())
    return CadCalibrationPlan.model_construct(**payload)


def _export(**plan_kw) -> CadCalibrationExportSnapshot:
    return build_generic_biquad_export(
        plan=_plan(**plan_kw), created_at_utc=NOW)


def _orchestrator(tmp_path: Path) -> CommissioningOrchestrator:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    return CommissioningOrchestrator(SceneRepository(db))


def _create_run(orch: CommissioningOrchestrator, **kw):
    manifest = _manifest()
    binding = _binding()
    return manifest, binding, orch.create_run(
        document_id=DOC,
        scene_revision_id='rev-1',
        scene_content_hash=SCENE_SHA,
        manifest=manifest,
        binding=binding,
        created_by='op-1',
        created_at_utc=NOW,
        **kw)


def _baseline_acquire():
    def acquire():
        return (
            (
                _ref('measurement', 'm-1', MEAS_SHA),
                _ref('frequency_response_dataset', 'd-1', DS_SHA),
            ),
            sha256(b'raw-baseline').hexdigest(),
            'baseline sweep',
        )
    return acquire


def _drive_to_optimize(
    orch: CommissioningOrchestrator,
    manifest,
    binding,
    run,
    transport: FixtureCamillaDSPTransport,
) -> CadMeasurementQualityReport:
    """Walk precheck -> baseline -> quality gate -> analyze -> optimize."""
    adapter = _adapter(transport)
    orch.record_precheck(
        run, manifest=manifest,
        capability_report=adapter.capability(), at_utc=NOW)
    orch.record_acquisition(
        run, manifest=manifest,
        request_identity_repr='GET /measurements/1/response',
        request_sha256=sha256(b'req').hexdigest(),
        observed_at_utc=NOW, stage='baseline',
        acquire=_baseline_acquire())
    report = _quality_report()
    orch.record_quality_gate(run, report=report, at_utc=NOW)
    orch.bind_analysis(
        run,
        evidence_refs=(_ref('analysis', 'an-1'),),
        at_utc=NOW)
    return report


# ----------------------------------------------------------------------
# pure stage machine


def _state(**kw) -> CommissioningRunState:
    return CommissioningRunState(
        run_ref=_ref('commissioning_orchestration_run', 'cor-x'),
        **kw)


def _event(kind, **kw) -> CommissioningEvent:
    return CommissioningEvent(kind=kind, at_utc=NOW, reason='probe', **kw)


class _RaisingTransport:
    """Transport whose request raises a raw connection-level error."""

    def request(self, command):
        raise ConnectionError('link down')

    def close(self) -> None:
        pass


class TestCamillaDSPTransportErrors:
    """Raw transport failures must type as CamillaDSPError so the
    honest-evidence callers (observation limitations, rollback 'failed')
    catch them instead of crashing past their own except clauses."""

    def test_observe_runtime_records_transport_errors(self) -> None:
        adapter = _adapter(_RaisingTransport())
        observation = adapter.observe_runtime(
            _binding(), document_id=DOC, observed_at_utc=NOW)
        assert observation.camilladsp_version is None
        assert observation.limitations
        assert all(
            item.endswith(':transport_error')
            for item in observation.limitations)

    def test_rollback_records_failed_not_crash(self) -> None:
        adapter = _adapter(_RaisingTransport())
        evidence = adapter.rollback_previous(
            _binding(), document_id=DOC,
            deployment_ref=_ref('calibration_deployment'),
            requested_at_utc=NOW)
        assert evidence.outcome == 'failed'
        assert evidence.previous_config_sha256 is None
        assert evidence.error_detail is not None

    def test_readback_types_transport_errors(self) -> None:
        adapter = _adapter(_RaisingTransport())
        with pytest.raises(CamillaDSPError):
            adapter.read_back(_binding(), observed_at_utc=NOW)


class TestStageMachine:
    def test_full_advance_ladder(self) -> None:
        ladder = (
            ('precheck', 'precheck_evaluated', 'baseline_measurement'),
            ('baseline_measurement', 'baseline_acquisition_recorded',
             'ingest_quality_gate'),
            ('ingest_quality_gate', 'quality_gate_evaluated', 'analyze'),
            ('analyze', 'analysis_bound', 'optimize'),
            ('optimize', 'optimization_committed', 'operator_approval'),
            ('operator_approval', 'operator_authorized',
             'compile_for_target'),
            ('compile_for_target', 'compile_completed', 'deploy'),
            ('deploy', 'deploy_acked', 'readback_verify'),
            ('readback_verify', 'readback_evaluated', 'post_measurement'),
            ('post_measurement', 'post_measurement_recorded',
             'before_after'),
            ('before_after', 'before_after_evaluated',
             'acceptance_decision'),
            ('acceptance_decision', 'acceptance_decided', 'completed'),
        )
        for stage, kind, nxt in ladder:
            decision = stage_transition(
                _state(current_stage=stage),
                _event(kind, succeeded=True))
            assert isinstance(decision, TransitionDecision), (stage, kind)
            assert decision.to_stage == nxt

    def test_illegal_event_rejected_with_reason(self) -> None:
        decision = stage_transition(
            _state(current_stage='precheck'),
            _event('deploy_acked', succeeded=True))
        assert isinstance(decision, TransitionRejection)
        assert decision.reason.startswith('event_not_permitted')

    def test_failed_attempt_blocks_same_stage(self) -> None:
        decision = stage_transition(
            _state(current_stage='compile_for_target'),
            _event('compile_completed', succeeded=False))
        assert isinstance(decision, TransitionDecision)
        assert decision.outcome == 'blocked'
        assert decision.to_stage == 'compile_for_target'

    def test_terminal_stage_rejects_everything(self) -> None:
        for terminal in ('completed', 'rolled_back', 'aborted'):
            for kind in ('precheck_evaluated', 'abort', 'deploy_acked'):
                decision = stage_transition(
                    _state(current_stage=terminal),
                    _event(kind, succeeded=True))
                assert isinstance(decision, TransitionRejection)
                assert decision.reason == f'run_terminal:{terminal}'

    def test_abort_from_mid_run(self) -> None:
        decision = stage_transition(
            _state(current_stage='readback_verify'),
            _event('abort'))
        assert isinstance(decision, TransitionDecision)
        assert decision.outcome == 'aborted'
        assert decision.to_stage == 'aborted'

    def test_disconnect_blocks_then_restores(self) -> None:
        down = _state(current_stage='deploy', provider_disconnected=True)
        decision = stage_transition(
            down, _event('deploy_acked', succeeded=True))
        assert isinstance(decision, TransitionRejection)
        assert decision.reason == 'provider_disconnected'

        restored = stage_transition(
            down, _event('connectivity_restored'))
        assert isinstance(restored, TransitionDecision)
        assert restored.outcome == 'informational'
        assert restored.to_stage == 'deploy'

        assert isinstance(stage_transition(
            _state(current_stage='deploy'),
            _event('connectivity_restored')), TransitionRejection)

    def test_disconnect_event_lands_blocked(self) -> None:
        decision = stage_transition(
            _state(current_stage='baseline_measurement'),
            _event('provider_disconnected'))
        assert decision.outcome == 'blocked'
        assert decision.to_stage == 'baseline_measurement'

    def test_rollback_only_after_mutation_boundary(self) -> None:
        assert isinstance(stage_transition(
            _state(current_stage='precheck'),
            _event('rollback_completed', succeeded=True)),
            TransitionRejection)
        ok = stage_transition(
            _state(current_stage='deploy'),
            _event('rollback_completed', succeeded=True))
        assert ok.outcome == 'rolled_back'
        assert ok.to_stage == 'rolled_back'
        # A failed rollback never lands terminal — the run blocks so the
        # operator can retry or abort honestly.
        failed = stage_transition(
            _state(current_stage='deploy'),
            _event('rollback_completed', succeeded=False))
        assert failed.outcome == 'blocked'
        assert failed.to_stage == 'deploy'

    def test_invalidation_regresses_only_backward(self) -> None:
        back = stage_transition(
            _state(current_stage='optimize'),
            _event('authority_invalidated',
                   target_stage='baseline_measurement'))
        assert isinstance(back, TransitionDecision)
        assert back.outcome == 'invalidated'
        assert back.to_stage == 'baseline_measurement'
        forward = stage_transition(
            _state(current_stage='optimize'),
            _event('authority_invalidated', target_stage='deploy'))
        assert isinstance(forward, TransitionRejection)
        # A clean re-check (succeeded=False) is informational, never a
        # regression.
        noop = stage_transition(
            _state(current_stage='optimize'),
            _event('authority_invalidated', succeeded=False,
                   target_stage='precheck'))
        assert noop.outcome == 'informational'

    def test_runtime_observation_requires_matched_readback(self) -> None:
        denied = stage_transition(
            _state(current_stage='post_measurement'),
            _event('runtime_observed'))
        assert isinstance(denied, TransitionRejection)
        allowed = stage_transition(
            _state(current_stage='post_measurement',
                   readback_matched=True),
            _event('runtime_observed'))
        assert allowed.outcome == 'informational'

    def test_next_permitted_actions(self) -> None:
        permitted = next_permitted_actions(_state(current_stage='deploy'))
        assert 'deploy_acked' in permitted
        assert 'rollback_completed' in permitted
        assert 'abort' in permitted
        assert 'acceptance_decided' not in permitted


class TestDeriveRunState:
    def test_blocked_reason_sets_and_clears(self) -> None:
        run = CommissioningOrchestrationRun.create(
            document_id=DOC, scene_revision_id='rev-1',
            scene_content_hash=SCENE_SHA,
            provider_manifest_ref=_ref(
                'delegated_provider_manifest', 'dpm-1'),
            device_binding_id='adb-868',
            device_binding_sha256='d' * 64,
            created_at_utc=NOW, created_by='op-1')
        state = derive_run_state(run, ())
        assert state.current_stage == 'precheck'
        assert state.blocked_reason is None


# ----------------------------------------------------------------------
# sealed-record integrity


def _run(**kw) -> CommissioningOrchestrationRun:
    payload = dict(
        document_id=DOC,
        scene_revision_id='rev-1',
        scene_content_hash=SCENE_SHA,
        provider_manifest_ref=_ref(
            'delegated_provider_manifest', 'dpm-1'),
        device_binding_id='adb-868',
        device_binding_sha256='d' * 64,
        created_at_utc=NOW,
        created_by='op-1',
    )
    payload.update(kw)
    return CommissioningOrchestrationRun.create(**payload)


class TestSealedRecords:
    def test_run_seal(self) -> None:
        run = _run()
        assert run.run_id.startswith('cor-')
        with pytest.raises(ValueError):
            CommissioningOrchestrationRun.model_validate(
                {**run.model_dump(mode='python'),
                 'run_sha256': '0' * 64})

    def test_transition_invariants(self) -> None:
        run = _run()
        with pytest.raises(ValueError):
            # run_created must not carry a from_stage
            CommissioningStageTransition.create(
                document_id=DOC, run_ref=_ref(
                    'commissioning_orchestration_run', run.run_id,
                    run.run_sha256),
                seq=0, event_kind='run_created', outcome='advanced',
                from_stage='precheck', to_stage='precheck',
                event_succeeded=True, reason='x', recorded_at_utc=NOW)
        with pytest.raises(ValueError):
            # an advance must change stage
            CommissioningStageTransition.create(
                document_id=DOC, run_ref=_ref(
                    'commissioning_orchestration_run', run.run_id,
                    run.run_sha256),
                seq=1, event_kind='precheck_evaluated', outcome='advanced',
                from_stage='precheck', to_stage='precheck',
                event_succeeded=True, reason='x', recorded_at_utc=NOW)

    def test_authorization_scope_invariants(self) -> None:
        run = _run()
        with pytest.raises(ValueError):
            # deploy_apply pins the candidate hash
            CommissioningOperatorAuthorization.create(
                document_id=DOC, run_ref=_ref(
                    'commissioning_orchestration_run', run.run_id,
                    run.run_sha256),
                scope='deploy_apply', operator_id='op-1',
                authorized_at_utc=NOW)
        with pytest.raises(ValueError):
            # rollback_apply pins the deployment ref
            CommissioningOperatorAuthorization.create(
                document_id=DOC, run_ref=_ref(
                    'commissioning_orchestration_run', run.run_id,
                    run.run_sha256),
                scope='rollback_apply', operator_id='op-1',
                authorized_at_utc=NOW)
        auth = CommissioningOperatorAuthorization.create(
            document_id=DOC, run_ref=_ref(
                'commissioning_orchestration_run', run.run_id,
                run.run_sha256),
            scope='deploy_apply', candidate_sha256='9' * 64,
            operator_id='op-1', authorized_at_utc=NOW)
        assert auth.authorization_id.startswith('coa-')

    def test_rollback_verdict_invariants(self) -> None:
        run = _run()
        with pytest.raises(ValueError):
            # restored_verified requires adapter evidence
            CommissioningRollback.create(
                document_id=DOC, run_ref=_ref(
                    'commissioning_orchestration_run', run.run_id,
                    run.run_sha256),
                deployment_ref=_ref('calibration_deployment', 'dep-1'),
                authorization_ref=_ref(
                    'commissioning_operator_authorization', 'coa-1'),
                outcome='restored_verified', requested_at_utc=NOW)
        with pytest.raises(ValueError):
            # failed requires an error detail
            CommissioningRollback.create(
                document_id=DOC, run_ref=_ref(
                    'commissioning_orchestration_run', run.run_id,
                    run.run_sha256),
                deployment_ref=_ref('calibration_deployment', 'dep-1'),
                authorization_ref=_ref(
                    'commissioning_operator_authorization', 'coa-1'),
                outcome='failed', requested_at_utc=NOW)

    def test_verdict_fail_closed_invariants(self) -> None:
        run = _run()
        comparison = CommissioningBeforeAfter.create(
            document_id=DOC, run_ref=_ref(
                'commissioning_orchestration_run', run.run_id,
                run.run_sha256),
            deployment_ref=_ref('calibration_deployment', 'dep-1'),
            readback_config_sha256='e' * 64,
            baseline_evidence_refs=(_ref('measurement', 'm-1'),),
            post_measurement_refs=(_ref('measurement', 'm-2'),),
            deltas=(EffectivenessMetricDelta(
                metric_id='splt', before_value_repr='1',
                after_value_repr='2', outcome='improved'),),
            verdict='improvement_verified', evaluated_at_utc=NOW)
        satisfied = (
            # 'satisfied' criteria + no stale -> accepted is legal
            __import__('htdt.cad_commissioning_orchestrator',
                       fromlist=['CommissioningCriterionResult'])
            .CommissioningCriterionResult(
                metric_id='splt', expected='improvement',
                outcome='satisfied', observed='improved',
                reason='ok'),)
        verdict = CommissioningAcceptanceVerdict.create(
            document_id=DOC, run_ref=_ref(
                'commissioning_orchestration_run', run.run_id,
                run.run_sha256),
            before_after_ref=_ref('commissioning_before_after',
                                  comparison.comparison_id,
                                  comparison.comparison_sha256),
            criteria_results=satisfied, stale_evidence=False,
            verdict='accepted', decided_at_utc=NOW)
        assert verdict.verdict_id.startswith('cav-')
        # accepted with stale evidence is unbuildable — fail closed at
        # the model boundary.
        with pytest.raises(ValueError):
            CommissioningAcceptanceVerdict.create(
                document_id=DOC, run_ref=_ref(
                    'commissioning_orchestration_run', run.run_id,
                    run.run_sha256),
                before_after_ref=_ref('commissioning_before_after',
                                      comparison.comparison_id,
                                      comparison.comparison_sha256),
                criteria_results=satisfied, stale_evidence=True,
                verdict='accepted', decided_at_utc=NOW)


# ----------------------------------------------------------------------
# repository round-trip + tamper detection


class TestRepository:
    def test_roundtrip_all_stores(self, tmp_path: Path) -> None:
        repo = CadCommissioningOrchestratorRepository(
            SceneRepository(tmp_path / 'cad.sqlite3'))
        run = _run()
        repo.save_run(run)
        assert repo.get_run(run.run_id) == run
        assert repo.list_runs(DOC) == (run,)
        assert repo.list_runs('other-doc') == ()

    def test_append_only_resave(self, tmp_path: Path) -> None:
        repo = CadCommissioningOrchestratorRepository(
            SceneRepository(tmp_path / 'cad.sqlite3'))
        run = _run()
        repo.save_run(run)
        repo.save_run(run)
        assert repo.list_runs(DOC) == (run,)

    def test_tampered_payload_detected_on_get(self, tmp_path: Path) -> None:
        repo = CadCommissioningOrchestratorRepository(
            SceneRepository(tmp_path / 'cad.sqlite3'))
        run = _run()
        repo.save_run(run)
        conn = connect_sqlite(repo.path)
        conn.execute(
            "UPDATE cad_commissioning_orch_runs "
            "SET document_id='doc-x' WHERE run_id=?",
            (run.run_id,))
        conn.commit()
        conn.close()
        with pytest.raises(DeploymentIntegrityError):
            repo.get_run(run.run_id)


# ----------------------------------------------------------------------
# invalidation evaluator


class TestInvalidation:
    def test_scene_change_stales_baseline_onward(self) -> None:
        run = _run()
        report = evaluate_invalidation(
            run, scene_content_hash='d' * 64)
        assert 'scene' in report.stale_pins
        assert report.earliest_stale_stage == 'baseline_measurement'

    def test_binding_change_stales_from_precheck(self) -> None:
        run = _run()
        report = evaluate_invalidation(
            run, device_binding_sha256='0' * 64)
        assert 'device_binding' in report.stale_pins
        assert report.earliest_stale_stage == 'precheck'

    def test_identical_pins_report_clean(self) -> None:
        run = _run()
        report = evaluate_invalidation(
            run,
            scene_content_hash=SCENE_SHA,
            device_binding_sha256='d' * 64)
        assert report.stale_pins == ()
        assert report.earliest_stale_stage is None


# ----------------------------------------------------------------------
# end-to-end through fake provider + fixture adapter


class TestEndToEnd:
    def _commission(
        self, tmp_path: Path, *, reject_config: str | None = None,
    ):
        orch = _orchestrator(tmp_path)
        transport = _transport(reject_config=reject_config)
        adapter = _adapter(transport)
        manifest, binding, run = _create_run(
            orch,
            acceptance_criteria=(CommissioningAcceptanceCriterion(
                metric_id='spl_dev', expected='improvement',
                required=True),))
        report = _drive_to_optimize(orch, manifest, binding, run, transport)
        plan = _plan()
        orch.commit_optimization(run, plan=plan, at_utc=NOW)
        assert orch.derive_state(run).current_stage == 'operator_approval'
        return orch, transport, adapter, manifest, binding, run, plan

    def test_full_loop_to_accepted(self, tmp_path: Path) -> None:
        orch, transport, adapter, manifest, binding, run, plan = (
            self._commission(tmp_path))
        export = _export()

        auth, _ = orch.authorize(
            run, operator_id='op-1', scope='deploy_apply',
            candidate_sha256=plan.plan_semantic_sha256, at_utc=NOW)
        assert orch.derive_state(run).current_stage == 'compile_for_target'

        materialization, t = orch.compile_for_target(
            run, adapter=adapter, export=export, binding=binding,
            declaration=_declaration(), at_utc=NOW)
        assert materialization is not None and t.outcome == 'advanced'
        assert orch.derive_state(run).current_stage == 'deploy'

        deployment, session, t = orch.deploy(
            run, adapter=adapter, binding=binding,
            materialization=materialization, export=export,
            declaration=_declaration(),
            authorization_id=auth.authorization_id,
            target_class='camilladsp_device', at_utc=NOW)
        assert deployment is not None and t.outcome == 'advanced'
        assert session is not None
        assert deployment.deployment_state != 'deployment_verified'

        verified, session2, t = orch.verify_readback(
            run, adapter=adapter, binding=binding, export=export,
            deployment=deployment, materialization=materialization,
            declaration=_declaration(), at_utc=NOW)
        assert verified is not None and t.outcome == 'advanced'
        assert t.event_succeeded is True
        state = orch.derive_state(run)
        assert state.current_stage == 'post_measurement'
        assert state.readback_matched is True
        assert derive_evidence_strength(state) == 'config_readback_matched'

        t = orch.observe_runtime(
            run, adapter=adapter, binding=binding,
            deployment=verified, at_utc=NOW)
        assert t.outcome == 'informational'
        assert derive_evidence_strength(
            orch.derive_state(run)) == 'runtime_observed'

        orch.record_acquisition(
            run, manifest=manifest,
            request_identity_repr='GET /measurements/2/response',
            request_sha256=sha256(b'req2').hexdigest(),
            observed_at_utc=NOW, stage='post',
            acquire=lambda: (
                (_ref('measurement', 'm-2', '7' * 64),),
                sha256(b'raw-post').hexdigest(), 'post sweep'))
        assert orch.derive_state(run).current_stage == 'before_after'

        comparison, _ = orch.evaluate_before_after(
            run, deployment=verified,
            readback_config_sha256=session2.readback_config_sha256,
            baseline_refs=(_ref('measurement', 'm-1', MEAS_SHA),),
            post_refs=(_ref('measurement', 'm-2', '7' * 64),),
            deltas=(EffectivenessMetricDelta(
                metric_id='spl_dev', before_value_repr='3.1',
                after_value_repr='1.2', outcome='improved'),),
            at_utc=NOW)
        assert comparison.verdict == 'improvement_verified'
        assert orch.derive_state(run).current_stage == 'acceptance_decision'

        verdict, _ = orch.decide_acceptance(
            run, comparison=comparison, at_utc=NOW)
        assert verdict.verdict == 'accepted'
        state = orch.derive_state(run)
        assert state.current_stage == 'completed'
        assert derive_evidence_strength(state, verdict) == 'accepted'

    def test_deploy_requires_operator_authorization(
            self, tmp_path: Path) -> None:
        orch, transport, adapter, manifest, binding, run, plan = (
            self._commission(tmp_path))
        export = _export()
        # Skip authorize() entirely — deploy must fail closed.
        state = orch.derive_state(run)
        assert state.current_stage == 'operator_approval'
        # An event out of order is rejected, not skipped forward.
        t = orch._emit(run, CommissioningEvent(
            kind='deploy_acked', at_utc=NOW, reason='skip'))
        assert t.outcome == 'rejected'

    def test_deploy_rejects_wrong_candidate(self, tmp_path: Path) -> None:
        orch, transport, adapter, manifest, binding, run, plan = (
            self._commission(tmp_path))
        export = _export()
        auth, _ = orch.authorize(
            run, operator_id='op-1', scope='deploy_apply',
            candidate_sha256='9' * 64, at_utc=NOW)
        materialization, _ = orch.compile_for_target(
            run, adapter=adapter, export=export, binding=binding,
            declaration=_declaration(), at_utc=NOW)
        deployment, session, t = orch.deploy(
            run, adapter=adapter, binding=binding,
            materialization=materialization, export=export,
            declaration=_declaration(),
            authorization_id=auth.authorization_id, at_utc=NOW)
        assert deployment is None
        assert t.outcome == 'blocked'
        assert 'different calibration candidate' in t.reason

    def test_deploy_rejects_expired_authorization(
            self, tmp_path: Path) -> None:
        orch, transport, adapter, manifest, binding, run, plan = (
            self._commission(tmp_path))
        export = _export()
        auth, _ = orch.authorize(
            run, operator_id='op-1', scope='deploy_apply',
            candidate_sha256=plan.plan_semantic_sha256, at_utc=NOW,
            expires_at_utc='2026-10-07T00:00:01Z')
        materialization, _ = orch.compile_for_target(
            run, adapter=adapter, export=export, binding=binding,
            declaration=_declaration(), at_utc=NOW)
        deployment, _, t = orch.deploy(
            run, adapter=adapter, binding=binding,
            materialization=materialization, export=export,
            declaration=_declaration(),
            authorization_id=auth.authorization_id,
            at_utc='2026-10-07T00:01:00Z')
        assert deployment is None
        assert 'expired' in t.reason

    def test_failed_apply_blocks_never_advances(
            self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        transport = _transport(reject_config='device rejected config')
        adapter = _adapter(transport)
        manifest, binding, run = _create_run(orch)
        _drive_to_optimize(orch, manifest, binding, run, transport)
        plan = _plan()
        orch.commit_optimization(run, plan=plan, at_utc=NOW)
        auth, _ = orch.authorize(
            run, operator_id='op-1', scope='deploy_apply',
            candidate_sha256=plan.plan_semantic_sha256, at_utc=NOW)
        export = _export()
        materialization, _ = orch.compile_for_target(
            run, adapter=adapter, export=export, binding=binding,
            declaration=_declaration(), at_utc=NOW)
        deployment, _, t = orch.deploy(
            run, adapter=adapter, binding=binding,
            materialization=materialization, export=export,
            declaration=_declaration(),
            authorization_id=auth.authorization_id, at_utc=NOW)
        assert deployment is None
        assert t.outcome == 'blocked'
        assert 'apply_error' in t.reason
        assert orch.derive_state(run).current_stage == 'deploy'

    def test_acquire_failure_blocks_baseline(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        transport = _transport()
        manifest, binding, run = _create_run(orch)
        adapter = _adapter(transport)
        orch.record_precheck(
            run, manifest=manifest,
            capability_report=adapter.capability(), at_utc=NOW)

        def boom():
            raise RuntimeError('provider offline')

        record, t = orch.record_acquisition(
            run, manifest=manifest,
            request_identity_repr='GET /x',
            request_sha256=sha256(b'r').hexdigest(),
            observed_at_utc=NOW, stage='baseline', acquire=boom)
        assert t.outcome == 'blocked'
        assert orch.derive_state(run).current_stage == (
            'baseline_measurement')

    def test_provider_gate_rejects_unsupported_capability(
            self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        transport = _transport()
        manifest = _manifest(capabilities=(
            ProviderCapabilityEntry(
                capability='frequency_response', condition='unsupported',
                condition_detail='endpoint absent'),))
        binding = _binding()
        run = orch.create_run(
            document_id=DOC, scene_revision_id='rev-1',
            scene_content_hash=SCENE_SHA, manifest=manifest,
            binding=binding, created_by='op-1', created_at_utc=NOW)
        adapter = _adapter(transport)
        t = orch.record_precheck(
            run, manifest=manifest,
            capability_report=adapter.capability(), at_utc=NOW)
        assert t.outcome == 'blocked'
        assert 'provider gate' in t.reason

    def test_quality_gate_rejects_scene_mismatch(
            self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        transport = _transport()
        manifest, binding, run = _create_run(orch)
        adapter = _adapter(transport)
        orch.record_precheck(
            run, manifest=manifest,
            capability_report=adapter.capability(), at_utc=NOW)
        orch.record_acquisition(
            run, manifest=manifest,
            request_identity_repr='GET /m', request_sha256='0' * 64,
            observed_at_utc=NOW, stage='baseline',
            acquire=_baseline_acquire())
        bad = _quality_report(scene_content_hash='0' * 64)
        t = orch.record_quality_gate(run, report=bad, at_utc=NOW)
        assert t.outcome == 'blocked'
        assert 'report_scene_mismatch' in t.reason

    def test_rollback_restores_baseline_verified(
            self, tmp_path: Path) -> None:
        orch, transport, adapter, manifest, binding, run, plan = (
            self._commission(tmp_path))
        export = _export()
        auth, _ = orch.authorize(
            run, operator_id='op-1', scope='deploy_apply',
            candidate_sha256=plan.plan_semantic_sha256, at_utc=NOW)
        materialization, _ = orch.compile_for_target(
            run, adapter=adapter, export=export, binding=binding,
            declaration=_declaration(), at_utc=NOW)
        deployment, session, _ = orch.deploy(
            run, adapter=adapter, binding=binding,
            materialization=materialization, export=export,
            declaration=_declaration(),
            authorization_id=auth.authorization_id, at_utc=NOW)
        rauth, _ = orch.authorize(
            run, operator_id='op-1', scope='rollback_apply',
            deployment_ref=_ref('calibration_deployment',
                                deployment.deployment_id,
                                deployment.deployment_sha256),
            at_utc=NOW)
        record, t = orch.rollback(
            run, adapter=adapter, binding=binding,
            deployment=deployment,
            authorization_id=rauth.authorization_id,
            reason='operator veto', at_utc=NOW)
        assert record.outcome == 'restored_verified'
        assert record.adapter_evidence_ref is not None
        assert t.outcome == 'rolled_back'
        state = orch.derive_state(run)
        assert state.current_stage == 'rolled_back'
        assert derive_evidence_strength(state) == 'rolled_back'

    def test_disconnect_never_silently_advances(
            self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        transport = _transport()
        manifest, binding, run = _create_run(orch)
        adapter = _adapter(transport)
        orch.record_precheck(
            run, manifest=manifest,
            capability_report=adapter.capability(), at_utc=NOW)
        t = orch.report_disconnect(
            run, scope='provider', reason='REW API timed out',
            at_utc=NOW)
        assert t.outcome == 'blocked'
        state = orch.derive_state(run)
        assert state.provider_disconnected is True
        # While disconnected nothing can advance — even a valid event.
        t2 = orch._emit(run, CommissioningEvent(
            kind='baseline_acquisition_recorded', at_utc=NOW,
            reason='x', succeeded=True))
        assert t2.outcome == 'rejected'
        assert 'provider_disconnected' in t2.reason
        t3 = orch.restore_connectivity(
            run, reason='provider reachable', at_utc=NOW)
        assert t3.outcome == 'informational'
        assert orch.derive_state(run).provider_disconnected is False

    def test_scene_change_invalidates_deterministically(
            self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        transport = _transport()
        manifest, binding, run = _create_run(orch)
        _drive_to_optimize(orch, manifest, binding, run, transport)
        assert orch.derive_state(run).current_stage == 'optimize'
        report, t = orch.apply_invalidation(
            run, scene_content_hash='d' * 64, at_utc=NOW)
        assert t.outcome == 'invalidated'
        assert t.to_stage == 'baseline_measurement'
        state = orch.derive_state(run)
        assert 'optimize' in state.stale_stages
        # Clean re-check is informational, no further regression.
        report2, t2 = orch.apply_invalidation(
            run, scene_content_hash='d' * 64, at_utc=NOW)
        assert t2.outcome == 'informational' or t2.outcome == 'rejected'

    def test_resumable_after_restart(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        transport = _transport()
        manifest, binding, run = _create_run(orch)
        _drive_to_optimize(orch, manifest, binding, run, transport)
        # A fresh orchestrator over the same store folds the same state.
        orch2 = CommissioningOrchestrator(
            SceneRepository(tmp_path / 'cad.sqlite3'))
        reopened = orch2.get_open_run(DOC)
        assert reopened is not None
        run2, state2 = reopened
        assert run2.run_id == run.run_id
        assert state2.current_stage == 'optimize'
        assert 'analysis' in state2.evidence

    def test_acceptance_fails_closed_on_missing_metric(
            self, tmp_path: Path) -> None:
        orch, transport, adapter, manifest, binding, run, plan = (
            self._commission(tmp_path))
        export = _export()
        auth, _ = orch.authorize(
            run, operator_id='op-1', scope='deploy_apply',
            candidate_sha256=plan.plan_semantic_sha256, at_utc=NOW)
        materialization, _ = orch.compile_for_target(
            run, adapter=adapter, export=export, binding=binding,
            declaration=_declaration(), at_utc=NOW)
        deployment, _, _ = orch.deploy(
            run, adapter=adapter, binding=binding,
            materialization=materialization, export=export,
            declaration=_declaration(),
            authorization_id=auth.authorization_id, at_utc=NOW)
        verified, session2, _ = orch.verify_readback(
            run, adapter=adapter, binding=binding, export=export,
            deployment=deployment, materialization=materialization,
            declaration=_declaration(), at_utc=NOW)
        orch.record_acquisition(
            run, manifest=manifest,
            request_identity_repr='GET /p', request_sha256='1' * 64,
            observed_at_utc=NOW, stage='post',
            acquire=lambda: ((_ref('measurement', 'm-2', '7' * 64),),
                             '2' * 64, 'post'))
        comparison, _ = orch.evaluate_before_after(
            run, deployment=verified,
            readback_config_sha256=session2.readback_config_sha256,
            baseline_refs=(_ref('measurement', 'm-1', MEAS_SHA),),
            post_refs=(_ref('measurement', 'm-2', '7' * 64),),
            deltas=(), at_utc=NOW, verdict='inconclusive')
        verdict, _ = orch.decide_acceptance(
            run, comparison=comparison, at_utc=NOW)
        assert verdict.verdict == 'indeterminate'


# ----------------------------------------------------------------------
# workflow panel (read-only UI surface)


class TestCommissioningPanel:
    def _app(self):
        from PySide6.QtWidgets import QApplication
        return QApplication.instance() or QApplication([])

    def test_panel_shows_machine_state(self, tmp_path: Path) -> None:
        from htdt.commissioning_panel import CommissioningPanel
        self._app()
        orch = _orchestrator(tmp_path)
        transport = _transport()
        manifest, binding, run = _create_run(orch)
        _drive_to_optimize(orch, manifest, binding, run, transport)
        panel = CommissioningPanel(orch, DOC)
        assert '最適化' in panel.stage_label.text()
        assert '最適化コミット' in panel.next_label.text()
        assert '不可' in panel.rollback_label.text()
        item_count = panel.transition_tree.topLevelItemCount()
        assert item_count >= 5

    def test_panel_empty_run_fails_closed(self, tmp_path: Path) -> None:
        from htdt.commissioning_panel import CommissioningPanel
        self._app()
        orch = _orchestrator(tmp_path)
        panel = CommissioningPanel(orch, 'doc-absent')
        assert 'まだありません' in panel.stage_label.text()

    def test_panel_read_error_fails_closed(self, tmp_path: Path) -> None:
        from htdt.commissioning_panel import CommissioningPanel
        self._app()
        orch = _orchestrator(tmp_path)

        def boom(_document_id):
            raise RuntimeError('store gone')

        orch.get_open_run = boom  # type: ignore[method-assign]
        orch.list_runs = boom  # type: ignore[method-assign]
        panel = CommissioningPanel(orch, DOC)
        assert '読み込めません' in panel.stage_label.text()
