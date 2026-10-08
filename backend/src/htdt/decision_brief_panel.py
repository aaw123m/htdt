"""Decision Brief panel for the optimization comparison page (#937).

Renders the newest sealed :class:`CadDecisionBrief` for the document —
top action, honest tiers, per-action gaps and the next step's
why/who/verify — and lets the operator compose a new brief from the
latest saved design-comparison set. Gate evidence is never inferred:
the compose path only carries pins it was explicitly given, so a
candidate without a closed evidence chain is listed ``not_ready`` with
its precise gaps instead of being promoted.

A brief pinned to a superseded scene revision is reported stale and its
conclusions are not re-presented as current.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .cad_decision_brief import (
    CadDecisionBrief,
    DecisionAction,
    DecisionGate,
    brief_freshness,
    build_decision_action,
    build_decision_brief,
)
from .cad_authority_resolver import AuthorityRef
from .cad_decision_brief_evidence import (
    DecisionBriefEvidenceResolver,
)
from .cad_decision_brief_repository import CadDecisionBriefRepository
from .cad_design_comparison import DesignComparisonSet
from .cad_design_comparison_repository import (
    CadDesignComparisonRepository,
)
from .cad_repository import SceneRepository
from .ui_theme import (
    SemanticState,
    SurfaceRole,
    TypographyRole,
    set_primary_action,
    set_semantic_state,
    set_surface_role,
    set_typography_role,
)
from .user_facing_error import operation_error_message


_TIER_LABELS = {
    'ready': '推奨可能',
    'conditional': '条件付き',
    'not_ready': '要検証の候補',
    'none': '候補なし',
}

_GAP_LABELS = {
    'evidence_missing': '証拠レコード未ピン',
    'evidence_stale': '証拠が最新でない',
    'verdict_failed': '検証レコード不合格',
    'not_comparable': '同一基準で比較不可',
}

_GATE_LABELS = {
    'solver_gate': 'ソルバー検証',
    'channel_verify': 'チャンネル検証',
    'deployment': 'デプロイ検証',
    'campaign': '測定キャンペーン',
    'production_gate': '本番適格ゲート',
    'comparison': '比較可能性',
}

_REC_KIND_LABELS = {
    'apply_candidate': '候補を適用',
    'remeasure': '再測定',
    'verify_channel': 'チャンネル確認',
    'deploy': 'デプロイ検証',
    'collect_evidence': '証拠の収集',
}

_ACTOR_LABELS = {
    'operator': 'オペレーター',
    'calibrator': '調整担当者',
    'system': 'システム',
}

_BASIS_LABELS = {
    'measured': '実測',
    'predicted': '予測',
    'derived': '導出',
    'unknown': '不明',
}


class DecisionBriefPanel(QFrame):
    """Comparison-page surface for the sealed next-action brief."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        document_id: str,
        *,
        brief_repository: CadDecisionBriefRepository | None = None,
        comparison_repository: CadDesignComparisonRepository | None = None,
        on_status: 'Callable[[str], None] | None' = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._scene_repository = scene_repository
        self._document_id = document_id
        self._evidence_resolver = DecisionBriefEvidenceResolver(
            scene_repository
        )
        self._brief_repository = brief_repository or (
            CadDecisionBriefRepository(
                scene_repository,
                kind_resolvers=self._evidence_resolver.kind_resolvers(),
            )
        )
        self._comparison_repository = (
            comparison_repository
            or CadDesignComparisonRepository(scene_repository)
        )
        self._on_status = on_status
        set_surface_role(self, SurfaceRole.RAISED)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)

        title = QLabel('決定ブリーフ')
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)
        description = QLabel(
            '最適化候補を「次の一手」に変換します。証拠チェーンが揃った候補'
            'だけが推奨可能になり、不足する証拠は候補ごとに明示されます。'
            '総合点や自動採用は作りません。'
        )
        description.setWordWrap(True)
        set_typography_role(description, TypographyRole.SECONDARY)
        layout.addWidget(description)

        self.freshness_label = QLabel()
        self.freshness_label.setWordWrap(True)
        layout.addWidget(self.freshness_label)

        self.headline_label = QLabel()
        self.headline_label.setWordWrap(True)
        layout.addWidget(self.headline_label)

        self.ranking_label = QLabel()
        self.ranking_label.setWordWrap(True)
        set_typography_role(self.ranking_label, TypographyRole.SECONDARY)
        layout.addWidget(self.ranking_label)

        self.actions_container = QWidget()
        self._actions_layout = QVBoxLayout(self.actions_container)
        self._actions_layout.setContentsMargins(0, 0, 0, 0)
        self._actions_layout.setSpacing(6)
        layout.addWidget(self.actions_container)

        controls = QHBoxLayout()
        self.rebuild_button = QPushButton('最新の比較セットから再計算')
        self.rebuild_button.setToolTip(
            '保存済みの最新比較セットから決定ブリーフを再計算して保存します。'
            'ゲート証拠は実際の検証レコードからスコープ一致で自動解決し、'
            '一致しない証拠は「証拠レコード未ピン」として候補を推奨しません。'
        )
        set_primary_action(self.rebuild_button)
        self.rebuild_button.clicked.connect(self._rebuild)
        controls.addWidget(self.rebuild_button)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        controls.addWidget(self.status_label, 1)
        layout.addLayout(controls)

        self.refresh()

    # -- rendering ---------------------------------------------------------

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)
        if self._on_status is not None:
            self._on_status(text)

    def _clear_actions(self) -> None:
        while self._actions_layout.count():
            item = self._actions_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _action_text(self, action: DecisionAction) -> str:
        lines = [
            f'{action.rank}. {action.label} — '
            f'{_TIER_LABELS.get(action.tier, action.tier)}',
        ]
        satisfied = action.satisfied_gate_count
        total = len(action.gates)
        lines.append(f'   証拠ゲート: {satisfied}/{total} 検証済み')
        for gap in action.gaps:
            gate = _GATE_LABELS.get(gap.gate, gap.gate)
            code = _GAP_LABELS.get(gap.code, gap.code)
            lines.append(f'   欠落: {gate}（{code}）— {gap.detail}')
        improved = [
            delta for delta in action.deltas
            if delta.direction == 'improved'
        ]
        regressed = [
            delta for delta in action.deltas
            if delta.direction == 'regressed'
        ]
        if action.deltas:
            basis = ', '.join(
                f'{delta.objective_id}={_BASIS_LABELS.get(delta.basis, delta.basis)}'
                for delta in action.deltas
            )
            lines.append(
                f'   指標寄与: 改善{len(improved)}件 / '
                f'後退{len(regressed)}件（{basis}）'
            )
        if action.cost.state == 'known':
            lines.append(
                f'   費用: {action.cost.amount} {action.cost.currency}'
                f'（出典: {action.cost.source}, {action.cost.quoted_on}）'
            )
        else:
            lines.append('   費用: 不明（出典なしの金額は表示しません）')
        rec = action.recommendation
        actor = _ACTOR_LABELS.get(rec.actor, rec.actor)
        kind = _REC_KIND_LABELS.get(rec.kind, rec.kind)
        lines.append(
            f'   次の一手: {kind} — {rec.why}（担当: {actor}、'
            f'確認方法: {rec.verify_by}）'
        )
        return '\n'.join(lines)

    def refresh(self) -> None:
        """Reload the newest persisted brief for this document."""

        try:  # error-boundary: repository read must never crash the page
            head = self._scene_repository.current_head(self._document_id)
            brief = self._brief_repository.latest_brief(self._document_id)
        except Exception as exc:  # noqa: BLE001
            self.freshness_label.setText('')
            self.headline_label.setText(
                f'決定ブリーフを読み込めません: '
                f'{operation_error_message(exc)}'
            )
            self.ranking_label.setText('')
            self._clear_actions()
            return
        if brief is None:
            self.freshness_label.setText('')
            self.headline_label.setText(
                '決定ブリーフはまだ生成されていません。'
            )
            self.ranking_label.setText(
                '比較セットを保存してから再計算してください。'
            )
            self._clear_actions()
            return
        self._render(brief, head_revision_id=(
            head.revision_id if head is not None else None
        ))

    def _render(
        self,
        brief: CadDecisionBrief,
        *,
        head_revision_id: str | None,
    ) -> None:
        if head_revision_id is not None:
            state, reason = brief_freshness(
                brief, current_scene_revision_id=head_revision_id
            )
        else:
            state, reason = 'stale', '現在のシーンリビジョンを確認できません'
        if state == 'stale':
            self.freshness_label.setText(
                f'この決定ブリーフは最新のシーンに対応しません（{reason}）。'
                '結論は現在の候補として再表示されません。'
            )
            set_semantic_state(self.freshness_label, SemanticState.WARNING)
        else:
            self.freshness_label.setText('')
        self.headline_label.setText(
            f'上位の次の一手: {_TIER_LABELS.get(brief.top_tier, brief.top_tier)}'
            f'（{brief.ready_count}/{brief.action_count} 件が推奨可能）'
        )
        self.ranking_label.setText(brief.ranking_explanation)
        self._clear_actions()
        for action in brief.actions:
            row = QLabel(self._action_text(action))
            row.setWordWrap(True)
            row.setTextInteractionFlags(
                row.textInteractionFlags()
            )
            self._actions_layout.addWidget(row)

    # -- compose ------------------------------------------------------------

    def _latest_comparison_set(self) -> DesignComparisonSet | None:
        sets = self._comparison_repository.latest_sets(self._document_id)
        return sets[-1] if sets else None

    def recompute_brief(self) -> CadDecisionBrief | None:
        """Public compose+save used by the #964 revalidation runner —
        returns the persisted brief (or None when it cannot compose)."""
        return self._rebuild()

    def _rebuild(self) -> CadDecisionBrief | None:
        """Compose + persist a brief from the newest comparison set.

        The baseline is the alternative pinned to the live scene head;
        gate evidence is declared-pins only — candidates without pinned
        verdicts land as ``not_ready`` with the precise missing gates.
        """

        try:  # error-boundary: compose must report, not crash the page
            head = self._scene_repository.current_head(self._document_id)
            if head is None:
                raise ValueError('現在のシーンリビジョンを解決できません')
            comparison_set = self._latest_comparison_set()
            if comparison_set is None:
                raise ValueError('保存済みの比較セットがありません')
            baseline = None
            for alternative in comparison_set.alternatives:
                if alternative.scene_revision_id == head.revision_id:
                    baseline = alternative
                    break
            if baseline is None:
                raise ValueError(
                    '比較セットに現在のシーンを基準とする代替がありません'
                )
            actions = []
            for alternative in comparison_set.alternatives:
                if alternative.alternative_id == baseline.alternative_id:
                    continue
                comparability = (
                    'comparable'
                    if alternative.scene_revision_id == baseline.scene_revision_id
                    else 'incompatible_fidelity'
                )
                gates = self._evidence_resolver.resolve_gates(
                    self._document_id, alternative
                )
                actions.append(
                    build_decision_action(
                        candidate_ref=AuthorityRef(
                            kind='comparison_alternative',
                            ref_id=alternative.alternative_id,
                            ref_sha256=alternative.alternative_sha256,
                        ),
                        label=alternative.label,
                        gates=gates,
                        comparability=comparability,
                        changes=(),
                        deltas=(),
                        recommendation=_gate_recommendation(
                            alternative.label, gates
                        ),
                    )
                )
            brief = build_decision_brief(
                document_id=self._document_id,
                scene_revision_id=head.revision_id,
                scene_content_hash=head.content_hash,
                baseline_ref=AuthorityRef(
                    kind='comparison_alternative',
                    ref_id=baseline.alternative_id,
                    ref_sha256=baseline.alternative_sha256,
                ),
                baseline_label=baseline.label,
                actions=tuple(actions),
                comparison_ref=AuthorityRef(
                    kind='design_comparison_set',
                    ref_id=comparison_set.set_id,
                    ref_sha256=comparison_set.set_sha256,
                ),
                provenance=('decision_brief_panel',),
                created_at_utc=datetime.now(timezone.utc).isoformat(),
            )
            self._brief_repository.save_brief(brief)
        except Exception as exc:  # noqa: BLE001
            self._set_status(
                f'決定ブリーフを作成できません: '
                f'{operation_error_message(exc)}'
            )
            return None
        self._set_status('決定ブリーフを保存しました')
        self.refresh()
        return brief


