"""Guided troubleshooting orchestrator (#885, REV68).

The diagnostic loop is machine-driven but evidence-bound:

    OBSERVATION -> HYPOTHESES -> TEST_PLANNING -> MEASUREMENT/READBACK
    -> EVIDENCE_UPDATE -> RANKING -> next TEST_PLANNING | RESOLUTION

This module orchestrates *discriminating tests* on top of the sealed
authorities that already own the evidence — #869 native sweep
acquisition, #876 channel verification, #806/#878 device read-back —
and reports ranking through the #719 residual-hypothesis verdict layer.
It never jumps from a residual to a root cause: a hypothesis only climbs
the #719 evidence ladder when a sealed test outcome names it.

Fail-closed rules enforced here:

* the stage-transition function is pure, deterministic and total —
  ``stage_transition(state, event)`` returns a decision or an explicit
  rejection for every input;
* a test plan is sealed *before* it runs: mechanism, safety class, the
  hypotheses it discriminates and the predeclared observation→effect
  mapping are pinned on the plan — an outcome cannot be reinterpreted
  after the fact;
* machine evidence is the only lane that produces observations —
  ``operator_observation`` plans are legal but never execute machine
  tests, and machine mechanisms never accept operator-entered facts;
* ranking changes are re-derivable: :func:`derive_session_state` folds
  the sealed transition log, and each :class:`DiagnosticEvidenceUpdate`
  pins the observation that caused every rank movement;
* safety is structural: protective-earth defeat and live-mains actions
  are *not representable* (``SAFETY_FORBIDDEN_ACTIONS`` rejects the
  labels at plan validation), device mutation needs a one-shot
  authorization, acoustic tests are capped by the session's level
  policy, and reconfiguration tests must pin a rollback plan;
* device read-back runs before any manual/re-entry plan — a manual
  step is only selectable once the tree's automated read-back evidence
  exists (``manual_requires_machine_exhaustion``);
* termination is honest: ``unresolved``/``needs_inspection`` are legal,
  common verdicts; ``root_cause_confirmed`` requires the #719 ladder's
  confirmation grade; a single surviving supported hypothesis resolves
  as ``fault_isolated`` within its declared scope — never as proof of a
  physical mechanism beyond that scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_calibration_deployment import _require_refs, _seal
from .cad_channel_verification import (
    ChannelVerificationPlan,
    run_verification_plan,
    verdict_binding as _cv_verdict_binding,
)
from .cad_device_adapter import (
    AdapterDeviceBinding,
    CalibrationDeviceAdapter,
    build_observation,
)
from .cad_diagnostic_hypothesis import (
    CadDiagnosticCase,
    CadDiagnosticHypothesis,
    CadDiagnosticTest,
    CadHypothesisOutcome,
    CadResidualSignature,
    CauseFamily,
    TestKind,
    build_diagnostic_case,
    build_diagnostic_hypothesis,
    build_diagnostic_test,
    diagnostic_case_binding,
    diagnostic_hypothesis_binding,
    diagnostic_verdict_binding,
    evaluate_diagnostic_verdict,
)
from .cad_sweep_acquisition import (
    AcquisitionRequest,
    ArmConfirmation,
    AudioIOBackend,
    LevelSafetyPolicy,
    MeasurementAcquisitionEngine,
    SweepStimulusSpec,
)
from .cad_sweep_acquisition_evidence import build_stimulus_definition
from .canonical_json import canonical_sha256 as _hash
from .clock import utc_now_iso as _utc_now


DIAGNOSTIC_ORCHESTRATOR_SCHEMA_VERSION = 'diagnostic-orchestrator-1'
DIAGNOSTIC_ORCHESTRATOR_EVALUATION_VERSION = 'diag-orch-eval-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _require_iso8601(value: str, label: str) -> None:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

DiagnosticStage = Literal[
    'observation',
    'hypotheses',
    'test_planning',
    'measurement',
    'evidence_update',
    'ranking',
    'resolution',
    'resolved',
    'unresolved',
    'needs_inspection',
    'aborted',
]

DIAGNOSTIC_STAGE_ORDER: tuple[DiagnosticStage, ...] = (
    'observation',
    'hypotheses',
    'test_planning',
    'measurement',
    'evidence_update',
    'ranking',
    'resolution',
)

DIAGNOSTIC_TERMINAL_STAGES: frozenset[DiagnosticStage] = frozenset(
    ('resolved', 'unresolved', 'needs_inspection', 'aborted'))

DiagnosticEventKind = Literal[
    'session_created',
    'observation_recorded',
    'hypotheses_registered',
    'test_selected',
    'test_executed',
    'evidence_updated',
    'ranking_evaluated',
    'next_test_selected',
    'operator_authorized',
    'resolution_decided',
    'abort',
]

DiagnosticTransitionOutcome = Literal[
    'advanced',
    'rejected',
    'blocked',
    'informational',
    'resolved',
    'unresolved',
    'needs_inspection',
    'aborted',
]

DiagnosticMechanism = Literal[
    'sweep_acquisition',
    'channel_verification',
    'device_readback',
    'operator_observation',
]
"""How a test produces evidence. ``operator_observation`` is a recorded
manual step — it never fabricates machine evidence and is only selectable
after the tree's automated read-back/measururement lane is exhausted."""

DiagnosticSafetyClass = Literal[
    'read_only',
    'acoustic',
    'device_mutation',
    'reconfiguration',
]
"""Safety envelope of a test. There is deliberately no class for mains/
protective-earth actions — they are unrepresentable, not just gated."""

SafetyVerdict = Literal[
    'permitted',
    'requires_authorization',
    'requires_rollback_plan',
    'requires_level_cap',
    'prohibited',
]

HypothesisEffect = Literal[
    'supported',
    'contradicted',
    'indistinguishable',
    'insufficient_evidence',
    'not_tested',
]
"""Effect one observation has on one hypothesis — mirrors the #719
``HypothesisOutcome`` minus ``out_of_domain`` (an executed in-tree test
is always in-domain for the hypotheses it discriminates)."""

HypothesisRankState = Literal[
    'candidate',
    'test_supported',
    'confirmed',
    'contradicted',
    'confounded',
]
"""Session-level derived state per hypothesis. ``confirmed`` is only
reachable through #719 confirmation-grade test kinds; discrimination
measurements stop at ``test_supported`` by construction."""

DiagnosticResolutionVerdict = Literal[
    'root_cause_confirmed',
    'fault_isolated',
    'unresolved',
    'needs_inspection',
]
"""Session termination. ``fault_isolated`` = exactly one hypothesis
survives uncontradicted *with* test support and every rival was
eliminated — the fault is located within the tree's declared scope.
``needs_inspection`` covers survivors that only physical/manual
inspection can settle, and cases where machine evidence stayed
insufficient."""

#: Action labels that can never be diagnostic tests — checked
#: structurally at plan validation so an unsafe test cannot even be
#: represented (#885 safety: protective-earth defeat, live-mains work).
SAFETY_FORBIDDEN_ACTION_TOKENS: frozenset[str] = frozenset((
    'protective_earth',
    'earth_defeat',
    'ground_lift',
    'mains_live',
    'cheater_plug',
    'lift_ground',
    'defeat_earth',
))

CAUSE_FAMILY_LABEL_KEYS: dict[CauseFamily, str] = {
    'measurement_chain': '測定チェーン',
    'registration_coordinate': '位置合わせ',
    'timebase_clock': 'タイムベース',
    'device_configuration': '機器設定',
    'routing_polarity': '配線・極性',
    'source_position_aim': '音源位置・指向',
    'receiver_position': '受音位置',
    'geometry_asbuilt': '竣工幾何',
    'material_boundary': '材料・境界',
    'source_model_directivity': '音源モデル',
    'nonlinear_level_state': '非線形・レベル状態',
    'environment_occupancy': '環境・占有',
    'numerical_solver': '数値ソルバー',
    'model_form_inadequacy': 'モデル形式の不備',
    'multiple_faults': '複合故障',
    'unknown': '不明',
}

DIAGNOSTIC_STAGE_LABELS: dict[str, str] = {
    'observation': '観測',
    'hypotheses': '仮説生成',
    'test_planning': '識別試験の選択',
    'measurement': '測定・読み戻し',
    'evidence_update': '証拠更新',
    'ranking': 'ランク付け・除外',
    'resolution': '解決判定',
    'resolved': '解決済み',
    'unresolved': '未解決',
    'needs_inspection': '要目視・実機確認',
    'aborted': '中止',
}

DIAGNOSTIC_RESOLUTION_LABELS: dict[str, str] = {
    'root_cause_confirmed': '根本原因確認（宣言範囲内）',
    'fault_isolated': '故障箇所を特定（宣言範囲内）',
    'unresolved': '未解決',
    'needs_inspection': '要目視・実機確認',
}

DIAGNOSTIC_MECHANISM_LABELS: dict[str, str] = {
    'sweep_acquisition': '掃引測定（#869）',
    'channel_verification': 'チャンネル検証（#876）',
    'device_readback': '機器読み戻し（#878）',
    'operator_observation': '作業者による観察',
}


# ---------------------------------------------------------------------------
# Fault trees — declarative, evidence-bound
# ---------------------------------------------------------------------------


class FaultTreeHypothesis(BaseModel):
    """One hypothesis definition inside a fault tree.

    Carries exactly what the issue asks a hypothesis to retain: identity,
    prerequisites, expected discriminating observations, available tests
    and assumptions/limitations. ``inspection_only`` marks hypotheses no
    machine test can ever confirm (real room behavior, hardware faults) —
    when such a hypothesis is the sole survivor the session resolves as
    ``needs_inspection``, not ``fault_isolated``.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    hypothesis_key: str = Field(min_length=1)
    cause_family: CauseFamily
    label: str = Field(min_length=1)
    prerequisites: tuple[str, ...] = ()
    expected_signatures: tuple[CadResidualSignature, ...]
    required_evidence: str = Field(min_length=1)
    known_confounders: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    inspection_only: bool = False

    @model_validator(mode='after')
    def _check(self) -> 'FaultTreeHypothesis':
        if not self.expected_signatures:
            raise ValueError(
                'a fault-tree hypothesis declares predicted observables')
        return self


class DiscriminationRule(BaseModel):
    """One observable→effect mapping evaluated after a test runs.

    ``observable`` names a fact key the mechanism's extractor produces;
    ``value_effects`` maps a canonical fact value ('true', 'false',
    'missing', or an explicit token) to per-hypothesis effects. A fact
    value with no mapped effect contributes ``insufficient_evidence`` —
    never a fabricated support.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    observable: str = Field(min_length=1)
    #: value -> hypothesis_key -> effect
    value_effects: dict[str, dict[str, HypothesisEffect]]
    note: str = ''


class FaultTestTemplate(BaseModel):
    """A planned discriminating test inside a fault tree.

    ``discriminates`` names the hypothesis keys this test can move;
    ``discriminations`` declares — before execution — how each possible
    observable value lands on those hypotheses. ``config`` is the
    mechanism's execution payload (routing/stimulus/channel keys); the
    orchestrator's mechanism drivers interpret it.

    ``manual_requires_machine_exhaustion`` marks manual-entry steps that
    must not be selectable while an automated read-back/measurement
    template in the same tree is still unexecuted (#885: read-back before
    proposing manual re-entry).
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    template_id: str = Field(min_length=1)
    mechanism: DiagnosticMechanism
    safety_class: DiagnosticSafetyClass
    test_label: str = Field(min_length=1)
    action_label: str = Field(min_length=1)
    discriminates: tuple[str, ...] = Field(min_length=1)
    discriminations: tuple[DiscriminationRule, ...] = ()
    config: dict[str, Any] = {}
    max_level_dbfs: float | None = None
    requires_rollback_plan: bool = False
    manual_requires_machine_exhaustion: bool = False
    predeclared_prediction: str = ''
    rollback_note: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'FaultTestTemplate':
        lowered = f'{self.test_label} {self.action_label}'.lower()
        for token in SAFETY_FORBIDDEN_ACTION_TOKENS:
            if token in lowered:
                raise ValueError(
                    f'forbidden action {token}: unsafe electrical/'
                    'protective-earth actions are not diagnostic tests')
        if self.safety_class == 'reconfiguration' and not self.rollback_note:
            raise ValueError(
                'reconfiguration tests require a rollback plan')
        if self.requires_rollback_plan and not self.rollback_note:
            raise ValueError(
                'requires_rollback_plan is set but no rollback_note is '
                'declared')
        for rule in self.discriminations:
            for key in rule.value_effects.values():
                for hypothesis_key in key:
                    if hypothesis_key not in self.discriminates:
                        raise ValueError(
                            f'discrimination rule names {hypothesis_key} '
                            'which this test does not discriminate')
        return self


class DiagnosticFaultTree(BaseModel):
    """A bounded fault tree: hypotheses + ordered discriminating tests."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    tree_id: str = Field(min_length=1)
    symptom_keys: tuple[str, ...] = Field(min_length=1)
    hypotheses: tuple[FaultTreeHypothesis, ...] = Field(min_length=1)
    test_templates: tuple[FaultTestTemplate, ...]
    scope_label: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'DiagnosticFaultTree':
        keys = [h.hypothesis_key for h in self.hypotheses]
        if len(set(keys)) != len(keys):
            raise ValueError('hypothesis keys must be unique')
        template_ids = [t.template_id for t in self.test_templates]
        if len(set(template_ids)) != len(template_ids):
            raise ValueError('test template ids must be unique')
        known = set(keys)
        for template in self.test_templates:
            for key in template.discriminates:
                if key not in known:
                    raise ValueError(
                        f'template {template.template_id} discriminates '
                        f'unknown hypothesis {key}')
        return self

    def hypothesis(self, key: str) -> FaultTreeHypothesis | None:
        for item in self.hypotheses:
            if item.hypothesis_key == key:
                return item
        return None

    def template(self, template_id: str) -> FaultTestTemplate | None:
        for item in self.test_templates:
            if item.template_id == template_id:
                return item
        return None


