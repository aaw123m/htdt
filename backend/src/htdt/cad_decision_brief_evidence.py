"""Automatic gate-evidence resolution for the decision brief (#950).

The #937 brief shipped fail-closed: every gate pin had to be declared by
the caller, so a rebuilt brief always landed ``not_ready``. This module
correlates each comparison candidate against the *real* sealed verdict
producers and turns only honestly-scope-matching records into
:class:`DecisionEvidencePin` values:

- ``solver_gate``     → :class:`CorrectionQualificationRecord` — the
  per-correction qualification authority, bound to the exact
  ``scene_revision_id`` / ``system_variant_id`` (+sha256) the candidate
  pins. Its ``correction_bands`` and ``region`` seat positions are the
  candidate's target band and seats.
- ``channel_verify``  → :class:`ChannelVerificationVerdict` — correlated
  only when every expected speaker entity of the verified plan resolves
  inside the candidate's exact scene revision AND (for variant
  candidates) the variant leaves the verified speaker set unchanged: no
  proposed speaker entities and no diff touching the verified speakers.
  A variant that changes the speaker set is a different physical map and
  needs its own verification run.
- ``deployment``      → :class:`CalibrationDeployment` — the
  deployed-state authority per calibration plan; scope is proven through
  the pinned plan's ``scene_revision_id`` / ``system_variant_id``
  (+sha256). Document-scoped :class:`DeploymentPipelineRecord` stages
  are also reported as pinned evidence for baseline candidates, but
  their binding is device-scoped (not candidate-scoped), so freshness
  stays ``unknown`` and the gate can never satisfy from them alone.
- ``campaign``        → :class:`CampaignVerdict` via its
  ``CampaignPreregistration`` scene/variant pins.
- ``production_gate`` → :class:`ProductionReadinessDecision` — direct
  scene/variant pins.

Records whose pinned scope does not match the candidate are *rejected* at
the domain layer — a verdict for another revision, variant, or physical
unit is not this candidate's evidence. Freshness is re-verified from the
sealed rows (identity + semantic hash), and anything that cannot be
proven lands as an honest ``evidence_missing`` gap whose note names what
was found and rejected.

:func:`gate_kind_resolvers` returns the ``kind_resolvers`` mapping a
:class:`CadDecisionBriefRepository` needs so its fail-closed write-time
resolution can prove the auto-pinned kinds — without it every persisted
pin would be an unresolvable authority.
"""

from __future__ import annotations

from contextlib import closing
from typing import Callable, Literal, Mapping

from .cad_authority_resolver import (
    KindResolver,
    ResolvedAuthority,
)
from .cad_calibration_deployment_repository import (
    CadCalibrationDeploymentRepository,
)
from .cad_calibration import CadCalibrationPlan
from .cad_calibration_repository import CadCalibrationRepository
from .cad_channel_verification_repository import (
    CadChannelVerificationRepository,
)
from .cad_correction_qualification_repository import (
    CadCorrectionQualificationRepository,
)
from .cad_decision_brief import (
    DECISION_GATE_ORDER,
    DecisionEvidencePin,
    DecisionGate,
    GateVerdict,
)
from .cad_deployment_pipeline_repository import (
    CadDeploymentPipelineRepository,
)
from .cad_design_comparison import ComparisonAlternative
from .cad_owned_room_campaign_repository import (
    CadOwnedRoomCampaignRepository,
)
from .cad_production_readiness_repository import (
    CadProductionReadinessRepository,
)
from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite
from .cad_system_variant_repository import CadSystemVariantRepository


#: Pin kinds the resolver emits per gate. The ones with canonical
#: binding helpers reuse those kind strings verbatim.
SOLVER_QUALIFICATION_PIN_KIND = 'correction_qualification'
CHANNEL_VERDICT_PIN_KIND = 'channel_verification_verdict'
CALIBRATION_DEPLOYMENT_PIN_KIND = 'calibration_deployment'
DEPLOYMENT_RECORD_PIN_KIND = 'deployment_pipeline_record'
CAMPAIGN_VERDICT_PIN_KIND = 'campaign_verdict'
PRODUCTION_DECISION_PIN_KIND = 'production_readiness_decision'

