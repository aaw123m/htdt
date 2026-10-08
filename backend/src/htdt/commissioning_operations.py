"""Operator command port for the #868 commissioning closed loop (#946).

Derives the operator's next executable commands from the state the
machine already computes — ``CommissioningRunState`` +
``next_permitted_actions`` — and delegates each command to the
orchestrator's existing service methods. This module owns no truth of
its own: every authorization stays one-shot and candidate/deployment
pinned inside the sealed ledger, and every outcome the machine returns
(advanced / blocked / rejected / informational / terminal) is surfaced
verbatim. Hardware inputs that are absent render as honest disabled
reasons — or as sealed ``blocked`` transitions when the orchestrator
can evaluate the attempt itself — never as fake progress.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Literal

from .cad_authority_resolver import AuthorityRef
from .cad_commissioning_orchestrator import (
    CommissioningBeforeAfter,
    CommissioningOperatorAuthorization,
    CommissioningOrchestrator,
    CommissioningOrchestrationRun,
    CommissioningRunState,
    CommissioningStage,
    CommissioningStageTransition,
    next_permitted_actions,
)
from .clock import utc_now_iso
from .user_facing_error import operation_error_message

if TYPE_CHECKING:
    from .cad_calibration import (
        CadCalibrationExportSnapshot,
        CadCalibrationPlan,
    )
    from .cad_calibration_deployment import (
        CalibrationDeployment,
        DeploymentCapabilityDeclaration,
        DeploymentTargetClass,
        EffectivenessMetricDelta,
        EffectivenessVerdict,
    )
    from .cad_delegated_provider import DelegatedProviderManifest
    from .cad_device_adapter import (
        AdapterCapabilityReport,
        AdapterDeviceBinding,
        CalibrationDeviceAdapter,
        MaterializedCalibrationSettings,
    )
    from .cad_measurement_quality import CadMeasurementQualityReport


CommissioningCommand = Literal[
    'advance',
    'authorize_deploy',
    'deploy',
    'readback',
    'rollback',
    'restore_connectivity',
    'cancel',
]


#: The forward op each stage owns; ``operator_approval`` / ``deploy`` /
#: ``readback_verify`` have dedicated commands instead.
_ADVANCE_STAGES: dict[str, tuple[str, str]] = {
    'precheck': ('precheck_evaluated', '事前チェックを実行'),
    'baseline_measurement': (
        'baseline_acquisition_recorded', 'ベースライン測定を実行'),
    'ingest_quality_gate': ('quality_gate_evaluated', '品質ゲートを評価'),
    'analyze': ('analysis_bound', '解析証跡をバインド'),
    'optimize': ('optimization_committed', '最適化をコミット'),
    'compile_for_target': (
        'compile_completed', 'ターゲット向けにコンパイル'),
    'post_measurement': (
        'post_measurement_recorded', 'ポスト測定を実行'),
    'before_after': ('before_after_evaluated', '前後比較を評価'),
    'acceptance_decision': ('acceptance_decided', '受け入れ判定'),
}

_COMMAND_LABELS: dict[str, str] = {
    'advance': '進める',
    'authorize_deploy': '承認して進める',
    'deploy': 'デプロイを実行',
    'readback': '読み戻しを検証',
    'rollback': 'ロールバック',
    'restore_connectivity': '接続復帰を記録',
    'cancel': 'ランを中止',
}

_OUTCOME_WORDS: dict[str, str] = {
    'advanced': '進行',
    'completed': '完了',
    'informational': '記録',
    'invalidated': '無効化',
    'rolled_back': 'ロールバック完了',
    'aborted': '中止',
    'blocked': 'ブロック',
    'rejected': '拒否',
    'unavailable': '実行不可',
    'busy': '実行中',
    'error': '失敗',
}

_OK_OUTCOMES = frozenset(
    ('advanced', 'completed', 'informational', 'rolled_back', 'aborted'))


@dataclass(frozen=True)
class CommissioningAcquisitionRequest:
    """One delegated provider measurement the panel can ask for."""

    request_identity_repr: str
    request_sha256: str
    #: The provider call — absent drivers still run so the machine can
    #: seal the honest 'no acquisition driver supplied' blocked row.
    acquire: Callable[
        [],
        tuple[tuple[AuthorityRef, ...], str | None, str | None],
    ] | None = None


@dataclass(frozen=True)
class CommissioningBeforeAfterInput:
    """Inputs for the before/after comparison the run needs."""

    baseline_refs: tuple[AuthorityRef, ...]
    post_refs: tuple[AuthorityRef, ...]
    deltas: tuple['EffectivenessMetricDelta', ...]
    #: Pins the comparison to the read-back device config; the default
    #: resolves the newest CamillaDSP deployment session for the
    #: deployment.
    readback_config_sha256: str | None = None
    verdict: 'EffectivenessVerdict | None' = None
    note: str | None = None


@dataclass(frozen=True)
class CommissioningOperatorServices:
    """The injection boundary — every input the operator commands need
    that the sealed stores cannot derive for them.

    ``None`` fields are honest absence: the matching command renders
    disabled with a JA reason (or seals a ``blocked`` attempt when the
    orchestrator can evaluate honestly without the object). A restart
    that loses in-memory objects (e.g. the compile materialization)
    therefore degrades to disabled, never to fabricated evidence.
    """

    adapter: 'CalibrationDeviceAdapter | None' = None
    binding: 'AdapterDeviceBinding | None' = None
    #: Defaults resolve the manifest pinned on the run.
    manifest: 'DelegatedProviderManifest | None' = None
    plan: 'CadCalibrationPlan | None' = None
    export: 'CadCalibrationExportSnapshot | None' = None
    declaration: 'DeploymentCapabilityDeclaration | None' = None
    #: The exact materialization compile produced — deploy/read-back
    #: pin its sha; a restart can only re-supply it honestly, never
    #: regenerate an equivalent one (timestamps change the seal).
    materialization: 'MaterializedCalibrationSettings | None' = None
    capability_report: 'AdapterCapabilityReport | None' = None
    quality_report: 'CadMeasurementQualityReport | None' = None
    analysis_refs: tuple[AuthorityRef, ...] = ()
    baseline_request: CommissioningAcquisitionRequest | None = None
    post_request: CommissioningAcquisitionRequest | None = None
    before_after: CommissioningBeforeAfterInput | None = None
    target_class: 'DeploymentTargetClass' = 'unknown'


@dataclass(frozen=True)
class CommissioningOperation:
    """One operator command and whether it can run right now."""

    command: CommissioningCommand
    label: str
    enabled: bool
    disabled_reason: str = ''


@dataclass(frozen=True)
class CommissioningOperationResult:
    """What one command attempt produced — including honest failure."""

    command: str
    ok: bool
    outcome: str
    summary: str
    transition: CommissioningStageTransition | None = None
    record_id: str | None = None


@dataclass(frozen=True)
class CommissioningMutationPreview:
    """What the approval dialog must surface before a device mutation.

    The operator sees the exact pins the one-shot authorization will
    cover — target, scene/candidate/config hashes and the change the
    mutation applies — before it is sealed.
    """

    command: str
    scope: str
    lines: tuple[tuple[str, str], ...]
    candidate_sha256: str | None = None
    deployment_id: str | None = None
    #: An already-sealed, still-valid authorization this command would
    #: reuse instead of minting a new one.
    reusable_authorization_id: str | None = None
    requires_reason: bool = False


def _short(sha: str | None) -> str:
    return (sha or '—')[:16]


class CommissioningOperatorController:
    """Drives the commissioning ladder for one document.

    Each public method re-derives the run state, asks the machine what
    it currently permits, and refuses honestly when the command is not
    available — so a stale panel or a double-click can never fabricate
    an attempt the machine did not accept.
    """

    def __init__(
        self,
        orchestrator: CommissioningOrchestrator,
        document_id: str,
        *,
        services: CommissioningOperatorServices | None = None,
        clock: Callable[[], str] = utc_now_iso,
    ) -> None:
        self._orchestrator = orchestrator
        self._document_id = document_id
        self._services = services or CommissioningOperatorServices()
        self._clock = clock
        self._busy = False
        #: The compile product of this session — deploy and read-back
        #: pin this exact materialization's sha.
        self._materialization: (
            'MaterializedCalibrationSettings | None') = None

    # ------------------------------------------------------------------
    # reads / resolution

    def current(
        self,
    ) -> tuple[CommissioningOrchestrationRun, CommissioningRunState] | None:
        opened = self._orchestrator.get_open_run(self._document_id)
        if opened is not None:
            return opened
        runs = self._orchestrator.list_runs(self._document_id)
        if not runs:
            return None
        run = runs[-1]
        return run, self._orchestrator.derive_state(run)

    def _manifest(
        self, run: CommissioningOrchestrationRun,
    ) -> 'DelegatedProviderManifest | None':
        if self._services.manifest is not None:
            return self._services.manifest
        ref = run.provider_manifest_ref
        return self._orchestrator.delegated_repository \
            .get_provider_manifest(ref.ref_id)

    def _resolve_binding(
        self, run: CommissioningOrchestrationRun,
    ) -> 'AdapterDeviceBinding | None':
        binding = self._services.binding
        if binding is None:
            return None
        if binding.binding_sha256 != run.device_binding_sha256:
            return None
        return binding

    def _resolve_materialization(
        self,
    ) -> 'MaterializedCalibrationSettings | None':
        return (
            self._services.materialization or self._materialization)

    def _resolve_declaration(
        self,
        run: CommissioningOrchestrationRun,
        adapter_id: str | None = None,
    ) -> 'DeploymentCapabilityDeclaration | None':
        if self._services.declaration is not None:
            return self._services.declaration
        found = None
        for declaration in (
                self._orchestrator.deployment_repository
                .list_capability_declarations(run.document_id)):
            if adapter_id is not None \
                    and declaration.adapter_id != adapter_id:
                continue
            found = declaration
        return found

    def _resolve_deployment(
        self, run: CommissioningOrchestrationRun,
    ) -> 'CalibrationDeployment | None':
        found = None
        for deployment in (
                self._orchestrator.deployment_repository
                .list_deployments(run.document_id)):
            if deployment.binding_sha256 != run.device_binding_sha256:
                continue
            found = deployment
        return found

    def _resolve_before_after(
        self, run: CommissioningOrchestrationRun,
    ) -> CommissioningBeforeAfter | None:
        found = None
        for comparison in (
                self._orchestrator.repository
                .list_before_after(run.document_id)):
            if comparison.run_ref.ref_id != run.run_id:
                continue
            found = comparison
        return found

    def _readback_config_sha256(
        self,
        run: CommissioningOrchestrationRun,
        deployment: 'CalibrationDeployment',
    ) -> str | None:
        # Sessions pin the deployment record issued at apply time,
        # while read-back re-seals a *new* verified deployment — the
        # lineage is matched by membership in the run's binding.
        lineage_ids = {
            item.deployment_id
            for item in self._orchestrator.deployment_repository
            .list_deployments(run.document_id)
            if item.binding_sha256 == run.device_binding_sha256}
        for session in reversed(
                self._orchestrator.camilladsp_repository
                .list_deployment_sessions(run.document_id)):
            if session.deployment_ref.ref_id not in lineage_ids:
                continue
            if session.readback_config_sha256:
                return session.readback_config_sha256
        return None

    def _find_deploy_authorization(
        self,
        run: CommissioningOrchestrationRun,
        candidate_sha256: str,
        at_utc: str,
    ) -> CommissioningOperatorAuthorization | None:
        """Newest sealed deploy_apply auth still valid for this candidate.

        One-shot semantics: an authorization already consumed by an
        ``advanced`` deploy transition is never reused; an attempt that
        the machine blocked leaves the authorization untouched.
        """
        consumed: set[str] = set()
        for transition in self._orchestrator.transitions(run):
            if transition.event_kind != 'deploy_acked' \
                    or transition.outcome != 'advanced':
                continue
            consumed.update(
                ref.ref_id for ref in transition.evidence_refs)
        found = None
        for auth in self._orchestrator.repository.list_authorizations(
                run.document_id):
            if auth.run_ref.ref_id != run.run_id:
                continue
            if auth.scope != 'deploy_apply':
                continue
            if auth.candidate_sha256 != candidate_sha256:
                continue
            if auth.authorization_id in consumed:
                continue
            if auth.expires_at_utc is not None \
                    and auth.expires_at_utc < at_utc:
                continue
            found = auth
        return found

    # ------------------------------------------------------------------
    # operation enablement — derived from the machine's permitted set

    def operations(self) -> tuple[CommissioningOperation, ...]:
        opened = self.current()
        if opened is None:
            return tuple(
                CommissioningOperation(
                    command=command, label=_COMMAND_LABELS[command],
                    enabled=False,
                    disabled_reason='コミッショニングランがありません')
                for command in _COMMAND_LABELS)
        run, state = opened
        permitted = set(next_permitted_actions(state))
        return (
            self._advance_operation(run, state, permitted),
            self._authorize_operation(run, state, permitted),
            self._deploy_operation(run, state, permitted),
            self._readback_operation(run, state, permitted),
            self._rollback_operation(run, state, permitted),
            CommissioningOperation(
                command='restore_connectivity',
                label=_COMMAND_LABELS['restore_connectivity'],
                enabled='connectivity_restored' in permitted,
                disabled_reason=''
                if 'connectivity_restored' in permitted
                else '接続断が記録されていません'),
            CommissioningOperation(
                command='cancel',
                label=_COMMAND_LABELS['cancel'],
                enabled='abort' in permitted,
                disabled_reason=''
                if 'abort' in permitted
                else 'ランは終端にあります'),
        )

    def _advance_operation(
        self,
        run: CommissioningOrchestrationRun,
        state: CommissioningRunState,
        permitted: set[str],
    ) -> CommissioningOperation:
        stage = state.current_stage
        label = _COMMAND_LABELS['advance']
        if stage == 'operator_approval':
            return CommissioningOperation(
                'advance', label, enabled=False,
                disabled_reason='オペレーター承認が必要です — '
                                '「承認して進める」を実行してください')
        if stage == 'deploy':
            return CommissioningOperation(
                'advance', label, enabled=False,
                disabled_reason='デバイスへの適用は「デプロイを実行」'
                                'から行ってください')
        if stage == 'readback_verify':
            return CommissioningOperation(
                'advance', label, enabled=False,
                disabled_reason='読み戻しは「読み戻しを検証」'
                                'から行ってください')
        spec = _ADVANCE_STAGES.get(stage)
        if spec is None:
            return CommissioningOperation(
                'advance', label, enabled=False,
                disabled_reason='進行できるステージがありません')
        event_kind, action = spec
        label = f'{_COMMAND_LABELS["advance"]}（{action}）'
        if event_kind not in permitted:
            return CommissioningOperation(
                'advance', label, enabled=False,
                disabled_reason='この操作は現在のステージでは'
                                '許可されていません')
        reason = self._advance_disabled_reason(run, stage)
        return CommissioningOperation(
            'advance', label,
            enabled=reason is None, disabled_reason=reason or '')

    def _advance_disabled_reason(
        self,
        run: CommissioningOrchestrationRun,
        stage: CommissioningStage,
    ) -> str | None:
        """``None`` = runnable; a JA reason = honestly disabled."""
        services = self._services
        if stage == 'precheck':
            if self._manifest(run) is None:
                return 'プロバイダマニフェストが解決できません'
            return None
        if stage in ('baseline_measurement', 'post_measurement'):
            if self._manifest(run) is None:
                return 'プロバイダマニフェストが解決できません'
            request = (services.baseline_request
                       if stage == 'baseline_measurement'
                       else services.post_request)
            if request is None:
                return '測定リクエストが構成されていません'
            return None
        if stage == 'ingest_quality_gate':
            if services.quality_report is None:
                return '品質レポートがありません'
            return None
        if stage == 'analyze':
            return None  # empty refs land as an honest blocked attempt
        if stage == 'optimize':
            if services.plan is None:
                return '最適化プランがありません'
            return None
        if stage == 'compile_for_target':
            if services.adapter is None:
                return 'デバイスアダプタが構成されていません'
            if services.binding is None:
                return 'デバイスバインディングが構成されていません'
            if self._resolve_binding(run) is None:
                return ('バインディングがランのデバイスピンと'
                        '一致しません')
            if services.export is None:
                return 'ターゲット向けエクスポートがありません'
            return None
        if stage == 'before_after':
            if self._resolve_deployment(run) is None:
                return 'デプロイ記録がありません'
            if services.before_after is None:
                return '前後比較の入力がありません'
            return None
        if stage == 'acceptance_decision':
            if self._resolve_before_after(run) is None:
                return '前後比較の記録がありません'
            return None
        return '進行できるステージがありません'

    def _authorize_operation(
        self,
        run: CommissioningOrchestrationRun,
        state: CommissioningRunState,
        permitted: set[str],
    ) -> CommissioningOperation:
        label = _COMMAND_LABELS['authorize_deploy']
        if state.current_stage != 'operator_approval' \
                or 'operator_authorized' not in permitted:
            return CommissioningOperation(
                'authorize_deploy', label, enabled=False,
                disabled_reason='この操作は現在のステージでは'
                                '許可されていません')
        if self._services.plan is None:
            return CommissioningOperation(
                'authorize_deploy', label, enabled=False,
                disabled_reason='承認対象の校正候補がありません')
        return CommissioningOperation(
            'authorize_deploy', label, enabled=True)

    def _deploy_operation(
        self,
        run: CommissioningOrchestrationRun,
        state: CommissioningRunState,
        permitted: set[str],
    ) -> CommissioningOperation:
        label = _COMMAND_LABELS['deploy']
        reason = self._device_op_disabled_reason(
            run, state, permitted, 'deploy_acked',
            needs=('adapter', 'binding', 'export', 'materialization',
                   'declaration'))
        return CommissioningOperation(
            'deploy', label,
            enabled=reason is None, disabled_reason=reason or '')

    def _readback_operation(
        self,
        run: CommissioningOrchestrationRun,
        state: CommissioningRunState,
        permitted: set[str],
    ) -> CommissioningOperation:
        label = _COMMAND_LABELS['readback']
        reason = self._device_op_disabled_reason(
            run, state, permitted, 'readback_evaluated',
            needs=('adapter', 'binding', 'export', 'materialization',
                   'declaration', 'deployment'))
        return CommissioningOperation(
            'readback', label,
            enabled=reason is None, disabled_reason=reason or '')

    def _rollback_operation(
        self,
        run: CommissioningOrchestrationRun,
        state: CommissioningRunState,
        permitted: set[str],
    ) -> CommissioningOperation:
        label = _COMMAND_LABELS['rollback']
        reason = self._device_op_disabled_reason(
            run, state, permitted, 'rollback_completed',
            needs=('adapter', 'binding', 'deployment'))
        return CommissioningOperation(
            'rollback', label,
            enabled=reason is None, disabled_reason=reason or '')

    def _device_op_disabled_reason(
        self,
        run: CommissioningOrchestrationRun,
        state: CommissioningRunState,
        permitted: set[str],
        event_kind: str,
        needs: tuple[str, ...],
    ) -> str | None:
        if event_kind not in permitted:
            return 'この操作は現在のステージでは許可されていません'
        services = self._services
        materialization = self._resolve_materialization()
        for need in needs:
            if need == 'adapter' and services.adapter is None:
                return 'デバイスアダプタが構成されていません'
            if need == 'binding':
                if services.binding is None:
                    return 'デバイスバインディングが構成されていません'
                if self._resolve_binding(run) is None:
                    return ('バインディングがランのデバイスピンと'
                            '一致しません')
            if need == 'export' and services.export is None:
                return 'ターゲット向けエクスポートがありません'
            if need == 'materialization' and materialization is None:
                return ('コンパイル結果がありません — '
                        '先にターゲット向けコンパイルを実行してください')
            if need == 'declaration' and self._resolve_declaration(
                    run, materialization.adapter_id
                    if materialization is not None else None) is None:
                return 'デプロイ能力宣言がありません'
            if need == 'deployment' \
                    and self._resolve_deployment(run) is None:
                return 'デプロイ記録がありません'
        return None

    # ------------------------------------------------------------------
    # previews — what the approval dialog must show before mutation

    def authorize_preview(
        self,
    ) -> CommissioningMutationPreview | None:
        opened = self.current()
        plan = self._services.plan
        if opened is None or plan is None:
            return None
        run, _state = opened
        binding = self._resolve_binding(run)
        return CommissioningMutationPreview(
            command='authorize_deploy',
            scope='deploy_apply',
            lines=(
                ('対象デバイス',
                 self._binding_label(binding)),
                ('シーン内容ハッシュ',
                 _short(run.scene_content_hash)),
                ('校正候補 (計画セマンティックハッシュ)',
                 _short(plan.plan_semantic_sha256)),
                ('計画',
                 f'{plan.plan_id} v{plan.plan_version} '
                 f'({plan.authority_version})'),
                ('承認スコープ',
                 'deploy_apply — この候補のデプロイに1回限り有効'),
            ),
            candidate_sha256=plan.plan_semantic_sha256,
        )

    def deploy_preview(
        self,
    ) -> CommissioningMutationPreview | None:
        opened = self.current()
        if opened is None:
            return None
        run, _state = opened
        services = self._services
        materialization = self._resolve_materialization()
        if (services.export is None or materialization is None
                or self._resolve_binding(run) is None):
            return None
        binding = services.binding
        candidate = services.export.requested_plan_semantic_sha256
        reusable = self._find_deploy_authorization(
            run, candidate, self._clock())
        diff: list[str] = [
            f'チャンネル設定 {len(materialization.channel_settings)} 件']
        if materialization.quantization_notes:
            diff.append('量子化: '
                        + ', '.join(materialization.quantization_notes))
        if materialization.unsupported_items:
            diff.append('未対応項目: '
                        + ', '.join(materialization.unsupported_items))
        return CommissioningMutationPreview(
            command='deploy',
            scope='deploy_apply',
            lines=(
                ('対象デバイス', self._binding_label(binding)),
                ('シーン内容ハッシュ',
                 _short(run.scene_content_hash)),
                ('校正候補 (承認ピン)', _short(candidate)),
                ('エクスポート',
                 _short(services.export
                        .exported_settings_semantic_sha256)),
                ('マテリアライゼーション',
                 _short(materialization.materialization_sha256)),
                ('変更差分', ' / '.join(diff)),
                ('承認スコープ',
                 'deploy_apply — この候補のデプロイに1回限り有効'),
            ),
            candidate_sha256=candidate,
            reusable_authorization_id=(
                reusable.authorization_id if reusable else None),
        )

    def rollback_preview(
        self,
    ) -> CommissioningMutationPreview | None:
        opened = self.current()
        if opened is None:
            return None
        run, _state = opened
        deployment = self._resolve_deployment(run)
        binding = self._resolve_binding(run)
        if deployment is None or binding is None:
            return None
        previous = '—'
        lineage_ids = {
            item.deployment_id
            for item in self._orchestrator.deployment_repository
            .list_deployments(run.document_id)
            if item.binding_sha256 == run.device_binding_sha256}
        for session in reversed(
                self._orchestrator.camilladsp_repository
                .list_deployment_sessions(run.document_id)):
            if session.deployment_ref.ref_id in lineage_ids:
                previous = _short(session.previous_config_sha256)
                break
        return CommissioningMutationPreview(
            command='rollback',
            scope='rollback_apply',
            lines=(
                ('対象デプロイメント',
                 f'{deployment.deployment_id} '
                 f'({_short(deployment.deployment_sha256)})'),
                ('対象デバイス', self._binding_label(binding)),
                ('復元先 (前回コンフィグ)', previous),
                ('承認スコープ',
                 'rollback_apply — このデプロイメントの'
                 'ロールバックに1回限り有効'),
            ),
            deployment_id=deployment.deployment_id,
            requires_reason=True,
        )

    def _binding_label(self, binding: 'AdapterDeviceBinding | None') -> str:
        if binding is None:
            return '未構成'
        return (f'{binding.device_family} {binding.device_model} '
                f'@ {binding.device_serial}')

    # ------------------------------------------------------------------
    # execution

    def _execute(
        self,
        command: CommissioningCommand,
        fn: Callable[
            [CommissioningOrchestrationRun, CommissioningRunState],
            CommissioningOperationResult],
    ) -> CommissioningOperationResult:
        """Permission re-check + failure containment for every command."""
        if self._busy:
            return CommissioningOperationResult(
                command=command, ok=False, outcome='busy',
                summary='別の操作を実行中です')
        opened = self.current()
        if opened is None:
            return CommissioningOperationResult(
                command=command, ok=False, outcome='unavailable',
                summary='コミッショニングランがありません')
        run, state = opened
        op = {item.command: item for item in self.operations()}[command]
        if not op.enabled:
            return CommissioningOperationResult(
                command=command, ok=False, outcome='unavailable',
                summary=op.disabled_reason
                or 'この操作は現在のステージでは許可されていません')
        self._busy = True
        try:
            result = fn(run, state)
        except Exception as exc:  # error-boundary: orchestrator call
            result = CommissioningOperationResult(
                command=command, ok=False, outcome='error',
                summary=operation_error_message(exc))
        finally:
            self._busy = False
        return result

    def _result(
        self,
        command: str,
        transition: CommissioningStageTransition | None,
        *,
        action: str,
        record_id: str | None = None,
    ) -> CommissioningOperationResult:
        if transition is None:
            return CommissioningOperationResult(
                command=command, ok=False, outcome='unavailable',
                summary=f'{action}: 実行できませんでした',
                record_id=record_id)
        word = _OUTCOME_WORDS.get(transition.outcome, transition.outcome)
        return CommissioningOperationResult(
            command=command,
            ok=transition.outcome in _OK_OUTCOMES,
            outcome=transition.outcome,
            summary=f'{action}: {word} — {transition.reason}',
            transition=transition,
            record_id=record_id)

    def advance(self) -> CommissioningOperationResult:
        return self._execute('advance', self._do_advance)

    def _do_advance(
        self,
        run: CommissioningOrchestrationRun,
        state: CommissioningRunState,
    ) -> CommissioningOperationResult:
        stage = state.current_stage
        at_utc = self._clock()
        services = self._services
        if stage == 'precheck':
            capability_report = services.capability_report
            if capability_report is None \
                    and services.adapter is not None:
                capability_report = services.adapter.capability()
            transition = self._orchestrator.record_precheck(
                run, manifest=self._manifest(run),
                capability_report=capability_report, at_utc=at_utc)
            return self._result(
                'advance', transition, action='事前チェック')
        if stage in ('baseline_measurement', 'post_measurement'):
            request = (services.baseline_request
                       if stage == 'baseline_measurement'
                       else services.post_request)
            record, transition = self._orchestrator.record_acquisition(
                run, manifest=self._manifest(run),
                request_identity_repr=request.request_identity_repr,
                request_sha256=request.request_sha256,
                observed_at_utc=at_utc,
                stage='baseline' if stage == 'baseline_measurement'
                else 'post',
                acquire=request.acquire)
            return self._result(
                'advance', transition,
                action='ベースライン測定'
                if stage == 'baseline_measurement' else 'ポスト測定',
                record_id=record.acquisition_id)
        if stage == 'ingest_quality_gate':
            transition = self._orchestrator.record_quality_gate(
                run, report=services.quality_report, at_utc=at_utc)
            return self._result(
                'advance', transition, action='品質ゲート評価',
                record_id=services.quality_report.report_id)
        if stage == 'analyze':
            transition = self._orchestrator.bind_analysis(
                run, evidence_refs=services.analysis_refs,
                at_utc=at_utc)
            return self._result(
                'advance', transition, action='解析バインド')
        if stage == 'optimize':
            transition = self._orchestrator.commit_optimization(
                run, plan=services.plan, at_utc=at_utc)
            return self._result(
                'advance', transition, action='最適化コミット',
                record_id=services.plan.plan_id)
        if stage == 'compile_for_target':
            materialization, transition = (
                self._orchestrator.compile_for_target(
                    run, adapter=services.adapter,
                    export=services.export,
                    binding=self._resolve_binding(run),
                    declaration=self._resolve_declaration(run),
                    at_utc=at_utc))
            if materialization is not None \
                    and transition.outcome == 'advanced':
                self._materialization = materialization
            return self._result(
                'advance', transition, action='ターゲット向けコンパイル',
                record_id=materialization.materialization_id
                if materialization is not None else None)
        if stage == 'before_after':
            spec = services.before_after
            deployment = self._resolve_deployment(run)
            readback_sha = spec.readback_config_sha256 \
                or self._readback_config_sha256(run, deployment)
            if readback_sha is None:
                return CommissioningOperationResult(
                    command='advance', ok=False, outcome='unavailable',
                    summary='読み戻しコンフィグハッシュが解決できません')
            comparison, transition = (
                self._orchestrator.evaluate_before_after(
                    run, deployment=deployment,
                    readback_config_sha256=readback_sha,
                    baseline_refs=spec.baseline_refs,
                    post_refs=spec.post_refs,
                    deltas=spec.deltas,
                    at_utc=at_utc, verdict=spec.verdict,
                    note=spec.note))
            return self._result(
                'advance', transition, action='前後比較評価',
                record_id=comparison.comparison_id)
        if stage == 'acceptance_decision':
            comparison = self._resolve_before_after(run)
            verdict, transition = self._orchestrator.decide_acceptance(
                run, comparison=comparison, at_utc=at_utc)
            return self._result(
                'advance', transition, action='受け入れ判定',
                record_id=verdict.verdict_id)
        return CommissioningOperationResult(
            command='advance', ok=False, outcome='unavailable',
            summary='進行できるステージがありません')

    def authorize_deploy(
        self,
        *,
        operator_id: str,
        note: str | None = None,
        expires_at_utc: str | None = None,
    ) -> CommissioningOperationResult:
        return self._execute(
            'authorize_deploy',
            lambda run, state: self._do_authorize_deploy(
                run, operator_id=operator_id, note=note,
                expires_at_utc=expires_at_utc))

    def _do_authorize_deploy(
        self,
        run: CommissioningOrchestrationRun,
        *,
        operator_id: str,
        note: str | None,
        expires_at_utc: str | None,
    ) -> CommissioningOperationResult:
        authorization, transition = self._orchestrator.authorize(
            run, operator_id=operator_id, scope='deploy_apply',
            candidate_sha256=self._services.plan.plan_semantic_sha256,
            at_utc=self._clock(), expires_at_utc=expires_at_utc,
            note=note)
        return self._result(
            'authorize_deploy', transition, action='デプロイ承認',
            record_id=authorization.authorization_id)

    def deploy(
        self,
        *,
        operator_id: str,
        note: str | None = None,
        expires_at_utc: str | None = None,
    ) -> CommissioningOperationResult:
        return self._execute(
            'deploy',
            lambda run, state: self._do_deploy(
                run, operator_id=operator_id, note=note,
                expires_at_utc=expires_at_utc))

    def _do_deploy(
        self,
        run: CommissioningOrchestrationRun,
        *,
        operator_id: str,
        note: str | None,
        expires_at_utc: str | None,
    ) -> CommissioningOperationResult:
        services = self._services
        at_utc = self._clock()
        export = services.export
        materialization = self._resolve_materialization()
        candidate = export.requested_plan_semantic_sha256
        auth = self._find_deploy_authorization(run, candidate, at_utc)
        if auth is None:
            auth, _auth_transition = self._orchestrator.authorize(
                run, operator_id=operator_id, scope='deploy_apply',
                candidate_sha256=candidate, at_utc=at_utc,
                expires_at_utc=expires_at_utc, note=note)
        deployment, _session, transition = self._orchestrator.deploy(
            run, adapter=services.adapter,
            binding=self._resolve_binding(run),
            materialization=materialization, export=export,
            declaration=self._resolve_declaration(
                run, materialization.adapter_id),
            authorization_id=auth.authorization_id,
            target_class=services.target_class, at_utc=at_utc)
        return self._result(
            'deploy', transition, action='デプロイ',
            record_id=deployment.deployment_id
            if deployment is not None else None)

    def readback(self) -> CommissioningOperationResult:
        return self._execute('readback', self._do_readback)

    def _do_readback(
        self,
        run: CommissioningOrchestrationRun,
        _state: CommissioningRunState,
    ) -> CommissioningOperationResult:
        services = self._services
        materialization = self._resolve_materialization()
        deployment = self._resolve_deployment(run)
        verified, _session, transition = (
            self._orchestrator.verify_readback(
                run, adapter=services.adapter,
                binding=self._resolve_binding(run),
                export=services.export, deployment=deployment,
                materialization=materialization,
                declaration=self._resolve_declaration(
                    run, materialization.adapter_id),
                at_utc=self._clock()))
        return self._result(
            'readback', transition, action='読み戻し検証',
            record_id=verified.deployment_id
            if verified is not None else None)

    def rollback(
        self,
        *,
        operator_id: str,
        reason: str,
        note: str | None = None,
    ) -> CommissioningOperationResult:
        return self._execute(
            'rollback',
            lambda run, state: self._do_rollback(
                run, operator_id=operator_id, reason=reason,
                note=note))

    def _do_rollback(
        self,
        run: CommissioningOrchestrationRun,
        *,
        operator_id: str,
        reason: str,
        note: str | None,
    ) -> CommissioningOperationResult:
        at_utc = self._clock()
        deployment = self._resolve_deployment(run)
        deployment_ref = AuthorityRef(
            kind='calibration_deployment',
            ref_id=deployment.deployment_id,
            ref_sha256=deployment.deployment_sha256)
        auth, _auth_transition = self._orchestrator.authorize(
            run, operator_id=operator_id, scope='rollback_apply',
            deployment_ref=deployment_ref, at_utc=at_utc, note=note)
        record, transition = self._orchestrator.rollback(
            run, adapter=self._services.adapter,
            binding=self._resolve_binding(run),
            deployment=deployment,
            authorization_id=auth.authorization_id,
            reason=reason, at_utc=at_utc)
        return self._result(
            'rollback', transition, action='ロールバック',
            record_id=record.rollback_id)

    def restore_connectivity(
        self, *, reason: str,
    ) -> CommissioningOperationResult:
        return self._execute(
            'restore_connectivity',
            lambda run, state: self._result(
                'restore_connectivity',
                self._orchestrator.restore_connectivity(
                    run, reason=reason, at_utc=self._clock()),
                action='接続復帰'))

    def cancel(self, *, reason: str) -> CommissioningOperationResult:
        return self._execute(
            'cancel',
            lambda run, state: self._result(
                'cancel',
                self._orchestrator.abort(
                    run, reason=reason, at_utc=self._clock()),
                action='中止'))


__all__ = [
    'CommissioningAcquisitionRequest',
    'CommissioningBeforeAfterInput',
    'CommissioningCommand',
    'CommissioningMutationPreview',
    'CommissioningOperation',
    'CommissioningOperationResult',
    'CommissioningOperatorController',
    'CommissioningOperatorServices',
]
