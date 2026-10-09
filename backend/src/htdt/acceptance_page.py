"""Guided acceptance wizard page (REV48-HWGUIDE).

App-scope destination where a user picks a physical acceptance gate and
steps through it. Auto steps run real checks on this machine; guided-manual
steps auto-capture whatever is checkable and record a human confirmation;
attest steps record a typed attestation plus optional digest-bound evidence.
Every transition lands as an append-only run revision in the authority DB —
the run is replayable end to end.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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

from .acceptance_checks import AUTO_CHECKS, CheckContext, run_auto_check
from .acceptance_gates import GATE_REGISTRY, get_gate
from .accessible_labels import wire_label_buddies
from .application_pages import _page_layout
from .cad_acceptance import (
    AcceptanceRun,
    AcceptanceStepRecord,
    build_acceptance_run,
    build_evidence_bundle,
    bundle_payload,
)
from .cad_acceptance_repository import AcceptanceRunRepository
from .cad_schema import NATIVE_SCHEMA_VERSION
from .clock import utc_now_iso
from .ui_theme import TypographyRole, set_typography_role

_STEP_STATUS_LABELS = {
    'pending': '未実施',
    'passed': '合格',
    'failed': '不合格',
    'blocked': '不可',
    'skipped': 'スキップ',
}

_STEP_KIND_LABELS = {
    'auto': '自動',
    'guided_manual': '手順',
    'attest': '証明',
}

_RUN_STATUS_LABELS = {
    'in_progress': '実行中',
    'passed': '合格',
    'failed': '不合格',
    'partial': '一部未実施',
}


def _environment_snapshot() -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        'hostname': platform.node(),
        'platform': platform.platform(),
        'machine': platform.machine(),
        'python': platform.python_version(),
        'schema_version': NATIVE_SCHEMA_VERSION,
    }
    try:
        import htdt

        snapshot['htdt_version'] = getattr(htdt, '__version__', None)
    except Exception:
        pass
    try:
        from PySide6.QtCore import qVersion

        snapshot['qt'] = qVersion()
    except Exception:
        pass
    try:
        from PySide6.QtGui import QGuiApplication

        gui = QGuiApplication.instance()
        if gui is not None:
            snapshot['screens'] = [
                {
                    'name': screen.name(),
                    'geometry': list(screen.geometry().getRect()),
                    'device_pixel_ratio': screen.devicePixelRatio(),
                }
                for screen in gui.screens()
            ]
    except Exception:
        pass
    try:
        # The code state under test — the run header renders this when
        # present; packaged installs simply have nothing to record.
        repo_root = Path(__file__).resolve().parents[3]
        git = shutil.which('git')
        if git is not None and (repo_root / '.git').exists():
            proc = subprocess.run(
                [git, '-C', str(repo_root), 'rev-parse', 'HEAD'],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc.returncode == 0:
                snapshot['code_sha'] = proc.stdout.strip()
    except Exception:
        pass
    return snapshot


def _apply_check_result(
    steps: list,
    step_id: str,
    result,
    capture_only: bool,
) -> list | None:
    """Fold one worker result into the run's step list.

    Returns the mutated step list, or ``None`` when the result was
    dropped — the step was decided while the check ran, or the step id
    no longer exists in this run. Callers must only commit a revision
    when the return is not ``None``: a dropped result that still
    commits writes a phantom no-op revision into the hash-chained
    journal.
    """

    steps = list(steps)
    for index, record in enumerate(steps):
        if record.step_id != step_id:
            continue
        if record.status != 'pending':
            # The step was decided while the check ran — a stale
            # result must never overwrite a recorded verdict.
            return None
        now = utc_now_iso()
        detail = {
            'verdict': result.verdict,
            'detail_ja': result.detail_ja,
            **result.evidence,
            'checked_at_utc': now,
        }
        if capture_only or record.kind != 'auto':
            # Auto-capture: record the observation; the human still
            # confirms the step. Failed captures stay pending.
            steps[index] = record.model_copy(
                update={'check_detail': detail}
            )
        elif result.verdict == 'pass':
            steps[index] = record.model_copy(
                update={
                    'status': 'passed',
                    'verdict_source': 'auto_check',
                    'check_detail': detail,
                    'recorded_at_utc': now,
                }
            )
        elif result.verdict in ('deferred', 'unavailable'):
            # Unavailable = the check could not run (REW down, missing
            # dependency). That is not a verdict on the system under
            # test — the step stays pending with the reason recorded,
            # never bricked into 'blocked'.
            steps[index] = record.model_copy(
                update={
                    'status': 'pending',
                    'check_detail': detail,
                    'note': result.detail_ja,
                }
            )
        else:
            steps[index] = record.model_copy(
                update={
                    'status': 'failed',
                    'verdict_source': 'auto_check',
                    'check_detail': detail,
                    'recorded_at_utc': now,
                }
            )
        return steps
    return None


class _AutoCheckWorker(QThread):
    """Runs a (possibly slow) auto check off the GUI thread."""

    finished_result = Signal(object)

    def __init__(
        self, check_spec: str, ctx: CheckContext, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._check_spec = check_spec
        self._ctx = ctx

    def run(self) -> None:  # pragma: no cover - exercised via GUI
        self.finished_result.emit(
            run_auto_check(self._check_spec, self._ctx)
        )


class AcceptancePage(QWidget):
    """受入検証ウィザード — gate picker + guided step run surface."""

    def __init__(
        self,
        data_dir: Path,
        *,
        rew_base_url: Callable[[], str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.data_dir = Path(data_dir)
        self._rew_base_url = rew_base_url
        self.repository = AcceptanceRunRepository(
            self.data_dir / 'cad-scenes.sqlite3'
        )
        self._gate = None
        self._run: AcceptanceRun | None = None
        self._worker: _AutoCheckWorker | None = None
        # #974: app ActivityCenter — wired post-construction by the
        # application; None keeps the page fully usable standalone.
        self._activity_center = None
        self._worker_op_id: str | None = None
        # Step id whose input/attestation text is currently in the editors.
        self._detail_step_id: str | None = None

        layout = _page_layout(
            self,
            '受入検証ウィザード',
            '実機での受入ゲートを順番にガイドします。自動チェックは機器が'
            '実際に確認した結果だけを記録し、人間の確認が必要な項目は'
            '証明として残します。',
        )

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        layout.addWidget(splitter, 1)

        # --- left: gate picker + in-progress runs -------------------------
        picker = QWidget(splitter)
        picker_layout = QVBoxLayout(picker)
        picker_layout.setContentsMargins(0, 0, 0, 0)

        gate_caption = QLabel('受入ゲート', picker)
        set_typography_role(gate_caption, TypographyRole.SECTION_TITLE)
        picker_layout.addWidget(gate_caption)
        self.gate_list = QListWidget(picker)
        self.gate_list.setAccessibleName('受入ゲート一覧')
        for gate in GATE_REGISTRY:
            item = QListWidgetItem(gate.title_ja)
            item.setData(Qt.ItemDataRole.UserRole, gate.gate_id)
            item.setToolTip(gate.description_ja)
            self.gate_list.addItem(item)
        picker_layout.addWidget(self.gate_list, 1)

        self.start_button = QPushButton('実行を開始', picker)
        self.start_button.clicked.connect(self._start_run)
        picker_layout.addWidget(self.start_button)

        resume_caption = QLabel('受入の実行', picker)
        set_typography_role(resume_caption, TypographyRole.SECTION_TITLE)
        picker_layout.addWidget(resume_caption)
        self.run_list = QListWidget(picker)
        self.run_list.setAccessibleName('受入実行一覧')
        self.run_list.itemSelectionChanged.connect(self._resume_selected)
        picker_layout.addWidget(self.run_list, 1)
        splitter.addWidget(picker)

        # --- right: run view ------------------------------------------------
        right = QWidget(splitter)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self.run_title = QLabel('実行するゲートを選択してください', right)
        set_typography_role(self.run_title, TypographyRole.SECTION_TITLE)
        self.run_title.setWordWrap(True)
        right_layout.addWidget(self.run_title)

        self.run_meta = QLabel('', right)
        set_typography_role(self.run_meta, TypographyRole.SECONDARY)
        self.run_meta.setWordWrap(True)
        right_layout.addWidget(self.run_meta)

        self.progress_label = QLabel('', right)
        right_layout.addWidget(self.progress_label)

        step_caption = QLabel('手順', right)
        set_typography_role(step_caption, TypographyRole.SECTION_TITLE)
        right_layout.addWidget(step_caption)
        self.step_list = QListWidget(right)
        self.step_list.setAccessibleName('受入手順一覧')
        self.step_list.itemSelectionChanged.connect(self._show_step_detail)
        right_layout.addWidget(self.step_list, 1)

        # --- step detail panel (scrollable) ---------------------------------
        detail_scroll = QScrollArea(right)
        detail_scroll.setWidgetResizable(True)
        detail = QWidget(detail_scroll)
        self._detail_layout = QVBoxLayout(detail)
        self._detail_layout.setContentsMargins(4, 4, 4, 4)

        self.step_title = QLabel('', detail)
        set_typography_role(self.step_title, TypographyRole.SECTION_TITLE)
        self.step_title.setWordWrap(True)
        self._detail_layout.addWidget(self.step_title)

        self.step_instruction = QLabel('', detail)
        self.step_instruction.setWordWrap(True)
        self._detail_layout.addWidget(self.step_instruction)

        self.input_label = QLabel('', detail)
        self.input_edit = QLineEdit(detail)
        self.input_edit.setAccessibleName('チェックパラメータ入力')
        self._detail_layout.addWidget(self.input_label)
        self._detail_layout.addWidget(self.input_edit)

        self.attest_label = QLabel('証明・観察の記録', detail)
        self.attest_edit = QPlainTextEdit(detail)
        self.attest_edit.setAccessibleName('証明・観察の記録')
        self.attest_edit.setPlaceholderText(
            '確認した内容・観察結果を記入してください'
        )
        self.attest_edit.setMaximumHeight(90)
        self._detail_layout.addWidget(self.attest_label)
        self._detail_layout.addWidget(self.attest_edit)

        self.step_result = QLabel('', detail)
        self.step_result.setWordWrap(True)
        set_typography_role(self.step_result, TypographyRole.SECONDARY)
        self._detail_layout.addWidget(self.step_result)

        self.evidence_label = QLabel('', detail)
        self.evidence_label.setWordWrap(True)
        set_typography_role(self.evidence_label, TypographyRole.SECONDARY)
        self._detail_layout.addWidget(self.evidence_label)

        buttons = QHBoxLayout()
        self.check_button = QPushButton('チェック実行', detail)
        self.check_button.clicked.connect(self._run_auto_check)
        buttons.addWidget(self.check_button)
        self.capture_button = QPushButton('証跡を自動取得', detail)
        self.capture_button.clicked.connect(self._run_auto_capture)
        buttons.addWidget(self.capture_button)
        self.attach_button = QPushButton('証拠を添付…', detail)
        self.attach_button.clicked.connect(self._attach_evidence)
        buttons.addWidget(self.attach_button)
        buttons.addStretch(1)
        self._detail_layout.addLayout(buttons)

        verdict_buttons = QHBoxLayout()
        self.confirm_button = QPushButton('確認しました（合格）', detail)
        self.confirm_button.clicked.connect(self._confirm_step)
        verdict_buttons.addWidget(self.confirm_button)
        self.fail_button = QPushButton('問題を記録（不合格）', detail)
        self.fail_button.clicked.connect(self._fail_step)
        verdict_buttons.addWidget(self.fail_button)
        self.skip_button = QPushButton('スキップ', detail)
        self.skip_button.clicked.connect(self._skip_step)
        verdict_buttons.addWidget(self.skip_button)
        verdict_buttons.addStretch(1)
        self._detail_layout.addLayout(verdict_buttons)
        self._detail_layout.addStretch(1)

        detail_scroll.setWidget(detail)
        right_layout.addWidget(detail_scroll, 1)

        footer = QHBoxLayout()
        self.export_button = QPushButton('証拠バンドルをエクスポート', right)
        self.export_button.clicked.connect(self._export_bundle)
        footer.addWidget(self.export_button)
        self.run_status_label = QLabel('', right)
        footer.addWidget(self.run_status_label, 1)
        right_layout.addLayout(footer)

        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 560])

        wire_label_buddies(self)
        self._refresh_run_list()
        self._update_step_panel(None)

    # ------------------------------------------------------------------
    # run lifecycle

    def _refresh_run_list(self) -> None:
        # Repopulating fires itemSelectionChanged; block it so a commit can
        # never swap the displayed run out from under the user, and restore
        # the current run's row afterwards. Finished runs stay listed —
        # labeled with their status — so a verifier can reopen and
        # re-export them (replayability).
        current_id = self._run.run_id if self._run is not None else None
        self.run_list.blockSignals(True)
        try:
            self.run_list.clear()
            selected_row = -1
            for row, run in enumerate(self.repository.list_runs()):
                gate = get_gate(run.gate_id)
                label = f'{gate.title_ja} — {run.started_at_utc[:10]}'
                if run.status != 'in_progress':
                    label += (
                        f'（{_RUN_STATUS_LABELS[run.status]}）'
                    )
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, run.run_id)
                self.run_list.addItem(item)
                if run.run_id == current_id:
                    selected_row = row
            if selected_row >= 0:
                self.run_list.setCurrentRow(selected_row)
        finally:
            self.run_list.blockSignals(False)

    def _start_run(self) -> None:
        row = self.gate_list.currentRow()
        if row < 0:
            return
        self._gate = GATE_REGISTRY[row]
        run = build_acceptance_run(
            self._gate,
            run_id=f'ac-{uuid.uuid4().hex[:12]}',
            environment=_environment_snapshot(),
        )
        self._run = self.repository.save(run)
        self._refresh_run_list()
        self._render_run()

    def _resume_selected(self) -> None:
        item = self.run_list.currentItem()
        if item is None:
            return
        run_id = item.data(Qt.ItemDataRole.UserRole)
        run = self.repository.latest(run_id)
        if run is None:
            return
        self._run = run
        self._gate = get_gate(run.gate_id)
        self._render_run()

    def _render_run(self) -> None:
        if self._run is None or self._gate is None:
            return
        done = sum(
            1 for s in self._run.steps if s.status != 'pending'
        )
        self.run_title.setText(self._gate.title_ja)
        env = self._run.environment
        meta_bits = [
            f"実行ID {self._run.run_id}",
            f"開始 {self._run.started_at_utc}",
            f"マシン {env.get('hostname', '?')}",
            f"OS {env.get('platform', '?')}",
        ]
        if env.get('code_sha'):
            meta_bits.append(f"コードSHA {env['code_sha'][:12]}")
        meta_bits.append(f"マニフェスト {self._run.gate_manifest_sha256[:12]}")
        self.run_meta.setText(' ・ '.join(meta_bits))
        self.run_meta.setToolTip(
            f"manifest_sha256={self._run.gate_manifest_sha256}"
        )
        self.progress_label.setText(
            f'進捗 {done} / {len(self._run.steps)}'
        )
        self.run_status_label.setText(
            f"実行状態: {_RUN_STATUS_LABELS[self._run.status]}"
        )
        self.export_button.setEnabled(self._run.status != 'in_progress')

        self.step_list.clear()
        for record in self._run.steps:
            definition = next(
                (s for s in self._gate.steps if s.step_id == record.step_id),
                None,
            )
            title = definition.title_ja if definition else record.step_id
            item = QListWidgetItem(
                f'[{_STEP_STATUS_LABELS[record.status]}] '
                f'({_STEP_KIND_LABELS[record.kind]}) {title}'
            )
            item.setData(Qt.ItemDataRole.UserRole, record.step_id)
            self.step_list.addItem(item)
        self._select_next_pending()

    def _select_next_pending(self) -> None:
        for index, record in enumerate(self._run.steps):
            if record.status == 'pending':
                self.step_list.setCurrentRow(index)
                return

    # ------------------------------------------------------------------
    # step detail

    def _current_def_and_record(self):
        item = self.step_list.currentItem()
        if item is None or self._gate is None or self._run is None:
            return None, None
        step_id = item.data(Qt.ItemDataRole.UserRole)
        definition = next(
            (s for s in self._gate.steps if s.step_id == step_id), None
        )
        record = next(
            (s for s in self._run.steps if s.step_id == step_id), None
        )
        return definition, record

    def _show_step_detail(self) -> None:
        definition, record = self._current_def_and_record()
        self._update_step_panel((definition, record))

    def _update_step_panel(self, pair) -> None:
        if pair is None or pair[0] is None:
            self.step_title.setText('')
            self.step_instruction.setText('')
            self.step_result.setText('')
            self.evidence_label.setText('')
            self.input_label.hide()
            self.input_edit.hide()
            self.attest_label.hide()
            self.attest_edit.hide()
            for b in (
                self.check_button,
                self.capture_button,
                self.attach_button,
                self.confirm_button,
                self.fail_button,
                self.skip_button,
            ):
                b.hide()
            return
        definition, record = pair
        editable = record.status == 'pending'
        self.step_title.setText(
            f'{definition.title_ja} — {_STEP_KIND_LABELS[record.kind]} '
            f'({_STEP_STATUS_LABELS[record.status]})'
        )
        self.step_instruction.setText(definition.instruction_ja)
        detail_bits: list[str] = []
        if record.check_detail:
            detail_bits.append(str(record.check_detail.get('detail_ja', '')))
        if record.note:
            detail_bits.append(record.note)
        self.step_result.setText(' / '.join(bit for bit in detail_bits if bit))
        ev_lines = [
            f'{ref.kind}: {ref.filename} ({ref.sha256[:12]}…)'
            for ref in record.evidence
        ]
        self.evidence_label.setText('\n'.join(ev_lines))

        self.input_label.setVisible(
            bool(definition.input_label_ja) and editable
        )
        if definition.input_label_ja:
            self.input_label.setText(definition.input_label_ja)
            self.input_edit.setAccessibleName(definition.input_label_ja)
        # Per-step scratch text must not bleed into the next step —
        # typed input/attestation belongs to exactly one step id.
        if record.step_id != self._detail_step_id:
            self.input_edit.clear()
            self.attest_edit.clear()
            self._detail_step_id = record.step_id
        self.input_edit.setVisible(
            bool(definition.input_label_ja) and editable
        )
        self.attest_label.setVisible(record.kind == 'attest' and editable)
        self.attest_edit.setVisible(record.kind == 'attest' and editable)

        self.check_button.setVisible(record.kind == 'auto' and editable)
        self.capture_button.setVisible(
            record.kind != 'auto' and bool(record.auto_check) and editable
        )
        self.attach_button.setVisible(record.kind != 'auto' and editable)
        self.confirm_button.setVisible(record.kind != 'auto' and editable)
        self.fail_button.setVisible(record.kind != 'auto' and editable)
        self.skip_button.setVisible(editable)

    # ------------------------------------------------------------------
    # transitions

    def _ctx(self, record: AcceptanceStepRecord) -> CheckContext:
        return CheckContext(
            data_dir=self.data_dir,
            db_path=self.data_dir / 'cad-scenes.sqlite3',
            rew_base_url=self._rew_base_url(),
            run_id=self._run.run_id,
            step_id=record.step_id,
            input_value=self.input_edit.text(),
            prior_detail=record.check_detail,
            repository=self.repository,
        )

    def _commit_steps(self, steps: list[AcceptanceStepRecord]) -> None:
        self._run = self.repository.commit(self._run, steps)
        self._refresh_run_list()
        self._render_run()

    def _mutate_current(self, mutate: Callable[[AcceptanceStepRecord], None]):
        definition, record = self._current_def_and_record()
        if definition is None or record is None:
            return None
        steps = list(self._run.steps)
        index = steps.index(record)
        updated = mutate(record)
        steps[index] = updated
        self._commit_steps(steps)
        return updated

    def _run_auto_check(self) -> None:
        self._launch_worker(capture_only=False)

    def _run_auto_capture(self) -> None:
        self._launch_worker(capture_only=True)

    def set_activity_center(self, activity_center) -> None:
        """Wire the app ActivityCenter for the auto-check run (#974)."""

        self._activity_center = activity_center

    def _launch_worker(self, *, capture_only: bool) -> None:
        definition, record = self._current_def_and_record()
        if definition is None or record is None or not record.auto_check:
            return
        if self._worker is not None and self._worker.isRunning():
            return
        self.check_button.setEnabled(False)
        self.capture_button.setEnabled(False)
        self.step_result.setText('チェック実行中…')
        ctx = self._ctx(record)
        # #974: the check writes a sealed run commit on completion —
        # EXCLUSIVE + NOT_CANCELLABLE is the honest registration for a
        # QThread that cannot cooperatively cancel.
        self._worker_op_id = None
        center = self._activity_center
        if center is not None:
            from .activity_center import (
                Cancellability,
                NavigationPolicy,
                OperationClass,
                OperationTransitionError,
                RetryPolicy,
            )
            from .workflow_navigation import (
                ApplicationDestinationId,
                WorkspaceDeepLink,
            )

            try:
                self._worker_op_id = center.submit(
                    operation_kind='acceptance.auto_check',
                    operation_class=OperationClass.COMPUTE,
                    title=(
                        '受入チェック（キャプチャ）'
                        if capture_only
                        else '受入チェック'
                    ),
                    document_ref=self._run.run_id,
                    input_authority_refs=(f'acceptance-run:{self._run.run_id}',),
                    cancellability=Cancellability.NOT_CANCELLABLE,
                    retry_policy=RetryPolicy.NONE,
                    navigation_policy=NavigationPolicy.EXCLUSIVE,
                    navigation_block_reason=(
                        'チェック結果の記録を伴うため画面を切り替えられません'
                    ),
                    deep_link=WorkspaceDeepLink(
                        ApplicationDestinationId.ACCEPTANCE
                    ),
                )
                center.mark_running(self._worker_op_id)
            except OperationTransitionError:
                self._worker_op_id = None
        worker = _AutoCheckWorker(record.auto_check, ctx, self)
        self._worker = worker
        worker.finished_result.connect(
            lambda result, cid=record.step_id, capture=capture_only,
            rid=self._run.run_id: (
                self._on_check_result(cid, result, capture, rid)
            )
        )
        # Clear the reference on finished BEFORE deleteLater drops the C++
        # object — otherwise the next launch reads isRunning() on a dead
        # pointer (RuntimeError) and only one check works per session.
        # Guard by identity so a relaunched worker is never cleared by a
        # stale finished signal.
        worker.finished.connect(lambda w=worker: self._clear_worker(w))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _clear_worker(self, worker) -> None:
        if self._worker is worker:
            self._worker = None

    def _on_check_result(self, step_id, result, capture_only, run_id) -> None:
        self.check_button.setEnabled(True)
        self.capture_button.setEnabled(True)
        op_id, self._worker_op_id = self._worker_op_id, None
        center = self._activity_center
        if self._run is None or self._run.run_id != run_id:
            # The user switched or started another run while the worker
            # was in flight — a result belongs to its own run only.
            if op_id is not None and center is not None:
                from .activity_center import OperationTransitionError

                try:
                    center.complete(
                        op_id,
                        result_summary=(
                            '別の実行へ切り替わったため結果を破棄しました'
                        ),
                    )
                except (KeyError, OperationTransitionError):
                    pass
            return
        latest = self.repository.latest(run_id)
        if latest is None:
            return
        steps = _apply_check_result(
            list(latest.steps), step_id, result, capture_only
        )
        if steps is None:
            # Result dropped — no step changed, so no journal revision.
            if op_id is not None and center is not None:
                from .activity_center import OperationTransitionError

                try:
                    center.complete(
                        op_id,
                        result_summary='チェック結果はステップを変更しませんでした',
                    )
                except (KeyError, OperationTransitionError):
                    pass
            self._select_row(step_id)
            return
        self._run = self.repository.commit(latest, steps)
        if op_id is not None and center is not None:
            from .activity_center import OperationTransitionError

            try:
                center.complete(
                    op_id,
                    result_summary='チェック結果を記録しました',
                )
            except (KeyError, OperationTransitionError):
                pass
        self._refresh_run_list()
        self._render_run()
        self._select_row(step_id)

    def _select_row(self, step_id: str) -> None:
        for index in range(self.step_list.count()):
            item = self.step_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == step_id:
                self.step_list.setCurrentRow(index)
                return

    def _attach_evidence(self) -> None:
        definition, record = self._current_def_and_record()
        if definition is None or record is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, '証拠ファイルを選択', '', 'すべてのファイル (*)'
        )
        if not path:
            return
        file_path = Path(path)
        payload = file_path.read_bytes()
        kind = 'file_evidence'
        if definition.evidence_required:
            # Fill the first unmet required kind.
            met = {ref.kind for ref in record.evidence}
            for required in definition.evidence_required:
                if required not in met:
                    kind = required
                    break
        ref = self.repository.attach_evidence(
            self._run.run_id,
            record.step_id,
            kind=kind,
            filename=file_path.name,
            payload=payload,
        )
        self._mutate_current(
            lambda r: r.model_copy(
                update={'evidence': (*r.evidence, ref)}
            )
        )

    def _confirm_step(self) -> None:
        definition, record = self._current_def_and_record()
        if definition is None or record is None:
            return
        missing = [
            kind
            for kind in definition.evidence_required
            if kind not in {ref.kind for ref in record.evidence}
        ]
        if missing:
            QMessageBox.information(
                self,
                '証拠が不足しています',
                'この手順には証拠の添付が必要です: ' + ', '.join(missing),
            )
            return
        note = ''
        if record.check_detail and record.check_detail.get('detail_ja'):
            note = f"自動取得: {record.check_detail['detail_ja']}"
        attestation = (
            self.attest_edit.toPlainText().strip()
            if record.kind == 'attest'
            else ''
        )
        if record.kind == 'attest' and not attestation:
            QMessageBox.information(
                self,
                '証明が未記入です',
                '確認した内容・観察結果を記入してください。',
            )
            return
        source = (
            'attestation' if record.kind == 'attest' else 'human_confirm'
        )
        self._mutate_current(
            lambda r: r.model_copy(
                update={
                    'status': 'passed',
                    'verdict_source': source,
                    'note': note,
                    'attestation': attestation,
                    'recorded_at_utc': utc_now_iso(),
                }
            )
        )
        self.attest_edit.clear()

    def _fail_step(self) -> None:
        definition, record = self._current_def_and_record()
        if definition is None or record is None:
            return
        source = (
            'attestation' if record.kind == 'attest' else 'human_confirm'
        )
        attestation = (
            self.attest_edit.toPlainText().strip()
            if record.kind == 'attest'
            else ''
        )
        self._mutate_current(
            lambda r: r.model_copy(
                update={
                    'status': 'failed',
                    'verdict_source': source,
                    'attestation': attestation,
                    'recorded_at_utc': utc_now_iso(),
                }
            )
        )
        self.attest_edit.clear()

    def _skip_step(self) -> None:
        self._mutate_current(
            lambda r: r.model_copy(
                update={
                    'status': 'skipped',
                    'verdict_source': 'human_confirm',
                    'recorded_at_utc': utc_now_iso(),
                }
            )
        )

    def _export_bundle(self) -> None:
        if self._run is None or self._gate is None:
            return
        bundle = build_evidence_bundle(self._run, self._gate)
        path, _ = QFileDialog.getSaveFileName(
            self,
            '証拠バンドルを保存',
            f'acceptance-{self._run.run_id}.json',
            'JSON (*.json)',
        )
        payload = bundle_payload(bundle)
        # Keep the exported bundle inside the authority record too: a
        # verifier holding the DB can re-derive the same bytes.
        self.repository.attach_evidence(
            self._run.run_id,
            '_run',
            kind='evidence_bundle',
            filename=f'acceptance-{self._run.run_id}.json',
            payload=payload,
        )
        if path:
            Path(path).write_bytes(payload)
            self.step_result.setText(f'証拠バンドルを保存しました: {path}')

    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        # A still-running check worker must outlive this page — detach it
        # from the result plumbing (a result emitted into a dead widget
        # is a RuntimeError) and let it delete itself on finish.
        worker = self._worker
        if worker is not None and worker.isRunning():
            self._worker = None
            try:
                worker.finished_result.disconnect()
            except (RuntimeError, TypeError):
                pass
            try:
                worker.finished.disconnect()
            except (RuntimeError, TypeError):
                pass
            worker.finished.connect(worker.deleteLater)
            worker.setParent(None)
        super().closeEvent(event)

    def refresh(self) -> None:
        """on_activate hook: refresh resumable runs."""
        self._refresh_run_list()
        if self._run is not None:
            latest = self.repository.latest(self._run.run_id)
            if latest is not None:
                self._run = latest
                self._render_run()