_ScopeState = Literal['current', 'stale', 'mismatch', 'unbound']


def _scope_state(
    alternative: ComparisonAlternative,
    *,
    scene_ref,
    variant_ref,
) -> _ScopeState:
    """Whether refs bound on a record cover this candidate exactly.

    ``mismatch`` means the record is pinned to a different revision or
    variant — another candidate's evidence, rejected outright. ``stale``
    is the same identity at a different content hash — evidence exists but
    no longer describes the current inputs. A bound variant ref on a
    variant-free candidate (or the reverse) is a mismatch either way.
    """

    if scene_ref is None:
        return 'unbound'
    if scene_ref.ref_id != alternative.scene_revision_id:
        return 'mismatch'
    scene_state: _ScopeState = (
        'stale'
        if scene_ref.ref_sha256 is not None
        and scene_ref.ref_sha256 != alternative.scene_content_hash
        else 'current'
    )
    if variant_ref is None:
        # A record bound only to the scene describes the baseline scope;
        # a variant candidate is not covered by it.
        if alternative.system_variant_id is not None:
            return 'unbound'
        return scene_state
    if (
        alternative.system_variant_id is None
        or variant_ref.ref_id != alternative.system_variant_id
    ):
        return 'mismatch'
    variant_state: _ScopeState = (
        'stale'
        if variant_ref.ref_sha256 is not None
        and variant_ref.ref_sha256 != alternative.system_variant_sha256
        else 'current'
    )
    if scene_state == 'stale' or variant_state == 'stale':
        return 'stale'
    return 'current'


def _scene_entity_ids(
    scene_repository: SceneRepository, revision_id: str
) -> frozenset[str]:
    revision = scene_repository.get(revision_id)
    if revision is None:
        return frozenset()
    return frozenset(
        entity.entity_id for entity in revision.document.entities
    )


