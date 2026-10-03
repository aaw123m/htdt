"""Reflection-guidance panel for the Room acoustics dock.

Gives ``htdt.cad_reflection_guidance`` a production surface: the ranked
``ReflectionGuidanceReport`` items derived from persisted deterministic
path artifacts (R150) are listed with JA labels — treat-zone, reposition,
verify and disambiguate actions ordered by rank, each bound to the exact
path/snapshot/request authority it was derived from.

The panel also wires the interactive scrub session
(``open_guidance_session``/``scrub_source``): the selected 診断ペア
supplies the pinned request/path pair, the snapshot payload supplies the
receiver position and the committed source reference point, and the
specular plane is the one the proven departure/arrival directions imply.
The user edits a candidate source position and each スクラブ実行 appends
one exact image-method preview frame — a proposal over committed
authority that never mutates the scene. Entries come from
:func:`reflection_guidance_presentation.load_reflection_guidance_view`
— nothing is synthesized: without persisted path artifacts the panel
shows the honest empty state, rows that fail replay are listed as
issues, and pairs whose snapshot geometry cannot be resolved disable the
scrub lane with its blocker reason rather than guessing.
"""

from __future__ import annotations

from contextlib import closing

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .accessible_labels import wire_label_buddies
from .cad_display_labels import revision_display_label, saved_label
from .cad_reflection_guidance import (
    ReflectionGuidanceSession,
    scrub_source,
)
from .cad_scene import Position3
from .cad_schema import connect_sqlite, ensure_native_schema
from .reflection_guidance_presentation import (
    ReflectionGuidanceEntry,
    ReflectionGuidanceIssue,
    ReflectionGuidanceView,
    load_reflection_guidance_view,
)
from .ui_theme import (
    SemanticState,
    TypographyRole,
    set_semantic_state,
    set_typography_role,
)


_KIND_LABELS = {
    'treat_reflection_zone': '一次反射ゾーンの処理',
    'reposition_source': '音源の再配置',
    'verify_with_measurement': '測定による検証',
    'resolve_ambiguity': '曖昧性の解消',
}

_CONFIDENCE_LABELS = {
    'authority_backed': '権威裏付けあり',
    'measured_supported': '測定裏付けあり',
    'unverified_hypothesis': '未検証の仮説',
}

_STATUS_LABELS = {
    'actionable': '対応可能',
    'diagnostics_only': '診断のみ',
    'blocked': 'ブロック',
}

_ENVIRONMENT_LABELS = {
    'bound': '環境権威に束縛',
    'absent': '既定値',
    'unreadable': '既定値（環境権威を読み取れません）',
}

_ISSUE_LABELS = {
    'unreadable_artifact': '読み取り不可のパス成果物',
    'binding_mismatch': 'スナップショット束縛不一致',
    'derivation_failed': 'ガイダンス導出失敗',
}

_SCRUB_DIRECTION_LABELS = {
    'improves': '改善（過剰遅延が増加）',
    'worsens': '悪化（過剰遅延が減少）',
    'unchanged': '変化なし',
}

_SCRUB_BLOCKER_LABELS = {
    'snapshot_payload_unreadable': (
        'スナップショットの幾何権威を読み取れないため、'
        'スクラブは利用できません。'
    ),
    'receiver_binding_missing': (
        '受音点のスナップショット束縛がないため、スクラブは利用できません。'
    ),
    'receiver_binding_unreadable': (
        '受音点束縛を検証できないため、スクラブは利用できません。'
    ),
    'source_binding_missing': (
        '音源の参照点束縛がないため、スクラブは利用できません。'
    ),
    'source_binding_unreadable': (
        '音源束縛を検証できないため、スクラブは利用できません。'
    ),
    'plane_derivation_failed': (
        'パス権威から反射面を導出できないため、スクラブは利用できません。'
    ),
}

_ALL_REVISIONS = '__all_revisions__'


