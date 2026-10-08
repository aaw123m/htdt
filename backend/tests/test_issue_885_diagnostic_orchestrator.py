"""#885 guided troubleshooting orchestrator.

The sealed diagnostic loop — OBSERVATION → HYPOTHESES → discriminating
test plans → machine measurement/read-back → evidence update →
rank/eliminate → resolution — over the #869 sweep engine, #876 channel
verification, #806/#878 device read-back and the #719 evidence ladder.

Covers: sealed-record integrity, the pure transition machine, plan-level
safety (forbidden actions unrepresentable, mutation authorization,
acoustic caps, rollback plans), deterministic ranking, repository
round-trip + tamper detection, and end-to-end walks of the declared
fault trees through scripted backends — including the honest
terminations ``unresolved``/``needs_inspection``.
"""

from __future__ import annotations

import itertools
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_avr_lan_adapter import (
    AVR_LAN_ADAPTER_ID,
    AVR_LAN_DEVICE_FAMILY,
    AvrLanCalibrationAdapter,
    FakeAvrLanTransport,
)
from htdt.cad_calibration import (
    CadCalibrationChannel,
    CadCalibrationPlan,
    CadDeviceCapabilityConstraints,
    build_generic_biquad_export,
)
from htdt.cad_channel_identity_authority import build_channel_identity_chain
from htdt.cad_channel_verification import build_verification_plan
from htdt.cad_channel_verification_repository import (
    CadChannelVerificationRepository,
)
from htdt.cad_device_adapter import build_device_binding
from htdt.cad_diagnostic_hypothesis_repository import (
    CadDiagnosticHypothesisRepository,
)
from htdt.cad_diagnostic_orchestrator import (
    DiagnosticEvent,
    DiagnosticObservationRecord,
    DiagnosticOrchestrationError,
    DiagnosticOrchestrator,
    DiagnosticRejection,
    DiagnosticSafetyError,
    DiagnosticSessionRecord,
    DiagnosticStageTransition,
    DiagnosticTestPlan,
    DiscriminationRule,
    FAULT_TREES,
    FaultTestTemplate,
    compute_hypothesis_states,
    derive_session_state,
    evaluate_observation_effects,
    evaluate_test_plan_safety,
    hypothesis_entry_binding,
    observation_binding,
    plan_binding,
    select_next_template,
    session_binding,
    stage_transition,
)
from htdt.cad_diagnostic_orchestrator_repository import (
    CadDiagnosticOrchestratorRepository,
    DeploymentConflictError,
    DeploymentIntegrityError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.cad_sweep_acquisition import (
    AcquisitionRequest,
    ArmConfirmation,
    ChannelRouting,
    FakeAudioBackend,
    UnsupportedConfigurationError,
    default_fake_scenario,
)
from htdt.cad_sweep_acquisition_repository import (
    CadSweepAcquisitionRepository,
)
from htdt.canonical_json import canonical_sha256

DOC = 'doc-885'
NOW = '2026-10-08T00:00:00+00:00'


def _clock():
    seq = itertools.count()
    base = '2026-10-08T00:00:%02dZ'
    return lambda: base % (next(seq) % 60)


def _symptom_ref() -> AuthorityRef:
    return AuthorityRef(
        kind='channel_verification_verdict',
        ref_id='cvvd-' + 'a' * 24,
        ref_sha256='a' * 64,
    )


def _routing(channel: int, loopback: int | None = 1) -> ChannelRouting:
    return ChannelRouting(
        playback_device_id='fake-duplex-0',
        playback_channel=channel,
        capture_device_id='fake-duplex-0',
        capture_channel=0,
        loopback_input_channel=loopback,
    )


def _arming(request: AcquisitionRequest) -> ArmConfirmation:
    r = request.routing
    return ArmConfirmation(
        acknowledged_playback_device_id=r.playback_device_id,
        acknowledged_playback_channel=r.playback_channel,
        acknowledged_capture_device_id=r.capture_device_id,
        acknowledged_capture_channel=r.capture_channel,
        acknowledged_level_dbfs=request.stimulus.level_dbfs,
    )


class _ScriptedChannelBackend(FakeAudioBackend):
    """Deterministic backend whose acoustic response differs per playback
    channel — stands in for distinct physical speakers."""

    backend_id = 'scripted-audio-io'
    backend_version = 'scripted-1'

    def __init__(self, channel_scenarios: dict[int, dict]):
        super().__init__(default_fake_scenario())
        self._channel_scenarios = dict(channel_scenarios)

    def open_stream(self, config):
        channel = config.routing.playback_channel
        knobs = self._channel_scenarios.get(channel)
        if knobs is None:
            raise UnsupportedConfigurationError(
                f'no output wired on channel {channel}')
        self.scenario = default_fake_scenario(**knobs)
        return super().open_stream(config)


def _channel(channel_id: str, gain_db: float) -> CadCalibrationChannel:
    return CadCalibrationChannel(
        channel_id=channel_id,
        role_id=channel_id.upper(),
        source_entity_id=f'spk-{channel_id}',
        physical_output_id=f'out-{channel_id}',
        sample_rate_hz=48000,
        gain_db=gain_db,
        delay_s=0.0,
        polarity='normal',
        crossovers=(),
        peq=(),
        routing=(f'out-{channel_id}',),
    )


def _calibration_plan(
    channels: tuple[CadCalibrationChannel, ...] = (_channel('fl', 1.5),),
) -> CadCalibrationPlan:
    payload = {
        'plan_id': 'plan-885',
        'plan_version': '1',
        'created_at_utc': NOW,
        'source_kind': 'provided_fixture',
        'document_id': DOC,
        'scene_revision_id': 'rev-1',
        'scene_content_hash': 'a' * 64,
        'system_variant_id': 'var-1',
        'system_variant_sha256': 'b' * 64,
        'source_measurement_id': 'm-1',
        'source_measurement_sha256': 'c' * 64,
        'source_dataset_id': 'd-1',
        'source_dataset_sha256': 'e' * 64,
        'measurement_quality_report_id': 'q-1',
        'measurement_quality_report_sha256': 'f' * 64,
        'sample_rate_hz': 48000,
        'channels': channels,
        'target_curve': None,
        'max_boost_db': 6.0,
        'max_cut_db': 10.0,
        'device_constraints': CadDeviceCapabilityConstraints(
            capability_id='test-device-1',
            capability_version='1',
            supported_sample_rates_hz=(48000,),
            supported_filter_types=('peaking',),
            channel_gain_resolution_db=0.5,
        ),
        'support_state': 'SUPPORTED',
        'unsupported_reasons': (),
        'plan_semantic_sha256': '0' * 64,
    }
    provisional = CadCalibrationPlan.model_construct(**payload)
    return CadCalibrationPlan(
        **{
            **payload,
            'plan_semantic_sha256': canonical_sha256(
                provisional.semantic_payload()),
        }
    )


def _export(channels=(_channel('fl', 1.5),)):
    return build_generic_biquad_export(
        plan=_calibration_plan(channels), created_at_utc=NOW)


def _avr_binding():
    return build_device_binding(
        adapter_id=AVR_LAN_ADAPTER_ID,
        device_family=AVR_LAN_DEVICE_FAMILY,
        device_model='AVR-X3800H',
        device_serial='localhost:23',
        firmware_version='1.4.0',
        routing=(('fl', 'out-fl'),),
        bound_at_utc=NOW,
    )


def _avr_adapter(transport: FakeAvrLanTransport) -> AvrLanCalibrationAdapter:
    return AvrLanCalibrationAdapter(
        {'localhost:23': transport, 'localhost': transport},
        simulated=True,
    )


def _cv_plan(channels=('fl', 'main')):
    def _chain(logical: str):
        return build_channel_identity_chain(
            document_id=DOC,
            logical_channel=logical,
            channel_class='bed_channel',
            expected_speaker_entity_ids=(f'spk-{logical}',),
            declared_at_utc=NOW,
        )
    return build_verification_plan(
        document_id=DOC,
        chains=[_chain(ch) for ch in channels],
        routings={
            ch: _routing(i) for i, ch in enumerate(channels)
        },
        sample_rate_hz=48000,
        stimulus_start_hz=100.0,
        stimulus_end_hz=8000.0,
        stimulus_duration_s=0.05,
        stimulus_level_dbfs=-12.0,
        repetitions=2,
        reference_channel='main',
        created_at_utc='2026-10-08T00:00:00Z',
    )


@pytest.fixture()
def repos(tmp_path: Path):
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    scene = SceneRepository(db)
    return dict(
        scene=scene,
        diagnostic=CadDiagnosticOrchestratorRepository(scene),
        hypotheses=CadDiagnosticHypothesisRepository(scene),
        sweep=CadSweepAcquisitionRepository(scene),
        channel=CadChannelVerificationRepository(scene),
    )


def _orchestrator(repos) -> DiagnosticOrchestrator:
    return DiagnosticOrchestrator(
        repository=repos['diagnostic'],
        hypothesis_repository=repos['hypotheses'],
        sweep_repository=repos['sweep'],
        channel_repository=repos['channel'],
        clock=_clock(),
    )


def _open(orch: DiagnosticOrchestrator, tree_id='channel_missing_output'):
    return orch.open_session(
        document_id=DOC,
        symptom_ref=_symptom_ref(),
        symptom_summary='fl チャンネルの出力がない',
        fault_tree_id=tree_id,
        reference_channel='main',
        operator_id='operator-1',
    )


# ---------------------------------------------------------------------------
# Record sealing
# ---------------------------------------------------------------------------


class TestRecordSealing:
    def test_session_seals_and_rejects_tamper(self, repos) -> None:
        orch = _orchestrator(repos)
        session, entries, case, hypotheses = _open(orch)
        assert session.session_id.startswith('diag-')
        assert session.session_sha256 == canonical_sha256(
            session.identity_payload())
        assert case.case_id == session.case_ref.ref_id
        assert len(entries) == len(
            FAULT_TREES['channel_missing_output'].hypotheses)
        assert len(hypotheses) == len(entries)
        payload = session.model_dump()
        payload['symptom_summary'] = 'tampered'
        with pytest.raises(ValidationError):
            DiagnosticSessionRecord.model_validate(payload)

    def test_every_session_table_present(self, repos) -> None:
        with connect_sqlite(repos['scene'].path) as connection:
            tables = {
                r[0] for r in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")
            }
        for table in (
            'cad_diagnostic_sessions',
            'cad_diagnostic_session_transitions',
            'cad_diagnostic_session_hypotheses',
            'cad_diagnostic_test_plans',
            'cad_diagnostic_observations',
            'cad_diagnostic_evidence_updates',
            'cad_diagnostic_resolutions',
            'cad_diagnostic_authorizations',
        ):
            assert table in tables

    def test_forbidden_action_unrepresentable(self) -> None:
        with pytest.raises(ValidationError, match='protective_earth'):
            FaultTestTemplate(
                template_id='unsafe',
                mechanism='operator_observation',
                safety_class='read_only',
                test_label='接地を外してテスト',
                action_label='protective_earth defeat test',
                discriminates=('x',),
            )

    def test_reconfiguration_requires_rollback(self) -> None:
        with pytest.raises(ValidationError, match='rollback'):
            FaultTestTemplate(
                template_id='reconf',
                mechanism='operator_observation',
                safety_class='reconfiguration',
                test_label='配線切替テスト',
                action_label='reconfigure wiring',
                discriminates=('x',),
            )

    def test_fault_trees_declared(self) -> None:
        assert len(FAULT_TREES) >= 3
        for tree in FAULT_TREES.values():
            assert tree.hypotheses
            assert tree.test_templates
            for h in tree.hypotheses:
                assert h.expected_signatures
            for t in tree.test_templates:
                for key in t.discriminates:
                    assert tree.hypothesis(key) is not None


# ---------------------------------------------------------------------------
# Pure transition machine + derived state
# ---------------------------------------------------------------------------


class TestStageMachine:
    def test_unknown_event_rejected(self, repos) -> None:
        orch = _orchestrator(repos)
        session, _e, _c, _h = _open(orch)
        state = orch.session_state(session)
        assert state.current_stage == 'test_planning'
        decision = stage_transition(state, DiagnosticEvent(
            kind='observation_recorded',
            at_utc=NOW,
            reason='late observation',
        ))
        assert isinstance(decision, DiagnosticRejection)

    def test_terminal_session_blocks_events(self, repos) -> None:
        orch = _orchestrator(repos)
        session, _e, _c, _h = _open(orch)
        orch.abort(session, reason='operator cancelled')
        state = orch.session_state(session)
        assert state.terminal and state.current_stage == 'aborted'
        decision = stage_transition(state, DiagnosticEvent(
            kind='test_selected', at_utc=NOW, reason='x'))
        assert isinstance(decision, DiagnosticRejection)

    def test_blocked_transition_does_not_advance(self, repos) -> None:
        orch = _orchestrator(repos)
        session, _e, _c, _h = _open(orch)
        t = orch._emit(session, DiagnosticEvent(
            kind='test_executed',
            at_utc=NOW,
            reason='attempted execute at wrong stage',
            succeeded=False,
        ))
        assert t.outcome == 'rejected'
        assert orch.session_state(session).current_stage == 'test_planning'

    def test_permitted_actions_follow_stage(self, repos) -> None:
        orch = _orchestrator(repos)
        session, _e, _c, _h = _open(orch)
        assert 'test_selected' in orch.next_permitted_actions(session)


# ---------------------------------------------------------------------------
# Safety evaluation
# ---------------------------------------------------------------------------


class TestSafetyPolicy:
    def test_acoustic_over_session_cap_blocked(self, repos) -> None:
        orch = _orchestrator(repos)
        session, _e, _c, _h = _open(orch, 'sub_output_low')
        # Drive to the first acoustic template: read-back first.
        plan = orch.plan_next_test(session)
        assert plan.mechanism == 'device_readback'
        assert plan.safety_class == 'read_only'
        # Tighten the session exposure cap below the template's level:
        payload = session.model_dump(
            mode='python',
            exclude={'session_id', 'session_sha256'})
        payload['max_stimulus_level_dbfs'] = -20.0
        tight = DiagnosticSessionRecord.create(**payload)
        orch._repository.save_session(tight)
        template = FAULT_TREES['sub_output_low'].template('sub_only_sweep')
        verdict = evaluate_test_plan_safety(template, tight)
        assert verdict.verdict == 'requires_level_cap'

    def test_device_mutation_requires_authorization(self, repos) -> None:
        template = FaultTestTemplate(
            template_id='mutate',
            mechanism='sweep_acquisition',
            safety_class='device_mutation',
            test_label='設定変更テスト',
            action_label='apply a device change',
            discriminates=('x',),
        )
        session = DiagnosticSessionRecord.create(
            document_id=DOC,
            symptom_ref=_symptom_ref(),
            symptom_summary='s',
            fault_tree_id='sub_output_low',
            case_ref=AuthorityRef(
                kind='diagnostic_case',
                ref_id='diagcase-' + 'b' * 24,
                ref_sha256='b' * 64),
            opened_at_utc=NOW,
            opened_by='op',
            authority_version='diagnostic-orchestrator-1',
        )
        verdict = evaluate_test_plan_safety(template, session)
        assert verdict.verdict == 'requires_authorization'
        plan = DiagnosticTestPlan.create(
            document_id=DOC,
            session_ref=session_binding(session),
            template_id='mutate',
            plan_seq=0,
            mechanism='sweep_acquisition',
            safety_class='device_mutation',
            test_label='設定変更テスト',
            action_label='apply a device change',
            discriminates=('x',),
            authorization_class='operator_authorization',
            created_at_utc=NOW,
        )
        assert plan.authorization_class == 'operator_authorization'

    def test_mutation_execute_blocked_without_authorization(
            self, repos) -> None:
        orch = _orchestrator(repos)
        session, _e, _c, _h = _open(orch)
        plan = DiagnosticTestPlan.create(
            document_id=DOC,
            session_ref=session_binding(session),
            template_id='manual-mutation',
            plan_seq=0,
            mechanism='sweep_acquisition',
            safety_class='device_mutation',
            test_label='設定変更',
            action_label='apply change',
            discriminates=('device_not_deployed',),
            authorization_class='operator_authorization',
            config={'channels': ('fl',)},
            created_at_utc=NOW,
        )
        orch._repository.save_test_plan(plan)
        orch._emit(session, DiagnosticEvent(
            kind='test_selected',
            at_utc=NOW,
            reason='manual mutation plan selected',
            evidence_refs=(plan_binding(plan),),
        ))
        with pytest.raises(DiagnosticSafetyError, match='authorization'):
            orch.execute_plan(
                session, plan,
                backend=FakeAudioBackend(default_fake_scenario()),
                routings={'fl': _routing(0)},
                arming=_arming,
            )
        assert orch.session_state(session).blocked_reason is not None

    def test_authorized_mutation_consumes_once(self, repos) -> None:
        orch = _orchestrator(repos)
        session, _e, _c, _h = _open(orch)
        plan = DiagnosticTestPlan.create(
            document_id=DOC,
            session_ref=session_binding(session),
            template_id='manual-mutation',
            plan_seq=0,
            mechanism='sweep_acquisition',
            safety_class='device_mutation',
            test_label='設定変更',
            action_label='apply change',
            discriminates=('device_not_deployed',),
            authorization_class='operator_authorization',
            config={'channels': ('fl',)},
            created_at_utc=NOW,
        )
        orch._repository.save_test_plan(plan)
        auth = orch.authorize_plan(
            session, plan, operator_id='operator-1')
        orch._emit(session, DiagnosticEvent(
            kind='test_selected',
            at_utc=NOW,
            reason='manual mutation plan selected',
            evidence_refs=(plan_binding(plan),),
        ))
        obs, _update = orch.execute_plan(
            session, plan,
            backend=FakeAudioBackend(default_fake_scenario()),
            routings={'fl': _routing(0)},
            arming=_arming,
            authorization=auth,
        )
        assert obs.authorization_ref is not None
        assert obs.authorization_ref.ref_id == auth.authorization_id
        # A second plan + the same one-shot authorization must fail.
        plan2 = DiagnosticTestPlan.create(
            document_id=DOC,
            session_ref=session_binding(session),
            template_id='manual-mutation-2',
            plan_seq=1,
            mechanism='sweep_acquisition',
            safety_class='device_mutation',
            test_label='設定変更2',
            action_label='apply change again',
            discriminates=('device_not_deployed',),
            authorization_class='operator_authorization',
            config={'channels': ('fl',)},
            created_at_utc=NOW,
        )
        orch._repository.save_test_plan(plan2)
        orch._emit(session, DiagnosticEvent(
            kind='test_selected',
            at_utc=NOW,
            reason='second mutation plan',
            evidence_refs=(plan_binding(plan2),),
        ))
        with pytest.raises(DiagnosticSafetyError):
            orch.execute_plan(
                session, plan2,
                backend=FakeAudioBackend(default_fake_scenario()),
                routings={'fl': _routing(0)},
                arming=_arming,
                authorization=auth,
            )


# ---------------------------------------------------------------------------
# Repository round-trip + tamper detection
# ---------------------------------------------------------------------------


class TestRepository:
    def test_round_trip_and_append_only(self, repos) -> None:
        repo = repos['diagnostic']
        orch = _orchestrator(repos)
        session, entries, _c, _h = _open(orch)
        assert repo.get_session(session.session_id) == session
        assert repo.list_sessions(DOC) == (session,)
        transitions = repo.list_transitions(session.session_id)
        assert len(transitions) == 3
        assert repo.get_hypothesis_entry(entries[0].entry_id) == entries[0]
        assert repo.list_hypothesis_entries(
            session.session_id) == entries
        # Re-saving the identical sealed record is idempotent.
        repo.save_session(session)
        repo.save_session(session)

    def test_tamper_detected_on_read(self, repos) -> None:
        repo = repos['diagnostic']
        orch = _orchestrator(repos)
        session, _e, _c, _h = _open(orch)
        with closing(connect_sqlite(repos['scene'].path)) as connection:
            connection.execute(
                "UPDATE cad_diagnostic_sessions SET fault_tree_id='x' "
                "WHERE session_id=?",
                (session.session_id,))
            connection.commit()
        with pytest.raises(DeploymentIntegrityError):
            repo.get_session(session.session_id)

    def test_transition_log_fold_is_deterministic(self, repos) -> None:
        orch = _orchestrator(repos)
        session, _e, _c, _h = _open(orch)
        transitions = tuple(
            repos['diagnostic'].list_transitions(session.session_id))
        state_a = derive_session_state(session, transitions)
        state_b = derive_session_state(session, transitions)
        assert state_a == state_b
        assert state_a.current_stage == 'test_planning'
        assert 'hypotheses_registered' in state_a.seen_kinds


# ---------------------------------------------------------------------------
# Ranking determinism + fail-closed evaluation
# ---------------------------------------------------------------------------


class TestRanking:
    def test_unmapped_fact_stays_insufficient(self, repos) -> None:
        orch = _orchestrator(repos)
        session, entries, _c, _h = _open(orch)
        plan = DiagnosticTestPlan.create(
            document_id=DOC,
            session_ref=session_binding(session),
            template_id='t',
            plan_seq=0,
            mechanism='device_readback',
            safety_class='read_only',
            test_label='t',
            action_label='t',
            discriminates=('device_not_deployed',),
            discriminations=(
                DiscriminationRule(
                    observable='deployed_config_match',
                    value_effects={
                        'true': {'device_not_deployed': 'contradicted'},
                        'false': {'device_not_deployed': 'supported'},
                    }),
            ),
            authorization_class='none',
            created_at_utc=NOW,
        )
        effects = evaluate_observation_effects(
            plan, {'unexpected_key': 'x'}, entries)
        by_key = {e.hypothesis_key: e.effect for e in effects}
        assert by_key['device_not_deployed'] == 'insufficient_evidence'

    def test_ranking_reproducible(self, repos) -> None:
        orch = _orchestrator(repos)
        session, entries, _c, _h = _open(orch)
        updates = ()
        states_a = compute_hypothesis_states(entries, updates)
        states_b = compute_hypothesis_states(entries, updates)
        assert states_a == states_b
        from htdt.cad_diagnostic_orchestrator import rank_hypotheses
        assert rank_hypotheses(entries, states_a) == rank_hypotheses(
            entries, states_b)


# ---------------------------------------------------------------------------
# End-to-end walks through scripted backends
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """Injected commissioning faults through the full sealed loop."""

    def _drive(self, orch, session, plan, *, backend, adapter=None,
               binding=None, export=None, verification_plan=None,
               operator_facts=None):
        return orch.execute_plan(
            session, plan,
            backend=backend,
            routings={'fl': _routing(0), 'main': _routing(0),
                      'sub': _routing(1), 'alternate': _routing(1),
                      'reference': _routing(0), 'failing': _routing(0)},
            arming=_arming,
            adapter=adapter,
            binding=binding,
            export=export,
            verification_plan=verification_plan,
            operator_facts=operator_facts,
        )

    def test_deployed_drift_isolates_device_fault(self, repos) -> None:
        """Injected fault: deployed config drifts from the export.

        The session must locate ``device_not_deployed`` as the sole
        surviving hypothesis — the owned-system acceptance walk.
        """
        orch = _orchestrator(repos)
        session, _e, _c, _h = _open(orch)
        binding = _avr_binding()
        export = _export((_channel('fl', 1.5),))
        # Device holds -2.0 dB where the export pinned +1.5 dB.
        adapter = _avr_adapter(FakeAvrLanTransport({'fl': -2.0}))
        backend = _ScriptedChannelBackend({
            0: {'measurement_ir_taps': ((0, 1.0),)},
            1: {'measurement_ir_taps': ((3, 1.0),)},
        })
        cv_plan = _cv_plan(('fl', 'main'))

        observations = []
        for _ in range(6):
            state = orch.session_state(session)
            if state.terminal:
                break
            if state.current_stage != 'test_planning':
                break
            plan = orch.plan_next_test(session)
            if plan is None:
                break
            obs, _upd = self._drive(
                orch, session, plan,
                backend=backend,
                adapter=adapter, binding=binding, export=export,
                verification_plan=cv_plan,
            )
            observations.append(obs)

        resolution, verdict719 = orch.resolve(session)
        state = orch.session_state(session)
        assert state.terminal and state.current_stage == 'resolved'
        assert resolution.verdict == 'fault_isolated'
        assert resolution.surviving_hypothesis_keys == (
            'device_not_deployed',)
        assert resolution.verdict_ref is not None
        assert verdict719 is not None
        # No machine path jumped straight to "root cause confirmed".
        assert resolution.verdict != 'root_cause_confirmed'

    def test_polarity_inversion_walks_sub_tree(self, repos) -> None:
        """Main/Sub IR polarity opposed → polarity hypothesis survives on
        top; inspection-only survivors keep the verdict honest."""
        orch = _orchestrator(repos)
        session, _e, _c, _h = _open(orch, 'sub_output_low')
        binding = _avr_binding()
        export = _export((_channel('fl', 1.5),))
        adapter = _avr_adapter(FakeAvrLanTransport({'fl': 1.5}))
        backend = _ScriptedChannelBackend({
            0: {'measurement_ir_taps': ((0, 1.0),)},
            # Sub channel arrives with inverted polarity.
            1: {'measurement_ir_taps': ((2, -1.0),)},
        })
        for _ in range(8):
            state = orch.session_state(session)
            if state.terminal or state.current_stage != 'test_planning':
                break
            plan = orch.plan_next_test(session)
            if plan is None:
                break
            facts = (
                {'operator_finding': 'nothing_found'}
                if plan.mechanism == 'operator_observation' else None)
            self._drive(
                orch, session, plan,
                backend=backend,
                adapter=adapter, binding=binding, export=export,
                operator_facts=facts,
            )
        resolution, _v = orch.resolve(session)
        assert orch.session_state(session).terminal
        # Inspection-only real-room behavior can never be machine-
        # eliminated, so the honest verdict is needs_inspection with the
        # polarity hypothesis ranked on top.
        assert resolution.verdict == 'needs_inspection'
        assert 'polarity_crossover_cancellation' in (
            resolution.surviving_hypothesis_keys)
        updates = repos['diagnostic'].list_evidence_updates(
            session.session_id)
        assert updates
        last = updates[-1]
        assert last.ranking_after[0] == 'polarity_crossover_cancellation'

    def test_dead_sub_chain_flags_measurement_chain(self, repos) -> None:
        """No response anywhere → measurement-chain fault survives; the
        session must not invent a routing claim."""
        orch = _orchestrator(repos)
        session, _e, _c, _h = _open(orch, 'sub_output_low')
        adapter = _avr_adapter(FakeAvrLanTransport({'fl': 1.5}))
        backend = _ScriptedChannelBackend({
            0: {'measurement_ir_taps': ((0, 0.0),)},
            1: {'measurement_ir_taps': ((0, 0.0),)},
        })
        for _ in range(8):
            state = orch.session_state(session)
            if state.terminal or state.current_stage != 'test_planning':
                break
            plan = orch.plan_next_test(session)
            if plan is None:
                break
            facts = (
                {'operator_finding': 'nothing_found'}
                if plan.mechanism == 'operator_observation' else None)
            self._drive(
                orch, session, plan,
                backend=backend,
                adapter=adapter,
                binding=_avr_binding(),
                export=_export((_channel('fl', 1.5),)),
                operator_facts=facts,
            )
        resolution, _v = orch.resolve(session)
        assert resolution.verdict in (
            'needs_inspection', 'unresolved')
        assert 'measurement_chain_fault' in (
            resolution.surviving_hypothesis_keys)

    def test_manual_step_waits_for_machine_lane(self, repos) -> None:
        """Read-back precedes manual re-entry — the operator-inspection
        template is gated until automated tests are exhausted."""
        orch = _orchestrator(repos)
        session, _e, _c, _h = _open(orch, 'sub_output_low')
        plan = orch.plan_next_test(session)
        assert plan.template_id == 'device_readback'
        # Manually re-check the selector: while automated tests remain,
        # the manual template is never the pick.
        tree = FAULT_TREES['sub_output_low']
        entries = repos['diagnostic'].list_hypothesis_entries(
            session.session_id)
        states = compute_hypothesis_states(tuple(entries), ())
        picked = select_next_template(tree, frozenset(), states)
        assert picked is not None
        assert picked.mechanism != 'operator_observation'
        # Once every automated template is executed, the manual gate
        # opens.
        automated = {
            t.template_id for t in tree.test_templates
            if t.mechanism != 'operator_observation'}
        picked = select_next_template(
            tree, frozenset(automated), states)
        assert picked is not None
        assert picked.mechanism == 'operator_observation'

    def test_evidence_pins_sealed_runs_and_verdicts(self, repos) -> None:
        """Observations pin the sealed records they consumed — sweep runs
        from #869 and the cvvd- verdict from #876."""
        orch = _orchestrator(repos)
        session, _e, _c, _h = _open(orch)
        adapter = _avr_adapter(FakeAvrLanTransport({'fl': 1.5}))
        backend = _ScriptedChannelBackend({
            0: {'measurement_ir_taps': ((0, 1.0),)},
            1: {'measurement_ir_taps': ((3, 1.0),)},
        })
        # read-back first, then the cvpl- plan.
        plan = orch.plan_next_test(session)
        assert plan.mechanism == 'device_readback'
        obs, _ = self._drive(
            orch, session, plan,
            backend=backend, adapter=adapter,
            binding=_avr_binding(),
            export=_export((_channel('fl', 1.5),)),
        )
        assert obs.mechanism == 'device_readback'
        assert dict(obs.facts)['deployed_config_match'] == 'true'
        kinds = {r.kind for r in obs.evidence_refs}
        assert 'effective_settings_snapshot' in kinds

        plan = orch.plan_next_test(session)
        assert plan.mechanism == 'channel_verification'
        obs, _ = self._drive(
            orch, session, plan,
            backend=backend, verification_plan=_cv_plan(('fl', 'main')),
        )
        kinds = {r.kind for r in obs.evidence_refs}
        assert 'channel_verification_verdict' in kinds
        assert dict(obs.facts)['reference_channel_responded'] == 'true'

    def test_resolution_unresolved_when_all_contradicted(
            self, repos) -> None:
        """Every hypothesis contradicted → unresolved, never a
        best-of-bad guess."""
        orch = _orchestrator(repos)
        session, entries, _c, hypotheses = _open(orch)
        states = {
            e.hypothesis_key: 'contradicted' for e in entries}
        from htdt.cad_diagnostic_orchestrator import derive_resolution
        case = repos['hypotheses'].get_case(session.case_ref.ref_id)
        resolution, _v = derive_resolution(
            session=session,
            entries=tuple(entries),
            states=states,
            tests=(),
            hypotheses=tuple(hypotheses),
            case=case,
            decided_at_utc=NOW,
            no_more_tests=True,
        )
        assert resolution.verdict == 'unresolved'
        assert not resolution.surviving_hypothesis_keys

    def test_execute_out_of_turn_is_blocked_and_recorded(
            self, repos) -> None:
        """An execute call against a non-selected plan must not advance
        the loop — the attempt is sealed as a blocked transition."""
        orch = _orchestrator(repos)
        session, _e, _c, _h = _open(orch)
        bogus = DiagnosticTestPlan.create(
            document_id=DOC,
            session_ref=session_binding(session),
            template_id='never-selected',
            plan_seq=99,
            mechanism='device_readback',
            safety_class='read_only',
            test_label='bogus',
            action_label='bogus',
            discriminates=('device_not_deployed',),
            authorization_class='none',
            created_at_utc=NOW,
        )
        with pytest.raises(DiagnosticOrchestrationError):
            orch.execute_plan(session, bogus)
        transitions = repos['diagnostic'].list_transitions(
            session.session_id)
        assert transitions[-1].outcome == 'rejected'
        assert orch.session_state(session).current_stage == (
            'test_planning')

    def test_observation_never_fabricates_machine_evidence(
            self, repos) -> None:
        orch = _orchestrator(repos)
        session, _e, _c, _h = _open(orch, 'sub_output_low')
        # Manual observation without operator_facts is an orchestration
        # error, not a silent pass.
        plan = DiagnosticTestPlan.create(
            document_id=DOC,
            session_ref=session_binding(session),
            template_id='manual-step',
            plan_seq=0,
            mechanism='operator_observation',
            safety_class='read_only',
            test_label='manual',
            action_label='manual check',
            discriminates=('real_room_behavior',),
            authorization_class='none',
            created_at_utc=NOW,
        )
        orch._repository.save_test_plan(plan)
        orch._emit(session, DiagnosticEvent(
            kind='test_selected',
            at_utc=NOW,
            reason='manual plan forced',
            evidence_refs=(plan_binding(plan),),
        ))
        with pytest.raises(DiagnosticOrchestrationError):
            orch.execute_plan(session, plan)
        # With operator-entered facts the step executes — and carries
        # no machine evidence refs by construction.
        obs, update = orch.execute_plan(
            session, plan,
            operator_facts={'operator_finding': 'nothing_found'},
        )
        assert obs.mechanism == 'operator_observation'
        assert obs.evidence_refs == ()
        assert dict(obs.facts)['operator_finding'] == 'nothing_found'