def _sig(label: str, observable: str, method: str) -> CadResidualSignature:
    return CadResidualSignature(
        signature_label=label,
        observable_label=observable,
        extraction_method=method,
    )


def _tree_hypotheses_sub_low() -> tuple[FaultTreeHypothesis, ...]:
    """'Sub response unexpectedly low' — the issue's example tree."""
    return (
        FaultTreeHypothesis(
            hypothesis_key='sub_not_routed',
            cause_family='device_configuration',
            label='サブ出力がルーティング/デプロイされていない',
            prerequisites=('サブ出力チャンネルがデプロイ対象に存在する',),
            expected_signatures=(
                _sig('sub-output-missing', 'sub excitation への応答なし',
                     'channel_verification'),
            ),
            required_evidence='機器読み戻し + サブ単独掃引で応答なし',
            known_confounders=('測定チェーン故障と区別不能な場合がある',),
            assumptions=('サブ出力経路が機器設定に存在すると仮定',),
            limitations=('機器が読み戻し非対応なら証拠階層が下がる',),
        ),
        FaultTreeHypothesis(
            hypothesis_key='polarity_crossover_cancellation',
            cause_family='routing_polarity',
            label='極性/クロスオーバーによる Main+Sub 相殺',
            expected_signatures=(
                _sig('crossover-notch',
                     'Main+Sub 合成でクロスオーバー帯域が落ちる',
                     'sweep_acquisition'),
                _sig('polarity-inversion',
                     'サブ IR の極性がメイン基準と逆', 'channel_verification'),
            ),
            required_evidence='Sub-only/Main-only/Main+Sub 掃引の比較',
            known_confounders=('境界干渉・設置位相と交絡する',),
        ),
        FaultTreeHypothesis(
            hypothesis_key='gain_preset_mismatch',
            cause_family='device_configuration',
            label='ゲイン/プリセット不一致',
            expected_signatures=(
                _sig('level-low', 'サブ応答レベルが期待より低い',
                     'sweep_acquisition'),
            ),
            required_evidence='読み戻しトリムとプリセット ID の差分',
        ),
        FaultTreeHypothesis(
            hypothesis_key='limiter_headroom',
            cause_family='nonlinear_level_state',
            label='リミッター/ヘッドルーム制限',
            expected_signatures=(
                _sig('level-capped',
                     '掃引レベルを上げても応答が伸びない', 'sweep_acquisition'),
            ),
            required_evidence='クリップ/圧縮挙動の掃引観測',
            limitations=('機器側リミッター読み戻しが無い場合は推定に留まる',),
        ),
        FaultTreeHypothesis(
            hypothesis_key='measurement_chain_fault',
            cause_family='measurement_chain',
            label='測定チェーン故障（マイク/IF/校正）',
            expected_signatures=(
                _sig('chain-dead',
                     '既知応答チャンネルでも応答が欠ける',
                     'sweep_acquisition'),
            ),
            required_evidence='基準チャンネル掃引が正常応答を返すこと',
        ),
        FaultTreeHypothesis(
            hypothesis_key='real_room_behavior',
            cause_family='geometry_asbuilt',
            label='実際の音源/部屋挙動（配置・境界由来）',
            expected_signatures=(
                _sig('room-notch', '位置依存的な実ディップ', 'sweep_acquisition'),
            ),
            required_evidence='機器側原因の全否定 + 物理再配置試験',
            inspection_only=True,
            limitations=('物理的な再配置・目視が最終判定を担う',),
        ),
    )


def _tree_sub_output_low() -> DiagnosticFaultTree:
    return DiagnosticFaultTree(
        tree_id='sub_output_low',
        symptom_keys=('sub_low', 'sub_missing', 'bass_cancellation'),
        hypotheses=_tree_hypotheses_sub_low(),
        scope_label='サブ出力低下 — ルーティング/極性/クロスオーバー/ゲイン/'
                    'リミッター/測定チェーン/実挙動',
        test_templates=(
            FaultTestTemplate(
                template_id='device_readback',
                mechanism='device_readback',
                safety_class='read_only',
                test_label='機器設定の機械読み戻し',
                action_label='read device configuration',
                discriminates=(
                    'sub_not_routed', 'gain_preset_mismatch'),
                discriminations=(
                    DiscriminationRule(
                        observable='deployed_config_match',
                        value_effects={
                            'true': {
                                'sub_not_routed': 'contradicted',
                                'gain_preset_mismatch': 'contradicted',
                            },
                            'false': {
                                'sub_not_routed': 'supported',
                                'gain_preset_mismatch': 'supported',
                            },
                            'missing': {
                                'sub_not_routed': 'insufficient_evidence',
                                'gain_preset_mismatch': (
                                    'insufficient_evidence'),
                            },
                        },
                        note='読み戻し差分は設定系仮説を直接識別する'),
                ),
                config={},
            ),
            FaultTestTemplate(
                template_id='sub_only_sweep',
                mechanism='sweep_acquisition',
                safety_class='acoustic',
                test_label='サブ単独掃引',
                action_label='sweep sub channel only',
                discriminates=(
                    'sub_not_routed', 'polarity_crossover_cancellation',
                    'gain_preset_mismatch', 'limiter_headroom',
                    'measurement_chain_fault'),
                discriminations=(
                    DiscriminationRule(
                        observable='response_detected',
                        value_effects={
                            'true': {
                                'sub_not_routed': 'contradicted',
                                'measurement_chain_fault': 'contradicted',
                            },
                            'false': {
                                'sub_not_routed': 'supported',
                                'measurement_chain_fault': (
                                    'insufficient_evidence'),
                            },
                            'missing': {
                                'sub_not_routed': 'insufficient_evidence',
                                'measurement_chain_fault': (
                                    'insufficient_evidence'),
                            },
                        }),
                    DiscriminationRule(
                        observable='level_dbfs',
                        value_effects={
                            'below_expectation': {
                                'gain_preset_mismatch': 'supported',
                                'limiter_headroom': 'insufficient_evidence',
                            },
                            'within_expectation': {
                                'gain_preset_mismatch': 'contradicted',
                            },
                            'missing': {
                                'gain_preset_mismatch': (
                                    'insufficient_evidence'),
                                'limiter_headroom': 'insufficient_evidence',
                            },
                        }),
                ),
                config={'channels': ('sub',)},
                max_level_dbfs=-12.0,
                predeclared_prediction=(
                    'ルーティング欠落なら応答なし、正常なら検出応答'),
            ),
            FaultTestTemplate(
                template_id='main_only_sweep',
                mechanism='sweep_acquisition',
                safety_class='acoustic',
                test_label='メイン単独掃引（基準チェーン健全性）',
                action_label='sweep main channel only',
                discriminates=('measurement_chain_fault',),
                discriminations=(
                    DiscriminationRule(
                        observable='response_detected',
                        value_effects={
                            'true': {
                                'measurement_chain_fault': 'contradicted',
                            },
                            'false': {
                                'measurement_chain_fault': 'supported',
                            },
                            'missing': {
                                'measurement_chain_fault': (
                                    'insufficient_evidence'),
                            },
                        }),
                ),
                config={'channels': ('main',)},
                max_level_dbfs=-12.0,
                predeclared_prediction='測定チェーン健全ならメインに応答あり',
            ),
            FaultTestTemplate(
                template_id='mainsub_sweep_compare',
                mechanism='sweep_acquisition',
                safety_class='acoustic',
                test_label='Main+Sub 合成掃引（極性/クロスオーバー識別）',
                action_label='sweep main+sub together',
                discriminates=(
                    'polarity_crossover_cancellation', 'limiter_headroom',
                    'real_room_behavior'),
                discriminations=(
                    DiscriminationRule(
                        observable='combined_polarity',
                        value_effects={
                            'opposed': {
                                'polarity_crossover_cancellation':
                                    'supported',
                                'real_room_behavior': 'indistinguishable',
                            },
                            'aligned': {
                                'polarity_crossover_cancellation':
                                    'contradicted',
                            },
                            'missing': {
                                'polarity_crossover_cancellation':
                                    'insufficient_evidence',
                                'limiter_headroom': 'insufficient_evidence',
                                'real_room_behavior': 'insufficient_evidence',
                            },
                        }),
                    DiscriminationRule(
                        observable='level_saturation',
                        value_effects={
                            'true': {'limiter_headroom': 'supported'},
                            'false': {'limiter_headroom': 'contradicted'},
                            'missing': {
                                'limiter_headroom': 'insufficient_evidence',
                            },
                        }),
                ),
                config={'channels': ('main', 'sub'), 'compare': True},
                max_level_dbfs=-12.0,
                predeclared_prediction=(
                    '極性反転/クロスオーバー相殺なら Main/Sub の IR 極性が'
                    '逆になる'),
            ),
            FaultTestTemplate(
                template_id='operator_inspection',
                mechanism='operator_observation',
                safety_class='read_only',
                test_label='物理確認（配線・設置・機器前面表示）',
                action_label='operator physical inspection',
                discriminates=('real_room_behavior', 'sub_not_routed'),
                discriminations=(
                    DiscriminationRule(
                        observable='operator_finding',
                        value_effects={
                            'wiring_fault_seen': {
                                'sub_not_routed': 'supported',
                            },
                            'nothing_found': {
                                'real_room_behavior': 'supported',
                            },
                            'missing': {
                                'real_room_behavior': (
                                    'insufficient_evidence'),
                            },
                        }),
                ),
                config={},
                manual_requires_machine_exhaustion=True,
            ),
        ),
    )


