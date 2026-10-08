"""#950 Decision Brief gate-evidence auto-resolution.

Covers: all five gates verified+current+comparable → ready; missing,
mismatched-scope, stale, cross-device, predicted-only and partial
evidence → never ready; repository save/read + UI recompute reproducing
the identical verdict+hash; variant candidates that change the physical
speaker set cannot inherit installed-room channel evidence.
"""

from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from pathlib import Path

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_calibration import (
    CadCalibrationChannel,
    CadCalibrationPlan,
    CadDeviceCapabilityConstraints,
)
from htdt.cad_calibration_deployment import CalibrationDeployment
from htdt.cad_calibration_deployment_repository import (
    CadCalibrationDeploymentRepository,
)
from htdt.cad_channel_verification import run_verification_plan
from htdt.cad_channel_verification_repository import (
    CadChannelVerificationRepository,
)
from htdt.cad_correction_qualification import (
    evaluate_correction_qualification,
)
from htdt.cad_correction_qualification_repository import (
    CadCorrectionQualificationRepository,
)
from htdt.cad_decision_brief import (
    CadDecisionBrief,
    build_decision_brief,
)
from htdt.cad_decision_brief_evidence import (
    DecisionBriefEvidenceResolver,
)
from htdt.cad_decision_brief_repository import CadDecisionBriefRepository
from htdt.cad_design_comparison import build_alternative, build_comparison_set
from htdt.cad_design_comparison_repository import (
    CadDesignComparisonRepository,
)
from htdt.cad_owned_room_campaign_repository import (
    CadOwnedRoomCampaignRepository,
)
from htdt.cad_production_readiness_repository import (
    CadProductionReadinessRepository,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_schema import ensure_native_schema
from htdt.cad_system_variant import build_system_variant
from htdt.cad_system_variant_repository import CadSystemVariantRepository

from test_issue_876_channel_verification import (
    _arming,
    _plan as _channel_plan,
    _ScriptedChannelBackend,
)
from test_issue_801_production_readiness import _decision as _prod_decision
from test_issue_801_production_readiness import _full_chain
from test_issue_813_owned_room_campaign import (
    _measurement as _campaign_measurement,
    _prereg as _campaign_prereg,
    _verdict as _campaign_verdict,
)
from test_rev55_corrqual import (
    _channel as _corr_channel,
    _constraints as _corr_constraints,
    _peq,
    _plan as _corr_plan,
    _region as _corr_region,
    _response_set,
    _subject as _corr_subject,
    _target as _corr_target,
)

NOW = '2026-10-08T00:00:00Z'
DOC = 'doc-950'
_SHA = 'a' * 64


def _scene(document_id: str = DOC, speakers=('spk-fl', 'spk-fr')) -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        room=RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4),
        entities=tuple(
            SceneEntity(
                entity_id=speaker_id,
                kind='speaker',
                name=speaker_id.upper(),
                speaker_role=speaker_id.upper(),
                position=Position3(
                    x_m=(1.2 if 'fl' in speaker_id else 5.0),
                    y_m=0.8,
                    z_m=1.0,
                ),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
            )
            for speaker_id in speakers
        ),
    )


def _ref(kind: str, rid: str, sha: str | None = None) -> AuthorityRef:
    return AuthorityRef(
        kind=kind, ref_id=rid, ref_sha256=sha or ('b' * 64)
    )


def _revision(tmp_path: Path, speakers=('spk-fl', 'spk-fr')):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        _scene(speakers=speakers), parent_revision_id=None
    ).revision
    return scene_repository, revision


