"""IFC diff-review panel (Issue #981).

Operator-facing surface for the IFC revision-diff chain: each
correspondence row shows its category (matched/changed/added/removed/
ambiguous/unknown), the element identity, and both revisions' geometry
fingerprints; the operator accepts or skips per row — or in bulk — and
applies the reviewed set. The panel is pure presentation: every decision
flows outward through signals, and the owning controller persists the
apply transactionally into a NEW merged subject — the pinned import is
never mutated.

Honesty rules baked into the view:

* ``unknown`` rows render gray — reported, never counted as removed;
* ``ambiguous`` rows are highlighted — contested identities (duplicate
  GlobalIds, renames, multi-candidate fuzzy keys) always require an
  explicit call;
* skipped rows keep their flag when the diff is re-opened.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_ifc_diff import (
    IfcDiffDecision,
    IfcDiffRow,
    ifc_diff_label,
    ifc_diff_summary,
)


_COL_CATEGORY = 0
_COL_ELEMENT = 1
_COL_GLOBAL_ID = 2
_COL_PRIOR_HASH = 3
_COL_NEW_HASH = 4
_COL_DECISION = 5
_COL_ACCEPT = 6
_COL_SKIP = 7
_COL_LOCATE = 8

_UNKNOWN_BRUSH = QBrush(QColor('#8a8a8a'))
_AMBIGUOUS_BRUSH = QBrush(QColor('#7a5c00'))
_SKIPPED_BRUSH = QBrush(QColor('#5a4633'))
_ACCEPTED_BRUSH = QBrush(QColor('#1d4d2b'))


def _hash_cell(fingerprint: str | None) -> str:
    return fingerprint[:12] if fingerprint else '—'


def _element_cell(row: IfcDiffRow) -> str:
    label = row.name or '—'
    return f'{label} ({row.ifc_type})'


class IfcDiffReviewPanel(QWidget):
    """Correspondence review surface for IFC revision diffs (#981).

    Signals:
      * ``importRevisionRequested`` — pick a revised IFC file.
      * ``reloadRequested`` — rebuild the review from persisted records.
      * ``diffDecisionRequested(str, str)`` — row_key, 'accepted' or
        'skipped'.
      * ``allDecisionsRequested(str)`` — bulk 'accepted'/'skipped' for
        every decision-required row.
      * ``applyRequested`` — commit the reviewed set transactionally.
      * ``locateRequested(tuple[str, ...])`` — subject part ids to
        highlight in the 3D workspace.
    """

    importRevisionRequested = Signal()
    reloadRequested = Signal()
    diffDecisionRequested = Signal(str, str)
    allDecisionsRequested = Signal(str)
    applyRequested = Signal()
    locateRequested = Signal(tuple)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: tuple[IfcDiffRow, ...] = ()
        self._decisions: dict[str, IfcDiffDecision] = {}
        self._row_keys: list[str] = []
        self._build_ui()

    # -- layout ---------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        action_row = QHBoxLayout()
        self.import_revision_button = QPushButton(
            ifc_diff_label('ui.import_revision')
        )
        self.import_revision_button.clicked.connect(
            self.importRevisionRequested
        )
        action_row.addWidget(self.import_revision_button)
        self.reload_button = QPushButton(
            ifc_diff_label('ui.reload_diff')
        )
        self.reload_button.clicked.connect(self.reloadRequested)
        action_row.addWidget(self.reload_button)
        self.accept_all_button = QPushButton(
            ifc_diff_label('ui.accept_all')
        )
        self.accept_all_button.clicked.connect(
            lambda: self.allDecisionsRequested.emit('accepted')
        )
        action_row.addWidget(self.accept_all_button)
        self.skip_all_button = QPushButton(
            ifc_diff_label('ui.skip_all')
        )
        self.skip_all_button.clicked.connect(
            lambda: self.allDecisionsRequested.emit('skipped')
        )
        action_row.addWidget(self.skip_all_button)
        self.apply_button = QPushButton(ifc_diff_label('ui.apply'))
        self.apply_button.clicked.connect(self.applyRequested)
        action_row.addWidget(self.apply_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        self.summary_label = QLabel('')
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels([
            ifc_diff_label('column.category'),
            ifc_diff_label('column.element'),
            ifc_diff_label('column.global_id'),
            ifc_diff_label('column.prior_hash'),
            ifc_diff_label('column.new_hash'),
            ifc_diff_label('column.decision'),
            ifc_diff_label('column.accept'),
            ifc_diff_label('column.skip'),
            ifc_diff_label('column.locate'),
        ])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table)
        self._sync_actions()

    # -- state ----------------------------------------------------------

    def set_rows(
        self,
        rows: Sequence[IfcDiffRow],
        decisions: Mapping[str, IfcDiffDecision] | None = None,
    ) -> None:
        """Render the correspondence rows + current decisions."""
        self._rows = tuple(rows)
        self._decisions = dict(decisions or {})
        self._row_keys = [row.row_key for row in self._rows]
        self.table.setRowCount(len(self._rows))
        for index, row in enumerate(self._rows):
            self._render_row(index, row)
        self._render_summary()
        self._sync_actions()

    def decisions_changed(self, decisions: Mapping[str, IfcDiffDecision]):
        """Refresh decision cells after controller-side updates."""
        self._decisions = dict(decisions)
        for index, row in enumerate(self._rows):
            self._render_decision_cells(index, row)
        self._render_summary()
        self._sync_actions()

    def _render_row(self, index: int, row: IfcDiffRow) -> None:
        category_item = QTableWidgetItem(
            f"{ifc_diff_label('category.' + row.category)} "
            f"{ifc_diff_label('change.' + row.change_kind)}"
        )
        element_item = QTableWidgetItem(_element_cell(row))
        element_item.setToolTip(row.detail or '')
        gid_item = QTableWidgetItem(row.ifc_global_id or '—')
        prior_item = QTableWidgetItem(
            _hash_cell(row.prior_geometry_fingerprint)
        )
        new_item = QTableWidgetItem(
            _hash_cell(row.new_geometry_fingerprint)
        )
        items = (
            category_item,
            element_item,
            gid_item,
            prior_item,
            new_item,
        )
        for col, item in enumerate(items):
            self.table.setItem(index, col, item)
        self._render_decision_cells(index, row)

        accept_button = QPushButton(ifc_diff_label('decision.accepted'))
        skip_button = QPushButton(ifc_diff_label('decision.skipped'))
        locate_button = QPushButton(ifc_diff_label('ui.locate'))
        if not row.decision_required:
            accept_button.setEnabled(False)
            skip_button.setEnabled(False)
        part_ids = tuple(
            p for p in (row.prior_part_id, row.new_part_id) if p
        )
        if not part_ids:
            locate_button.setEnabled(False)
        else:
            locate_button.clicked.connect(
                lambda _=False, ids=part_ids: (
                    self.locateRequested.emit(ids)
                )
            )
        accept_button.clicked.connect(
            lambda _=False, key=row.row_key: (
                self.diffDecisionRequested.emit(key, 'accepted')
            )
        )
        skip_button.clicked.connect(
            lambda _=False, key=row.row_key: (
                self.diffDecisionRequested.emit(key, 'skipped')
            )
        )
        self.table.setCellWidget(index, _COL_ACCEPT, accept_button)
        self.table.setCellWidget(index, _COL_SKIP, skip_button)
        self.table.setCellWidget(index, _COL_LOCATE, locate_button)
        self._apply_row_style(index, row)

    def _render_decision_cells(self, index: int, row: IfcDiffRow) -> None:
        decision = self._decisions.get(row.row_key)
        text = (
            ifc_diff_label(f'decision.{decision}')
            if decision is not None
            else (
                ifc_diff_label('decision.pending')
                if row.decision_required
                else '—'
            )
        )
        decision_item = QTableWidgetItem(text)
        self.table.setItem(index, _COL_DECISION, decision_item)
        self._apply_row_style(index, row)

    def _apply_row_style(self, index: int, row: IfcDiffRow) -> None:
        decision = self._decisions.get(row.row_key)
        if row.category == 'unknown':
            brush = _UNKNOWN_BRUSH
        elif row.category == 'ambiguous':
            brush = _AMBIGUOUS_BRUSH
        elif decision == 'accepted':
            brush = _ACCEPTED_BRUSH
        elif decision == 'skipped':
            brush = _SKIPPED_BRUSH
        else:
            brush = None
        for col in range(self.table.columnCount()):
            item = self.table.item(index, col)
            if item is not None:
                item.setBackground(
                    brush if brush is not None else QBrush()
                )

    def _render_summary(self) -> None:
        if not self._rows:
            self.summary_label.setText(
                '改訂 IFC を取り込むと差分行がここに表示されます。'
            )
            return
        summary = ifc_diff_summary(self._rows)
        decided = sum(
            1
            for row in self._rows
            if row.decision_required
            and row.row_key in self._decisions
        )
        required = sum(1 for row in self._rows if row.decision_required)
        self.summary_label.setText(
            '差分: '
            f"一致 {summary['matched']} / 変更 {summary['changed']} / "
            f"追加 {summary['added']} / 削除 {summary['removed']} / "
            f"要確認 {summary['ambiguous']} / "
            f"不明 {summary['unknown']}（除去とは数えません）"
            f'　—　決定済み {decided}/{required}'
        )

    def _sync_actions(self) -> None:
        has_rows = bool(self._rows)
        self.accept_all_button.setEnabled(has_rows)
        self.skip_all_button.setEnabled(has_rows)
        self.apply_button.setEnabled(has_rows)

    # -- inspection helpers (tests / controller) -------------------------

    def row_keys(self) -> tuple[str, ...]:
        return tuple(self._row_keys)

    def row_at(self, index: int) -> IfcDiffRow | None:
        if 0 <= index < len(self._rows):
            return self._rows[index]
        return None


__all__ = [
    'IfcDiffReviewPanel',
]