def _tree_channel_missing() -> DiagnosticFaultTree:
    return DiagnosticFaultTree(
        tree_id='channel_missing_output',
        symptom_keys=('channel_missing', 'channel_low', 'routing_wrong'),
        hypotheses=(
            FaultTreeHypothesis(
                hypothesis_key='routing_wrong_output',
                cause_family='routing_polarity',
                label='論理チャンネルが別の物理出力に配線',
                expected_signatures=(
                    _sig('misroute',
                         '他チャンネルで重複シグネチャまたは当該出力で応答なし',
                         'channel_verification'),
                ),
                required_evidence='チャンネル検証計画の per-channel 結果',
            ),
            FaultTreeHypothesis(
                hypothesis_key='device_not_deployed',
                cause_family='device_configuration',
                label='意図した設定が実機にデプロイされていない',
                expected_signatures=(
                    _sig('config-drift',
                         '読み戻し値がデプロイ済みエクスポートと不一致',
                         'device_readback'),
                ),
                required_evidence='機器読み戻し差分',
            ),
            FaultTreeHypothesis(
                hypothesis_key='muted_or_zero_gain',
                cause_family='nonlinear_level_state',
                label='当該出力がミュート/ゼロゲイン',
                expected_signatures=(
                    _sig('silent-output', '有効ルートだが応答ゼロ',
                         'channel_verification'),
                ),
                required_evidence='読み戻しトリム値またはレベル応答なし',
            ),
            FaultTreeHypothesis(
                hypothesis_key='measurement_chain_fault',
                cause_family='measurement_chain',
                label='測定チェーン故障（マイク/IF/ケーブル）',
                expected_signatures=(
                    _sig('chain-dead',
                         '基準チャンネルでも応答が欠ける', 'sweep_acquisition'),
                ),
                required_evidence='基準チャンネル掃引の正常応答',
            ),
            FaultTreeHypothesis(
                hypothesis_key='hardware_failure',
                cause_family='device_configuration',
                label='出力段/スピーカー実故障',
                expected_signatures=(
                    _sig('dead-stage', '設定正常・配線正常だが無応答',
                         'channel_verification'),
                ),
                required_evidence='機械検証全否定後の実機確認',
                inspection_only=True,
            ),
        ),
        scope_label='チャンネル出力欠落/低下 — 誤配線/未デプロイ/'
                    'ミュート/測定チェーン/実故障',
        test_templates=(
            FaultTestTemplate(
                template_id='device_readback',
                mechanism='device_readback',
                safety_class='read_only',
                test_label='機器設定の機械読み戻し',
                action_label='read device configuration',
                discriminates=(
                    'device_not_deployed', 'muted_or_zero_gain'),
                discriminations=(
                    DiscriminationRule(
                        observable='deployed_config_match',
                        value_effects={
                            'true': {
                                'device_not_deployed': 'contradicted',
                            },
                            'false': {
                                'device_not_deployed': 'supported',
                            },
                            'missing': {
                                'device_not_deployed':
                                    'insufficient_evidence',
                            },
                        }),
                    DiscriminationRule(
                        observable='channel_muted_or_zero',
                        value_effects={
                            'true': {'muted_or_zero_gain': 'supported'},
                            'false': {'muted_or_zero_gain': 'contradicted'},
                            'missing': {
                                'muted_or_zero_gain':
                                    'insufficient_evidence',
                            },
                        }),
                ),
                config={},
            ),
            FaultTestTemplate(
                template_id='channel_verify',
                mechanism='channel_verification',
                safety_class='acoustic',
                test_label='チャンネル検証計画（#876 cvpl-）',
                action_label='run channel verification plan',
                discriminates=(
                    'routing_wrong_output', 'muted_or_zero_gain',
                    'measurement_chain_fault', 'hardware_failure'),
                discriminations=(
                    DiscriminationRule(
                        observable='channel_routing_state',
                        value_effects={
                            'verified': {
                                'routing_wrong_output': 'contradicted',
                            },
                            'duplicate_suspect': {
                                'routing_wrong_output': 'supported',
                            },
                            'level_mismatch': {
                                'muted_or_zero_gain': 'supported',
                            },
                            'unexpected_response': {
                                'routing_wrong_output': 'supported',
                            },
                            'missing_output': {
                                'routing_wrong_output': 'insufficient_evidence',
                                'muted_or_zero_gain': 'insufficient_evidence',
                                'hardware_failure': 'insufficient_evidence',
                            },
                            'missing': {
                                'routing_wrong_output':
                                    'insufficient_evidence',
                            },
                        }),
                    DiscriminationRule(
                        observable='reference_channel_responded',
                        value_effects={
                            'true': {
                                'measurement_chain_fault': 'contradicted',
                            },
                            'false': {
                                'measurement_chain_fault': 'supported',
                            },
                            'missing': {
                                'measurement_chain_fault':
                                    'insufficient_evidence',
                            },
                        }),
                ),
                config={'plan': 'bound_verification_plan'},
                max_level_dbfs=-12.0,
                predeclared_prediction=(
                    '誤配線なら重複/不一致シグネチャ、健全なら verified'),
            ),
            FaultTestTemplate(
                template_id='alternate_output_sweep',
                mechanism='sweep_acquisition',
                safety_class='acoustic',
                test_label='代替出力掃引（出力段の切り分け）',
                action_label='sweep an alternate output',
                discriminates=('hardware_failure', 'routing_wrong_output'),
                discriminations=(
                    DiscriminationRule(
                        observable='response_detected',
                        value_effects={
                            'true': {
                                'hardware_failure': 'contradicted',
                            },
                            'false': {
                                'hardware_failure': 'supported',
                            },
                            'missing': {
                                'hardware_failure': 'insufficient_evidence',
                            },
                        }),
                ),
                config={'channels': ('alternate',)},
                max_level_dbfs=-12.0,
            ),
            FaultTestTemplate(
                template_id='operator_inspection',
                mechanism='operator_observation',
                safety_class='read_only',
                test_label='物理確認（端子・ケーブル・表示）',
                action_label='operator physical inspection',
                discriminates=('hardware_failure', 'routing_wrong_output'),
                discriminations=(
                    DiscriminationRule(
                        observable='operator_finding',
                        value_effects={
                            'wiring_fault_seen': {
                                'routing_wrong_output': 'supported',
                            },
                            'hardware_fault_seen': {
                                'hardware_failure': 'supported',
                            },
                            'missing': {
                                'hardware_failure': 'insufficient_evidence',
                            },
                        }),
                ),
                config={},
                manual_requires_machine_exhaustion=True,
            ),
        ),
    )


def _tree_prediction_residual() -> DiagnosticFaultTree:
    return DiagnosticFaultTree(
        tree_id='prediction_residual_unexplained',
        symptom_keys=('residual', 'prediction_mismatch'),
        hypotheses=(
            FaultTreeHypothesis(
                hypothesis_key='registration_error',
                cause_family='registration_coordinate',
                label='測定位置/座標の位置合わせ誤差',
                expected_signatures=(
                    _sig('registration-drift',
                         '再測定で残差パターンが位置依存で動く',
                         'sweep_acquisition'),
                ),
                required_evidence='再測定と位置合わせ記録の突合',
            ),
            FaultTreeHypothesis(
                hypothesis_key='measurement_chain_fault',
                cause_family='measurement_chain',
                label='測定チェーン較正/タイミング劣化',
                expected_signatures=(
                    _sig('chain-degraded',
                         'ループバック/基準応答で SNR・タイミング劣化',
                         'sweep_acquisition'),
                ),
                required_evidence='測定チェーン健全性試験',
            ),
            FaultTreeHypothesis(
                hypothesis_key='device_config_drift',
                cause_family='device_configuration',
                label='実機設定がモデル前提からずれている',
                expected_signatures=(
                    _sig('config-drift', '読み戻し値が前提と不一致',
                         'device_readback'),
                ),
                required_evidence='機器読み戻し差分',
            ),
            FaultTreeHypothesis(
                hypothesis_key='model_form_inadequacy',
                cause_family='model_form_inadequacy',
                label='モデル形式の不備（予測系が物理を表せない）',
                expected_signatures=(
                    _sig('model-gap',
                         'パラメータ調整で消えない構造的残差', 'none'),
                ),
                required_evidence='他仮説全否定 + モデル検討',
                inspection_only=True,
                limitations=('本層では残存候補として保持するのみ',),
            ),
        ),
        scope_label='予測-実測残差 — 位置合わせ/測定チェーン/機器設定/'
                    'モデル形式',
        test_templates=(
            FaultTestTemplate(
                template_id='device_readback',
                mechanism='device_readback',
                safety_class='read_only',
                test_label='機器設定の機械読み戻し',
                action_label='read device configuration',
                discriminates=('device_config_drift',),
                discriminations=(
                    DiscriminationRule(
                        observable='deployed_config_match',
                        value_effects={
                            'true': {'device_config_drift': 'contradicted'},
                            'false': {'device_config_drift': 'supported'},
                            'missing': {
                                'device_config_drift':
                                    'insufficient_evidence',
                            },
                        }),
                ),
                config={},
            ),
            FaultTestTemplate(
                template_id='chain_check_sweep',
                mechanism='sweep_acquisition',
                safety_class='acoustic',
                test_label='測定チェーン健全性掃引（基準出力）',
                action_label='sweep reference output',
                discriminates=('measurement_chain_fault',),
                discriminations=(
                    DiscriminationRule(
                        observable='response_detected',
                        value_effects={
                            'true': {
                                'measurement_chain_fault': 'contradicted',
                            },
                            'false': {
                                'measurement_chain_fault': 'supported',
                            },
                            'missing': {
                                'measurement_chain_fault':
                                    'insufficient_evidence',
                            },
                        }),
                    DiscriminationRule(
                        observable='quality_verdict',
                        value_effects={
                            'valid': {
                                'measurement_chain_fault': 'contradicted',
                            },
                            'invalid': {
                                'measurement_chain_fault': 'supported',
                            },
                            'limited': {
                                'measurement_chain_fault':
                                    'insufficient_evidence',
                            },
                            'missing': {
                                'measurement_chain_fault':
                                    'insufficient_evidence',
                            },
                        }),
                ),
                config={'channels': ('reference',)},
                max_level_dbfs=-12.0,
            ),
            FaultTestTemplate(
                template_id='remeasure_sweep',
                mechanism='sweep_acquisition',
                safety_class='acoustic',
                test_label='同条件再測定（残差再現性）',
                action_label='repeat the failing measurement',
                discriminates=('registration_error', 'model_form_inadequacy'),
                discriminations=(
                    DiscriminationRule(
                        observable='residual_reproduced',
                        value_effects={
                            'true': {
                                'registration_error': 'insufficient_evidence',
                                'model_form_inadequacy': 'supported',
                            },
                            'false': {
                                'registration_error': 'supported',
                            },
                            'missing': {
                                'registration_error':
                                    'insufficient_evidence',
                                'model_form_inadequacy':
                                    'insufficient_evidence',
                            },
                        }),
                ),
                config={'channels': ('failing',), 'repeat_of': 'symptom'},
                max_level_dbfs=-12.0,
            ),
        ),
    )


FAULT_TREES: dict[str, DiagnosticFaultTree] = {
    tree.tree_id: tree
    for tree in (
        _tree_sub_output_low(),
        _tree_channel_missing(),
        _tree_prediction_residual(),
    )
}
"""The representative executable fault trees (#885: ≥3 domains)."""


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


