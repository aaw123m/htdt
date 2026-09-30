"""Solver-output diagnostics dialog (REV24-SURFACE).

Read-only ledger view over :class:`SolverOutputLedger`: pick a scene
revision, see every persisted solver-stack row resolved to it — bound
artifacts (canonical replay path exists) and the unbound/orphaned payload
ledger — with type, observable, produced-by, provenance/capability and
verification state. Payloads that fail to parse are listed with their
honest state; nothing is fabricated and no write path exists.

Mounted from the Support page next to the authority inspector — the
surface never mutates authority: it renders the projection only.
"""

from __future__ import annotations

from typing import Mapping

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_display_labels import revision_display_label
from .solver_output_ledger import SolverArtifactEntry, SolverOutputLedger
from .ui_theme import SemanticState, TypographyRole, set_semantic_state, set_typography_role


_KIND_LABELS = {
    'solver_adapter': 'ソルバーアダプタ',
    'scene_snapshot': '音響シーンスナップショット',
    'compiled_geometry': 'コンパイル済みジオメトリ',
    'leak_portal': '漏洩ポータル診断',
    'prediction_request': '予測要求',
    'dispatch_binding': 'ソルバーディスパッチ',
    'execution_input': 'ソルバー実行入力',
    'solver_result': 'ソルバー結果',
    'path_artifact': '確定的パス成果物',
    'late_field': '遅延フィールドエネルギー成果物',
    'late_energy_decay': '遅延エネルギー減衰成果物',
    'stitched_response': '統合ハイブリッド応答成果物',
    'hybrid_result': 'ハイブリッド音響結果',
    'stitching_policy': 'スティッチングポリシー',
    'prediction_provider': 'ハイブリッド予測プロバイダ',
    'provider_binding': 'プロバイダ束縛',
    'provider_objective': 'プロバイダ評価接続',
    'boundary_overlay': '処理境界オーバーレイ',
    'boundary_composition': '処理境界構成',
}

_OBSERVABLE_LABELS = {
    'direct_and_first_order_specular': '直接音＋1次鏡面',
    'direct_through_second_order_specular': '直接音〜2次鏡面',
    'direct_single_portal_propagation': '直接音（単一ポータル）',
    'direct_bounded_portal_graph_propagation': '直接音（ポータルグラフ）',
    'single_portal_first_order_specular': '単一ポータル1次鏡面',
    'multi_portal_first_order_specular': 'マルチポータル1次鏡面',
    'multi_portal_second_order_specular': 'マルチポータル2次鏡面',
    'single_region_bounded_late_field_energy_v1': '遅延フィールドエネルギー（有界）',
    'late_energy_density_per_m2': '遅延エネルギー密度',
    'late_energy_decay': '遅延エネルギー減衰',
    'magnitude_energy': '振幅エネルギー',
    'coherent_phase': 'コヒーレント位相',
    'arrival_timing': '到達時刻',
    'deterministic_path_identity': '確定的パス同一性',
    'late_decay': '遅延減衰',
    'complex_pressure': '複素音圧',
    'deterministic_paths': '確定的パス',
}

_BINDING_LABELS = {
    'bound': '検証済み',
    'unbound': '台帳のみ',
}

_LINK_LABELS = {
    'resolved': '解決済み',
    'unresolved': '未解決',
}

_PAYLOAD_STATE_LABELS = {
    'ok': '読み取り済み',
    'unreadable': '読み取り不可',
}

_CAPABILITY_LABELS = {
    'COMPLETED': '完了',
    'READY': '実行可能',
    'SUPPORTED': '対応',
    'UNSUPPORTED': '非対応',
    'CONTINUOUS': '連続',
    'GAP_PRESERVED': 'ギャップ保持',
    'disjoint_by_observable': '観測量別分離',
    'frequency_partition_no_blend': '周波数分割（混合なし）',
    'overlap_preserve_components': '成分保持オーバーラップ',
    'upper_bound_not_point_estimate': '上限推定',
    'EXECUTED_UNVALIDATED': '実行済み・未検証',
    'NOT_APPLICABLE': '該当なし',
    'UNAVAILABLE_NOT_SYNTHESIZED': '利用不可（合成なし）',
}

_DOMAIN_LABELS = {
    'acoustic': '音響',
    'structural': '構造',
}

#: Combo sentinel for rows whose declared linkage resolves to no persisted
#: revision of this document — the ledger's orphaned payloads.
_UNRESOLVED = '__unresolved__'


def _observable_label(value: str) -> str:
    return _OBSERVABLE_LABELS.get(value, _DOMAIN_LABELS.get(value, value))


def _capability_label(value: str) -> str:
    return _CAPABILITY_LABELS.get(value, value)