class DecisionBriefEvidenceResolver:
    """Correlates one comparison candidate to the real gate producers.

    All stores are lazily bound to the same :class:`SceneRepository` path
    the brief repository uses, so the pins it emits resolve at save time.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        correction_qualification_repository:
            CadCorrectionQualificationRepository | None = None,
        channel_verification_repository:
            CadChannelVerificationRepository | None = None,
        deployment_pipeline_repository:
            CadDeploymentPipelineRepository | None = None,
        calibration_deployment_repository:
            CadCalibrationDeploymentRepository | None = None,
        calibration_repository: CadCalibrationRepository | None = None,
        campaign_repository:
            CadOwnedRoomCampaignRepository | None = None,
        production_readiness_repository:
            CadProductionReadinessRepository | None = None,
        system_variant_repository:
            CadSystemVariantRepository | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.system_variants = (
            system_variant_repository
            or CadSystemVariantRepository(scene_repository)
        )
        self.correction_qualifications = (
            correction_qualification_repository
            or CadCorrectionQualificationRepository(scene_repository)
        )
        self.channel_verification = (
            channel_verification_repository
            or CadChannelVerificationRepository(scene_repository)
        )
        self.deployment_pipeline = (
            deployment_pipeline_repository
            or CadDeploymentPipelineRepository(scene_repository)
        )
        self.calibration_deployments = (
            calibration_deployment_repository
            or CadCalibrationDeploymentRepository(scene_repository)
        )
        self._calibration_repository = calibration_repository
        self.campaign = (
            campaign_repository
            or CadOwnedRoomCampaignRepository(scene_repository)
        )
        self.production_readiness = (
            production_readiness_repository
            or CadProductionReadinessRepository(scene_repository)
        )

    def _sealed_plan(self, plan_id: str) -> CadCalibrationPlan | None:
        """Read a sealed calibration plan's payload for scope correlation.

        Unlike ``CadCalibrationRepository.get_plan`` this does NOT
        re-validate the plan's source authorities (scene / variant /
        measurement / dataset / quality): those are write-path
        invariants of ``save_plan`` and may reference stores that were
        pruned or never materialized on this machine. The deployment
        record's ``calibration_plan_sha256`` already authenticates the
        sealed payload, so scope correlation only needs the payload
        itself. Injected repositories still honour the same contract.
        """

        if self._calibration_repository is not None:
            try:
                return self._calibration_repository.get_plan(plan_id)
            except Exception:  # noqa: BLE001 — probes never raise
                return None
        try:
            with closing(connect_sqlite(self.scene_repository.path)) as conn:
                row = conn.execute(
                    'SELECT payload_json FROM cad_calibration_plans '
                    'WHERE plan_id=?',
                    (plan_id,),
                ).fetchone()
            if row is None:
                return None
            return CadCalibrationPlan.model_validate_json(row['payload_json'])
        except Exception:  # noqa: BLE001 — probes never raise
            return None

    # -- public API -----------------------------------------------------

    def resolve_gates(
        self,
        document_id: str,
        alternative: ComparisonAlternative,
    ) -> tuple[DecisionGate, ...]:
        """The five gates for ``alternative``, in canonical order.

        A probe that cannot prove scope or verdict leaves the gate
        unpinned (``evidence_missing``); probes never raise — an
        unexpected store failure is itself honest absence.
        """

        probes: dict[
            str, Callable[[str, ComparisonAlternative], DecisionGate]
        ] = {
            'solver_gate': self._resolve_solver_gate,
            'channel_verify': self._resolve_channel_gate,
            'deployment': self._resolve_deployment_gate,
            'campaign': self._resolve_campaign_gate,
            'production_gate': self._resolve_production_gate,
        }
        gates: list[DecisionGate] = []
        for gate in DECISION_GATE_ORDER:
            try:
                gates.append(probes[gate](document_id, alternative))
            except Exception as exc:  # noqa: BLE001 — fail closed
                gates.append(
                    DecisionGate(
                        gate=gate,
                        note=f'証拠レコードの解決に失敗しました（{exc}）',
                    )
                )
        return tuple(gates)

    def kind_resolvers(self) -> Mapping[str, KindResolver]:
        """Save-time resolvers for the kinds :meth:`resolve_gates` emits.

        A :class:`CadDecisionBriefRepository` created with this mapping can
        prove every pin this resolver emits at write time.
        """

        def bind(
            kind: str, get: Callable[[str], object], sha_attr: str
        ) -> KindResolver:
            def resolve(ref_id: str) -> ResolvedAuthority | None:
                record = get(ref_id)
                if record is None:
                    return None
                return ResolvedAuthority(
                    kind=kind,
                    ref_id=ref_id,
                    document_id=getattr(record, 'document_id', '') or '',
                    semantic_sha256=getattr(record, sha_attr),
                )

            return resolve

        return {
            SOLVER_QUALIFICATION_PIN_KIND: bind(
                SOLVER_QUALIFICATION_PIN_KIND,
                self.correction_qualifications.get,
                'semantic_sha256',
            ),
            CHANNEL_VERDICT_PIN_KIND: bind(
                CHANNEL_VERDICT_PIN_KIND,
                self.channel_verification.get_verdict,
                'verdict_sha256',
            ),
            CALIBRATION_DEPLOYMENT_PIN_KIND: bind(
                CALIBRATION_DEPLOYMENT_PIN_KIND,
                self.calibration_deployments.get_deployment,
                'deployment_sha256',
            ),
            DEPLOYMENT_RECORD_PIN_KIND: bind(
                DEPLOYMENT_RECORD_PIN_KIND,
                self.deployment_pipeline.get_pipeline_record,
                'record_sha256',
            ),
            CAMPAIGN_VERDICT_PIN_KIND: bind(
                CAMPAIGN_VERDICT_PIN_KIND,
                self.campaign.get_verdict,
                'verdict_sha256',
            ),
            PRODUCTION_DECISION_PIN_KIND: bind(
                PRODUCTION_DECISION_PIN_KIND,
                self.production_readiness.get_decision,
                'decision_sha256',
            ),
        }

    # -- helpers --------------------------------------------------------

    def _declared_records(
        self, alternative: ComparisonAlternative
    ) -> list[object]:
        """Records the alternative declares via ``evidence_refs``.

        Declared evidence is re-verified like everything else: it lands
        in each probe's candidate pool and still must pass the scope and
        verdict checks — declaring a pin never bypasses the domain rules.
        """

        getters = (
            self.correction_qualifications.get,
            self.channel_verification.get_verdict,
            self.deployment_pipeline.get_pipeline_record,
            self.calibration_deployments.get_deployment,
            self.campaign.get_verdict,
            self.production_readiness.get_decision,
        )
        records: list[object] = []
        for ref in alternative.evidence_refs:
            for get in getters:
                try:
                    record = get(ref.ref_id)
                except Exception:  # noqa: BLE001 — wrong store shape
                    continue
                if record is not None:
                    records.append(record)
                    break
        return records

    @staticmethod
    def _pin(
        kind: str,
        ref_id: str,
        ref_sha256: str,
        evidence_class: Literal['measured', 'predicted', 'derived'],
        freshness: Literal['current', 'stale', 'unknown'],
    ) -> DecisionEvidencePin:
        return DecisionEvidencePin(
            kind=kind,
            ref_id=ref_id,
            ref_sha256=ref_sha256,
            evidence_class=evidence_class,
            freshness=freshness,
        )

    @staticmethod
    def _missing(gate: str, note: str) -> DecisionGate:
        return DecisionGate(gate=gate, pin=None, verdict=None, note=note)

    @staticmethod
    def _rejected_note(
        gate: str, rejected: int, what: str
    ) -> DecisionGate:
        return DecisionGate(
            gate=gate,
            pin=None,
            verdict=None,
            note=(
                f'{what}のうち候補とスコープが一致するものがありません'
                f'（{rejected} 件除外）'
                if rejected
                else f'{what}がありません'
            ),
        )

    # -- per-gate probes -------------------------------------------------

    _SOLVER_OUTCOME: dict[str, tuple[GateVerdict, str]] = {
        'DEPLOYED_AND_REMEASURED': ('verified', 'measured'),
        'SPATIALLY_HOLDOUT_VERIFIED': ('verified', 'measured'),
        'MEASURED_AT_CONTROL_POINTS': ('verified', 'measured'),
        'QUALIFIED_WITH_LIMITATIONS': ('conditional', 'measured'),
        'SIMULATED': ('conditional', 'predicted'),
        'DESIGN_ONLY': ('conditional', 'predicted'),
        'INSUFFICIENT_EVIDENCE': ('conditional', 'predicted'),
        'INCOMPATIBLE': ('failed', 'derived'),
    }

    def _resolve_solver_gate(
        self, document_id: str, alternative: ComparisonAlternative
    ) -> DecisionGate:
        gate = 'solver_gate'
        rejected = 0
        matched = None
        pool = [
            *self.correction_qualifications.list_for_document(document_id),
            *(
                record for record in self._declared_records(alternative)
                if hasattr(record, 'qualification_id')
            ),
        ]
        for record in pool:
            if alternative.system_variant_id is not None:
                if record.system_variant_id != alternative.system_variant_id:
                    rejected += 1
                    continue
                state: _ScopeState = (
                    'current'
                    if record.system_variant_sha256
                    == alternative.system_variant_sha256
                    else 'stale'
                )
                if (
                    record.scene_revision_id is not None
                    and record.scene_revision_id
                    != alternative.scene_revision_id
                ):
                    rejected += 1
                    continue
            else:
                if (
                    record.system_variant_id is not None
                    or record.scene_revision_id
                    != alternative.scene_revision_id
                ):
                    rejected += 1
                    continue
                state = 'current'
            matched = (record, state)
        if matched is None:
            return self._rejected_note(
                gate, rejected, '補正適格レコード'
            )
        record, state = matched
        verdict, evidence_class = self._SOLVER_OUTCOME.get(
            record.state, ('conditional', 'derived')
        )
        note = (
            f'補正適格レコード {record.qualification_id} '
            f'（状態 {record.state}）'
        )
        if record.correction_bands:
            bands = ', '.join(
                f'{lo:g}–{hi:g}Hz' for lo, hi in record.correction_bands
            )
            note += f' 対象帯域: {bands}'
        if (
            getattr(record, 'region', None) is not None
            and record.region.design_position_ids
        ):
            note += (
                ' 対象席: ' + ', '.join(record.region.design_position_ids)
            )
        return DecisionGate(
            gate=gate,
            pin=self._pin(
                SOLVER_QUALIFICATION_PIN_KIND,
                record.qualification_id,
                record.semantic_sha256,
                evidence_class,
                state,
            ),
            verdict=verdict,
            note=note,
        )

    def _resolve_channel_gate(
        self, document_id: str, alternative: ComparisonAlternative
    ) -> DecisionGate:
        gate = 'channel_verify'
        variant = None
        if alternative.system_variant_id is not None:
            variant = self.system_variants.get_variant(
                alternative.system_variant_id
            )
            if variant is None:
                return self._missing(
                    gate,
                    f'バリアント {alternative.system_variant_id} を解決'
                    'できないためチャンネル検証のスコープを確認できません',
                )
        entities = _scene_entity_ids(
            self.scene_repository, alternative.scene_revision_id
        )
        verdicts = list(
            self.channel_verification.list_verdicts(document_id)
        )
        verdicts.extend(
            record for record in self._declared_records(alternative)
            if hasattr(record, 'plan_ref') and hasattr(record, 'map_state')
        )
        rejected = 0
        matched = None
        for verdict_record in verdicts:
            plan = self.channel_verification.get_plan(
                verdict_record.plan_ref.ref_id
            )
            if plan is None:
                rejected += 1
                continue
            expected = {
                entity_id
                for target in plan.targets
                for entity_id in target.expected_speaker_entity_ids
            }
            if not expected or not expected.issubset(entities):
                rejected += 1
                continue
            if variant is not None:
                # The verified channel map covers a variant only when the
                # variant keeps the verified speaker set physically
                # unchanged: no proposed speaker entities and no diff
                # touching a verified speaker.
                proposes_speakers = any(
                    item.role_binding_id is not None
                    for item in variant.proposed_entities
                )
                touches_speakers = any(
                    item.entity_id in expected for item in variant.diff
                )
                if proposes_speakers or touches_speakers:
                    rejected += 1
                    continue
            matched = verdict_record
        if matched is None:
            return self._rejected_note(
                gate, rejected, 'チャンネル検証レコード'
            )
        verdict_map: dict[str, GateVerdict] = {
            'verified': 'verified',
            'failed': 'failed',
            'ambiguous': 'conditional',
            'incomplete': 'conditional',
            'unknown': 'conditional',
        }
        return DecisionGate(
            gate=gate,
            pin=self._pin(
                CHANNEL_VERDICT_PIN_KIND,
                matched.verdict_id,
                matched.verdict_sha256,
                'measured',
                'current',
            ),
            verdict=verdict_map.get(matched.map_state, 'conditional'),
            note=(
                f'チャンネル検証 {matched.verdict_id} '
                f'（状態 {matched.map_state}）'
            ),
        )

    def _resolve_deployment_gate(
        self, document_id: str, alternative: ComparisonAlternative
    ) -> DecisionGate:
        gate = 'deployment'
        rejected = 0
        matched = None
        deployments = list(
            self.calibration_deployments.list_deployments(document_id)
        )
        deployments.extend(
            record for record in self._declared_records(alternative)
            if hasattr(record, 'deployment_state')
            and hasattr(record, 'calibration_plan_id')
        )
        for deployment in deployments:
            plan = self._sealed_plan(deployment.calibration_plan_id)
            if plan is None:
                rejected += 1
                continue
            state = _scope_state(
                alternative,
                scene_ref=_PlanRefAdapter(
                    plan.scene_revision_id, plan.scene_content_hash
                ),
                variant_ref=_PlanRefAdapter(
                    plan.system_variant_id, plan.system_variant_sha256
                ),
            )
            if state in ('mismatch', 'unbound'):
                rejected += 1
                continue
            matched = (deployment, state)
        if matched is not None:
            deployment, state = matched
            verdict_map: dict[str, tuple[GateVerdict, str]] = {
                'deployment_verified': ('verified', 'measured'),
                'deployment_attested': ('conditional', 'measured'),
                'deployment_unverified': ('conditional', 'derived'),
                'deployment_mismatch': ('failed', 'measured'),
                'deployment_superseded': ('failed', 'derived'),
            }
            verdict, evidence_class = verdict_map.get(
                deployment.deployment_state, ('conditional', 'derived')
            )
            return DecisionGate(
                gate=gate,
                pin=self._pin(
                    CALIBRATION_DEPLOYMENT_PIN_KIND,
                    deployment.deployment_id,
                    deployment.deployment_sha256,
                    evidence_class,
                    state,
                ),
                verdict=verdict,
                note=(
                    f'デプロイ状態 {deployment.deployment_id} '
                    f'（{deployment.deployment_state}、'
                    f'証拠 {deployment.evidence_mode}）'
                ),
            )

        # Fallback: document-scoped pipeline stages. They bind a device
        # (binding_sha256), not the candidate — honest evidence of a
        # deployment on this document, but freshness can only ever be
        # ``unknown`` so the gate stays open, never satisfied.
        if alternative.system_variant_id is not None:
            return self._rejected_note(
                gate, rejected, 'デプロイ記録'
            )
        latest: dict[str, object] = {}
        for record in self.deployment_pipeline.list_pipeline_records(
            document_id
        ):
            latest[record.pipeline_id] = record
        if not latest:
            return self._rejected_note(
                gate, rejected, 'デプロイ記録'
            )
        record = list(latest.values())[-1]
        if (
            record.stage
            in ('failed', 'rollback_failed', 'readback_diverged')
            or record.readback_verdict == 'diverged'
            or record.rollback_outcome == 'failed'
            or record.partial_write in ('partial', 'failed')
        ):
            verdict = 'failed'
        elif (
            record.evidence_strength == 'machine_readback'
            and record.readback_verdict == 'matched'
        ):
            verdict = 'conditional'  # verified outcome but not candidate-bound
        else:
            verdict = 'conditional'
        return DecisionGate(
            gate=gate,
            pin=self._pin(
                DEPLOYMENT_RECORD_PIN_KIND,
                record.record_id,
                record.record_sha256,
                'measured'
                if record.evidence_strength == 'machine_readback'
                else 'derived',
                'unknown',
            ),
            verdict=verdict,
            note=(
                f'デプロイパイプライン {record.record_id} '
                f'（段階 {record.stage}、証拠強度 '
                f'{record.evidence_strength}）— 機器スコープの記録の'
                'ため候補への紐付けは確認できません'
            ),
        )

    def _resolve_campaign_gate(
        self, document_id: str, alternative: ComparisonAlternative
    ) -> DecisionGate:
        gate = 'campaign'
        verdicts = list(self.campaign.verdicts.list(document_id))
        verdicts.extend(
            record for record in self._declared_records(alternative)
            if hasattr(record, 'promotion_outcome')
        )
        rejected = 0
        matched = None
        for verdict_record in verdicts:
            prereg = self.campaign.get_preregistration(
                verdict_record.campaign_ref.ref_id
            )
            if prereg is None:
                rejected += 1
                continue
            state = _scope_state(
                alternative,
                scene_ref=prereg.scene_ref,
                variant_ref=prereg.system_variant_ref,
            )
            if state in ('mismatch', 'unbound'):
                rejected += 1
                continue
            matched = (verdict_record, state)
        if matched is None:
            return self._rejected_note(
                gate, rejected, '測定キャンペーン評決'
            )
        verdict_record, state = matched
        outcome_map: dict[str, GateVerdict] = {
            'recommendation_eligible': 'verified',
            'owned_room_domain_validated': 'conditional',
            'owned_room_trend_validated': 'conditional',
            'owned_room_absolute_prediction_limited': 'conditional',
            'external_only': 'conditional',
            'owned_room_insufficient': 'failed',
            'not_evaluated': 'failed',
        }
        return DecisionGate(
            gate=gate,
            pin=self._pin(
                CAMPAIGN_VERDICT_PIN_KIND,
                verdict_record.verdict_id,
                verdict_record.verdict_sha256,
                'measured',
                state,
            ),
            verdict=outcome_map.get(
                verdict_record.promotion_outcome, 'conditional'
            ),
            note=(
                f'測定キャンペーン評決 {verdict_record.verdict_id} '
                f'（推進結果 {verdict_record.promotion_outcome}）'
            ),
        )

    def _resolve_production_gate(
        self, document_id: str, alternative: ComparisonAlternative
    ) -> DecisionGate:
        gate = 'production_gate'
        decisions = list(
            self.production_readiness.decisions.list(document_id)
        )
        decisions.extend(
            record for record in self._declared_records(alternative)
            if hasattr(record, 'outcome')
            and hasattr(record, 'solver_path_kind')
        )
        rejected = 0
        matched = None
        for decision in decisions:
            state = _scope_state(
                alternative,
                scene_ref=decision.scene_ref,
                variant_ref=decision.system_variant_ref,
            )
            if state in ('mismatch', 'unbound'):
                rejected += 1
                continue
            matched = (decision, state)
        if matched is None:
            return self._rejected_note(
                gate, rejected, '本番適格決定'
            )
        decision, state = matched
        outcome_map: dict[str, GateVerdict] = {
            'production_ready': 'verified',
            'limited': 'conditional',
            'no_go': 'failed',
        }
        return DecisionGate(
            gate=gate,
            pin=self._pin(
                PRODUCTION_DECISION_PIN_KIND,
                decision.decision_id,
                decision.decision_sha256,
                'derived',
                state,
            ),
            verdict=outcome_map.get(decision.outcome, 'conditional'),
            note=(
                f'本番適格決定 {decision.decision_id} '
                f'（結果 {decision.outcome}）'
            ),
        )


class _PlanRefAdapter:
    """``ref_id``/``ref_sha256`` view of a calibration plan's scope pins."""

    __slots__ = ('ref_id', 'ref_sha256')

    def __init__(self, ref_id: str, ref_sha256: str | None) -> None:
        self.ref_id = ref_id
        self.ref_sha256 = ref_sha256


__all__ = [
    'CALIBRATION_DEPLOYMENT_PIN_KIND',
    'CAMPAIGN_VERDICT_PIN_KIND',
    'CHANNEL_VERDICT_PIN_KIND',
    'DEPLOYMENT_RECORD_PIN_KIND',
    'PRODUCTION_DECISION_PIN_KIND',
    'SOLVER_QUALIFICATION_PIN_KIND',
    'DecisionBriefEvidenceResolver',
]