class DiagnosticSessionRecord(BaseModel):
    """One guided-troubleshooting session (diag-).

    Pins the triggering symptom, the fault tree in force, the #719 case
    this session escalates into, and the session's safety envelope —
    level cap and whether device mutation was authorized at all.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    session_id: str = Field(min_length=1)
    session_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    symptom_ref: AuthorityRef
    symptom_summary: str = Field(min_length=1)
    fault_tree_id: str = Field(min_length=1)
    case_ref: AuthorityRef
    device_binding_id: str | None = None
    device_binding_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN)
    expected_deployed_config_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN)
    reference_channel: str = 'main'
    max_stimulus_level_dbfs: float = -12.0
    opened_at_utc: str = Field(min_length=1)
    opened_by: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'DiagnosticSessionRecord':
        _require_iso8601(self.opened_at_utc, 'opened_at_utc')
        _require_refs(self.symptom_ref, self.case_ref)
        if self.case_ref.kind != 'diagnostic_case':
            raise ValueError("case_ref must pin a 'diagnostic_case'")
        if (self.device_binding_id is None) != (
                self.device_binding_sha256 is None):
            raise ValueError(
                'device binding id and sha pin together or not at all')
        if self.fault_tree_id not in FAULT_TREES:
            raise ValueError(
                f'unknown fault tree {self.fault_tree_id} — sessions only '
                'run declared trees')
        if self.session_sha256 != _hash(self.identity_payload()):
            raise ValueError('DiagnosticSessionRecord hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'session_id', 'session_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'DiagnosticSessionRecord':
        return _seal(
            cls, payload, 'session_id', 'session_sha256', 'diag')


def session_binding(session: DiagnosticSessionRecord) -> AuthorityRef:
    return AuthorityRef(
        kind='diagnostic_session',
        ref_id=session.session_id,
        ref_sha256=session.session_sha256,
    )


class DiagnosticStageTransition(BaseModel):
    """One sealed state-machine transition for a session (dtr-).

    The append-only log is the state — :func:`derive_session_state`
    folds it deterministically so a restarted app resumes exactly where
    the evidence left off.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    transition_id: str = Field(min_length=1)
    transition_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    session_ref: AuthorityRef
    seq: int = Field(ge=0)
    event_kind: DiagnosticEventKind
    outcome: DiagnosticTransitionOutcome
    actor: Literal['machine', 'operator', 'system'] = 'machine'
    from_stage: DiagnosticStage | None = None
    to_stage: DiagnosticStage
    event_succeeded: bool | None = None
    reason: str = Field(min_length=1)
    evidence_refs: tuple[AuthorityRef, ...] = ()
    recorded_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'DiagnosticStageTransition':
        _require_iso8601(self.recorded_at_utc, 'recorded_at_utc')
        _require_refs(self.session_ref)
        if self.session_ref.kind != 'diagnostic_session':
            raise ValueError(
                "session_ref must pin a 'diagnostic_session'")
        _require_refs(*self.evidence_refs)
        if self.event_kind == 'session_created':
            if self.from_stage is not None:
                raise ValueError('session_created has no from_stage')
        elif self.from_stage is None:
            raise ValueError('non-creation transitions require from_stage')
        if self.outcome == 'advanced' and self.to_stage == self.from_stage:
            raise ValueError('advanced transitions must change stage')
        if self.outcome in ('rejected', 'blocked', 'informational') and (
                self.to_stage != self.from_stage):
            raise ValueError(
                f'{self.outcome} transitions must not change stage')
        terminal_map = {
            'resolved': 'resolved',
            'unresolved': 'unresolved',
            'needs_inspection': 'needs_inspection',
            'aborted': 'aborted',
        }
        for outcome, stage in terminal_map.items():
            if self.outcome == outcome and self.to_stage != stage:
                raise ValueError(
                    f'{outcome} transitions must land on {stage}')
        if self.transition_sha256 != _hash(self.identity_payload()):
            raise ValueError('DiagnosticStageTransition hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'transition_id', 'transition_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'DiagnosticStageTransition':
        return _seal(
            cls, payload, 'transition_id', 'transition_sha256', 'dtr')


class DiagnosticHypothesisEntry(BaseModel):
    """The session's instance of one tree hypothesis (dhyp-).

    Pins the #719 hypothesis record this entry escalates into plus the
    tree-declared metadata — prerequisites, expected discriminating
    observations, available tests, assumptions/limitations. Rank is
    derived from evidence updates, never stored here.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    entry_id: str = Field(min_length=1)
    entry_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    session_ref: AuthorityRef
    hypothesis_key: str = Field(min_length=1)
    hypothesis_ref: AuthorityRef
    cause_family: CauseFamily
    label: str = Field(min_length=1)
    prerequisites: tuple[str, ...] = ()
    expected_observations: tuple[str, ...] = ()
    available_test_ids: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    inspection_only: bool = False
    rank_seed: int = Field(ge=0)
    declared_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'DiagnosticHypothesisEntry':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        _require_refs(self.session_ref, self.hypothesis_ref)
        if self.session_ref.kind != 'diagnostic_session':
            raise ValueError(
                "session_ref must pin a 'diagnostic_session'")
        if self.hypothesis_ref.kind != 'diagnostic_hypothesis':
            raise ValueError(
                "hypothesis_ref must pin a 'diagnostic_hypothesis'")
        if self.entry_sha256 != _hash(self.identity_payload()):
            raise ValueError('DiagnosticHypothesisEntry hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'entry_id', 'entry_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'DiagnosticHypothesisEntry':
        return _seal(
            cls, payload, 'entry_id', 'entry_sha256', 'dhyp')


def hypothesis_entry_binding(
    entry: DiagnosticHypothesisEntry,
) -> AuthorityRef:
    return AuthorityRef(
        kind='diagnostic_session_hypothesis',
        ref_id=entry.entry_id,
        ref_sha256=entry.entry_sha256,
    )


class DiagnosticTestPlan(BaseModel):
    """A sealed discriminating-test plan (dtp-) — sealed before it runs.

    Pins the mechanism, safety class, the hypotheses it discriminates,
    the predeclared observation→effect rules and the execution payload.
    ``authorization_class`` is derived from the safety class at build
    time: a device_mutation plan can never carry
    ``authorization_class='none'``.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    plan_id: str = Field(min_length=1)
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    session_ref: AuthorityRef
    template_id: str = Field(min_length=1)
    plan_seq: int = Field(ge=0)
    mechanism: DiagnosticMechanism
    safety_class: DiagnosticSafetyClass
    test_label: str = Field(min_length=1)
    action_label: str = Field(min_length=1)
    discriminates: tuple[str, ...] = Field(min_length=1)
    discriminations: tuple[DiscriminationRule, ...] = ()
    config: dict[str, Any] = {}
    level_cap_dbfs: float | None = None
    authorization_class: Literal['none', 'operator_authorization']
    rollback_note: str = ''
    manual_gate: bool = False
    predeclared_prediction: str = ''
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'DiagnosticTestPlan':
        _require_iso8601(self.created_at_utc, 'plan created_at_utc')
        _require_refs(self.session_ref)
        if self.session_ref.kind != 'diagnostic_session':
            raise ValueError(
                "session_ref must pin a 'diagnostic_session'")
        lowered = f'{self.test_label} {self.action_label}'.lower()
        for token in SAFETY_FORBIDDEN_ACTION_TOKENS:
            if token in lowered:
                raise ValueError(
                    f'forbidden action {token}: unsafe electrical/'
                    'protective-earth actions are not diagnostic tests')
        if self.safety_class == 'device_mutation' and (
                self.authorization_class != 'operator_authorization'):
            raise ValueError(
                'device mutation plans require operator authorization')
        if self.safety_class == 'reconfiguration' and not self.rollback_note:
            raise ValueError('reconfiguration requires a rollback plan')
        if self.plan_sha256 != _hash(self.identity_payload()):
            raise ValueError('DiagnosticTestPlan hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'plan_id', 'plan_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'DiagnosticTestPlan':
        return _seal(cls, payload, 'plan_id', 'plan_sha256', 'dtp')


def plan_binding(plan: DiagnosticTestPlan) -> AuthorityRef:
    return AuthorityRef(
        kind='diagnostic_test_plan',
        ref_id=plan.plan_id,
        ref_sha256=plan.plan_sha256,
    )


class DiagnosticOperatorAuthorization(BaseModel):
    """One-shot operator authorization for a mutating diagnostic test
    (dau-) — same semantics as #878's DeploymentOperatorAuthorization."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authorization_id: str = Field(min_length=1)
    authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    session_ref: AuthorityRef
    plan_ref: AuthorityRef
    operator_id: str = Field(min_length=1)
    authorized_at_utc: str = Field(min_length=1)
    expires_at_utc: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'DiagnosticOperatorAuthorization':
        _require_iso8601(self.authorized_at_utc, 'authorized_at_utc')
        if self.expires_at_utc is not None:
            _require_iso8601(self.expires_at_utc, 'expires_at_utc')
            if self.expires_at_utc <= self.authorized_at_utc:
                raise ValueError('expiry must be after authorized_at_utc')
        _require_refs(self.session_ref, self.plan_ref)
        if self.session_ref.kind != 'diagnostic_session':
            raise ValueError(
                "session_ref must pin a 'diagnostic_session'")
        if self.plan_ref.kind != 'diagnostic_test_plan':
            raise ValueError(
                "plan_ref must pin a 'diagnostic_test_plan'")
        if self.authorization_sha256 != _hash(self.identity_payload()):
            raise ValueError(
                'DiagnosticOperatorAuthorization hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'authorization_id', 'authorization_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'DiagnosticOperatorAuthorization':
        return _seal(
            cls, payload, 'authorization_id', 'authorization_sha256',
            'dau')


def authorization_binding(
    authorization: DiagnosticOperatorAuthorization,
) -> AuthorityRef:
    return AuthorityRef(
        kind='diagnostic_operator_authorization',
        ref_id=authorization.authorization_id,
        ref_sha256=authorization.authorization_sha256,
    )


class HypothesisEffectEntry(BaseModel):
    """One hypothesis's evaluated effect inside an evidence update."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    hypothesis_key: str = Field(min_length=1)
    hypothesis_ref: AuthorityRef
    effect: HypothesisEffect
    reason: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'HypothesisEffectEntry':
        _require_refs(self.hypothesis_ref)
        if self.hypothesis_ref.kind != 'diagnostic_session_hypothesis':
            raise ValueError(
                "hypothesis_ref must pin a "
                "'diagnostic_session_hypothesis'")
        return self


class DiagnosticObservationRecord(BaseModel):
    """The sealed observation one executed test produced (dob-).

    ``facts`` is the extracted evidence — a canonical tuple of
    (observable, value) pairs the plan's discrimination rules consume.
    Machine mechanisms pin the sealed evidence they produced; an
    ``operator_observation`` carries only operator-entered facts and
    never machine evidence refs.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    observation_id: str = Field(min_length=1)
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    session_ref: AuthorityRef
    plan_ref: AuthorityRef
    mechanism: DiagnosticMechanism
    facts: tuple[tuple[str, str], ...] = ()
    evidence_refs: tuple[AuthorityRef, ...] = ()
    backend_id: str | None = None
    backend_is_simulated: bool = False
    authorization_ref: AuthorityRef | None = None
    observed_at_utc: str = Field(min_length=1)
    notes: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'DiagnosticObservationRecord':
        _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        _require_refs(self.session_ref, self.plan_ref)
        if self.session_ref.kind != 'diagnostic_session':
            raise ValueError(
                "session_ref must pin a 'diagnostic_session'")
        if self.plan_ref.kind != 'diagnostic_test_plan':
            raise ValueError(
                "plan_ref must pin a 'diagnostic_test_plan'")
        _require_refs(self.authorization_ref, *self.evidence_refs)
        if self.authorization_ref is not None and (
                self.authorization_ref.kind
                != 'diagnostic_operator_authorization'):
            raise ValueError(
                "authorization_ref must pin a "
                "'diagnostic_operator_authorization'")
        keys = [k for k, _ in self.facts]
        if len(set(keys)) != len(keys):
            raise ValueError('fact observable keys must be unique')
        if self.mechanism == 'operator_observation' and self.evidence_refs:
            raise ValueError(
                'operator observations carry no machine evidence refs')
        if self.observation_sha256 != _hash(self.identity_payload()):
            raise ValueError('DiagnosticObservationRecord hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'DiagnosticObservationRecord':
        return _seal(
            cls, payload, 'observation_id', 'observation_sha256', 'dob')


def observation_binding(
    observation: DiagnosticObservationRecord,
) -> AuthorityRef:
    return AuthorityRef(
        kind='diagnostic_observation',
        ref_id=observation.observation_id,
        ref_sha256=observation.observation_sha256,
    )


class DiagnosticEvidenceUpdate(BaseModel):
    """One sealed evidence transition (dev-).

    Pins the observation that caused it and the deterministic rank order
    before/after — a ranking change with no observation pin is
    unrepresentable, which is what makes ranking evidence-driven and
    reproducible.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    update_id: str = Field(min_length=1)
    update_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    session_ref: AuthorityRef
    observation_ref: AuthorityRef
    effects: tuple[HypothesisEffectEntry, ...] = ()
    ranking_before: tuple[str, ...] = ()
    ranking_after: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_version: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'DiagnosticEvidenceUpdate':
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        _require_refs(self.session_ref, self.observation_ref)
        if self.session_ref.kind != 'diagnostic_session':
            raise ValueError(
                "session_ref must pin a 'diagnostic_session'")
        if self.observation_ref.kind != 'diagnostic_observation':
            raise ValueError(
                "observation_ref must pin a 'diagnostic_observation'")
        if sorted(self.ranking_before) != sorted(self.ranking_after):
            raise ValueError(
                'ranking_before/after must order the same hypothesis '
                'keys')
        if self.update_sha256 != _hash(self.identity_payload()):
            raise ValueError('DiagnosticEvidenceUpdate hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'update_id', 'update_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'DiagnosticEvidenceUpdate':
        return _seal(
            cls, payload, 'update_id', 'update_sha256', 'dev')


class DiagnosticResolutionRecord(BaseModel):
    """The sealed termination verdict (dres-).

    ``verdict_ref`` pins the #719 case verdict this resolution derives
    from — the session never outruns the evidence ladder it composes.
    ``unresolved``/``needs_inspection`` are honest terminal states.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    resolution_id: str = Field(min_length=1)
    resolution_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    session_ref: AuthorityRef
    verdict: DiagnosticResolutionVerdict
    verdict_ref: AuthorityRef
    surviving_hypothesis_keys: tuple[str, ...] = ()
    eliminated_hypothesis_keys: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    manual_next_steps: tuple[str, ...] = ()
    decided_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'DiagnosticResolutionRecord':
        _require_iso8601(self.decided_at_utc, 'decided_at_utc')
        _require_refs(self.session_ref, self.verdict_ref)
        if self.session_ref.kind != 'diagnostic_session':
            raise ValueError(
                "session_ref must pin a 'diagnostic_session'")
        if self.verdict_ref.kind != 'diagnostic_verdict':
            raise ValueError(
                "verdict_ref must pin a 'diagnostic_verdict'")
        if self.verdict == 'root_cause_confirmed' and (
                not self.surviving_hypothesis_keys):
            raise ValueError(
                'a confirmed resolution names the confirmed hypotheses')
        if self.verdict == 'fault_isolated' and (
                len(self.surviving_hypothesis_keys) != 1):
            raise ValueError(
                'fault_isolated requires exactly one surviving '
                'hypothesis')
        if self.resolution_sha256 != _hash(self.identity_payload()):
            raise ValueError('DiagnosticResolutionRecord hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'resolution_id', 'resolution_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'DiagnosticResolutionRecord':
        return _seal(
            cls, payload, 'resolution_id', 'resolution_sha256', 'dres')


# ---------------------------------------------------------------------------
# Events + the pure transition machine
# ---------------------------------------------------------------------------


class DiagnosticEvent(BaseModel):
    """One fact offered to the state machine."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: DiagnosticEventKind
    at_utc: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    actor: Literal['machine', 'operator', 'system'] = 'machine'
    succeeded: bool | None = None
    evidence_refs: tuple[AuthorityRef, ...] = ()
    target_stage: DiagnosticStage | None = None

    @model_validator(mode='after')
    def _check(self) -> 'DiagnosticEvent':
        _require_iso8601(self.at_utc, 'event at_utc')
        _require_refs(*self.evidence_refs)
        return self


@dataclass(frozen=True)
class DiagnosticSessionState:
    """Folded state of one session — pure derivation."""

    session_ref: AuthorityRef
    current_stage: DiagnosticStage = 'observation'
    last_event_at_utc: str | None = None
    last_reason: str = ''
    blocked_reason: str | None = None
    terminal: bool = False
    terminated_stage: DiagnosticStage | None = None
    transition_count: int = 0
    seen_kinds: frozenset[str] = frozenset()
    executed_plan_ids: tuple[str, ...] = ()
    planned_template_ids: tuple[str, ...] = ()
    evidence: dict[str, AuthorityRef] = field(default_factory=dict)


@dataclass(frozen=True)
class DiagnosticDecision:
    to_stage: DiagnosticStage
    outcome: DiagnosticTransitionOutcome
    reason: str


@dataclass(frozen=True)
class DiagnosticRejection:
    reason: str


#: (stage, event) -> next stage. Everything else is a non-advancing
#: event handled below or an explicit rejection.
_ADVANCE_RULES: dict[
    tuple[DiagnosticStage, DiagnosticEventKind], DiagnosticStage
] = {
    ('observation', 'observation_recorded'): 'hypotheses',
    ('hypotheses', 'hypotheses_registered'): 'test_planning',
    ('test_planning', 'test_selected'): 'measurement',
    ('measurement', 'test_executed'): 'evidence_update',
    ('evidence_update', 'evidence_updated'): 'ranking',
    ('ranking', 'ranking_evaluated'): 'test_planning',
    ('ranking', 'resolution_decided'): 'resolution',
    ('test_planning', 'resolution_decided'): 'resolution',
    ('resolution', 'resolution_decided'): 'resolved',
}


def stage_transition(
    state: DiagnosticSessionState,
    event: DiagnosticEvent,
) -> DiagnosticDecision | DiagnosticRejection:
    """Pure, deterministic, total: (state, event) -> decision | rejection."""
    kind = event.kind
    current = state.current_stage

    if kind == 'session_created':
        return DiagnosticDecision('observation', 'advanced', event.reason)

    if current in DIAGNOSTIC_TERMINAL_STAGES:
        return DiagnosticRejection(f'session_terminal:{current}')

    if kind == 'abort':
        return DiagnosticDecision('aborted', 'aborted', event.reason)

    if kind == 'operator_authorized':
        if current in ('test_planning', 'measurement'):
            return DiagnosticDecision(
                current, 'informational', event.reason)
        return DiagnosticRejection(
            f'event_not_permitted:{kind}@{current}')

    if kind == 'resolution_decided':
        if current in ('ranking', 'test_planning', 'resolution'):
            target = event.target_stage
            if target in ('resolved', 'unresolved', 'needs_inspection'):
                # The terminal outcome is pinned on the event so the
                # transition log alone re-derives the end state.
                return DiagnosticDecision(target, target, event.reason)
            return DiagnosticDecision(
                'resolution', 'advanced', event.reason)
        return DiagnosticRejection(
            f'event_not_permitted:{kind}@{current}')

    nxt = _ADVANCE_RULES.get((current, kind))
    if nxt is None:
        return DiagnosticRejection(
            f'event_not_permitted:{kind}@{current}')
    if event.succeeded is False:
        return DiagnosticDecision(current, 'blocked', event.reason)
    return DiagnosticDecision(nxt, 'advanced', event.reason)


def derive_session_state(
    session: DiagnosticSessionRecord,
    transitions: tuple[DiagnosticStageTransition, ...],
) -> DiagnosticSessionState:
    """Fold the sealed transition log into the current session state.

    Pure and deterministic — the same log folds to the same state after
    a restart, which is what makes a diagnostic session resumable.
    """
    session_ref = session_binding(session)
    current: DiagnosticStage = 'observation'
    terminal = False
    terminated: DiagnosticStage | None = None
    blocked_reason: str | None = None
    last_reason = ''
    last_at: str | None = None
    seen: set[str] = set()
    evidence: dict[str, AuthorityRef] = {}
    executed: list[str] = []
    planned: list[str] = []

    for t in sorted(transitions, key=lambda item: item.seq):
        seen.add(t.event_kind)
        last_at = t.recorded_at_utc
        last_reason = t.reason
        if t.outcome == 'advanced' or t.outcome in (
                'resolved', 'unresolved', 'needs_inspection', 'aborted'):
            current = t.to_stage
            blocked_reason = None
            if t.outcome != 'advanced':
                terminal = True
                terminated = t.to_stage
            if t.event_kind == 'test_executed':
                for ref in t.evidence_refs:
                    if ref.kind == 'diagnostic_observation':
                        executed.append(ref.ref_id)
            if t.event_kind == 'test_selected':
                for ref in t.evidence_refs:
                    if ref.kind == 'diagnostic_test_plan':
                        planned.append(ref.ref_id)
        elif t.outcome in ('blocked', 'rejected'):
            blocked_reason = t.reason
        for ref in t.evidence_refs:
            evidence[ref.kind] = ref

    return DiagnosticSessionState(
        session_ref=session_ref,
        current_stage=current,
        last_event_at_utc=last_at,
        last_reason=last_reason,
        blocked_reason=blocked_reason,
        terminal=terminal,
        terminated_stage=terminated,
        transition_count=len(transitions),
        seen_kinds=frozenset(seen),
        executed_plan_ids=tuple(executed),
        planned_template_ids=tuple(planned),
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Safety evaluation (plan level)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SafetyEvaluation:
    verdict: SafetyVerdict
    reasons: tuple[str, ...]


def evaluate_test_plan_safety(
    template: FaultTestTemplate,
    session: DiagnosticSessionRecord,
) -> SafetyEvaluation:
    """Plan-level safety gate — evaluated before any plan is sealed.

    Returns ``prohibited`` for anything structurally unsafe or
    unrepresentable; ``requires_authorization`` for device mutation;
    ``requires_rollback_plan`` for reconfiguration without a declared
    rollback; ``requires_level_cap`` when the stimulus exceeds the
    session's exposure cap.
    """
    reasons: list[str] = []
    lowered = f'{template.test_label} {template.action_label}'.lower()
    for token in SAFETY_FORBIDDEN_ACTION_TOKENS:
        if token in lowered:
            return SafetyEvaluation('prohibited', (
                f'{token}: unsafe electrical/protective-earth actions '
                'are prohibited by policy — they are not diagnostic '
                'tests',))
    if template.safety_class == 'device_mutation':
        reasons.append(
            'device mutation requires an explicit one-shot operator '
            'authorization (#885 safety)')
        return SafetyEvaluation('requires_authorization', tuple(reasons))
    if (template.safety_class == 'reconfiguration'
            or template.requires_rollback_plan):
        if not template.rollback_note:
            return SafetyEvaluation('requires_rollback_plan', (
                'reconfiguration tests require a declared rollback plan',))
        reasons.append('rollback plan declared: ' + template.rollback_note)
    if template.safety_class == 'acoustic':
        cap = template.max_level_dbfs
        if cap is None or cap > session.max_stimulus_level_dbfs:
            return SafetyEvaluation('requires_level_cap', (
                f'acoustic test level cap {cap} exceeds the session '
                f'exposure policy {session.max_stimulus_level_dbfs} '
                'dBFS (#602/#885)',))
        reasons.append(
            f'acoustic level capped at {cap} dBFS (session policy '
            f'{session.max_stimulus_level_dbfs} dBFS)')
    if template.mechanism == 'operator_observation' and (
            template.manual_requires_machine_exhaustion):
        reasons.append(
            'manual step gated behind automated machine evidence — '
            'read-back runs before manual re-entry is proposed')
    return SafetyEvaluation(
        'permitted', tuple(reasons) or ('no safety conditions',))


# ---------------------------------------------------------------------------
# Ranking + resolution (deterministic)
# ---------------------------------------------------------------------------


_RANK_ORDER: dict[HypothesisRankState, int] = {
    'confirmed': 0,
    'test_supported': 1,
    'candidate': 2,
    'confounded': 3,
    'contradicted': 4,
}


def compute_hypothesis_states(
    entries: tuple[DiagnosticHypothesisEntry, ...],
    updates: tuple[DiagnosticEvidenceUpdate, ...],
) -> dict[str, HypothesisRankState]:
    """Derived per-hypothesis state from every evidence update.

    Deterministic fold: later observations override earlier effects for
    the same hypothesis except ``contradicted``, which is absorbing — a
    contradicted hypothesis never climbs back.
    """
    states: dict[str, HypothesisRankState] = {
        entry.hypothesis_key: 'candidate' for entry in entries
    }
    for update in updates:
        for effect in update.effects:
            key = effect.hypothesis_key
            if key not in states:
                continue
            if states[key] == 'contradicted':
                continue
            if effect.effect == 'contradicted':
                states[key] = 'contradicted'
            elif effect.effect == 'supported':
                states[key] = 'test_supported'
            elif effect.effect == 'indistinguishable':
                states[key] = 'confounded'
    return states


def rank_hypotheses(
    entries: tuple[DiagnosticHypothesisEntry, ...],
    states: Mapping[str, HypothesisRankState],
) -> tuple[str, ...]:
    """Deterministic hypothesis ordering: derived state first, then the
    tree-declared seed order — an evidence-free reorder cannot happen."""
    indexed = {entry.hypothesis_key: entry.rank_seed for entry in entries}
    return tuple(sorted(
        states,
        key=lambda key: (
            _RANK_ORDER[states[key]], indexed.get(key, 1 << 20)),
    ))


def select_next_template(
    tree: DiagnosticFaultTree,
    executed_template_ids: frozenset[str],
    states: Mapping[str, HypothesisRankState],
) -> FaultTestTemplate | None:
    """Deterministic next-test selection.

    In declared tree order, pick the first unexecuted template that still
    discriminates at least one live (non-contradicted) hypothesis.
    Manual steps gated by ``manual_requires_machine_exhaustion`` are only
    eligible once every automated template is executed or useless —
    device read-back precedes manual re-entry by construction.
    """
    live = {
        key for key, state in states.items() if state != 'contradicted'
    }
    automated_pending = any(
        template.mechanism != 'operator_observation'
        and template.template_id not in executed_template_ids
        and any(k in live for k in template.discriminates)
        for template in tree.test_templates
    )
    for template in tree.test_templates:
        if template.template_id in executed_template_ids:
            continue
        if not any(k in live for k in template.discriminates):
            continue
        if (template.manual_requires_machine_exhaustion
                and automated_pending):
            continue
        return template
    return None


def derive_resolution(
    session: DiagnosticSessionRecord,
    entries: tuple[DiagnosticHypothesisEntry, ...],
    states: Mapping[str, HypothesisRankState],
    tests: tuple[CadDiagnosticTest, ...],
    hypotheses: tuple[CadDiagnosticHypothesis, ...],
    case: CadDiagnosticCase,
    *,
    decided_at_utc: str,
    no_more_tests: bool,
) -> tuple[DiagnosticResolutionRecord, Any]:
    """Evaluate the terminal verdict via the #719 ladder.

    Returns (resolution_record, cad_verdict). The session verdict maps
    the #719 case verdict honestly: confirmation-grade evidence →
    ``root_cause_confirmed``; a single surviving supported hypothesis →
    ``fault_isolated`` (declared scope only); inspection-only survivors
    or insufficient machine evidence → ``needs_inspection``;
    contradicted-out or confounded campaigns → ``unresolved``/
    ``needs_inspection``.
    """
    verdict_719 = evaluate_diagnostic_verdict(
        document_id=session.document_id,
        case=case,
        hypotheses=hypotheses,
        tests=tests,
        evaluated_at_utc=decided_at_utc,
    )
    surviving = [
        key for key, state in states.items()
        if state != 'contradicted'
    ]
    eliminated = [
        key for key, state in states.items()
        if state == 'contradicted'
    ]
    supported = [
        key for key in surviving
        if states[key] in ('test_supported', 'confirmed')
    ]
    inspection_only_keys = {
        entry.hypothesis_key
        for entry in entries if entry.inspection_only
    }
    reasons: list[str] = []
    manual_steps: list[str] = []

    if verdict_719.verdict == 'root_cause_confirmed_within_declared_scope':
        verdict: DiagnosticResolutionVerdict = 'root_cause_confirmed'
        reasons.append(
            '#719 verdict: root cause confirmed within declared scope')
    elif len(surviving) == 1 and surviving[0] in supported:
        only = surviving[0]
        if only in inspection_only_keys:
            verdict = 'needs_inspection'
            reasons.append(
                f'sole survivor {only} is inspection-only — machine '
                'evidence cannot settle it')
            manual_steps.append('qualified physical/inspection step')
        else:
            verdict = 'fault_isolated'
            reasons.append(
                f'{only} is the only surviving hypothesis with test '
                'support — fault located within the declared tree scope')
    elif not surviving:
        verdict = 'unresolved'
        reasons.append(
            'every hypothesis was contradicted — the symptom needs new '
            'candidates, not a best-of-bad pick (#719 §1)')
        manual_steps.append('re-open with an extended fault tree')
    elif no_more_tests:
        non_inspection = [
            k for k in surviving if k not in inspection_only_keys]
        if not non_inspection:
            verdict = 'needs_inspection'
            reasons.append(
                'all surviving hypotheses are inspection-only — machine '
                'discrimination is exhausted')
            manual_steps.append('qualified physical/inspection step')
        elif supported:
            verdict = 'needs_inspection'
            reasons.append(
                'surviving hypotheses cannot be separated by the '
                'tree\'s tests — observationally equivalent under the '
                'current campaign')
            manual_steps.append('qualified physical/inspection step')
        else:
            verdict = 'unresolved'
            reasons.append(
                'no discriminating evidence separates the survivors')
    else:
        verdict = 'needs_inspection'
        reasons.append(
            'session terminated with surviving hypotheses while tests '
            'remained — evidence is insufficient for a machine verdict')

    resolution = DiagnosticResolutionRecord.create(
        document_id=session.document_id,
        session_ref=session_binding(session),
        verdict=verdict,
        verdict_ref=diagnostic_verdict_binding(verdict_719),
        surviving_hypothesis_keys=tuple(surviving),
        eliminated_hypothesis_keys=tuple(eliminated),
        reasons=tuple(reasons),
        manual_next_steps=tuple(manual_steps),
        decided_at_utc=decided_at_utc,
    )
    return resolution, verdict_719


# ---------------------------------------------------------------------------
# Mechanism drivers — real engines only
# ---------------------------------------------------------------------------


class DiagnosticOrchestrationError(RuntimeError):
    """A diagnostic call that cannot produce honest evidence."""


class DiagnosticSafetyError(RuntimeError):
    """A plan was executed against its declared safety envelope."""


@dataclass(frozen=True)
class MechanismResult:
    """What one executed plan produced — facts + sealed evidence pins."""

    facts: tuple[tuple[str, str], ...]
    evidence_refs: tuple[AuthorityRef, ...]
    backend_id: str | None = None
    backend_is_simulated: bool = False
    notes: str = ''


def _run_sweep_acquisition(
    plan: DiagnosticTestPlan,
    *,
    backend: AudioIOBackend,
    routings: Mapping[str, Any],
    arming: Callable[[AcquisitionRequest], ArmConfirmation],
    sweep_repository: Any | None,
    document_id: str,
    clock: Callable[[], str],
) -> MechanismResult:
    """Drive the plan's channels through the #869 acquisition engine."""
    from .cad_sweep_acquisition import ChannelRouting

    channels = tuple(plan.config.get('channels', ()))
    if not channels:
        raise DiagnosticOrchestrationError(
            'sweep plan config names no channels')
    facts: dict[str, str] = {}
    refs: list[AuthorityRef] = []
    levels: list[float] = []
    polarities: list[int] = []
    detected_any = False
    verdict_seen: str | None = None
    engine_results: list[tuple[str, Any]] = []
    for channel in channels:
        routing = routings.get(channel)
        if routing is None or not isinstance(routing, ChannelRouting):
            raise DiagnosticOrchestrationError(
                f'no routing bound for diagnostic channel {channel}')
        request = AcquisitionRequest(
            stimulus=SweepStimulusSpec(
                sample_rate_hz=int(plan.config.get('sample_rate_hz', 48000)),
                start_frequency_hz=float(
                    plan.config.get('stimulus_start_hz', 20.0)),
                end_frequency_hz=float(
                    plan.config.get('stimulus_end_hz', 20000.0)),
                duration_s=float(plan.config.get('stimulus_duration_s', 0.5)),
                level_dbfs=float(
                    plan.level_cap_dbfs
                    if plan.level_cap_dbfs is not None else -12.0),
                repetitions=int(plan.config.get('repetitions', 2)),
            ),
            routing=routing,
            level_policy=LevelSafetyPolicy(
                max_output_level_dbfs=float(
                    plan.level_cap_dbfs
                    if plan.level_cap_dbfs is not None else -12.0)),
        )
        engine = MeasurementAcquisitionEngine(backend=backend, clock=clock)
        try:
            report = engine.configure(request)
            if not report.ok:
                engine_results.append((channel, engine))
                continue
            engine.arm(arming(request))
            outcome = engine.start()
        except Exception:  # error-boundary: per-channel lane — a crashing engine config/run lands the channel in engine_results with the failed engine, never aborts the sweep (noqa: BLE001)
            engine_results.append((channel, engine))
            continue
        engine_results.append((channel, engine))
        if engine.stage != 'completed' or outcome.quality is None:
            continue
        verdict_seen = outcome.quality.verdict
        if outcome.quality.verdict not in ('valid', 'limited'):
            continue
        ir_out = outcome.impulse_response
        if ir_out is None or ir_out.status != 'derived' or not ir_out.ir:
            continue
        import numpy as np

        ir = np.asarray(ir_out.ir, dtype=np.float64)
        idx = int(np.argmax(np.abs(ir)))
        if float(np.abs(ir[idx])) <= 0:
            continue
        detected_any = True
        measured = (outcome.quality.measured
                    if outcome.quality is not None else {}) or {}
        snr_db = measured.get('snr_db')
        floor_dbfs = measured.get('noise_floor_dbfs')
        if snr_db is not None and floor_dbfs is not None:
            levels.append(float(floor_dbfs + snr_db))
        polarities.append(1 if ir[idx] >= 0 else -1)
        if sweep_repository is not None:
            stimulus_def = build_stimulus_definition(
                document_id=document_id,
                stimulus=outcome.stimulus,
                created_at_utc=clock(),
            )
            sweep_repository.save_stimulus_definition(stimulus_def)
            sealed_run = sweep_repository.record_run(
                document_id=document_id,
                engine=engine,
                result=outcome,
                stimulus_ref=AuthorityRef(
                    kind='sweep_stimulus_definition',
                    ref_id=stimulus_def.stimulus_definition_id,
                    ref_sha256=stimulus_def.stimulus_sha256,
                ),
            )
            refs.append(AuthorityRef(
                kind='sweep_acquisition_run',
                ref_id=sealed_run.acquisition_id,
                ref_sha256=sealed_run.acquisition_sha256,
            ))

    facts['response_detected'] = 'true' if detected_any else 'false'
    if verdict_seen is not None:
        facts['quality_verdict'] = verdict_seen
    if levels:
        cap = plan.level_cap_dbfs if plan.level_cap_dbfs is not None else -12.0
        expectation = float(
            plan.config.get('expected_response_level_dbfs', cap - 30.0))
        best = max(levels)
        facts['level_dbfs'] = (
            'below_expectation' if best < expectation
            else 'within_expectation')
    if len(channels) > 1 and len(polarities) > 1:
        # The discriminator a Main+Sub compare can actually measure:
        # whether the per-channel impulse responses arrive with the
        # same sign (aligned) or opposite sign (opposed — a polarity
        # inversion/cancellation suspect).
        facts['combined_polarity'] = (
            'aligned' if len(set(polarities)) == 1 else 'opposed')
    bid = getattr(backend, 'backend_id', type(backend).__name__)
    simulated = bool(getattr(backend, 'is_simulated', bid == 'fake-audio-io'))
    return MechanismResult(
        facts=tuple(sorted(facts.items())),
        evidence_refs=tuple(refs),
        backend_id=bid,
        backend_is_simulated=simulated,
        notes=f'{len(engine_results)} channel sweep(s) executed',
    )


def _run_channel_verification(
    plan: DiagnosticTestPlan,
    *,
    verification_plan: ChannelVerificationPlan,
    backend: AudioIOBackend | Callable[[Any], AudioIOBackend],
    arming: Callable[[AcquisitionRequest], ArmConfirmation],
    channel_repository: Any | None,
    sweep_repository: Any | None,
    reference_channel: str,
    document_id: str,
    clock: Callable[[], str],
) -> MechanismResult:
    """Drive a #876 verification plan for the discriminated channel set."""
    verdict, results, _engines = run_verification_plan(
        plan=verification_plan,
        backend=backend,
        arming=arming,
        document_id=document_id,
        sweep_repository=sweep_repository,
        clock=clock,
    )
    refs: list[AuthorityRef] = [_cv_verdict_binding(verdict)]
    if channel_repository is not None:
        channel_repository.save_plan(verification_plan)
        for result in results.values():
            channel_repository.save_excitation_result(result)
        channel_repository.save_verdict(verdict)
    facts: dict[str, str] = {
        'map_state': verdict.map_state,
    }
    target_channels = tuple(plan.config.get('channels', ()))
    focus = target_channels[0] if target_channels else None
    states = {c.logical_channel: c for c in verdict.channels}
    if focus is not None and focus in states:
        facts['channel_routing_state'] = states[focus].routing_state
        facts['channel_polarity_state'] = states[focus].polarity_state
    elif states:
        first = next(iter(states.values()))
        facts['channel_routing_state'] = first.routing_state
    responded = any(
        r.capture_quality == 'passed' and r.response_detected
        for key, r in results.items() if key == reference_channel)
    if reference_channel in results:
        facts['reference_channel_responded'] = (
            'true' if responded else 'false')
    bid: str | None = None
    simulated = False
    for result in results.values():
        bid = result.backend_id
        simulated = simulated or result.backend_is_simulated
    return MechanismResult(
        facts=tuple(sorted(facts.items())),
        evidence_refs=tuple(refs),
        backend_id=bid,
        backend_is_simulated=simulated,
        notes=f'channel verification map_state={verdict.map_state}',
    )


def _run_device_readback(
    plan: DiagnosticTestPlan,
    *,
    adapter: CalibrationDeviceAdapter,
    binding: AdapterDeviceBinding,
    export: Any,
    session: DiagnosticSessionRecord,
    clock: Callable[[], str],
) -> MechanismResult:
    """Machine read-back of the bound device via the #806/#878 adapter.

    Runs BEFORE any manual/re-entry plan by tree construction; the
    observed state is diffed against the pinned export — never assumed.
    """
    observed = adapter.read_back(binding, observed_at_utc=clock())
    snapshot = build_observation(
        document_id=session.document_id,
        binding=binding,
        export=export,
        observed_channels=observed,
        observed_at_utc=clock(),
        source='read_back',
    )
    deviations = snapshot.deviations
    facts: dict[str, str] = {
        'deployed_config_match': 'true' if not deviations else 'false',
        'deviation_count': str(len(deviations)),
    }
    if session.expected_deployed_config_sha256 is not None:
        facts['expected_config_pin'] = (
            'pinned' if session.expected_deployed_config_sha256
            else 'missing')
    muted_channels = [
        channel.channel_id for channel in observed
        if channel.gain_db is not None and channel.gain_db <= -60.0
    ]
    facts['channel_muted_or_zero'] = 'true' if muted_channels else 'false'
    ref = AuthorityRef(
        kind='effective_settings_snapshot',
        ref_id=snapshot.snapshot_id,
        ref_sha256=snapshot.snapshot_sha256,
    )
    return MechanismResult(
        facts=tuple(sorted(facts.items())),
        evidence_refs=(ref,),
        notes=f'read-back {snapshot.snapshot_id}: '
              f'{len(deviations)} deviation(s)',
    )


def evaluate_observation_effects(
    plan: DiagnosticTestPlan,
    observation_facts: Mapping[str, str],
    entries: tuple[DiagnosticHypothesisEntry, ...],
) -> tuple[HypothesisEffectEntry, ...]:
    """Deterministic per-hypothesis effect of one observation.

    Applies the plan's predeclared discrimination rules verbatim — a
    fact value with no mapped effect contributes
    ``insufficient_evidence`` for every discriminated hypothesis, and
    hypotheses the plan does not discriminate are untouched.
    """
    by_key = {entry.hypothesis_key: entry for entry in entries}
    # A test that ran but produced nothing discriminating is honestly
    # 'insufficient_evidence', not 'not_tested' — the machine reported
    # and it discriminated nothing.
    effects: dict[str, HypothesisEffect] = {
        key: 'insufficient_evidence' for key in plan.discriminates
    }
    reasons: dict[str, list[str]] = {key: [] for key in plan.discriminates}
    rank_effect = {
        'contradicted': 0,
        'supported': 1,
        'indistinguishable': 2,
        'insufficient_evidence': 3,
        'not_tested': 4,
    }
    for rule in plan.discriminations:
        value = observation_facts.get(rule.observable, 'missing')
        mapped = rule.value_effects.get(value)
        if mapped is None:
            mapped = rule.value_effects.get('missing', {})
        for key in plan.discriminates:
            effect = mapped.get(key)
            if effect is None:
                continue
            reasons[key].append(
                f'{rule.observable}={value} → {effect}')
            current = effects[key]
            # 'contradicted' absorbs; otherwise the strongest claim wins.
            if current == 'contradicted':
                continue
            if effect == 'contradicted' or (
                    rank_effect[effect] < rank_effect[current]):
                effects[key] = effect
    out: list[HypothesisEffectEntry] = []
    for key in plan.discriminates:
        entry = by_key.get(key)
        if entry is None:
            continue
        reason = '; '.join(reasons[key]) or (
            'no rule matched — insufficient evidence')
        out.append(HypothesisEffectEntry(
            hypothesis_key=key,
            hypothesis_ref=hypothesis_entry_binding(entry),
            effect=effects[key],
            reason=reason,
        ))
    return tuple(out)


def update_binding(update: DiagnosticEvidenceUpdate) -> AuthorityRef:
    return AuthorityRef(
        kind='diagnostic_evidence_update',
        ref_id=update.update_id,
        ref_sha256=update.update_sha256,
    )


def resolution_binding(
    resolution: DiagnosticResolutionRecord,
) -> AuthorityRef:
    return AuthorityRef(
        kind='diagnostic_resolution',
        ref_id=resolution.resolution_id,
        ref_sha256=resolution.resolution_sha256,
    )


# ---------------------------------------------------------------------------
# The orchestrating service
# ---------------------------------------------------------------------------


_MECHANISM_TEST_KIND: dict[str, TestKind] = {
    'sweep_acquisition': 'discrimination_measurement',
    'channel_verification': 'discrimination_measurement',
    'device_readback': 'isolation_measurement',
    'operator_observation': 'other_declared',
}

_PERMITTED_EVENTS: dict[DiagnosticStage, tuple[str, ...]] = {
    'observation': ('observation_recorded', 'abort'),
    'hypotheses': ('hypotheses_registered', 'abort'),
    'test_planning': (
        'test_selected', 'resolution_decided', 'operator_authorized',
        'abort'),
    'measurement': ('test_executed', 'operator_authorized', 'abort'),
    'evidence_update': ('evidence_updated', 'abort'),
    'ranking': ('ranking_evaluated', 'resolution_decided', 'abort'),
    'resolution': ('resolution_decided',),
    'resolved': (),
    'unresolved': (),
    'needs_inspection': (),
    'aborted': (),
}


class DiagnosticOrchestrator:
    """The sealed-authority diagnostic loop (#885).

    ``open_session`` binds a symptom + fault tree into a session and
    registers the tree's hypotheses as #719 case hypotheses. The loop
    then alternates :meth:`plan_next_test` (deterministic selection,
    safety-gated) and :meth:`execute_plan` (real engine invocation →
    sealed observation → sealed evidence update → re-rank) until
    :meth:`resolve` terminates it through the #719 verdict ladder.

    Every decision is a sealed transition — a restarted app calls
    :meth:`session_state` and resumes exactly where the evidence left
    off. Nothing here fabricates observations: machine mechanisms only
    accept engine-produced records, and manual observations only
    arrive through ``operator_facts``.
    """

    def __init__(
        self,
        *,
        repository: Any,
        hypothesis_repository: Any,
        sweep_repository: Any | None = None,
        channel_repository: Any | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._repository = repository
        self._hypothesis_repository = hypothesis_repository
        self._sweep_repository = sweep_repository
        self._channel_repository = channel_repository
        self._clock = clock or _utc_now

    # -- emission ------------------------------------------------------

    def _emit(
        self,
        session: DiagnosticSessionRecord,
        event: DiagnosticEvent,
    ) -> DiagnosticStageTransition:
        transitions = tuple(
            self._repository.list_transitions(session.session_id))
        state = derive_session_state(session, transitions)
        decision = stage_transition(state, event)
        from_stage = (
            None if event.kind == 'session_created'
            else state.current_stage)
        if isinstance(decision, DiagnosticRejection):
            transition = DiagnosticStageTransition.create(
                document_id=session.document_id,
                session_ref=session_binding(session),
                seq=len(transitions),
                event_kind=event.kind,
                outcome='rejected',
                actor=event.actor,
                from_stage=from_stage,
                to_stage=from_stage,
                event_succeeded=False,
                reason=decision.reason,
                evidence_refs=event.evidence_refs,
                recorded_at_utc=event.at_utc,
            )
            self._repository.save_transition(transition)
            return transition
        transition = DiagnosticStageTransition.create(
            document_id=session.document_id,
            session_ref=session_binding(session),
            seq=len(transitions),
            event_kind=event.kind,
            outcome=decision.outcome,
            actor=event.actor,
            from_stage=from_stage,
            to_stage=decision.to_stage,
            event_succeeded=event.succeeded,
            reason=decision.reason,
            evidence_refs=event.evidence_refs,
            recorded_at_utc=event.at_utc,
        )
        self._repository.save_transition(transition)
        return transition

    # -- session lifecycle ----------------------------------------------

    def open_session(
        self,
        *,
        document_id: str,
        symptom_ref: AuthorityRef,
        symptom_summary: str,
        fault_tree_id: str,
        reference_channel: str = 'main',
        device_binding_id: str | None = None,
        device_binding_sha256: str | None = None,
        expected_deployed_config_sha256: str | None = None,
        max_stimulus_level_dbfs: float = -12.0,
        operator_id: str = 'system',
    ) -> tuple[
        DiagnosticSessionRecord,
        tuple[DiagnosticHypothesisEntry, ...],
        CadDiagnosticCase,
        tuple[CadDiagnosticHypothesis, ...],
    ]:
        """Bind a symptom + fault tree into a resumable session."""
        tree = FAULT_TREES.get(fault_tree_id)
        if tree is None:
            raise DiagnosticOrchestrationError(
                f'unknown fault tree {fault_tree_id} — sessions only '
                'run declared trees')
        case = build_diagnostic_case(
            document_id=document_id,
            symptom_ref=symptom_ref,
            symptom_summary=symptom_summary,
            opened_at_utc=self._clock(),
        )
        self._hypothesis_repository.save_case(case)
        session = DiagnosticSessionRecord.create(
            document_id=document_id,
            symptom_ref=symptom_ref,
            symptom_summary=symptom_summary,
            fault_tree_id=fault_tree_id,
            case_ref=diagnostic_case_binding(case),
            device_binding_id=device_binding_id,
            device_binding_sha256=device_binding_sha256,
            expected_deployed_config_sha256=(
                expected_deployed_config_sha256),
            reference_channel=reference_channel,
            max_stimulus_level_dbfs=max_stimulus_level_dbfs,
            opened_at_utc=self._clock(),
            opened_by=operator_id,
            authority_version=DIAGNOSTIC_ORCHESTRATOR_SCHEMA_VERSION,
        )
        self._repository.save_session(session)
        session_ref = session_binding(session)
        entries: list[DiagnosticHypothesisEntry] = []
        hypotheses719: list[CadDiagnosticHypothesis] = []
        for index, spec in enumerate(tree.hypotheses):
            hypothesis = build_diagnostic_hypothesis(
                document_id=document_id,
                case_ref=diagnostic_case_binding(case),
                cause_family=spec.cause_family,
                hypothesis_label=spec.label,
                expected_signatures=spec.expected_signatures,
                required_evidence=spec.required_evidence,
                known_confounders=spec.known_confounders,
                domain_scope=tree.scope_label,
                source_label=f'fault_tree:{fault_tree_id}',
                declared_at_utc=self._clock(),
            )
            self._hypothesis_repository.save_hypothesis(hypothesis)
            hypotheses719.append(hypothesis)
            available = tuple(
                template.template_id
                for template in tree.test_templates
                if spec.hypothesis_key in template.discriminates
            )
            entry = DiagnosticHypothesisEntry.create(
                document_id=document_id,
                session_ref=session_ref,
                hypothesis_key=spec.hypothesis_key,
                hypothesis_ref=diagnostic_hypothesis_binding(hypothesis),
                cause_family=spec.cause_family,
                label=spec.label,
                prerequisites=spec.prerequisites,
                expected_observations=tuple(
                    sig.signature_label
                    for sig in spec.expected_signatures),
                available_test_ids=available,
                assumptions=spec.assumptions,
                limitations=spec.limitations,
                inspection_only=spec.inspection_only,
                rank_seed=index,
                declared_at_utc=self._clock(),
            )
            self._repository.save_hypothesis_entry(entry)
            entries.append(entry)
        self._emit(session, DiagnosticEvent(
            kind='session_created',
            at_utc=self._clock(),
            reason=f'session opened for {fault_tree_id}',
            actor='system',
        ))
        self._emit(session, DiagnosticEvent(
            kind='observation_recorded',
            at_utc=self._clock(),
            reason=symptom_summary,
            actor='operator',
            evidence_refs=(symptom_ref,),
        ))
        self._emit(session, DiagnosticEvent(
            kind='hypotheses_registered',
            at_utc=self._clock(),
            reason=f'{len(entries)} hypotheses registered from '
                   f'{fault_tree_id}',
            evidence_refs=tuple(
                hypothesis_entry_binding(e) for e in entries),
        ))
        return session, tuple(entries), case, tuple(hypotheses719)

    def session_state(
        self,
        session: DiagnosticSessionRecord,
    ) -> DiagnosticSessionState:
        transitions = tuple(
            self._repository.list_transitions(session.session_id))
        return derive_session_state(session, transitions)

    def next_permitted_actions(
        self,
        session: DiagnosticSessionRecord,
    ) -> tuple[str, ...]:
        return _PERMITTED_EVENTS[self.session_state(session).current_stage]

    def _entries(
        self,
        session: DiagnosticSessionRecord,
    ) -> tuple[DiagnosticHypothesisEntry, ...]:
        return tuple(
            self._repository.list_hypothesis_entries(session.session_id))

    def _executed_template_ids(
        self,
        session: DiagnosticSessionRecord,
    ) -> frozenset[str]:
        executed_plan_ids = {
            obs.plan_ref.ref_id
            for obs in self._repository.list_observations(
                session.session_id)
        }
        return frozenset(
            plan.template_id
            for plan in self._repository.list_test_plans(
                session.session_id)
            if plan.plan_id in executed_plan_ids
        )

    # -- test planning ------------------------------------------------------

    def plan_next_test(
        self,
        session: DiagnosticSessionRecord,
    ) -> DiagnosticTestPlan | None:
        """Select + seal the next discriminating test.

        Deterministic: tree order, skipping executed templates and
        templates whose discriminated hypotheses are all contradicted;
        manual steps stay gated behind unexhausted automated lanes
        (read-back before manual re-entry). Safety is evaluated before
        the plan is sealed — ``prohibited`` plans are never created.
        Returns ``None`` when the tree is exhausted.
        """
        state = self.session_state(session)
        if state.terminal:
            raise DiagnosticOrchestrationError(
                f'session terminated at {state.terminated_stage}')
        if state.current_stage != 'test_planning':
            raise DiagnosticOrchestrationError(
                f'plan_next_test requires test_planning stage, got '
                f'{state.current_stage}')
        tree = FAULT_TREES[session.fault_tree_id]
        entries = self._entries(session)
        updates = tuple(
            self._repository.list_evidence_updates(session.session_id))
        states = compute_hypothesis_states(entries, updates)
        executed = self._executed_template_ids(session)
        template = select_next_template(tree, executed, states)
        if template is None:
            return None
        safety = evaluate_test_plan_safety(template, session)
        if safety.verdict in ('prohibited', 'requires_level_cap',
                              'requires_rollback_plan'):
            self._emit(session, DiagnosticEvent(
                kind='test_selected',
                at_utc=self._clock(),
                reason='; '.join(safety.reasons),
                succeeded=False,
            ))
            raise DiagnosticSafetyError(
                f'{template.template_id}: {safety.verdict} — '
                + '; '.join(safety.reasons))
        plan_seq = len(tuple(
            self._repository.list_test_plans(session.session_id)))
        level_cap = None
        if template.safety_class == 'acoustic':
            level_cap = min(
                template.max_level_dbfs
                if template.max_level_dbfs is not None
                else session.max_stimulus_level_dbfs,
                session.max_stimulus_level_dbfs)
        plan = DiagnosticTestPlan.create(
            document_id=session.document_id,
            session_ref=session_binding(session),
            template_id=template.template_id,
            plan_seq=plan_seq,
            mechanism=template.mechanism,
            safety_class=template.safety_class,
            test_label=template.test_label,
            action_label=template.action_label,
            discriminates=template.discriminates,
            discriminations=template.discriminations,
            config=dict(template.config),
            level_cap_dbfs=level_cap,
            authorization_class=(
                'operator_authorization'
                if template.safety_class == 'device_mutation'
                else 'none'),
            rollback_note=template.rollback_note,
            manual_gate=template.manual_requires_machine_exhaustion,
            predeclared_prediction=template.predeclared_prediction,
            created_at_utc=self._clock(),
        )
        self._repository.save_test_plan(plan)
        self._emit(session, DiagnosticEvent(
            kind='test_selected',
            at_utc=self._clock(),
            reason=f'{template.template_id}: '
                   + '; '.join(safety.reasons),
            evidence_refs=(plan_binding(plan),),
        ))
        return plan

    def authorize_plan(
        self,
        session: DiagnosticSessionRecord,
        plan: DiagnosticTestPlan,
        *,
        operator_id: str,
        expires_at_utc: str | None = None,
    ) -> DiagnosticOperatorAuthorization:
        """Issue the one-shot authorization a mutating plan needs."""
        if plan.safety_class != 'device_mutation':
            raise DiagnosticOrchestrationError(
                'only device_mutation plans take an authorization')
        authorization = DiagnosticOperatorAuthorization.create(
            document_id=session.document_id,
            session_ref=session_binding(session),
            plan_ref=plan_binding(plan),
            operator_id=operator_id,
            authorized_at_utc=self._clock(),
            expires_at_utc=expires_at_utc,
        )
        self._repository.save_authorization(authorization)
        self._emit(session, DiagnosticEvent(
            kind='operator_authorized',
            at_utc=self._clock(),
            reason=f'{operator_id} authorized {plan.template_id}',
            actor='operator',
            evidence_refs=(authorization_binding(authorization),),
        ))
        return authorization

    # -- execution ---------------------------------------------------------

    def execute_plan(
        self,
        session: DiagnosticSessionRecord,
        plan: DiagnosticTestPlan,
        *,
        backend: AudioIOBackend | Callable[[Any], AudioIOBackend] | None = None,
        routings: Mapping[str, Any] | None = None,
        arming: Callable[[AcquisitionRequest], ArmConfirmation] | None = None,
        adapter: CalibrationDeviceAdapter | None = None,
        binding: AdapterDeviceBinding | None = None,
        export: Any | None = None,
        verification_plan: ChannelVerificationPlan | None = None,
        operator_facts: Mapping[str, str] | None = None,
        authorization: DiagnosticOperatorAuthorization | None = None,
    ) -> tuple[DiagnosticObservationRecord, DiagnosticEvidenceUpdate]:
        """Run the selected plan through its real engine.

        Machine mechanisms only accept engine-produced evidence; the
        sealed run/verdict/snapshot records are pinned on the
        observation. Then the predeclared discrimination rules are
        applied verbatim and the evidence update + re-rank are sealed —
        the whole path is reproducible from the sealed log.
        """
        state = self.session_state(session)
        if state.terminal:
            raise DiagnosticOrchestrationError(
                f'session terminated at {state.terminated_stage}')
        if (state.current_stage != 'measurement'
                or plan.plan_id not in state.planned_template_ids):
            self._emit(session, DiagnosticEvent(
                kind='test_executed',
                at_utc=self._clock(),
                reason='plan is not the session\'s selected test',
                succeeded=False,
            ))
            raise DiagnosticOrchestrationError(
                f'plan {plan.plan_id} is not the selected test '
                f'(stage={state.current_stage})')
        if plan.safety_class == 'device_mutation':
            consumed_ids = {
                obs.authorization_ref.ref_id
                for obs in self._repository.list_observations(
                    session.session_id)
                if obs.authorization_ref is not None
            }
            if (authorization is None
                    or authorization.plan_ref != plan_binding(plan)
                    or authorization.authorization_id in consumed_ids
                    or (authorization.expires_at_utc is not None
                        and authorization.expires_at_utc
                        < self._clock())):
                self._emit(session, DiagnosticEvent(
                    kind='test_executed',
                    at_utc=self._clock(),
                    reason='device mutation requires an unused, '
                           'unexpired, plan-bound operator '
                           'authorization',
                    succeeded=False,
                ))
                raise DiagnosticSafetyError(
                    'device mutation blocked: no valid authorization')
        mechanism = plan.mechanism
        if mechanism == 'sweep_acquisition':
            if backend is None or routings is None or arming is None:
                raise DiagnosticOrchestrationError(
                    'sweep_acquisition requires backend, routings and '
                    'arming')
            result = _run_sweep_acquisition(
                plan,
                backend=backend,
                routings=routings,
                arming=arming,
                sweep_repository=self._sweep_repository,
                document_id=session.document_id,
                clock=self._clock,
            )
        elif mechanism == 'channel_verification':
            if (verification_plan is None or backend is None
                    or arming is None):
                raise DiagnosticOrchestrationError(
                    'channel_verification requires verification_plan, '
                    'backend and arming')
            result = _run_channel_verification(
                plan,
                verification_plan=verification_plan,
                backend=backend,
                arming=arming,
                channel_repository=self._channel_repository,
                sweep_repository=self._sweep_repository,
                reference_channel=session.reference_channel,
                document_id=session.document_id,
                clock=self._clock,
            )
        elif mechanism == 'device_readback':
            if adapter is None or binding is None or export is None:
                raise DiagnosticOrchestrationError(
                    'device_readback requires adapter, binding and '
                    'export')
            result = _run_device_readback(
                plan,
                adapter=adapter,
                binding=binding,
                export=export,
                session=session,
                clock=self._clock,
            )
        elif mechanism == 'operator_observation':
            if operator_facts is None:
                raise DiagnosticOrchestrationError(
                    'operator_observation requires operator_facts — '
                    'manual steps never fabricate machine evidence')
            result = MechanismResult(
                facts=tuple(sorted(
                    (str(k), str(v))
                    for k, v in operator_facts.items())),
                evidence_refs=(),
                notes='operator-entered observation',
            )
        else:  # pragma: no cover - literal exhaustion
            raise DiagnosticOrchestrationError(
                f'unknown mechanism {mechanism}')

        observation = DiagnosticObservationRecord.create(
            document_id=session.document_id,
            session_ref=session_binding(session),
            plan_ref=plan_binding(plan),
            mechanism=mechanism,
            facts=result.facts,
            evidence_refs=result.evidence_refs,
            backend_id=result.backend_id,
            backend_is_simulated=result.backend_is_simulated,
            authorization_ref=(
                authorization_binding(authorization)
                if authorization is not None else None),
            observed_at_utc=self._clock(),
            notes=result.notes,
        )
        self._repository.save_observation(observation)

        entries = self._entries(session)
        updates_prior = tuple(
            self._repository.list_evidence_updates(session.session_id))
        states_before = compute_hypothesis_states(entries, updates_prior)
        ranking_before = rank_hypotheses(entries, states_before)
        effects = evaluate_observation_effects(
            plan, dict(observation.facts), entries)
        states_after = dict(states_before)
        for effect in effects:
            key = effect.hypothesis_key
            current = states_after.get(key, 'candidate')
            if current == 'contradicted':
                continue
            if effect.effect == 'contradicted':
                states_after[key] = 'contradicted'
            elif effect.effect == 'supported':
                states_after[key] = 'test_supported'
            elif effect.effect == 'indistinguishable':
                states_after[key] = 'confounded'
        ranking_after = rank_hypotheses(entries, states_after)
        update = DiagnosticEvidenceUpdate.create(
            document_id=session.document_id,
            session_ref=session_binding(session),
            observation_ref=observation_binding(observation),
            effects=effects,
            ranking_before=ranking_before,
            ranking_after=ranking_after,
            evaluated_at_utc=self._clock(),
            evaluation_version=DIAGNOSTIC_ORCHESTRATOR_EVALUATION_VERSION,
        )
        self._repository.save_evidence_update(update)

        # Escalate into the #719 evidence layer — same effects, verbatim.
        self._record_diagnostic_test(
            session=session,
            plan=plan,
            observation=observation,
            effects=effects,
            entries=entries,
        )

        obs_ref = observation_binding(observation)
        upd_ref = update_binding(update)
        self._emit(session, DiagnosticEvent(
            kind='test_executed',
            at_utc=self._clock(),
            reason=f'{plan.template_id} executed via {mechanism}: '
                   f'{result.notes}',
            evidence_refs=(plan_binding(plan), obs_ref),
        ))
        self._emit(session, DiagnosticEvent(
            kind='evidence_updated',
            at_utc=self._clock(),
            reason=f'{len(effects)} hypothesis effect(s) applied',
            evidence_refs=(upd_ref,),
        ))
        self._emit(session, DiagnosticEvent(
            kind='ranking_evaluated',
            at_utc=self._clock(),
            reason='ranking: ' + ' > '.join(ranking_after),
            evidence_refs=(upd_ref,),
        ))
        return observation, update

    def _record_diagnostic_test(
        self,
        *,
        session: DiagnosticSessionRecord,
        plan: DiagnosticTestPlan,
        observation: DiagnosticObservationRecord,
        effects: tuple[HypothesisEffectEntry, ...],
        entries: tuple[DiagnosticHypothesisEntry, ...],
    ) -> CadDiagnosticTest:
        """Mirror the executed test into the #719 case — verbatim."""
        by_key = {e.hypothesis_key: e for e in entries}
        outcomes: list[CadHypothesisOutcome] = []
        for effect in effects:
            entry = by_key[effect.hypothesis_key]
            outcomes.append(CadHypothesisOutcome(
                hypothesis_ref=entry.hypothesis_ref,
                outcome=effect.effect,
                note=effect.reason,
            ))
        supported = [e for e in effects if e.effect == 'supported']
        contradicted = [e for e in effects if e.effect == 'contradicted']
        if supported:
            verdict = 'supported_but_confounded'
        elif contradicted and len(contradicted) == len(effects):
            verdict = 'contradicted'
        else:
            verdict = 'no_material_effect'
        result_ref: AuthorityRef | None = (
            observation.evidence_refs[0]
            if observation.evidence_refs
            else observation_binding(observation)
        )
        test = build_diagnostic_test(
            document_id=session.document_id,
            case_ref=session.case_ref,
            test_kind=_MECHANISM_TEST_KIND[plan.mechanism],
            test_label=plan.test_label,
            predeclared_prediction=plan.predeclared_prediction,
            result_evidence_ref=result_ref,
            outcomes=tuple(outcomes),
            verdict=verdict,
            executed_at_utc=observation.observed_at_utc,
            declared_at_utc=observation.observed_at_utc,
        )
        self._hypothesis_repository.save_test(test)
        return test

    # -- resolution ---------------------------------------------------------

    def resolve(
        self,
        session: DiagnosticSessionRecord,
        *,
        no_more_tests: bool | None = None,
    ) -> tuple[DiagnosticResolutionRecord, Any]:
        """Terminate the session through the #719 verdict ladder.

        Idempotent on a terminal session — returns the stored
        resolution. When ``no_more_tests`` is omitted the tree is
        re-checked for remaining discriminating templates.
        """
        state = self.session_state(session)
        if state.terminal:
            stored = tuple(
                self._repository.list_resolutions(session.session_id))
            if stored:
                verdict719 = self._hypothesis_repository.get_verdict(
                    stored[-1].verdict_ref.ref_id)
                return stored[-1], verdict719
            raise DiagnosticOrchestrationError(
                'terminal session without a stored resolution')
        tree = FAULT_TREES[session.fault_tree_id]
        entries = self._entries(session)
        updates = tuple(
            self._repository.list_evidence_updates(session.session_id))
        states = compute_hypothesis_states(entries, updates)
        if no_more_tests is None:
            executed = self._executed_template_ids(session)
            no_more_tests = (
                select_next_template(tree, executed, states) is None)
        case = self._hypothesis_repository.get_case(
            session.case_ref.ref_id)
        if case is None:
            raise DiagnosticOrchestrationError(
                'diagnostic case backing this session is missing')
        case_ref = session.case_ref
        hypotheses719 = tuple(
            item
            for item in self._hypothesis_repository.list_hypotheses(
                session.document_id)
            if item.case_ref == case_ref)
        tests719 = tuple(
            item
            for item in self._hypothesis_repository.list_tests(
                session.document_id)
            if item.case_ref == case_ref)
        resolution, verdict719 = derive_resolution(
            session=session,
            entries=entries,
            states=states,
            tests=tests719,
            hypotheses=hypotheses719,
            case=case,
            decided_at_utc=self._clock(),
            no_more_tests=no_more_tests,
        )
        self._hypothesis_repository.save_verdict(verdict719)
        self._repository.save_resolution(resolution)
        terminal_stage: DiagnosticStage = {
            'root_cause_confirmed': 'resolved',
            'fault_isolated': 'resolved',
            'unresolved': 'unresolved',
            'needs_inspection': 'needs_inspection',
        }[resolution.verdict]
        self._emit(session, DiagnosticEvent(
            kind='resolution_decided',
            at_utc=self._clock(),
            reason='; '.join(resolution.reasons),
            target_stage=terminal_stage,
            evidence_refs=(
                resolution_binding(resolution),
                resolution.verdict_ref,
            ),
        ))
        return resolution, verdict719

    def abort(
        self,
        session: DiagnosticSessionRecord,
        *,
        reason: str,
        actor: Literal['machine', 'operator', 'system'] = 'system',
    ) -> DiagnosticStageTransition:
        return self._emit(session, DiagnosticEvent(
            kind='abort',
            at_utc=self._clock(),
            reason=reason,
            actor=actor,
        ))


__all__ = [
    'DIAGNOSTIC_MECHANISM_LABELS',
    'DIAGNOSTIC_ORCHESTRATOR_EVALUATION_VERSION',
    'DIAGNOSTIC_ORCHESTRATOR_SCHEMA_VERSION',
    'DIAGNOSTIC_RESOLUTION_LABELS',
    'DIAGNOSTIC_STAGE_LABELS',
    'DIAGNOSTIC_STAGE_ORDER',
    'DIAGNOSTIC_TERMINAL_STAGES',
    'DiagnosticDecision',
    'DiagnosticEvent',
    'DiagnosticEvidenceUpdate',
    'DiagnosticFaultTree',
    'DiagnosticHypothesisEntry',
    'DiagnosticMechanism',
    'DiagnosticObservationRecord',
    'DiagnosticOperatorAuthorization',
    'DiagnosticOrchestrationError',
    'DiagnosticOrchestrator',
    'DiagnosticRejection',
    'DiagnosticResolutionRecord',
    'DiagnosticResolutionVerdict',
    'DiagnosticSafetyClass',
    'DiagnosticSafetyError',
    'DiagnosticSessionRecord',
    'DiagnosticSessionState',
    'DiagnosticStage',
    'DiagnosticStageTransition',
    'DiscriminationRule',
    'FAULT_TREES',
    'FaultTestTemplate',
    'FaultTreeHypothesis',
    'HypothesisEffect',
    'HypothesisEffectEntry',
    'HypothesisRankState',
    'MechanismResult',
    'SAFETY_FORBIDDEN_ACTION_TOKENS',
    'SafetyEvaluation',
    'SafetyVerdict',
    'authorization_binding',
    'compute_hypothesis_states',
    'derive_resolution',
    'derive_session_state',
    'evaluate_observation_effects',
    'evaluate_test_plan_safety',
    'hypothesis_entry_binding',
    'observation_binding',
    'plan_binding',
    'rank_hypotheses',
    'resolution_binding',
    'select_next_template',
    'session_binding',
    'stage_transition',
    'update_binding',
]
