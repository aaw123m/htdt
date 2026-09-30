"""Authority-graph inspector dialog (#590, deferred from round-6).

Thin read-only view over :class:`AuthorityGraph` /
:class:`AuthorityInspector`: pick an authority node, see its summary
(freshness, lifecycle, evidence completeness), its upstream/downstream
lineage, and — for stale nodes — the recorded reasons. Nodes carrying a
``deep_link`` expose a 開く button that navigates to the owning
workspace via the composition's deep-link handler.

The dialog never mutates authority: it renders the projection only.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .authority_graph import (
    AuthorityGraph,
    AuthorityInspector,
    AuthorityNode,
)
from .ui_theme import SemanticState, set_semantic_state


_FRESHNESS_LABELS = {
    'current': '現在の状態',
    'historical': '履歴',
    'stale': '古い',
}

_COMPLETENESS_LABELS = {
    'complete': '証拠あり',
    'partial': '部分的',
    'missing': '未解決',
    'unknown': '不明',
}

_DOMAIN_LABELS = {
    'project': 'プロジェクト',
    'room': '部屋',
    'capture': 'キャプチャ',
    'equipment': '機材',
    'measurement': '測定',
    'prediction': '予測',
    'optimization': '最適化',
    'installation': '設置',
    'other': 'その他',
    'missing': '欠損',
}

_LIFECYCLE_LABELS = {
    'current': '現在',
    'proposed': '提案済み',
    'as_built': '竣工',
    'measured': '実測',
    'derived': '派生',
    'hypothesis': '仮説',
    'historical': '履歴',
    'stale': '古い',
    'invalid': '無効',
    'unknown': '不明',
}

_NODE_TYPE_LABELS = {
    'unknown': '不明',
    'document': 'ドキュメント',
    'scene_revision': 'シーンリビジョン',
    'measurement': '測定',
    'system_variant': 'システム提案',
}

_EVIDENCE_TYPE_LABELS = {
    'measured': '実測',
    'predicted': '予測',
}


def _node_label(node: AuthorityNode) -> str:
    """Presentation label — the graph keeps diagnostic labels; the dialog
    renders the localized form of the labels it emits."""
    label = node.label
    if node.node_type == 'document' and label.startswith('Document '):
        return f'ドキュメント {label[len("Document "):]}'
    if node.node_type == 'scene_revision' and label.startswith('SceneRevision '):
        return f'シーンリビジョン {label[len("SceneRevision "):]}'
    if node.node_type == 'measurement':
        for evidence_type, evidence_label in _EVIDENCE_TYPE_LABELS.items():
            suffix = f' ({evidence_type})'
            if label.endswith(suffix):
                return f'{label[:-len(suffix)]}（{evidence_label}）'
    return label


class AuthorityInspectorDialog(QDialog):
    """Inspect authority lineage for the current document."""

    def __init__(
        self,
        graph: AuthorityGraph,
        *,
        on_deep_link: Callable[[object], bool] | None = None,
        initial_node_id: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName('authorityInspectorDialog')
        self.setWindowTitle('権威グラフ')
        self.resize(640, 520)
        self._graph = graph
        self._inspector = AuthorityInspector(graph)
        self._on_deep_link = on_deep_link

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('対象:'))
        self.node_combo = QComboBox()
        self.node_combo.setObjectName('authorityNodeCombo')
        self._node_ids: list[str] = []
        for node in self._sorted_nodes():
            domain = _DOMAIN_LABELS.get(node.domain.value, node.domain.value)
            stale_mark = ' ⚠' if node.stale else ''
            self.node_combo.addItem(
                f'[{domain}] {_node_label(node)}{stale_mark}', node.node_id
            )
            self._node_ids.append(node.node_id)
        layout.addWidget(self.node_combo)

        form = QWidget()
        form_layout = QFormLayout(form)
        form_layout.setContentsMargins(0, 0, 0, 0)
        self.authority_class_label = QLabel('-')
        self.lifecycle_label = QLabel('-')
        self.freshness_label = QLabel('-')
        self.completeness_label = QLabel('-')
        self.hash_label = QLabel('-')
        self.hash_label.setWordWrap(True)
        self.created_label = QLabel('-')
        form_layout.addRow('区分:', self.authority_class_label)
        form_layout.addRow('ライフサイクル:', self.lifecycle_label)
        form_layout.addRow('鮮度:', self.freshness_label)
        form_layout.addRow('証拠:', self.completeness_label)
        form_layout.addRow('ハッシュ:', self.hash_label)
        form_layout.addRow('作成:', self.created_label)
        layout.addWidget(form)

        lineage_row = QHBoxLayout()
        for title, attr in (('上流:', 'upstream_list'), ('下流:', 'downstream_list')):
            column = QVBoxLayout()
            column.addWidget(QLabel(title))
            widget = QListWidget()
            widget.setObjectName(f'authority_{attr}')
            setattr(self, attr, widget)
            column.addWidget(widget)
            lineage_row.addLayout(column)
        layout.addLayout(lineage_row)

        self.why_stale_label = QLabel('')
        self.why_stale_label.setObjectName('authorityWhyStale')
        self.why_stale_label.setWordWrap(True)
        set_semantic_state(self.why_stale_label, SemanticState.WARNING)
        layout.addWidget(self.why_stale_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        self.open_button = QPushButton('ワークスペースで開く')
        self.open_button.setObjectName('authorityOpenButton')
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._open_deep_link)
        buttons.addButton(self.open_button, QDialogButtonBox.ActionRole)
        layout.addWidget(buttons)

        self.node_combo.currentIndexChanged.connect(self._refresh_summary)
        if initial_node_id in self._node_ids:
            self.node_combo.setCurrentIndex(self._node_ids.index(initial_node_id))
        else:
            self._refresh_summary(0)

    def _sorted_nodes(self) -> list[AuthorityNode]:
        return sorted(
            self._graph.nodes.values(),
            key=lambda n: (n.domain.value, n.label, n.node_id),
        )

    def _current_node_id(self) -> str | None:
        if not self._node_ids:
            return None
        return self.node_combo.currentData()

    def _refresh_summary(self, _index: int) -> None:
        node_id = self._current_node_id()
        self.open_button.setEnabled(False)
        self.why_stale_label.setText('')
        self.upstream_list.clear()
        self.downstream_list.clear()
        if node_id is None:
            self.authority_class_label.setText('-')
            self.lifecycle_label.setText('-')
            self.freshness_label.setText('-')
            self.completeness_label.setText('-')
            self.hash_label.setText('-')
            self.created_label.setText('-')
            return
        node = self._graph.node(node_id)
        summary = self._inspector.summary(node_id)
        domain, _, node_type = summary.authority_class.partition(':')
        self.authority_class_label.setText(
            f"{_DOMAIN_LABELS.get(domain, domain)}"
            f"・{_NODE_TYPE_LABELS.get(node_type, node_type)}"
        )
        self.lifecycle_label.setText(
            _LIFECYCLE_LABELS.get(
                summary.lifecycle.value, summary.lifecycle.value
            )
        )
        self.freshness_label.setText(
            _FRESHNESS_LABELS.get(summary.freshness, summary.freshness)
        )
        self.completeness_label.setText(
            _COMPLETENESS_LABELS.get(
                summary.evidence_completeness.value, summary.evidence_completeness.value
            )
        )
        self.hash_label.setText(summary.authority_hash or '-')
        self.created_label.setText(summary.created_at_utc or '-')
        for upstream in self._graph.upstream(node_id):
            QListWidgetItem(_node_label(upstream), self.upstream_list)
        for downstream in self._graph.downstream(node_id):
            QListWidgetItem(_node_label(downstream), self.downstream_list)
        reasons = self._inspector.why_stale(node_id)
        if reasons:
            self.why_stale_label.setText('古い理由: ' + ' / '.join(reasons))
        self.open_button.setEnabled(
            node is not None and node.deep_link is not None and self._on_deep_link is not None
        )

    def _open_deep_link(self) -> None:
        node_id = self._current_node_id()
        if node_id is None or self._on_deep_link is None:
            return
        node = self._graph.node(node_id)
        if node is not None and node.deep_link is not None:
            self._on_deep_link(node.deep_link)


__all__ = ['AuthorityInspectorDialog']
