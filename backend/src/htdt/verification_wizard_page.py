"""Zero-knowledge guided verification wizard (REV59-GUIDEDWIZ).

App-scope destination: pick an open issue from the verification
manifest, run its automated checks with one button, follow the manual
check's plain-language instructions, and commit evidence — every
transition seals into the manifest-gate ledger, and the issue verdict
banner is ``evaluate_issue_verdict`` rendered for someone who has never
heard of pytest.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .accessible_labels import wire_label_buddies
from .application_pages import _page_layout
from .cad_manifest_verification import (
    MANIFEST_VERDICT_LABELS,
    ManifestGate,
    derive_verification_requirement,
    evaluate_gate,
)
from .ui_theme import TypographyRole, set_typography_role
from .user_facing_error import operation_error_message
from .verification_wizard import (
    MANIFEST_RELATIVE_PATH,
    CheckRunOutcome,
    ManifestGateStore,
    WizardCheck,
    WizardIssue,
    load_wizard_issues,
    run_check,
)

_CHECK_KIND_LABELS = {
    'pytest': '自動',
    'script': '自動',
    'manual': '実機',
}

_OUTCOME_LABELS = {
    'passed': '合格',
    'failed': '不合格',
    'timeout': 'タイムアウト',
    'error': 'エラー',
    'evidence_committed': '証跡コミット済み',
    'pending': '未実施',
}

# Verdict banner palette — zero-knowledge readability: green good,
# amber partial, red failing, gray unstarted.
_VERDICT_COLORS = {
    'verified': '#1a7f37',
    'partially_verified': '#9a6700',
    'failing': '#cf222e',
    'manual_required': '#57606a',
    'unevaluated': '#57606a',
}


def _repo_root() -> Path:
    """Repository root when running from a checkout; else the package's
    ancestor (packaged installs have no manifest — the page surfaces
    that honestly)."""
    return Path(__file__).resolve().parents[3]


class _CheckRunWorker(QThread):
    """Runs automated checks off the GUI thread, one signal per check."""

    check_finished = Signal(object, object)  # (WizardCheck, CheckRunOutcome)
    all_finished = Signal()

    def __init__(
        self,
        checks: tuple[WizardCheck, ...],
        *,
        repo_root: Path,
        report_dir: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._checks = checks
        self._repo_root = repo_root
        self._report_dir = report_dir

    def run(self) -> None:  # pragma: no cover - exercised via GUI
        for check in self._checks:
            outcome = run_check(
                check,
                repo_root=self._repo_root,
                report_dir=self._report_dir,
            )
            self.check_finished.emit(check, outcome)
        self.all_finished.emit()


class _ManifestLoadWorker(QThread):
    """Seals the manifest gates and builds the store off the GUI
    thread — the first mount otherwise freezes the shell for tens of
    seconds while ~300 gate rows write to sqlite."""

    loaded = Signal(object, object, object)  # store, issues, gates
    failed = Signal(object)

    def __init__(
        self,
        db_path: Path,
        manifest_path: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._db_path = db_path
        self._manifest_path = manifest_path

    def run(self) -> None:  # pragma: no cover - exercised via GUI
        try:
            # Parse first so a missing/bad manifest fails before the
            # sqlite file is even created.
            issues = load_wizard_issues(self._manifest_path)
            store = ManifestGateStore(self._db_path)
            gates = store.load_gates(self._manifest_path)
        except Exception as exc:  # error-boundary: worker dispatch — every manifest/store load failure crosses as the failed payload verbatim (noqa: BLE001)
            self.failed.emit(exc)
            return
        self.loaded.emit(store, issues, gates)


class VerificationWizardPage(QWidget):
    """検証ウィザード — issue picker + guided check steps."""

    def __init__(
        self,
        data_dir: Path,
        *,
        repo_root: Path | None = None,
        manifest_path: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.data_dir = Path(data_dir)
        self.repo_root = Path(repo_root) if repo_root else _repo_root()
        self.manifest_path = Path(
            manifest_path
            if manifest_path is not None
            else self.repo_root / MANIFEST_RELATIVE_PATH
        )
        # The store arrives via the load worker — every store touch
        # below guards on None until _on_manifest_loaded lands.
        self.store: ManifestGateStore | None = None
        self._issues: tuple[WizardIssue, ...] = ()
        self._gates: dict[tuple[str, str], ManifestGate] = {}
        self._gates_by_issue: dict[str, tuple[ManifestGate, ...]] = {}
        self._latest: dict = {}
        self._issue: WizardIssue | None = None
        self._check: WizardCheck | None = None
        self._worker: _CheckRunWorker | None = None
        self._load_worker: _ManifestLoadWorker | None = None
        self._pending_files: list[Path] = []
        self._detail_check_id: str | None = None

        layout = _page_layout(
            self,
            '検証ウィザード',
            '未解決課題の検証を順番に案内します。「自動」のチェックは'
            'このPCが実行し、「実機」のチェックは手順どおりに確認して'
            '証跡を記録します。すべての結果は検証台帳に記録されます。',
        )

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        layout.addWidget(splitter, 1)

        # --- left: issue picker ------------------------------------------
        picker = QWidget(splitter)
        picker_layout = QVBoxLayout(picker)
        picker_layout.setContentsMargins(0, 0, 0, 0)

        issue_caption = QLabel('検証する課題', picker)
        set_typography_role(issue_caption, TypographyRole.SECTION_TITLE)
        picker_layout.addWidget(issue_caption)
        self.issue_list = QListWidget(picker)
        self.issue_list.setAccessibleName('検証課題一覧')
        self.issue_list.itemSelectionChanged.connect(self._select_issue)
        picker_layout.addWidget(self.issue_list, 1)
        splitter.addWidget(picker)

        # --- right: issue verdict + checks --------------------------------
        right = QWidget(splitter)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self.issue_title = QLabel('課題を選択してください', right)
        set_typography_role(self.issue_title, TypographyRole.SECTION_TITLE)
        self.issue_title.setWordWrap(True)
        right_layout.addWidget(self.issue_title)

        self.verdict_label = QLabel('', right)
        set_typography_role(self.verdict_label, TypographyRole.SECTION_TITLE)
        right_layout.addWidget(self.verdict_label)
        self.verdict_reason = QLabel('', right)
        set_typography_role(self.verdict_reason, TypographyRole.SECONDARY)
        self.verdict_reason.setWordWrap(True)
        right_layout.addWidget(self.verdict_reason)

        checks_caption = QLabel('チェック', right)
        set_typography_role(checks_caption, TypographyRole.SECTION_TITLE)
        right_layout.addWidget(checks_caption)
        self.check_list = QListWidget(right)
        self.check_list.setAccessibleName('課題のチェック一覧')
        self.check_list.itemSelectionChanged.connect(
            self._show_check_detail
        )
        right_layout.addWidget(self.check_list, 1)

        # --- check detail panel (scrollable) ------------------------------
        detail_scroll = QScrollArea(right)
        detail_scroll.setWidgetResizable(True)
        detail = QWidget(detail_scroll)
        self._detail_layout = QVBoxLayout(detail)
        self._detail_layout.setContentsMargins(4, 4, 4, 4)

        self.check_title = QLabel('', detail)
        set_typography_role(self.check_title, TypographyRole.SECTION_TITLE)
        self.check_title.setWordWrap(True)
        self._detail_layout.addWidget(self.check_title)

        self.check_description = QLabel('', detail)
        self.check_description.setWordWrap(True)
        self._detail_layout.addWidget(self.check_description)

        self.cells_label = QLabel('', detail)
        self.cells_label.setWordWrap(True)
        set_typography_role(self.cells_label, TypographyRole.SECONDARY)
        self._detail_layout.addWidget(self.cells_label)

        self.last_result_label = QLabel('', detail)
        self.last_result_label.setWordWrap(True)
        set_typography_role(self.last_result_label, TypographyRole.SECONDARY)
        self._detail_layout.addWidget(self.last_result_label)

        buttons = QHBoxLayout()
        self.run_button = QPushButton('自動チェックを実行', detail)
        self.run_button.clicked.connect(self._run_selected_check)
        buttons.addWidget(self.run_button)
        self.attach_button = QPushButton('証拠ファイルを選ぶ…', detail)
        self.attach_button.clicked.connect(self._attach_evidence)
        buttons.addWidget(self.attach_button)
        buttons.addStretch(1)
        self._detail_layout.addLayout(buttons)

        self.attached_label = QLabel('', detail)
        self.attached_label.setWordWrap(True)
        set_typography_role(self.attached_label, TypographyRole.SECONDARY)
        self._detail_layout.addWidget(self.attached_label)

        self.attest_label = QLabel('確認した内容・観察結果', detail)
        self.attest_edit = QPlainTextEdit(detail)
        self.attest_edit.setAccessibleName('確認した内容・観察結果')
        self.attest_edit.setPlaceholderText(
            '例：サブウーファーから測定音が再生され、REWの画面で波形を確認しました'
        )
        self.attest_edit.setMaximumHeight(90)
        self._detail_layout.addWidget(self.attest_label)
        self._detail_layout.addWidget(self.attest_edit)

        self.commit_button = QPushButton(
            '確認しました（証跡を記録）', detail
        )
        self.commit_button.clicked.connect(self._commit_manual_evidence)
        self._detail_layout.addWidget(self.commit_button)

        self._detail_layout.addStretch(1)
        detail_scroll.setWidget(detail)
        right_layout.addWidget(detail_scroll, 1)

        footer = QHBoxLayout()
        self.run_all_button = QPushButton(
            'この課題の自動チェックをすべて実行', right
        )
        self.run_all_button.clicked.connect(self._run_all_auto_checks)
        footer.addWidget(self.run_all_button)
        self.status_label = QLabel('', right)
        self.status_label.setWordWrap(True)
        footer.addWidget(self.status_label, 1)
        right_layout.addLayout(footer)

        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 560])

        wire_label_buddies(self)
        self._start_manifest_load()
        self._update_check_panel(None)

    # ------------------------------------------------------------------
    # manifest + issue list

    def _start_manifest_load(self) -> None:
        worker = _ManifestLoadWorker(
            self.data_dir / 'cad-scenes.sqlite3',
            self.manifest_path,
            parent=self,
        )
        self._load_worker = worker
        self.status_label.setText(
            '検証マニフェストを読み込んでいます…'
        )
        worker.loaded.connect(self._on_manifest_loaded)
        worker.failed.connect(self._on_manifest_failed)
        worker.finished.connect(
            lambda w=worker: self._clear_load_worker(w)
        )
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _clear_load_worker(self, worker) -> None:
        if self._load_worker is worker:
            self._load_worker = None

    def _on_manifest_loaded(self, store, issues, gates) -> None:
        self.store = store
        self._issues = issues
        self._gates = {(g.issue_ref, g.check_id): g for g in gates}
        grouped: dict[str, list[ManifestGate]] = {}
        for gate in gates:
            grouped.setdefault(gate.issue_ref, []).append(gate)
        self._gates_by_issue = {
            ref: tuple(items) for ref, items in grouped.items()
        }
        self.status_label.setText('')
        self._refresh_issue_list()

    def _on_manifest_failed(self, exc) -> None:
        self._issues = ()
        self._gates = {}
        self._gates_by_issue = {}
        self.issue_list.clear()
        self.issue_title.setText('検証マニフェストを読み込めません')
        self.verdict_label.setText('')
        self.verdict_reason.setText(
            f'{self.manifest_path}: {operation_error_message(exc)}'
        )
        self.status_label.setText('')

    def _refresh_issue_list(self) -> None:
        if self.store is None:
            return
        self._latest = self.store.latest_results()
        self.issue_list.blockSignals(True)
        self.issue_list.clear()
        for issue in self._issues:
            gates = self._gates_by_issue.get(issue.issue_ref, ())
            verdict, _ = self.store.issue_verdict(gates, self._latest)
            label = MANIFEST_VERDICT_LABELS.get(verdict, verdict)
            title = issue.title or ''
            item = QListWidgetItem(
                f'#{issue.issue_number} {title} — {label}'
            )
            item.setData(Qt.ItemDataRole.UserRole, issue.issue_ref)
            item.setToolTip(f'{len(issue.checks)} 件のチェック / {label}')
            self.issue_list.addItem(item)
        self.issue_list.blockSignals(False)
        if self._issue is not None:
            self._reselect_issue()

    def _reselect_issue(self) -> None:
        for index in range(self.issue_list.count()):
            item = self.issue_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == self._issue.issue_ref:
                self.issue_list.setCurrentRow(index)
                return
        self._issue = None

    def _select_issue(self) -> None:
        items = self.issue_list.selectedItems()
        if not items:
            self._issue = None
            self.check_list.clear()
            self._update_check_panel(None)
            return
        ref = items[0].data(Qt.ItemDataRole.UserRole)
        self._issue = next(
            (i for i in self._issues if i.issue_ref == ref), None
        )
        if self._issue is None:
            return
        title = self._issue.title or ''
        self.issue_title.setText(f'#{self._issue.issue_number} {title}')
        self._refresh_checks()
        self._render_verdict()

    def _render_verdict(self) -> None:
        if self._issue is None:
            self.verdict_label.setText('')
            self.verdict_reason.setText('')
            return
        gates = self._gates_by_issue.get(self._issue.issue_ref, ())
        if self.store is None:
            return
        self._latest = self.store.latest_results()
        verdict, reason = self.store.issue_verdict(gates, self._latest)
        label = MANIFEST_VERDICT_LABELS.get(verdict, verdict)
        color = _VERDICT_COLORS.get(verdict, '#57606a')
        self.verdict_label.setText(f'判定: {label}')
        self.verdict_label.setStyleSheet(f'color: {color};')
        self.verdict_reason.setText(reason)
        if self._issue.notes:
            self.verdict_reason.setText(
                f'{reason}\n{self._issue.notes}'
            )

    def _refresh_checks(self) -> None:
        if self._issue is None or self.store is None:
            return
        self._latest = self.store.latest_results()
        self.check_list.blockSignals(True)
        self.check_list.clear()
        for check in self._issue.checks:
            gate = self._gates.get((self._issue.issue_ref, check.check_id))
            result = (
                self._latest.get(gate.gate_id) if gate is not None else None
            )
            status = (
                _OUTCOME_LABELS.get(result.outcome, result.outcome)
                if result is not None
                else '未実施'
            )
            kind = _CHECK_KIND_LABELS.get(check.kind, check.kind)
            item = QListWidgetItem(
                f'[{kind}] {check.check_id} — {status}'
            )
            item.setData(Qt.ItemDataRole.UserRole, check.check_id)
            item.setToolTip(check.description)
            self.check_list.addItem(item)
        self.check_list.blockSignals(False)

    def _current_gate(self) -> ManifestGate | None:
        if self._issue is None or self._check is None:
            return None
        return self._gates.get((self._issue.issue_ref, self._check.check_id))

    # ------------------------------------------------------------------
    # check detail panel

    def _show_check_detail(self) -> None:
        items = self.check_list.selectedItems()
        if not items or self._issue is None:
            self._check = None
            self._update_check_panel(None)
            return
        check_id = items[0].data(Qt.ItemDataRole.UserRole)
        self._check = next(
            (c for c in self._issue.checks if c.check_id == check_id),
            None,
        )
        self._update_check_panel(self._check)

    def _update_check_panel(self, check: WizardCheck | None) -> None:
        busy = self._worker is not None and self._worker.isRunning()
        visible = check is not None
        manual = visible and check.kind == 'manual'
        auto = visible and check.kind != 'manual'

        # Per-check scratch (file picks + typed note) must not bleed into
        # the next check — same rule as the acceptance wizard.
        if (
            check is None
            or check.check_id != self._detail_check_id
        ):
            self._pending_files = []
            self.attest_edit.clear()
            self._detail_check_id = check.check_id if check else None

        self.check_title.setText(
            f'{check.check_id} ({_CHECK_KIND_LABELS.get(check.kind, check.kind)})'
            if check
            else ''
        )
        self.check_description.setText(check.description if check else '')

        cells_text = ''
        gate = self._current_gate()
        if gate is not None:
            requirement = derive_verification_requirement(gate)
            if requirement is not None:
                lines = [
                    '実機で確認する項目（自動生成チェックリスト）:',
                    *(
                        f'・{c.channel_role}: {c.purpose or "確認"}'
                        f'（{c.repeats}回）'
                        for c in requirement.required_cells
                    ),
                ]
                cells_text = '\n'.join(lines)
        self.cells_label.setText(cells_text)
        self.cells_label.setVisible(bool(cells_text))

        result = None
        if gate is not None:
            result = self._latest.get(gate.gate_id)
        if result is not None:
            verdict, why = evaluate_gate(gate, result)
            status = _OUTCOME_LABELS.get(result.outcome, result.outcome)
            satisfied = '充足' if verdict == 'satisfied' else (
                '未充足' if verdict == 'unsatisfied' else '未評価'
            )
            detail_bits = [result.finished_at_utc, status, satisfied]
            if result.detail:
                detail_bits.append(result.detail)
            self.last_result_label.setText(
                '前回の結果: ' + ' / '.join(b for b in detail_bits if b)
            )
        else:
            self.last_result_label.setText('まだ結果がありません')

        self.attached_label.setText(
            '選択済み: '
            + ', '.join(p.name for p in self._pending_files)
            if self._pending_files
            else ''
        )
        self.attached_label.setVisible(bool(self._pending_files))

        self.run_button.setVisible(auto)
        self.run_button.setEnabled(auto and not busy)
        self.attach_button.setVisible(manual)
        self.attach_button.setEnabled(manual and not busy)
        self.attest_label.setVisible(manual)
        self.attest_edit.setVisible(manual)
        self.attest_edit.setEnabled(not busy)
        self.commit_button.setVisible(manual)
        self.commit_button.setEnabled(manual and not busy)
        self.run_all_button.setEnabled(
            not busy
            and self._issue is not None
            and any(c.kind != 'manual' for c in self._issue.checks)
        )

    # ------------------------------------------------------------------
    # automated checks

    def _launch_worker(self, checks: tuple[WizardCheck, ...]) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        report_dir = self.data_dir / 'verification-runs'
        report_dir.mkdir(parents=True, exist_ok=True)
        worker = _CheckRunWorker(
            checks,
            repo_root=self.repo_root,
            report_dir=report_dir,
            parent=self,
        )
        self._worker = worker
        self.status_label.setText('チェック実行中…')
        self._update_check_panel(self._check)
        worker.check_finished.connect(self._on_check_finished)
        worker.all_finished.connect(self._on_all_finished)
        # Clear the reference on finished BEFORE deleteLater drops the
        # C++ object — otherwise the next launch reads isRunning() on a
        # dead pointer (RuntimeError). Guard by identity so a relaunched
        # worker is never cleared by a stale finished signal.
        worker.finished.connect(lambda w=worker: self._clear_worker(w))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _clear_worker(self, worker) -> None:
        if self._worker is worker:
            self._worker = None

    def _run_selected_check(self) -> None:
        if self._check is None or self._check.kind == 'manual':
            return
        self._launch_worker((self._check,))

    def _run_all_auto_checks(self) -> None:
        if self._issue is None:
            return
        auto = tuple(c for c in self._issue.checks if c.kind != 'manual')
        if auto:
            self._launch_worker(auto)

    def _on_check_finished(
        self, check: WizardCheck, outcome: CheckRunOutcome
    ) -> None:
        # Bind the result to the check's own issue — the picker may have
        # moved on while the worker was in flight.
        gate = self._gates.get((check.issue_ref, check.check_id))
        if gate is None or self.store is None:
            return
        self.store.record_check_outcome(gate, outcome)
        self._latest = self.store.latest_results()
        self._refresh_checks()
        self._render_verdict()
        self._update_check_panel(self._check)

    def _on_all_finished(self) -> None:
        self.status_label.setText('チェック完了')
        self._refresh_issue_list()
        self._update_check_panel(self._check)

    # ------------------------------------------------------------------
    # manual checks — evidence commit

    def _attach_evidence(self) -> None:
        if self._check is None or self._check.kind != 'manual':
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, '証拠ファイルを選択', '', 'すべてのファイル (*)'
        )
        if not paths:
            return
        self._pending_files.extend(Path(p) for p in paths)
        self._update_check_panel(self._check)

    def _commit_manual_evidence(self) -> None:
        if self._check is None or self._check.kind != 'manual':
            return
        note = self.attest_edit.toPlainText().strip()
        if not note and not self._pending_files:
            QMessageBox.information(
                self,
                '証跡がありません',
                '確認した内容を記入するか、証拠ファイルを添付してください。',
            )
            return
        gate = self._current_gate()
        if gate is None or self.store is None:
            return
        payloads: list[tuple[str, bytes]] = []
        for path in self._pending_files:
            try:
                payloads.append((path.name, path.read_bytes()))
            except OSError as exc:
                QMessageBox.warning(
                    self,
                    'ファイルを読み込めません',
                    f'{path}: {operation_error_message(exc)}',
                )
                return
        self.store.commit_evidence(
            gate, files=tuple(payloads), note=note
        )
        self._pending_files = []
        self.attest_edit.clear()
        self._latest = self.store.latest_results()
        self._refresh_checks()
        self._render_verdict()
        self._update_check_panel(self._check)
        self.status_label.setText(
            f'{self._check.check_id}: 証跡を記録しました'
        )

    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        # A still-running check worker must outlive this page — detach
        # it from the result plumbing (a result emitted into a dead
        # widget is a RuntimeError) and let it delete itself on finish.
        worker = self._worker
        if worker is not None and worker.isRunning():
            self._worker = None
            try:
                worker.check_finished.disconnect()
                worker.all_finished.disconnect()
            except (RuntimeError, TypeError):
                pass
            try:
                worker.finished.disconnect()
            except (RuntimeError, TypeError):
                pass
            worker.finished.connect(worker.deleteLater)
            worker.setParent(None)
        loader = self._load_worker
        if loader is not None and loader.isRunning():
            self._load_worker = None
            try:
                loader.loaded.disconnect()
                loader.failed.disconnect()
            except (RuntimeError, TypeError):
                pass
            try:
                loader.finished.disconnect()
            except (RuntimeError, TypeError):
                pass
            loader.finished.connect(loader.deleteLater)
            loader.setParent(None)
        super().closeEvent(event)

    def refresh(self) -> None:
        """on_activate hook: re-read the ledger and re-render."""
        if self.store is None:
            return
        self._latest = self.store.latest_results()
        self._refresh_issue_list()
        if self._issue is not None:
            self._refresh_checks()
            self._render_verdict()
            self._update_check_panel(self._check)