class SolverOutputDiagnosticsDialog(QDialog):
    """Inspect the solver-output ledger for the current document."""

    def __init__(
        self,
        ledger: SolverOutputLedger,
        revisions: tuple,
        labels: Mapping[str, object] | None = None,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName('solverOutputDiagnosticsDialog')
        self.setWindowTitle('ソルバー出力の診断')
        self.resize(760, 560)
        self._ledger = ledger
        self._displayed: list[SolverArtifactEntry] = []

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('対象リビジョン:'))
        self.revision_combo = QComboBox()
        self.revision_combo.setObjectName('solverRevisionCombo')
        self.revision_combo.addItem('すべてのリビジョン', None)
        for revision in revisions:
            self.revision_combo.addItem(
                revision_display_label(revision, labels),
                revision.revision_id,
            )
        if ledger.unresolved:
            self.revision_combo.addItem('（リビジョン未解決）', _UNRESOLVED)
        layout.addWidget(self.revision_combo)

        self.summary_label = QLabel('')
        self.summary_label.setObjectName('solverSummary')
        layout.addWidget(self.summary_label)

        self.table = QTableWidget(0, 5, self)
        self.table.setObjectName('solverArtifactTable')
        self.table.setHorizontalHeaderLabels(
            ('種別', '観測量・スコープ', '生成元', '検証', '記録時刻')
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        self.empty_label = QLabel('')
        self.empty_label.setObjectName('solverEmptyLabel')
        set_typography_role(self.empty_label, TypographyRole.SECONDARY)
        self.empty_label.setWordWrap(True)
        layout.addWidget(self.empty_label)

        form = QWidget()
        form_layout = QFormLayout(form)
        form_layout.setContentsMargins(0, 0, 0, 0)
        self.artifact_id_label = QLabel('-')
        self.artifact_id_label.setWordWrap(True)
        self.provenance_label = QLabel('-')
        self.provenance_label.setWordWrap(True)
        self.capability_label = QLabel('-')
        self.hash_label = QLabel('-')
        self.payload_state_label = QLabel('-')
        form_layout.addRow('成果物ID:', self.artifact_id_label)
        form_layout.addRow('プロベナンス:', self.provenance_label)
        form_layout.addRow('能力:', self.capability_label)
        form_layout.addRow('ハッシュ:', self.hash_label)
        form_layout.addRow('状態:', self.payload_state_label)
        layout.addWidget(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.revision_combo.currentIndexChanged.connect(self._refresh_table)
        self.table.itemSelectionChanged.connect(self._refresh_detail)
        self._refresh_table(0)

    def _selected_filter(self) -> str | None:
        return self.revision_combo.currentData()

    def _entries(self) -> list[SolverArtifactEntry]:
        selection = self._selected_filter()
        if selection is None:
            return list(self._ledger.entries)
        if selection == _UNRESOLVED:
            return list(self._ledger.unresolved)
        return list(self._ledger.for_revision(selection))

    def _refresh_table(self, _index: int) -> None:
        entries = self._entries()
        self._displayed = entries
        bound = sum(1 for entry in entries if entry.binding == 'bound')
        note = ''
        if any(entry.link_state == 'unresolved' for entry in entries):
            note = ' · リンク未解決を含む'
        if any(entry.payload_state == 'unreadable' for entry in entries):
            note += ' · 読み取り不可を含む'
        self.summary_label.setText(
            f'ソルバー成果物 {len(entries)} 件 '
            f'（検証済み {bound} / 台帳のみ {len(entries) - bound}）{note}'
        )
        self.table.setRowCount(len(entries))
        for row_index, entry in enumerate(entries):
            observables = (
                '、'.join(_observable_label(item) for item in entry.observables)
                or '—'
            )
            values = (
                _KIND_LABELS.get(entry.kind, entry.kind),
                observables,
                entry.produced_by or '—',
                _BINDING_LABELS.get(entry.binding, entry.binding),
                entry.recorded_at_utc or '—',
            )
            for column, text in enumerate(values):
                self.table.setItem(row_index, column, QTableWidgetItem(text))
        self.table.resizeColumnsToContents()

        if not entries:
            selection = self._selected_filter()
            if selection is None:
                self.empty_label.setText(
                    'このプロジェクトにはソルバー成果物はまだありません。'
                )
            elif selection == _UNRESOLVED:
                self.empty_label.setText(
                    'リビジョン未解決のソルバー成果物はありません。'
                )
            else:
                self.empty_label.setText(
                    'このリビジョンにはソルバー成果物はありません。'
                )
        else:
            self.empty_label.setText('')
        self.empty_label.setVisible(not entries)
        self._refresh_detail()

    def _refresh_detail(self) -> None:
        selected = self.table.selectedItems()
        if not selected:
            entry = None
        else:
            row = selected[0].row()
            entry = (
                self._displayed[row]
                if 0 <= row < len(self._displayed)
                else None
            )
        if entry is None:
            self.artifact_id_label.setText('-')
            self.provenance_label.setText('-')
            self.capability_label.setText('-')
            self.hash_label.setText('-')
            self.payload_state_label.setText('-')
            return
        self.artifact_id_label.setText(entry.artifact_id)
        self.provenance_label.setText(entry.provenance_ref or '—')
        capability = entry.capability
        capability_text = _capability_label(capability) if capability else '—'
        if entry.item_count is not None:
            capability_text += f'（要素数 {entry.item_count}）'
        self.capability_label.setText(capability_text)
        digest = entry.semantic_sha256
        self.hash_label.setText(
            f'{digest[:16]}…' if isinstance(digest, str) and len(digest) > 16
            else (digest or '—')
        )
        link = _LINK_LABELS.get(entry.link_state, entry.link_state)
        payload = _PAYLOAD_STATE_LABELS.get(entry.payload_state, entry.payload_state)
        self.payload_state_label.setText(f'{payload} · リンク{link}')
        if entry.payload_state == 'unreadable' or entry.link_state == 'unresolved':
            set_semantic_state(self.payload_state_label, SemanticState.WARNING)
        else:
            set_semantic_state(self.payload_state_label, None)


__all__ = [
    'SolverOutputDiagnosticsDialog',
]