def _gate_recommendation(
    label: str, gates: 'tuple[DecisionGate, ...]'
) -> 'DecisionRecommendation':
    """Next-step recommendation derived from the resolved gate states.

    ``apply_candidate`` only when every real gate is verified+current;
    otherwise a ``collect_evidence`` step naming the unsatisfied gates,
    who must act, and how the gap is closed.
    """

    from .cad_decision_brief import DecisionRecommendation

    if all(gate.gate_state() == 'satisfied' for gate in gates):
        return DecisionRecommendation(
            kind='apply_candidate',
            why=(
                f'{label} は5つの証拠ゲートがすべて検証済みで、'
                '推奨可能です'
            ),
            actor='operator',
            verify_by=(
                '適用後に実機の実効設定を再読み出しし、'
                'チャンネル検証と測定キャンペーンで確認してください'
            ),
        )
    missing = ', '.join(
        _GATE_LABELS.get(gate.gate, gate.gate)
        for gate in gates
        if gate.gate_state() != 'satisfied'
    )
    return DecisionRecommendation(
        kind='collect_evidence',
        why=(
            f'{label} は {missing} の証拠が不足しており'
            '採否を判断できません'
        ),
        actor='operator',
        verify_by='未充足ゲートの検証レコードをこの候補のスコープで'
        '収集し、再計算してください',
    )


__all__ = ['DecisionBriefPanel']