class _Env:
    """One document: revision + speaker-unchanged variant + all repos."""

    def __init__(self, tmp_path: Path, *, speakers=('spk-fl', 'spk-fr'),
                 remove_entity_ids: tuple[str, ...] = ()) -> None:
        db = tmp_path / 'cad.sqlite3'
        ensure_native_schema(db)
        self.scene_repository = SceneRepository(db)
        self.revision = self.scene_repository.save(
            _scene(speakers=speakers), parent_revision_id=None
        ).revision
        self.variants = CadSystemVariantRepository(self.scene_repository)
        self.variant = build_system_variant(
            baseline=self.revision,
            name='candidate-v1',
            role_bindings=(),
            proposed_entities=(),
            remove_entity_ids=remove_entity_ids,
            created_at_utc=NOW,
        )
        self.variants.save_variant(self.variant)
        self.corrections = CadCorrectionQualificationRepository(
            self.scene_repository
        )
        self.channels = CadChannelVerificationRepository(
            self.scene_repository
        )
        self.deployments = CadCalibrationDeploymentRepository(
            self.scene_repository
        )
        self.campaigns = CadOwnedRoomCampaignRepository(
            self.scene_repository
        )
        self.production = CadProductionReadinessRepository(
            self.scene_repository
        )
        self.comparisons = CadDesignComparisonRepository(
            self.scene_repository
        )
        self.resolver = DecisionBriefEvidenceResolver(
            self.scene_repository,
            correction_qualification_repository=self.corrections,
            channel_verification_repository=self.channels,
            calibration_deployment_repository=self.deployments,
            campaign_repository=self.campaigns,
            production_readiness_repository=self.production,
            system_variant_repository=self.variants,
        )
        self.briefs = CadDecisionBriefRepository(
            self.scene_repository,
            system_variant_repository=self.variants,
            kind_resolvers=self.resolver.kind_resolvers(),
        )
        self.baseline_alt = build_alternative(
            label='baseline',
            scene_revision=self.revision,
            created_at_utc=NOW,
        )
        self.candidate_alt = build_alternative(
            label='candidate',
            scene_revision=self.revision,
            kind='system_variant',
            system_variant_id=self.variant.variant_id,
            system_variant_sha256=self.variant.variant_sha256,
            created_at_utc=NOW,
        )
        self._comparison_set = None

    # -- producers ------------------------------------------------------

    def add_solver_record(
        self,
        *,
        post_kind: str = 'physical_measurement',
        holdout: tuple[str, ...] = ('seat-h',),
        scene_revision_id: str | None = None,
        system_variant_id: str | None = 'use-env',
        system_variant_sha256: str | None = 'use-env',
    ):
        plan = _corr_plan(
            channels=(_corr_channel(peq=(_peq('eq-1', 60.0, -4.0),)),)
        )
        target = _corr_target()
        level = target.points[0].level_db
        design = ('mlp', 'seat-a')
        positions = design + holdout
        record = evaluate_correction_qualification(
            subject=_corr_subject(plan),
            region=_corr_region(design=design, holdout=holdout),
            constraints=_corr_constraints(),
            target=target,
            correction_bands=((30.0, 150.0),),
            plan=plan,
            baseline=_response_set(positions, level_db=84.0),
            post=_response_set(
                positions, level_db=level, kind=post_kind
            ),
            document_id=DOC,
            scene_revision_id=(
                self.revision.revision_id
                if scene_revision_id is None
                else scene_revision_id
            ),
            system_variant_id=(
                self.variant.variant_id
                if system_variant_id == 'use-env'
                else system_variant_id
            ),
            system_variant_sha256=(
                self.variant.variant_sha256
                if system_variant_sha256 == 'use-env'
                else system_variant_sha256
            ),
            recorded_at_utc=NOW,
        )
        self.corrections.save(record)
        return record

    def add_channel_verdict(self, *, expected=('spk-fl', 'spk-fr')):
        from htdt.cad_channel_identity_authority import (
            build_channel_identity_chain,
        )
        from htdt.cad_channel_verification import (
            build_verification_plan,
        )
        from htdt.cad_sweep_acquisition import ChannelRouting

        logical = tuple(s[4:] for s in expected)
        chains = tuple(
            build_channel_identity_chain(
                document_id=DOC,
                logical_channel=channel,
                channel_class='bed_channel',
                expected_speaker_entity_ids=(entity_id,),
                declared_at_utc=NOW,
            )
            for channel, entity_id in zip(logical, expected)
        )
        plan = build_verification_plan(
            document_id=DOC,
            chains=chains,
            routings={
                channel: ChannelRouting(
                    playback_device_id='fake-duplex-0',
                    playback_channel=index,
                    capture_device_id='fake-duplex-0',
                    capture_channel=0,
                    loopback_input_channel=1,
                )
                for index, channel in enumerate(logical)
            },
            sample_rate_hz=48000,
            stimulus_start_hz=100.0,
            stimulus_end_hz=8000.0,
            stimulus_duration_s=0.05,
            stimulus_level_dbfs=-12.0,
            repetitions=2,
            created_at_utc=NOW,
        )
        if len(chains) == 2:
            backend = _ScriptedChannelBackend({
                0: {'measurement_ir_taps': ((0, 1.0),)},
                1: {'measurement_ir_taps': ((3, 1.0),)},
            })
        else:
            backend = _ScriptedChannelBackend({
                index: {'measurement_ir_taps': ((index, 1.0),)}
                for index in range(len(chains))
            })
        verdict, results, _engines = run_verification_plan(
            plan=plan, backend=backend, arming=_arming,
            clock=lambda: NOW,
        )
        self.channels.save_plan(plan)
        self.channels.save_verdict(verdict)
        return verdict

    def add_deployment(self, *, variant_id=None, variant_sha256=None):
        plan_id = 'plan-950'
        plan = _calibration_plan(
            plan_id=plan_id,
            scene_revision_id=self.revision.revision_id,
            scene_content_hash=self.revision.content_hash,
            variant_id=(
                self.variant.variant_id
                if variant_id is None else variant_id
            ),
            variant_sha256=(
                self.variant.variant_sha256
                if variant_sha256 is None else variant_sha256
            ),
        )
        self._insert_plan(plan)
        deployment = CalibrationDeployment.create(
            document_id=DOC,
            calibration_plan_id=plan_id,
            calibration_plan_sha256=plan.plan_semantic_sha256,
            export_id='exp-1',
            export_sha256=_SHA,
            materialization_id='mat-1',
            materialization_sha256=_SHA,
            binding_id='bind-1',
            binding_sha256=_SHA,
            adapter_id='htdt-generic-biquad',
            adapter_version='1',
            capability_declaration_sha256=_SHA,
            target_class='generic_peq_fir_file',
            evidence_mode='machine_readback',
            deployment_state='deployment_verified',
            observed_snapshot_id='snap-1',
            observed_snapshot_sha256=_SHA,
            deployed_at_utc=NOW,
        )
        self.deployments.save_deployment(deployment)
        return deployment

    def _insert_plan(self, plan: CadCalibrationPlan) -> None:
        with sqlite3.connect(self.scene_repository.path) as conn, conn:
            conn.execute(
                """
                INSERT INTO cad_calibration_plans(
                    plan_id, document_id, scene_revision_id,
                    system_variant_id, source_measurement_id,
                    source_dataset_id, quality_report_id,
                    plan_semantic_sha256, support_state, created_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.document_id,
                    plan.scene_revision_id,
                    plan.system_variant_id,
                    plan.source_measurement_id,
                    plan.source_dataset_id,
                    plan.measurement_quality_report_id,
                    plan.plan_semantic_sha256,
                    plan.support_state,
                    plan.created_at_utc,
                    plan.model_dump_json(),
                ),
            )

    def add_campaign(self, *, outcome='recommendation_eligible',
                     variant_sha256: str | None = 'use-env'):
        prereg = _campaign_prereg(
            document_id=DOC,
            scene_ref=AuthorityRef(
                kind='scene_revision',
                ref_id=self.revision.revision_id,
                ref_sha256=self.revision.content_hash,
            ),
            system_variant_ref=AuthorityRef(
                kind='system_variant',
                ref_id=self.variant.variant_id,
                ref_sha256=(
                    self.variant.variant_sha256
                    if variant_sha256 == 'use-env'
                    else variant_sha256
                ),
            ),
        )
        self.campaigns.preregistrations.save(prereg)
        measurement = _campaign_measurement(prereg)
        self.campaigns.measurements.save(measurement)
        verdict = _campaign_verdict(
            prereg, (measurement,), outcome=outcome,
            external_benchmark_ref=_ref('bench', 'b-1'),
            numerical_convergence_ref=_ref('conv', 'c-1'),
            input_qualification_ref=_ref('inpq', 'i-1'),
            holdout_residual_ref=_ref('resid', 'r-1'),
            applicability_ref=_ref('appl', 'a-1'),
        )
        self.campaigns.verdicts.save(verdict)
        return verdict

    def add_production(self, *, outcome='production_ready',
                       variant_sha256: str | None = 'use-env'):
        decision = _prod_decision(
            document_id=DOC,
            scene_ref=AuthorityRef(
                kind='scene_revision',
                ref_id=self.revision.revision_id,
                ref_sha256=self.revision.content_hash,
            ),
            system_variant_ref=AuthorityRef(
                kind='system_variant',
                ref_id=self.variant.variant_id,
                ref_sha256=(
                    self.variant.variant_sha256
                    if variant_sha256 == 'use-env'
                    else variant_sha256
                ),
            ),
            outcome=outcome,
            **(_full_chain() if outcome == 'production_ready' else {}),
        )
        self.production.decisions.save(decision)
        return decision

    def add_all(self) -> None:
        self.add_solver_record()
        self.add_channel_verdict()
        self.add_deployment()
        self.add_campaign()
        self.add_production()

    # -- compose --------------------------------------------------------

    def comparison(self):
        if self._comparison_set is None:
            self._comparison_set = build_comparison_set(
                document_id=DOC,
                name='set-1',
                alternatives=(self.baseline_alt, self.candidate_alt),
                created_at_utc=NOW,
            )
            self.comparisons.save_set(self._comparison_set)
        return self._comparison_set

    def gates(self, alternative=None):
        return self.resolver.resolve_gates(
            DOC, alternative or self.candidate_alt
        )


def _calibration_plan(
    *, plan_id: str, variant_id: str, variant_sha256: str,
    scene_revision_id: str | None = None,
    scene_content_hash: str | None = None,
) -> CadCalibrationPlan:
    from htdt.canonical_json import canonical_sha256

    payload = {
        'plan_id': plan_id,
        'plan_version': '1',
        'created_at_utc': NOW,
        'source_kind': 'provided_fixture',
        'document_id': DOC,
        'scene_revision_id': scene_revision_id or 'rev-x',
        'scene_content_hash': scene_content_hash or _SHA,
        'system_variant_id': variant_id,
        'system_variant_sha256': variant_sha256,
        'source_measurement_id': 'm-1',
        'source_measurement_sha256': _SHA,
        'source_dataset_id': 'd-1',
        'source_dataset_sha256': _SHA,
        'measurement_quality_report_id': 'q-1',
        'measurement_quality_report_sha256': _SHA,
        'sample_rate_hz': 48000,
        'channels': (
            CadCalibrationChannel(
                channel_id='fl',
                role_id='FL',
                source_entity_id='spk-fl',
                physical_output_id='out-fl',
                sample_rate_hz=48000,
                gain_db=0.0,
                delay_s=0.0,
                polarity='normal',
                crossovers=(),
                peq=(),
                routing=('out-fl',),
            ),
        ),
        'target_curve': None,
        'max_boost_db': 6.0,
        'max_cut_db': 12.0,
        'device_constraints': CadDeviceCapabilityConstraints(
            capability_id='cap-1',
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
    payload['plan_semantic_sha256'] = canonical_sha256(
        provisional.semantic_payload()
    )
    return CadCalibrationPlan(**payload)


def _action(brief: CadDecisionBrief):
    assert len(brief.actions) == 1
    return brief.actions[0]


def _compose(env: _Env, gates) -> CadDecisionBrief:
    """Build the brief exactly like the panel does."""

    from htdt.cad_decision_brief import build_decision_action
    from htdt.decision_brief_panel import _gate_recommendation

    action = build_decision_action(
        candidate_ref=AuthorityRef(
            kind='comparison_alternative',
            ref_id=env.candidate_alt.alternative_id,
            ref_sha256=env.candidate_alt.alternative_sha256,
        ),
        label=env.candidate_alt.label,
        gates=gates,
        comparability='comparable',
        changes=(),
        deltas=(),
        recommendation=_gate_recommendation(
            env.candidate_alt.label, gates
        ),
    )
    comparison_set = env.comparison()
    return build_decision_brief(
        document_id=DOC,
        scene_revision_id=env.revision.revision_id,
        scene_content_hash=env.revision.content_hash,
        baseline_ref=AuthorityRef(
            kind='comparison_alternative',
            ref_id=env.baseline_alt.alternative_id,
            ref_sha256=env.baseline_alt.alternative_sha256,
        ),
        baseline_label=env.baseline_alt.label,
        actions=(action,),
        comparison_ref=AuthorityRef(
            kind='design_comparison_set',
            ref_id=comparison_set.set_id,
            ref_sha256=comparison_set.set_sha256,
        ),
        provenance=('test',),
        created_at_utc=NOW,
    )


# ---------------------------------------------------------------
# ready path


class TestReady:
    def test_all_five_gates_verified_is_ready(self, tmp_path):
        env = _Env(tmp_path)
        env.add_all()
        gates = env.gates()
        states = {g.gate: g.gate_state() for g in gates}
        assert set(states.values()) == {'satisfied'}
        brief = _compose(env, gates)
        env.briefs.save_brief(brief)
        action = _action(brief)
        assert action.tier == 'ready'
        assert action.recommendation.kind == 'apply_candidate'
        assert brief.top_tier == 'ready'
        # save/read reproduces the identical sealed record
        loaded = env.briefs.latest_brief(DOC)
        assert loaded is not None
        assert loaded.brief_sha256 == brief.brief_sha256
        assert loaded.actions[0].tier == 'ready'
        # deterministic: re-resolve and re-compose → identical hash
        brief2 = _compose(env, env.gates())
        assert brief2.brief_sha256 == brief.brief_sha256


# ---------------------------------------------------------------
# honest negatives


class TestMissing:
    def test_no_records_all_gates_missing(self, tmp_path):
        env = _Env(tmp_path)
        gates = env.gates()
        assert all(g.pin is None for g in gates)
        assert all(g.gate_state() == 'blocked' for g in gates)
        brief = _compose(env, gates)
        env.briefs.save_brief(brief)
        assert _action(brief).tier == 'not_ready'
        assert _action(brief).recommendation.kind == 'collect_evidence'

    def test_partial_evidence_not_ready(self, tmp_path):
        env = _Env(tmp_path)
        env.add_solver_record()
        env.add_production()
        # campaign / channel / deployment absent
        gates = env.gates()
        satisfied = sum(
            g.gate_state() == 'satisfied' for g in gates
        )
        assert satisfied == 2
        brief = _compose(env, gates)
        env.briefs.save_brief(brief)
        assert _action(brief).tier != 'ready'


class TestScopeRejection:
    def test_cross_revision_records_rejected(self, tmp_path):
        env = _Env(tmp_path)
        env.add_solver_record(scene_revision_id='rev-other')
        env.add_campaign(
            outcome='recommendation_eligible',
        )
        gates = env.gates()
        solver = next(g for g in gates if g.gate == 'solver_gate')
        assert solver.pin is None  # wrong revision → rejected
        brief = _compose(env, gates)
        env.briefs.save_brief(brief)
        assert _action(brief).tier != 'ready'

    def test_stale_variant_sha_is_stale_not_ready(self, tmp_path):
        env = _Env(tmp_path)
        env.add_production(
            outcome='production_ready',
            variant_sha256='d' * 64,
        )
        gates = env.gates()
        gate = next(g for g in gates if g.gate == 'production_gate')
        assert gate.pin is not None
        assert gate.pin.freshness == 'stale'
        assert gate.gate_state() == 'blocked'
        brief = _compose(env, gates)
        env.briefs.save_brief(brief)
        assert _action(brief).tier != 'ready'

    def test_cross_variant_deployment_rejected(self, tmp_path):
        env = _Env(tmp_path)
        env.add_deployment(variant_id='var-other',
                           variant_sha256='e' * 64)
        gates = env.gates()
        gate = next(g for g in gates if g.gate == 'deployment')
        assert gate.pin is None
        brief = _compose(env, gates)
        env.briefs.save_brief(brief)
        assert _action(brief).tier != 'ready'

    def test_variant_speaker_change_loses_channel_evidence(
        self, tmp_path
    ):
        env = _Env(tmp_path, remove_entity_ids=('spk-fr',))
        env.add_all()
        gates = env.gates()
        gate = next(g for g in gates if g.gate == 'channel_verify')
        assert gate.pin is None  # variant changes the physical map
        brief = _compose(env, gates)
        env.briefs.save_brief(brief)
        assert _action(brief).tier != 'ready'


class TestVerdictLadder:
    def test_predicted_only_solver_not_ready(self, tmp_path):
        env = _Env(tmp_path)
        env.add_solver_record(post_kind='simulated_prediction')
        env.add_channel_verdict()
        env.add_deployment()
        env.add_campaign()
        env.add_production()
        gates = env.gates()
        solver = next(g for g in gates if g.gate == 'solver_gate')
        assert solver.verdict == 'conditional'
        assert solver.pin.evidence_class == 'predicted'
        brief = _compose(env, gates)
        env.briefs.save_brief(brief)
        assert _action(brief).tier != 'ready'

    def test_failed_production_not_ready(self, tmp_path):
        env = _Env(tmp_path)
        env.add_all()
        env.add_production(outcome='no_go')
        gates = env.gates()
        gate = next(g for g in gates if g.gate == 'production_gate')
        assert gate.verdict == 'failed'
        assert gate.gate_state() == 'blocked'
        brief = _compose(env, gates)
        env.briefs.save_brief(brief)
        assert _action(brief).tier != 'ready'


# ---------------------------------------------------------------
# UI path parity


class TestPanelParity:
    def test_recompute_reproduces_identical_brief(self, tmp_path):
        from PySide6.QtWidgets import QApplication

        if QApplication.instance() is None:
            QApplication([])
        env = _Env(tmp_path)
        env.add_all()
        env.comparison()
        from htdt.decision_brief_panel import DecisionBriefPanel

        brief_repository = CadDecisionBriefRepository(
            env.scene_repository,
            system_variant_repository=env.variants,
            kind_resolvers=env.resolver.kind_resolvers(),
        )
        panel = DecisionBriefPanel(
            env.scene_repository,
            DOC,
            brief_repository=brief_repository,
            comparison_repository=env.comparisons,
        )
        brief = panel.recompute_brief()
        assert brief is not None
        assert _action(brief).tier == 'ready'
        loaded = brief_repository.latest_brief(DOC)
        assert loaded is not None
        assert loaded.brief_sha256 == brief.brief_sha256
        # resolver-side compose of the same inputs is identical except
        # the wall-clock stamp the panel seals at build time
        expected = _compose(env, env.gates())
        assert brief.actions == expected.actions
        assert brief.baseline_ref == expected.baseline_ref
        assert brief.comparison_ref == expected.comparison_ref
        assert brief.top_tier == expected.top_tier == 'ready'
        panel.deleteLater()