class ReflectionGuidancePanel(QWidget):
    """Dock tab listing ranked reflection-guidance items for the scene.

    ``room_controller`` supplies the document id, the CAD database path
    and the committed scene used to resolve entity display names.
    """

    def __init__(self, room_controller, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName('reflectionGuidancePanel')
        self._controller = room_controller
        self._view = ReflectionGuidanceView(
            document_id=room_controller.document_id, entries=(), issues=()
        )
        self._entries: list[ReflectionGuidanceEntry] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        title = QLabel('反射ガイダンス')
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)

        subtitle = QLabel(
            '確定的パス権威（R150）が実証した一次反射に基づく推奨です。'
            '簡易矩形予測の候補は対象外です。'
        )
        subtitle.setObjectName('guidanceSubtitle')
        subtitle.setWordWrap(True)
        set_typography_role(subtitle, TypographyRole.SECONDARY)
        layout.addWidget(subtitle)

        revision_row = QFormLayout()
        revision_row.setContentsMargins(0, 0, 0, 0)
        self.revision_combo = QComboBox()
        self.revision_combo.setObjectName('guidanceRevisionCombo')
        self.revision_combo.setToolTip(
            'ガイダンスを絞り込むシーンリビジョン'
        )
        revision_row.addRow('対象リビジョン:', self.revision_combo)
        layout.addLayout(revision_row)

        self.summary_label = QLabel('')
        self.summary_label.setObjectName('guidanceSummary')
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.issue_label = QLabel('')
        self.issue_label.setObjectName('guidanceIssueLabel')
        self.issue_label.setWordWrap(True)
        set_typography_role(self.issue_label, TypographyRole.SECONDARY)
        layout.addWidget(self.issue_label)

        entry_row = QFormLayout()
        entry_row.setContentsMargins(0, 0, 0, 0)
        self.entry_combo = QComboBox()
        self.entry_combo.setObjectName('guidanceEntryCombo')
        self.entry_combo.setToolTip(
            'ガイダンスを表示する音源→受音点・反射面の組み合わせ'
        )
        entry_row.addRow('診断ペア:', self.entry_combo)
        layout.addLayout(entry_row)

        self.item_list = QListWidget()
        self.item_list.setObjectName('guidanceItemList')
        self.item_list.setAccessibleName('ガイダンス項目')
        self.item_list.setToolTip(
            '順位付きのガイダンス項目 — 選択すると下に根拠と権威参照を表示します'
        )
        self.item_list.setMaximumHeight(160)
        layout.addWidget(self.item_list)

        form = QWidget()
        form_layout = QFormLayout(form)
        form_layout.setContentsMargins(0, 0, 0, 0)
        self.status_label = QLabel('-')
        self.status_label.setWordWrap(True)
        self.confidence_label = QLabel('-')
        self.confidence_label.setWordWrap(True)
        self.surface_label = QLabel('-')
        self.surface_label.setWordWrap(True)
        self.excess_delay_label = QLabel('-')
        self.excess_delay_label.setWordWrap(True)
        self.speed_label = QLabel('-')
        self.speed_label.setWordWrap(True)
        self.rationale_label = QLabel('-')
        self.rationale_label.setWordWrap(True)
        self.action_label = QLabel('-')
        self.action_label.setWordWrap(True)
        self.authority_label = QLabel('-')
        self.authority_label.setWordWrap(True)
        self.authority_label.setObjectName('guidanceAuthorityLabel')
        self.authority_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        form_layout.addRow('レポート状態:', self.status_label)
        form_layout.addRow('信頼度:', self.confidence_label)
        form_layout.addRow('対象面:', self.surface_label)
        form_layout.addRow('過剰遅延:', self.excess_delay_label)
        form_layout.addRow('音速:', self.speed_label)
        form_layout.addRow('根拠:', self.rationale_label)
        form_layout.addRow('推奨アクション:', self.action_label)
        form_layout.addRow('権威参照:', self.authority_label)
        layout.addWidget(form)

        scrub_title = QLabel('音源スクラブ（プレビュー）')
        set_typography_role(scrub_title, TypographyRole.SECTION_TITLE)
        layout.addWidget(scrub_title)

        scrub_hint = QLabel(
            '診断ペアの確定権威に固定された反射面で、候補の音源位置が'
            '過剰遅延と処理ゾーン（新しい反射点）に与える変化を試算します。'
            'コミット済みレポートとシーン権威は変更されません。'
        )
        scrub_hint.setObjectName('guidanceScrubHint')
        scrub_hint.setWordWrap(True)
        set_typography_role(scrub_hint, TypographyRole.SECONDARY)
        layout.addWidget(scrub_hint)

        self._scrub_session: ReflectionGuidanceSession | None = None

        candidate_row = QHBoxLayout()
        candidate_row.setContentsMargins(0, 0, 0, 0)
        self.scrub_x = self._position_spin('候補音源位置 X')
        self.scrub_y = self._position_spin('候補音源位置 Y')
        self.scrub_z = self._position_spin('候補音源位置 Z')
        for axis, spin in (
            ('X', self.scrub_x),
            ('Y', self.scrub_y),
            ('Z', self.scrub_z),
        ):
            axis_label = QLabel(f'{axis}:')
            candidate_row.addWidget(axis_label)
            candidate_row.addWidget(spin)
        layout.addLayout(candidate_row)

        scrub_buttons = QHBoxLayout()
        scrub_buttons.setContentsMargins(0, 0, 0, 0)
        self.scrub_reset_button = QPushButton('確定位置に戻す')
        self.scrub_reset_button.setObjectName('guidanceScrubReset')
        self.scrub_reset_button.setToolTip(
            '候補位置をスナップショット確定の音源参照点に戻します'
        )
        self.scrub_button = QPushButton('スクラブ実行')
        self.scrub_button.setObjectName('guidanceScrubRun')
        self.scrub_button.setToolTip(
            '候補位置で鏡像法プレビューを1フレーム計算します'
        )
        scrub_buttons.addWidget(self.scrub_reset_button)
        scrub_buttons.addWidget(self.scrub_button)
        layout.addLayout(scrub_buttons)

        self.scrub_result_label = QLabel('')
        self.scrub_result_label.setObjectName('guidanceScrubResult')
        self.scrub_result_label.setWordWrap(True)
        self.scrub_result_label.setToolTip(
            'スクラブフレームの試算結果 — 方向・過剰遅延・新しい反射点'
        )
        layout.addWidget(self.scrub_result_label)

        self.empty_label = QLabel('')
        self.empty_label.setObjectName('guidanceEmptyLabel')
        self.empty_label.setWordWrap(True)
        set_typography_role(self.empty_label, TypographyRole.SECONDARY)
        layout.addWidget(self.empty_label)
        layout.addStretch(1)

        self.revision_combo.currentIndexChanged.connect(
            self._refresh_entries
        )
        self.entry_combo.currentIndexChanged.connect(self._refresh_items)
        self.item_list.itemSelectionChanged.connect(self._refresh_detail)
        self.scrub_button.clicked.connect(self._run_scrub)
        self.scrub_reset_button.clicked.connect(
            self._reset_scrub_position
        )

        self.refresh()
        wire_label_buddies(self)

    # --- data ---------------------------------------------------------------

    def refresh(self) -> None:
        """Reload the persisted-artifact projection from the CAD store."""
        path = self._controller.repository.path
        try:
            ensure_native_schema(path)
            with closing(connect_sqlite(path)) as connection:
                self._view = load_reflection_guidance_view(
                    connection, self._controller.document_id
                )
        except Exception as exc:  # noqa: BLE001 — surface honest failure
            self.summary_label.setText(
                f'ガイダンスの読み込みに失敗しました: {exc}'
            )
            set_semantic_state(self.summary_label, SemanticState.WARNING)
            self._view = ReflectionGuidanceView(
                document_id=self._controller.document_id,
                entries=(),
                issues=(),
            )
        self._refresh_revision_combo()
        self._refresh_entries()

    def _entity_label(self, entity_id: str) -> str:
        try:
            entity = self._controller.committed_document.entity(entity_id)
        except KeyError:
            return entity_id
        return entity.name or entity_id

    def _refresh_revision_combo(self) -> None:
        revisions = {
            revision.revision_id: revision
            for revision in self._controller.repository.list_revisions(
                self._controller.document_id
            )
        }
        try:
            labels = self._controller.repository.revision_labels(
                self._controller.document_id
            )
        except Exception:  # noqa: BLE001 — labels are display-only
            labels = None
        current = self.revision_combo.currentData()
        self.revision_combo.blockSignals(True)
        self.revision_combo.clear()
        self.revision_combo.addItem('すべてのリビジョン', _ALL_REVISIONS)
        for revision_id in self._view.scene_revision_ids:
            revision = revisions.get(revision_id)
            label = (
                revision_display_label(revision, labels)
                if revision is not None
                else revision_id
            )
            self.revision_combo.addItem(label, revision_id)
        index = self.revision_combo.findData(current)
        self.revision_combo.setCurrentIndex(index if index >= 0 else 0)
        self.revision_combo.blockSignals(False)

    # --- widgets ------------------------------------------------------------

    def _visible_entries(self) -> list[ReflectionGuidanceEntry]:
        selection = self.revision_combo.currentData()
        if selection in (None, _ALL_REVISIONS):
            return list(self._view.entries)
        return list(self._view.for_revision(selection))

    def _refresh_entries(self, _index: int = 0) -> None:
        self._entries = self._visible_entries()
        current = self.entry_combo.currentData()
        self.entry_combo.blockSignals(True)
        self.entry_combo.clear()
        for index, entry in enumerate(self._entries):
            source = self._entity_label(entry.source_entity_id)
            receiver = self._entity_label(entry.receiver_entity_id)
            label = f'{source} → {receiver} · {entry.surface_id}'
            if entry.recorded_at_utc:
                label += f' · {saved_label(entry.recorded_at_utc)}'
            self.entry_combo.addItem(label, index)
        index = self.entry_combo.findData(current)
        self.entry_combo.setCurrentIndex(index if index >= 0 else 0)
        self.entry_combo.blockSignals(False)

        issues = self._view.issues
        item_total = sum(len(entry.report.items) for entry in self._entries)
        self.summary_label.setText(
            f'ガイダンス {item_total} 件（{len(self._entries)} 組）'
            + (f' · 読み飛ばし {len(issues)} 行' if issues else '')
        )
        set_semantic_state(self.summary_label, None)
        if issues:
            lines = []
            for issue in issues:
                lines.append(
                    f'{_ISSUE_LABELS.get(issue.kind, issue.kind)}'
                    f'（{issue.artifact_id or "—"}）'
                )
            self.issue_label.setText('\n'.join(lines))
            set_semantic_state(self.issue_label, SemanticState.WARNING)
        else:
            self.issue_label.setText('')
            set_semantic_state(self.issue_label, None)
        self.issue_label.setVisible(bool(issues))

        if not self._entries:
            self.empty_label.setText(
                '確定的パス成果物がまだありません。'
                'ガイダンスは R150 確定的パス権威が生成された後に表示されます。'
            )
        else:
            self.empty_label.setText('')
        self.empty_label.setVisible(not self._entries)
        has_entries = bool(self._entries)
        self.entry_combo.setEnabled(has_entries)
        self._refresh_items()

    def _current_entry(self) -> ReflectionGuidanceEntry | None:
        index = self.entry_combo.currentData()
        if index is None or not (0 <= index < len(self._entries)):
            return None
        return self._entries[index]

    def _refresh_items(self, _index: int = 0) -> None:
        entry = self._current_entry()
        self.item_list.clear()
        if entry is not None:
            for position, item in enumerate(entry.report.items):
                kind = _KIND_LABELS.get(item.kind, item.kind)
                confidence = _CONFIDENCE_LABELS.get(
                    item.confidence, item.confidence
                )
                QListWidgetItem(
                    f'{item.rank}. {kind} — {confidence}', self.item_list
                ).setData(Qt.ItemDataRole.UserRole, position)
        self.item_list.setEnabled(
            entry is not None and bool(entry.report.items)
        )
        if entry is not None and entry.report.items:
            self.item_list.setCurrentRow(0)
        self._refresh_detail()
        self._open_scrub(entry)

    def _refresh_detail(self) -> None:
        entry = self._current_entry()
        selected = self.item_list.currentItem()
        item = None
        if entry is not None and selected is not None:
            position = selected.data(Qt.ItemDataRole.UserRole)
            if isinstance(position, int) and position < len(
                entry.report.items
            ):
                item = entry.report.items[position]
        if entry is None:
            self.status_label.setText('-')
            self.confidence_label.setText('-')
            self.surface_label.setText('-')
            self.excess_delay_label.setText('-')
            self.speed_label.setText('-')
            self.rationale_label.setText('-')
            self.action_label.setText('-')
            self.authority_label.setText('-')
            return

        report = entry.report
        status = _STATUS_LABELS.get(report.status, report.status)
        self.status_label.setText(f'{status} · 項目 {len(report.items)} 件')
        if item is None:
            self.confidence_label.setText('-')
        else:
            confidence = _CONFIDENCE_LABELS.get(
                item.confidence, item.confidence
            )
            self.confidence_label.setText(confidence)
            if item.confidence == 'unverified_hypothesis':
                set_semantic_state(
                    self.confidence_label, SemanticState.WARNING
                )
            else:
                set_semantic_state(self.confidence_label, None)
        self.surface_label.setText(entry.surface_id)
        self.excess_delay_label.setText(
            f'{entry.excess_delay_s * 1e3:.2f} ms'
        )
        environment = _ENVIRONMENT_LABELS.get(
            entry.environment_state, entry.environment_state
        )
        self.speed_label.setText(
            f'{entry.speed_of_sound_m_s:g} m/s · {environment}'
        )
        if item is None:
            self.rationale_label.setText('-')
            self.action_label.setText('-')
        else:
            self.rationale_label.setText(item.rationale)
            self.action_label.setText(item.action)
        authority_lines = [f'成果物: {entry.artifact_id}']
        if item is not None and item.path_id is not None:
            authority_lines.append(f'パス: {item.path_id}')
        if item is not None and item.hypothesis_id is not None:
            authority_lines.append(f'仮説: {item.hypothesis_id}')
        authority_lines.append(
            f'リクエスト: {report.request_semantic_sha256[:16]}…'
        )
        self.authority_label.setText('\n'.join(authority_lines))

    # --- scrub session --------------------------------------------------

    @staticmethod
    def _position_spin(accessible_name: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(-200.0, 200.0)
        spin.setDecimals(3)
        spin.setSingleStep(0.1)
        spin.setSuffix(' m')
        spin.setAccessibleName(accessible_name)
        spin.setToolTip(
            '候補音源位置（世界座標 m）— 確定権威は変更しません'
        )
        return spin

    def _set_scrub_position(self, point: Position3) -> None:
        self.scrub_x.setValue(point.x_m)
        self.scrub_y.setValue(point.y_m)
        self.scrub_z.setValue(point.z_m)

    def _open_scrub(
        self, entry: ReflectionGuidanceEntry | None
    ) -> None:
        """(Re)open the scrub session for the selected diagnostic pair.

        The session is pinned to the entry's committed request/path
        authority; the spins default to the snapshot's source reference
        point so the first candidate is the proven baseline.
        """
        self._scrub_session = None
        if (
            entry is not None
            and entry.scrub_unavailable_reason is None
        ):
            self._scrub_session = entry.open_scrub_session()
        ready = self._scrub_session is not None
        for widget in (
            self.scrub_x,
            self.scrub_y,
            self.scrub_z,
            self.scrub_button,
            self.scrub_reset_button,
        ):
            widget.setEnabled(ready)
        if ready and entry is not None:
            if entry.source_reference_point is not None:
                self._set_scrub_position(entry.source_reference_point)
            baseline_ms = (
                self._scrub_session.baseline_excess_delay_s * 1e3
            )
            self.scrub_result_label.setText(
                f'基準の過剰遅延: {baseline_ms:.2f} ms — '
                '候補位置を調整して「スクラブ実行」を押してください。'
            )
            set_semantic_state(self.scrub_result_label, None)
        elif entry is None:
            self.scrub_result_label.setText(
                '診断ペアを選択してください。'
            )
            set_semantic_state(self.scrub_result_label, None)
        else:
            reason = entry.scrub_unavailable_reason
            self.scrub_result_label.setText(
                _SCRUB_BLOCKER_LABELS.get(reason, reason or '')
            )
            set_semantic_state(
                self.scrub_result_label, SemanticState.WARNING
            )

    def _reset_scrub_position(self) -> None:
        entry = self._current_entry()
        if entry is None or entry.source_reference_point is None:
            return
        self._set_scrub_position(entry.source_reference_point)

    def _run_scrub(self) -> None:
        if self._scrub_session is None:
            return
        candidate = Position3(
            x_m=self.scrub_x.value(),
            y_m=self.scrub_y.value(),
            z_m=self.scrub_z.value(),
        )
        try:
            self._scrub_session = scrub_source(
                self._scrub_session, candidate
            )
        except ValueError as exc:
            self.scrub_result_label.setText(
                f'スクラブを計算できませんでした: {exc}'
            )
            set_semantic_state(
                self.scrub_result_label, SemanticState.WARNING
            )
            return
        frame = self._scrub_session.frames[-1]
        preview = frame.preview
        direction = _SCRUB_DIRECTION_LABELS[frame.direction]
        point = preview.reflection_point
        self.scrub_result_label.setText(
            '\n'.join(
                [
                    f'フレーム {frame.frame_index + 1}: {direction}',
                    (
                        f'過剰遅延 {preview.excess_delay_s * 1e3:.2f} ms '
                        f'（基準比 {frame.excess_delay_delta_s * 1e3:+.2f} ms）'
                    ),
                    (
                        '新しい反射点（処理ゾーンの中心候補）: '
                        f'({point.x_m:.2f}, {point.y_m:.2f}, '
                        f'{point.z_m:.2f}) m'
                    ),
                    (
                        f'パス長 — 直接 {preview.direct_length_m:.2f} m · '
                        f'一次反射 {preview.specular_length_m:.2f} m'
                    ),
                    f'フレーム累計: {len(self._scrub_session.frames)}',
                ]
            )
        )
        set_semantic_state(
            self.scrub_result_label,
            (
                SemanticState.SUCCESS
                if frame.direction == 'improves'
                else (
                    SemanticState.WARNING
                    if frame.direction == 'worsens'
                    else None
                )
            ),
        )


__all__ = [
    'ReflectionGuidancePanel',
]
