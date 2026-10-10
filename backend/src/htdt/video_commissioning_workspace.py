"""Guided video commissioning workspace — the #541 journey surface.

One bounded UI that walks an operator through the persisted evidence
chain: session → readiness → import → diagnose → act → verify → report.
Every write goes through :class:`CadVideoCommissioningRepository` and
:class:`CadColorimetryRepository`; every verdict shown is derived by the
authorities in ``cad_video_commissioning`` — the page never re-computes
color science.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_colorimetry import (
    VideoColorMeasurementSet,
    VideoColorTargetProfile,
)
from .cad_colorimetry_repository import CadColorimetryRepository
from .cad_repository import SceneRepository
from .cad_video_commissioning import (
    ACTION_KIND_LABELS,
    FINDING_KIND_LABELS,
    MODE_LABELS,
    READINESS_STATE_LABELS,
    SESSION_STATUS_LABELS,
    SIGNAL_RANGE_LABELS,
    SURFACE_KIND_LABELS,
    GuidedVideoCommissioningSession,
    VideoCommissioningDiagnosis,
    build_guided_video_session,
    compare_video_measurements,
    diagnose_video_measurement,
    evaluate_video_journey,
    evaluate_video_readiness,
    propose_video_actions,
    record_video_operator_adjustment,
    rebind_video_session,
)
from .cad_video_commissioning_repository import (
    CadVideoCommissioningRepository,
    VideoCommissioningConflictError,
    VideoCommissioningIntegrityError,
)
from .cad_video_measure_import import (
    VIDEO_IMPORT_FORMAT_LABELS,
    import_video_measurements,
)
from .clock import utc_now_iso as _utc_now
from .workflow_navigation import WorkspaceDeepLink, WorkspaceId
from .ui_theme import (
    SemanticState,
    SurfaceRole,
    TypographyRole,
    set_semantic_state,
    set_surface_role,
    set_typography_role,
)
from .operation_error_dialog import warn_user


_CONTEXT_IDS = (
    'session',
    'import',
    'diagnose',
    'actions',
    'verify',
    'report',
)

# Journey step keys → owning context id.
_STEP_CONTEXT = {
    'session': 'session',
    'readiness': 'session',
    'measure': 'import',
    'diagnose': 'diagnose',
    'adjust': 'actions',
    'verify': 'verify',
    'report': 'report',
}

_STEP_STATUS_MARKS = {
    'done': '✓',
    'current': '▶',
    'pending': '○',
    'blocked': '✕',
}

_VERDICT_LABELS = {
    'PASS': '合格',
    'FAIL': '範囲外',
    'UNKNOWN': '不明',
    'NOT_APPLICABLE': '対象外',
}

_CONFIDENCE_LABELS = {
    'evidence_bound': '証拠に基づく',
    'indicative': '参考指標',
}

_DIRECTION_LABELS = {
    'improved': '改善',
    'regressed': '悪化',
    'inconclusive': '判定不能',
    'unknown': '不明',
}


class VideoCommissioningWorkspace(QWidget):
    """The journey surface — one workflow per commissioning session."""

    CONTEXTS = _CONTEXT_IDS

    def __init__(
        self,
        repository: SceneRepository,
        document_id: str,
        navigate=None,
    ) -> None:
        super().__init__()
        self.repository = repository
        self.document_id = document_id
        self._on_navigate = navigate
        self.commissioning = CadVideoCommissioningRepository(repository)
        self.colorimetry = CadColorimetryRepository(repository)

        self._sessions: list[GuidedVideoCommissioningSession] = []
        self._current_session: GuidedVideoCommissioningSession | None = None
        self._measurement_sets: dict[str, VideoColorMeasurementSet] = {}
        self._diagnoses: dict[str, VideoCommissioningDiagnosis] = {}
        self.current_context_id = _CONTEXT_IDS[0]

        self.setObjectName('videoCommissioningWorkspace')
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Session context row — which commissioning session is on the bench.
        context_row = QHBoxLayout()
        context_row.setContentsMargins(24, 8, 24, 4)
        context_row.setSpacing(8)
        context_label = QLabel('調整セッション', self)
        set_typography_role(context_label, TypographyRole.SECONDARY)
        context_row.addWidget(context_label)
        self.session_combo = QComboBox(self)
        self.session_combo.setObjectName('videoSessionCombo')
        self.session_combo.currentIndexChanged.connect(
            self._on_session_combo_changed
        )
        context_row.addWidget(self.session_combo, 1)
        self.session_state_label = QLabel('', self)
        self.session_state_label.setObjectName('videoSessionState')
        set_typography_role(
            self.session_state_label, TypographyRole.SECONDARY
        )
        context_row.addWidget(self.session_state_label)
        root.addLayout(context_row)

        # Notice row — import/commit results and honest failure states.
        self.notice = QLabel(self)
        self.notice.setObjectName('videoCommissioningNotice')
        self.notice.setWordWrap(True)
        self.notice.setContentsMargins(24, 4, 24, 4)
        self.notice.setVisible(False)
        root.addWidget(self.notice)

        # Journey strip — derived from persisted evidence, click a step to
        # jump to the owning page.
        self.journey_card = QFrame(self)
        self.journey_card.setObjectName('videoJourneyCard')
        set_surface_role(self.journey_card, SurfaceRole.RAISED)
        journey_layout = QVBoxLayout(self.journey_card)
        journey_layout.setContentsMargins(16, 10, 16, 10)
        journey_layout.setSpacing(6)
        journey_head = QHBoxLayout()
        journey_title = QLabel('映像調整の手順', self.journey_card)
        set_typography_role(journey_title, TypographyRole.SECTION_TITLE)
        journey_head.addWidget(journey_title)
        journey_head.addStretch(1)
        self.journey_progress = QLabel('', self.journey_card)
        set_typography_role(self.journey_progress, TypographyRole.SECONDARY)
        journey_head.addWidget(self.journey_progress)
        journey_layout.addLayout(journey_head)
        self.journey_steps_row = QHBoxLayout()
        self.journey_steps_row.setSpacing(6)
        journey_layout.addLayout(self.journey_steps_row)
        root.addWidget(self.journey_card)

        self.pages = QStackedWidget(self)
        self.pages.setObjectName('videoCommissioningPages')
        root.addWidget(self.pages, 1)

        self._build_session_page()
        self._build_import_page()
        self._build_diagnose_page()
        self._build_actions_page()
        self._build_verify_page()
        self._build_report_page()
        self.refresh()

    # ------------------------------------------------------------------
    # Shell interface

    def set_context(self, context_id: str) -> None:
        if context_id not in _CONTEXT_IDS:
            raise ValueError(
                f'unknown video commissioning context: {context_id}'
            )
        if context_id == self.current_context_id:
            return
        self.current_context_id = context_id
        self.pages.setCurrentIndex(_CONTEXT_IDS.index(context_id))
        self.refresh()

    def refresh(self) -> None:
        self._reload_sessions()
        self._reload_evidence()
        self._refresh_journey()
        self._refresh_session_page()
        self._refresh_import_page()
        self._refresh_diagnose_page()
        self._refresh_actions_page()
        self._refresh_verify_page()
        self._refresh_report_page()

    # ------------------------------------------------------------------
    # Evidence loading

    def _reload_sessions(self) -> None:
        current_id = (
            self._current_session.session_id
            if self._current_session is not None
            else None
        )
        self._sessions = list(
            self.commissioning.list_sessions(self.document_id)
        )
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        for session in self._sessions:
            status = self.commissioning.current_status(
                self.document_id, session.session_id
            )
            label = (
                f'{session.surface_entity_id} / '
                f'{MODE_LABELS[session.mode]} '
                f'({SESSION_STATUS_LABELS[status]})'
            )
            self.session_combo.addItem(label, session.session_id)
        self.session_combo.blockSignals(False)
        self._current_session = None
        if current_id is not None:
            for i, session in enumerate(self._sessions):
                if session.session_id == current_id:
                    self.session_combo.setCurrentIndex(i)
                    self._current_session = session
                    break
        if self._current_session is None and self._sessions:
            self.session_combo.setCurrentIndex(len(self._sessions) - 1)
            self._current_session = self._sessions[-1]

    def _reload_evidence(self) -> None:
        self._measurement_sets = {}
        self._diagnoses = {}
        if self._current_session is None:
            return
        state = self.commissioning.journey_state(
            self.document_id, self._current_session.session_id
        )
        for set_id in state.measurement_set_ids:
            record = self.colorimetry.get_measurement_set(
                self.document_id, set_id
            )
            if record is not None:
                self._measurement_sets[set_id] = record
        # Surface-scoped sets also qualify as session evidence.
        for record in self.colorimetry.list_measurement_sets(
            self.document_id,
            self._current_session.surface_entity_id,
        ):
            self._measurement_sets.setdefault(
                record.measurement_set_id, record
            )
        for diagnosis_id in state.diagnosis_ids:
            diagnosis = self.commissioning.get_diagnosis(
                self.document_id, diagnosis_id
            )
            if diagnosis is not None:
                self._diagnoses[diagnosis_id] = diagnosis

    def _targets(self) -> list[VideoColorTargetProfile]:
        return list(
            self.colorimetry.list_target_profiles(self.document_id)
        )

    def _session_target(
        self,
    ) -> VideoColorTargetProfile | None:
        session = self._current_session
        if session is None:
            return None
        if session.target_sha256 is None:
            return None
        # Fail closed on the bound pin: a session records which target it
        # is measured against — silently substituting the currently
        # selected target would evaluate against an authority the session
        # never bound.
        return self.colorimetry.get_target_profile_by_hash(
            session.target_sha256
        )

    def _surface_entities(self) -> list[tuple[str, str]]:
        head = self.repository.current_head(self.document_id)
        if head is None:
            return []
        out: list[tuple[str, str]] = []
        for entity in head.document.entities:
            if entity.kind in ('display', 'screen', 'projector'):
                name = getattr(entity, 'name', entity.entity_id)
                out.append((entity.entity_id, f'{name} ({entity.kind})'))
        return out

    # ------------------------------------------------------------------
    # Journey strip

    def _refresh_journey(self) -> None:
        while self.journey_steps_row.count():
            item = self.journey_steps_row.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        session = self._current_session
        if session is None:
            self.journey_progress.setText('セッション未作成')
            return
        state = self.commissioning.journey_state(
            self.document_id, session.session_id
        )
        steps = evaluate_video_journey(
            session_exists=True,
            readiness_state=state.readiness_state,
            measurement_set_count=len(state.measurement_set_ids),
            diagnosis_count=len(state.diagnosis_ids),
            adjustment_count=len(state.adjustment_ids),
            comparison_count=len(state.comparison_ids),
            session_status=state.current_status,
        )
        done = sum(1 for s in steps if s.status == 'done')
        self.journey_progress.setText(f'{done}/{len(steps)} 完了')
        for step in steps:
            button = QPushButton(
                f'{step.number}. {step.title}\n'
                f'{_STEP_STATUS_MARKS[step.status]}',
                self.journey_card,
            )
            button.setObjectName(f'videoJourneyStep_{step.key}')
            context = _STEP_CONTEXT.get(step.key, 'session')
            button.clicked.connect(
                lambda _checked=False, c=context: self.set_context(c)
            )
            button.setToolTip(step.detail)
            self.journey_steps_row.addWidget(button)
        self.journey_steps_row.addStretch(1)

    def _on_session_combo_changed(self, index: int) -> None:
        if 0 <= index < len(self._sessions):
            self._current_session = self._sessions[index]
        self._reload_evidence()
        self._refresh_journey()
        self._refresh_session_page()
        self._refresh_import_page()
        self._refresh_diagnose_page()
        self._refresh_actions_page()
        self._refresh_verify_page()
        self._refresh_report_page()

    def _notice(self, text: str, *, error: bool = False) -> None:
        self.notice.setText(text)
        self.notice.setVisible(True)
        set_semantic_state(
            self.notice,
            SemanticState.ERROR if error else SemanticState.SUCCESS,
        )

    # ------------------------------------------------------------------
    # Session page — binding + readiness

    def _build_session_page(self) -> None:
        page = QWidget(self)
        scroll = QScrollArea(self.pages)
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 12, 24, 24)
        layout.setSpacing(10)

        form_group = QGroupBox('新しい調整セッション', page)
        form = QFormLayout(form_group)
        self.surface_combo = QComboBox(form_group)
        form.addRow('測定対象の画面', self.surface_combo)
        self.surface_kind_combo = QComboBox(form_group)
        for value, label in SURFACE_KIND_LABELS.items():
            self.surface_kind_combo.addItem(label, value)
        form.addRow('表示面の種別', self.surface_kind_combo)
        self.mode_combo = QComboBox(form_group)
        for value, label in MODE_LABELS.items():
            self.mode_combo.addItem(label, value)
        form.addRow('調整モード', self.mode_combo)
        self.target_combo = QComboBox(form_group)
        form.addRow('色ターゲット', self.target_combo)
        self.picture_mode_edit = QLineEdit(form_group)
        self.picture_mode_edit.setPlaceholderText('例: Cinema, ISF')
        form.addRow('ピクチャーモード', self.picture_mode_edit)
        self.meter_edit = QLineEdit(form_group)
        self.meter_edit.setPlaceholderText('例: Klein K-10A, X-Rite i1Pro3')
        form.addRow('測定器', self.meter_edit)
        self.signal_range_combo = QComboBox(form_group)
        for value, label in SIGNAL_RANGE_LABELS.items():
            self.signal_range_combo.addItem(label, value)
        form.addRow('信号レンジ', self.signal_range_combo)
        self.encoding_combo = QComboBox(form_group)
        for value, label in (
            ('rgb_limited', 'RGB リミテッド'),
            ('rgb_full', 'RGB フル'),
            ('ycbcr_limited', 'YCbCr リミテッド'),
            ('ycbcr_full', 'YCbCr フル'),
            ('unknown', '不明'),
        ):
            self.encoding_combo.addItem(label, value)
        form.addRow('カラーエンコーディング', self.encoding_combo)
        self.bit_depth_spin = QSpinBox(form_group)
        self.bit_depth_spin.setMinimum(0)
        self.bit_depth_spin.setMaximum(16)
        self.bit_depth_spin.setSpecialValueText('不明')
        self.bit_depth_spin.setValue(10)
        form.addRow('ビット深度', self.bit_depth_spin)
        create_button = QPushButton('セッションを作成', form_group)
        create_button.setObjectName('videoSessionCreate')
        create_button.clicked.connect(self._create_session)
        form.addRow(create_button)
        layout.addWidget(form_group)

        readiness_group = QGroupBox('準備状況の評価', page)
        readiness_layout = QVBoxLayout(readiness_group)
        self.readiness_state_label = QLabel('未評価', readiness_group)
        set_typography_role(
            self.readiness_state_label, TypographyRole.SECTION_TITLE
        )
        readiness_layout.addWidget(self.readiness_state_label)
        evaluate_button = QPushButton(
            '準備状況を評価', readiness_group
        )
        evaluate_button.setObjectName('videoReadinessEvaluate')
        evaluate_button.clicked.connect(self._evaluate_readiness)
        readiness_layout.addWidget(evaluate_button)
        self.readiness_checks = QTableWidget(0, 3, readiness_group)
        self.readiness_checks.setObjectName('videoReadinessChecks')
        self.readiness_checks.setHorizontalHeaderLabels(
            ('チェック', '結果', '説明')
        )
        self.readiness_checks.horizontalHeader().setStretchLastSection(True)
        self.readiness_checks.verticalHeader().setVisible(False)
        readiness_layout.addWidget(self.readiness_checks)
        layout.addWidget(readiness_group)
        layout.addStretch(1)
        self.pages.addWidget(scroll)

    def _refresh_session_page(self) -> None:
        # Repopulation must not clobber a selection the user already made.
        previous_surface = self.surface_combo.currentData()
        previous_target = self.target_combo.currentData()
        self.surface_combo.blockSignals(True)
        self.surface_combo.clear()
        for entity_id, label in self._surface_entities():
            self.surface_combo.addItem(label, entity_id)
        if previous_surface is not None:
            index = self.surface_combo.findData(previous_surface)
            if index >= 0:
                self.surface_combo.setCurrentIndex(index)
        self.surface_combo.blockSignals(False)
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        for target in self._targets():
            self.target_combo.addItem(
                f'{target.target_id} v{target.version}', target.target_sha256
            )
        if previous_target is not None:
            index = self.target_combo.findData(previous_target)
            if index >= 0:
                self.target_combo.setCurrentIndex(index)
        self.target_combo.blockSignals(False)
        session = self._current_session
        if session is None:
            self.readiness_state_label.setText('セッション未作成')
            self.session_state_label.setText('')
            self.readiness_checks.setRowCount(0)
            return
        status = self.commissioning.current_status(
            self.document_id, session.session_id
        )
        self.session_state_label.setText(
            f'{session.session_id} / {SESSION_STATUS_LABELS[status]}'
        )
        latest = self.commissioning.latest_readiness_report(
            self.document_id, session.session_id
        )
        if latest is None:
            self.readiness_state_label.setText('未評価')
            self.readiness_checks.setRowCount(0)
            return
        self.readiness_state_label.setText(
            READINESS_STATE_LABELS[latest.state]
        )
        self.readiness_checks.setRowCount(len(latest.checks))
        for row, check in enumerate(latest.checks):
            self.readiness_checks.setItem(
                row, 0, QTableWidgetItem(check.title)
            )
            status_item = QTableWidgetItem(
                _VERDICT_LABELS.get(check.status, check.status)
            )
            if check.blocking:
                status_item.setText(status_item.text() + '（必須）')
            self.readiness_checks.setItem(row, 1, status_item)
            self.readiness_checks.setItem(
                row, 2, QTableWidgetItem(check.detail)
            )

    def _create_session(self) -> None:
        surface_id = self.surface_combo.currentData()
        target_sha = self.target_combo.currentData()
        meter = self.meter_edit.text().strip()
        if surface_id is None:
            self._notice('測定対象の画面がプロジェクトにありません', error=True)
            return
        if not meter:
            self._notice('測定器を入力してください', error=True)
            return
        if target_sha is None:
            self._notice(
                '色ターゲットがありません — 先にターゲット権威を登録してください',
                error=True,
            )
            return
        target = self.colorimetry.get_target_profile_by_hash(target_sha)
        if target is None:
            self._notice('選択したターゲットが解決できません', error=True)
            return
        bit_depth_value = self.bit_depth_spin.value()
        if 0 < bit_depth_value < 8:
            self._notice(
                'ビット深度は 8 以上を指定してください（不明なら「不明」のままに）',
                error=True,
            )
            return
        bit_depth = bit_depth_value if bit_depth_value >= 8 else None
        try:
            session = build_guided_video_session(
                document_id=self.document_id,
                surface_entity_id=surface_id,
                surface_kind=self.surface_kind_combo.currentData(),
                mode=self.mode_combo.currentData(),
                target_id=target.target_id,
                target_version=target.version,
                target_sha256=target.target_sha256,
                meter=meter,
                signal_range=self.signal_range_combo.currentData(),
                encoding=self.encoding_combo.currentData(),
                bit_depth=bit_depth,
                picture_mode=self.picture_mode_edit.text().strip() or None,
                created_at_utc=_utc_now(),
            )
            if (
                self._current_session is not None
                and self._current_session.surface_entity_id == surface_id
            ):
                # Rebinding records lineage — only meaningful against the
                # same physical surface; a different surface starts a
                # fresh session instead of inheriting another surface's
                # history.
                session = rebind_video_session(
                    self._current_session,
                    surface_entity_id=surface_id,
                    surface_kind=self.surface_kind_combo.currentData(),
                    mode=self.mode_combo.currentData(),
                    target_id=target.target_id,
                    target_version=target.version,
                    target_sha256=target.target_sha256,
                    meter=meter,
                    signal_range=self.signal_range_combo.currentData(),
                    encoding=self.encoding_combo.currentData(),
                    bit_depth=bit_depth,
                    picture_mode=(
                        self.picture_mode_edit.text().strip() or None
                    ),
                    created_at_utc=_utc_now(),
                )
            self.commissioning.save_session(session)
        except (
            ValueError,
            VideoCommissioningConflictError,
            VideoCommissioningIntegrityError,
        ) as exc:
            warn_user(self, 'セッションの作成', exc)
            return
        self._current_session = session
        self._notice(f'セッション {session.session_id} を作成しました')
        self.refresh()

    def _evaluate_readiness(self) -> None:
        session = self._current_session
        if session is None:
            self._notice('先にセッションを作成してください', error=True)
            return
        target = self._session_target()
        try:
            report = evaluate_video_readiness(
                session,
                target=target,
                measurement_sets=tuple(self._measurement_sets.values()),
                evaluated_at_utc=_utc_now(),
            )
            self.commissioning.save_readiness_report(
                report, self.document_id
            )
        except ValueError as exc:
            warn_user(self, '準備状況の評価', exc)
            return
        self._notice(
            f'準備状況: {READINESS_STATE_LABELS[report.state]}'
        )
        self.refresh()

    # ------------------------------------------------------------------
    # Import page

    def _build_import_page(self) -> None:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 12, 24, 24)
        layout.setSpacing(10)

        pick_row = QHBoxLayout()
        pick_button = QPushButton('測定ファイルを選択…', page)
        pick_button.setObjectName('videoImportPick')
        pick_button.clicked.connect(self._import_file)
        pick_row.addWidget(pick_button)
        self.import_file_label = QLabel('未選択', page)
        set_typography_role(
            self.import_file_label, TypographyRole.SECONDARY
        )
        pick_row.addWidget(self.import_file_label, 1)
        layout.addLayout(pick_row)

        hint = QLabel(
            '対応形式: HTDT JSON (.json)、HCFR の *.GrayScaleSheet.csv / '
            '*.PrimariesSheet.csv。.chc は独自バイナリのため非対応です。',
            page,
        )
        hint.setWordWrap(True)
        set_typography_role(hint, TypographyRole.SECONDARY)
        layout.addWidget(hint)

        self.import_result_label = QLabel('', page)
        self.import_result_label.setObjectName('videoImportResult')
        self.import_result_label.setWordWrap(True)
        layout.addWidget(self.import_result_label)

        sets_group = QGroupBox('このセッションの測定セット', page)
        sets_layout = QVBoxLayout(sets_group)
        self.imported_sets_list = QListWidget(sets_group)
        self.imported_sets_list.setObjectName('videoImportedSets')
        sets_layout.addWidget(self.imported_sets_list)
        layout.addWidget(sets_group)
        layout.addStretch(1)
        self.pages.addWidget(page)

    def _refresh_import_page(self) -> None:
        self.imported_sets_list.clear()
        for record in self._measurement_sets.values():
            item = QListWidgetItem(
                f'{record.measurement_set_id} — '
                f'{len(record.samples)} 点 / {record.meter}'
            )
            item.setData(Qt.ItemDataRole.UserRole, record.measurement_set_id)
            self.imported_sets_list.addItem(item)

    def _import_file(self) -> None:
        session = self._current_session
        if session is None:
            self._notice('先にセッションを作成してください', error=True)
            return
        path_str, _filter = QFileDialog.getOpenFileName(
            self,
            '測定ファイルを選択',
            '',
            '測定ファイル (*.json *.csv *.chc);;すべてのファイル (*)',
        )
        if not path_str:
            return
        path = Path(path_str)
        self.import_file_label.setText(path.name)
        data = path.read_bytes()
        result = import_video_measurements(
            file_name=path.name,
            data=data,
            surface_entity_id=session.surface_entity_id,
            meter=session.meter,
            stimulus=session.stimulus,
            meter_correction=session.meter_correction,
            session_id=session.session_id,
        )
        if result.status != 'imported':
            failure = result.failure
            detail = failure.reason if failure is not None else ''
            if failure is not None and failure.interchange_hint:
                detail += '\n' + failure.interchange_hint
            self.import_result_label.setText(
                f'{VIDEO_IMPORT_FORMAT_LABELS[result.format_id]}: {detail}'
            )
            set_semantic_state(
                self.import_result_label, SemanticState.ERROR
            )
            self._notice(
                f'取り込みできませんでした ({result.status}): {detail}',
                error=True,
            )
            return
        record = result.measurement_set
        batch = result.batch
        assert record is not None and batch is not None
        try:
            self.colorimetry.save_measurement_set(record, self.document_id)
            self.commissioning.save_import_batch(batch, self.document_id)
        except (
            ValueError,
            VideoCommissioningConflictError,
            VideoCommissioningIntegrityError,
        ) as exc:
            warn_user(self, '測定の取り込み', exc)
            return
        warning_text = (
            ' / '.join(result.warnings) if result.warnings else ''
        )
        self.import_result_label.setText(
            f'{VIDEO_IMPORT_FORMAT_LABELS[result.format_id]}: '
            f'{record.measurement_set_id} — '
            f'{len(record.samples)} 点を取り込みました'
            + (f'\n警告: {warning_text}' if warning_text else '')
        )
        set_semantic_state(self.import_result_label, SemanticState.SUCCESS)
        self._notice(f'{record.measurement_set_id} を取り込みました')
        self.refresh()

    # ------------------------------------------------------------------
    # Diagnose page

    def _build_diagnose_page(self) -> None:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 12, 24, 24)
        layout.setSpacing(10)

        pick_row = QHBoxLayout()
        pick_row.addWidget(QLabel('測定セット', page))
        self.diagnose_set_combo = QComboBox(page)
        pick_row.addWidget(self.diagnose_set_combo, 1)
        pick_row.addWidget(QLabel('ΔE規格', page))
        self.metric_combo = QComboBox(page)
        for value, label in (
            ('dE2000', 'ΔE2000'),
            ('dEuv1976', "ΔE u'v' (1976)"),
            ('dEICtCp', 'ΔE ITP (ICtCp)'),
        ):
            self.metric_combo.addItem(label, value)
        pick_row.addWidget(self.metric_combo)
        diagnose_button = QPushButton('診断を実行', page)
        diagnose_button.setObjectName('videoDiagnoseRun')
        diagnose_button.clicked.connect(self._run_diagnosis)
        pick_row.addWidget(diagnose_button)
        layout.addLayout(pick_row)

        self.diagnosis_overall = QLabel('未診断', page)
        self.diagnosis_overall.setObjectName('videoDiagnosisOverall')
        set_typography_role(
            self.diagnosis_overall, TypographyRole.SECTION_TITLE
        )
        layout.addWidget(self.diagnosis_overall)

        self.findings_table = QTableWidget(0, 4, page)
        self.findings_table.setObjectName('videoFindingsTable')
        self.findings_table.setHorizontalHeaderLabels(
            ('項目', '判定', '最大ΔE', '説明')
        )
        self.findings_table.horizontalHeader().setStretchLastSection(True)
        self.findings_table.verticalHeader().setVisible(False)
        layout.addWidget(self.findings_table, 1)
        self.pages.addWidget(page)

    def _refresh_diagnose_page(self) -> None:
        self.diagnose_set_combo.blockSignals(True)
        self.diagnose_set_combo.clear()
        for set_id, record in self._measurement_sets.items():
            self.diagnose_set_combo.addItem(
                f'{set_id} — {len(record.samples)} 点', set_id
            )
        self.diagnose_set_combo.blockSignals(False)
        diagnoses = self._ordered_diagnoses()
        if not diagnoses:
            self.diagnosis_overall.setText('未診断')
            self.findings_table.setRowCount(0)
            return
        self._render_diagnosis(diagnoses[-1])

    def _ordered_diagnoses(self) -> list[VideoCommissioningDiagnosis]:
        return sorted(
            self._diagnoses.values(),
            key=lambda d: (d.diagnosed_at_utc, d.diagnosis_id),
        )

    def _render_diagnosis(
        self, diagnosis: VideoCommissioningDiagnosis
    ) -> None:
        self.diagnosis_overall.setText(
            f'診断 {diagnosis.diagnosis_id}: '
            f'{_VERDICT_LABELS.get(diagnosis.overall, diagnosis.overall)}'
        )
        self.findings_table.setRowCount(len(diagnosis.findings))
        for row, finding in enumerate(diagnosis.findings):
            self.findings_table.setItem(
                row,
                0,
                QTableWidgetItem(FINDING_KIND_LABELS[finding.kind]),
            )
            self.findings_table.setItem(
                row,
                1,
                QTableWidgetItem(
                    _VERDICT_LABELS.get(finding.verdict, finding.verdict)
                ),
            )
            worst = finding.deviation
            self.findings_table.setItem(
                row,
                2,
                QTableWidgetItem(
                    f'{worst:.2f}' if worst is not None else '—'
                ),
            )
            self.findings_table.setItem(
                row, 3, QTableWidgetItem(finding.explanation)
            )

    def _run_diagnosis(self) -> None:
        session = self._current_session
        if session is None:
            self._notice('先にセッションを作成してください', error=True)
            return
        set_id = self.diagnose_set_combo.currentData()
        record = (
            self._measurement_sets.get(set_id)
            if set_id is not None
            else None
        )
        if record is None:
            self._notice('測定セットを取り込んでください', error=True)
            return
        target = self._session_target()
        if target is None:
            self._notice('色ターゲットが未設定です', error=True)
            return
        try:
            diagnosis = diagnose_video_measurement(
                session=session,
                measurement_set=record,
                target=target,
                metric_family=self.metric_combo.currentData(),
                diagnosed_at_utc=_utc_now(),
            )
            self.commissioning.save_diagnosis(diagnosis, self.document_id)
        except ValueError as exc:
            warn_user(self, '診断の実行', exc)
            return
        self._notice(
            f'診断 {diagnosis.diagnosis_id}: '
            f'{_VERDICT_LABELS.get(diagnosis.overall, diagnosis.overall)}'
        )
        self.refresh()

    # ------------------------------------------------------------------
    # Actions page — proposals + adjustment records

    def _build_actions_page(self) -> None:
        page = QWidget(self)
        scroll = QScrollArea(self.pages)
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 12, 24, 24)
        layout.setSpacing(10)

        propose_row = QHBoxLayout()
        propose_button = QPushButton('対策案を生成', page)
        propose_button.setObjectName('videoProposeActions')
        propose_button.clicked.connect(self._propose_actions)
        propose_row.addWidget(propose_button)
        propose_row.addStretch(1)
        layout.addLayout(propose_row)

        self.proposals_list = QListWidget(page)
        self.proposals_list.setObjectName('videoProposalsList')
        self.proposals_list.setAccessibleName('調整提案の一覧')
        self.proposals_list.currentRowChanged.connect(
            self._on_proposal_selected
        )
        layout.addWidget(self.proposals_list)

        self.actions_table = QTableWidget(0, 4, page)
        self.actions_table.setObjectName('videoActionsTable')
        self.actions_table.setAccessibleName('推奨される調整操作の一覧')
        self.actions_table.setHorizontalHeaderLabels(
            ('操作', '確度', '説明', '限界')
        )
        self.actions_table.horizontalHeader().setStretchLastSection(True)
        self.actions_table.verticalHeader().setVisible(False)
        layout.addWidget(self.actions_table)

        adjust_group = QGroupBox('実施した調整を記録', page)
        adjust_form = QFormLayout(adjust_group)
        self.adjust_note_edit = QLineEdit(adjust_group)
        self.adjust_note_edit.setPlaceholderText(
            '例: ホワイトバランス High の R を -3'
        )
        adjust_form.addRow('操作メモ', self.adjust_note_edit)
        self.baseline_set_combo = QComboBox(adjust_group)
        adjust_form.addRow('ベースライン測定セット', self.baseline_set_combo)
        self.followup_set_combo = QComboBox(adjust_group)
        self.followup_set_combo.addItem('再測定なし', None)
        adjust_form.addRow('再測定セット', self.followup_set_combo)
        record_button = QPushButton('調整を記録', adjust_group)
        record_button.setObjectName('videoRecordAdjustment')
        record_button.clicked.connect(self._record_adjustment)
        adjust_form.addRow(record_button)
        layout.addWidget(adjust_group)

        history_group = QGroupBox('調整履歴', page)
        history_layout = QVBoxLayout(history_group)
        self.adjustments_list = QListWidget(history_group)
        self.adjustments_list.setObjectName('videoAdjustmentsList')
        history_layout.addWidget(self.adjustments_list)
        layout.addWidget(history_group)
        layout.addStretch(1)
        self.pages.addWidget(scroll)

    def _proposals(self):
        return [
            p
            for p in self.commissioning.list_proposals(self.document_id)
            if self._diagnosis_in_session(p.diagnosis_id)
        ]

    def _diagnosis_in_session(self, diagnosis_id: str) -> bool:
        return diagnosis_id in self._diagnoses

    def _refresh_actions_page(self) -> None:
        self.proposals_list.blockSignals(True)
        self.proposals_list.clear()
        for proposal in self._proposals():
            item = QListWidgetItem(
                f'{proposal.proposal_id} — '
                f'{len(proposal.actions)} 件の提案'
            )
            item.setData(
                Qt.ItemDataRole.UserRole, proposal.proposal_id
            )
            self.proposals_list.addItem(item)
        self.proposals_list.blockSignals(False)
        if self.proposals_list.count():
            self.proposals_list.setCurrentRow(
                self.proposals_list.count() - 1
            )
        else:
            self.actions_table.setRowCount(0)

        diagnoses = self._ordered_diagnoses()
        default_baseline = (
            diagnoses[-1].measurement_set_id if diagnoses else None
        )
        self.baseline_set_combo.blockSignals(True)
        self.baseline_set_combo.clear()
        for set_id in self._measurement_sets:
            self.baseline_set_combo.addItem(set_id, set_id)
        if default_baseline is not None:
            index = self.baseline_set_combo.findData(default_baseline)
            if index >= 0:
                self.baseline_set_combo.setCurrentIndex(index)
        self.baseline_set_combo.blockSignals(False)

        self.followup_set_combo.blockSignals(True)
        self.followup_set_combo.clear()
        self.followup_set_combo.addItem('再測定なし', None)
        for set_id in self._measurement_sets:
            self.followup_set_combo.addItem(set_id, set_id)
        self.followup_set_combo.blockSignals(False)

        self.adjustments_list.clear()
        session = self._current_session
        if session is not None:
            for adjustment in self.commissioning.list_adjustments(
                self.document_id, session.session_id
            ):
                followup = (
                    f' → 再測定 {adjustment.followup_measurement_set_id}'
                    if adjustment.followup_measurement_set_id
                    else ''
                )
                QListWidgetItem(
                    f'#{adjustment.iteration_index} '
                    f'{adjustment.adjustment_id}: '
                    f'{", ".join(ACTION_KIND_LABELS[a] for a in adjustment.selected_actions)}'
                    f'{followup}',
                    self.adjustments_list,
                )

    def _on_proposal_selected(self, row: int) -> None:
        item = self.proposals_list.item(row)
        if item is None:
            return
        proposal = self.commissioning.get_proposal(
            self.document_id, item.data(Qt.ItemDataRole.UserRole)
        )
        if proposal is None:
            return
        self.actions_table.setRowCount(len(proposal.actions))
        for r, action in enumerate(proposal.actions):
            self.actions_table.setItem(
                r, 0, QTableWidgetItem(ACTION_KIND_LABELS[action.kind])
            )
            self.actions_table.setItem(
                r,
                1,
                QTableWidgetItem(_CONFIDENCE_LABELS[action.confidence]),
            )
            self.actions_table.setItem(
                r, 2, QTableWidgetItem(action.explanation)
            )
            self.actions_table.setItem(
                r,
                3,
                QTableWidgetItem(' / '.join(action.limitations) or '—'),
            )

    def _propose_actions(self) -> None:
        diagnoses = self._ordered_diagnoses()
        if not diagnoses:
            self._notice('先に診断を実行してください', error=True)
            return
        diagnosis = diagnoses[-1]
        record = self._measurement_sets.get(diagnosis.measurement_set_id)
        try:
            proposal = propose_video_actions(
                diagnosis,
                session=self._current_session,
                measurement_set=record,
                target=self._session_target(),
                proposed_at_utc=_utc_now(),
            )
            self.commissioning.save_proposal(proposal, self.document_id)
        except ValueError as exc:
            warn_user(self, '対策案の生成', exc)
            return
        self._notice(
            f'対策案 {proposal.proposal_id}: '
            f'{len(proposal.actions)} 件'
        )
        self.refresh()

    def _record_adjustment(self) -> None:
        session = self._current_session
        if session is None:
            self._notice('先にセッションを作成してください', error=True)
            return
        note = self.adjust_note_edit.text().strip()
        diagnoses = self._ordered_diagnoses()
        diagnosis = diagnoses[-1] if diagnoses else None
        if diagnosis is None:
            self._notice('先に診断を実行してください', error=True)
            return
        baseline_id = (
            self.baseline_set_combo.currentData()
            or diagnosis.measurement_set_id
        )
        before = self._measurement_sets.get(baseline_id)
        if before is None:
            self._notice('ベースラインの測定セットが見つかりません', error=True)
            return
        selected_kinds = (
            self._selected_action_kinds()
            or ('remeasure',)
        )
        followup_id = self.followup_set_combo.currentData()
        followup = (
            self._measurement_sets.get(followup_id)
            if followup_id is not None
            else None
        )
        iteration_index = len(
            self.commissioning.list_adjustments(
                self.document_id, session.session_id
            )
        ) + 1
        try:
            adjustment = record_video_operator_adjustment(
                session=session,
                iteration_index=iteration_index,
                before_measurement_set=before,
                selected_actions=selected_kinds,
                diagnosis=diagnosis,
                operator_note=note or None,
                followup_measurement_set=followup,
                recorded_at_utc=_utc_now(),
            )
            self.commissioning.save_adjustment(
                adjustment, self.document_id
            )
        except ValueError as exc:
            warn_user(self, '調整の記録', exc)
            return
        self._notice(f'調整 {adjustment.adjustment_id} を記録しました')
        self.refresh()

    def _selected_action_kinds(self) -> tuple:
        item = self.proposals_list.currentItem()
        proposal = (
            self.commissioning.get_proposal(
                self.document_id, item.data(Qt.ItemDataRole.UserRole)
            )
            if item is not None
            else None
        )
        if proposal is None:
            proposals = self._proposals()
            proposal = proposals[-1] if proposals else None
        if proposal is None:
            return ()
        return tuple(a.kind for a in proposal.actions)

    # ------------------------------------------------------------------
    # Verify page — before/after

    def _build_verify_page(self) -> None:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 12, 24, 24)
        layout.setSpacing(10)

        compare_row = QHBoxLayout()
        compare_button = QPushButton(
            '最新の調整をビフォーアフター比較', page
        )
        compare_button.setObjectName('videoRunComparison')
        compare_button.clicked.connect(self._run_comparison)
        compare_row.addWidget(compare_button)
        quality_map_button = QPushButton(
            'スクリーン品質マップを開く', page
        )
        quality_map_button.setObjectName('videoQualityMapLink')
        quality_map_button.setAccessibleName('スクリーン品質マップを開く')
        quality_map_button.setToolTip(
            '部屋ワークスペースの映像パネルで9点品質マップを表示します'
        )
        # #1003: the quality map lives on the Room 3D viewport — this
        # route deep-links into the Room placement context where the
        # video panel exposes the measured-point overlay.
        quality_map_button.clicked.connect(self._open_quality_map)
        compare_row.addWidget(quality_map_button)
        compare_row.addStretch(1)
        layout.addLayout(compare_row)

        self.comparison_overall = QLabel('未比較', page)
        self.comparison_overall.setObjectName('videoComparisonOverall')
        set_typography_role(
            self.comparison_overall, TypographyRole.SECTION_TITLE
        )
        layout.addWidget(self.comparison_overall)

        self.comparisons_list = QListWidget(page)
        self.comparisons_list.setObjectName('videoComparisonsList')
        self.comparisons_list.currentRowChanged.connect(
            self._on_comparison_selected
        )
        layout.addWidget(self.comparisons_list)

        self.deltas_table = QTableWidget(0, 5, page)
        self.deltas_table.setObjectName('videoDeltasTable')
        self.deltas_table.setAccessibleName('調整前後の指標差分表')
        self.deltas_table.setHorizontalHeaderLabels(
            ('指標', '前', '後', '差分', '方向')
        )
        self.deltas_table.horizontalHeader().setStretchLastSection(True)
        self.deltas_table.verticalHeader().setVisible(False)
        layout.addWidget(self.deltas_table, 1)
        self.pages.addWidget(page)

    def _open_quality_map(self) -> None:
        """#1003 route — deep link to the Room video panel's quality map."""

        if self._on_navigate is not None:
            self._on_navigate(
                WorkspaceDeepLink(WorkspaceId.ROOM, 'placement')
            )

    def _session_comparisons(self):
        session = self._current_session
        if session is None:
            return []
        return list(
            self.commissioning.list_comparisons(
                self.document_id, session.session_id
            )
        )

    def _refresh_verify_page(self) -> None:
        self.comparisons_list.blockSignals(True)
        self.comparisons_list.clear()
        for comparison in self._session_comparisons():
            item = QListWidgetItem(
                f'#{comparison.iteration_index} '
                f'{comparison.comparison_id} — '
                + (
                    _DIRECTION_LABELS[comparison.overall]
                    if comparison.status == 'comparable'
                    else '比較不能'
                )
            )
            item.setData(
                Qt.ItemDataRole.UserRole, comparison.comparison_id
            )
            self.comparisons_list.addItem(item)
        self.comparisons_list.blockSignals(False)
        if self.comparisons_list.count():
            self.comparisons_list.setCurrentRow(
                self.comparisons_list.count() - 1
            )
        else:
            self.comparison_overall.setText('未比較')
            self.deltas_table.setRowCount(0)

    def _on_comparison_selected(self, row: int) -> None:
        item = self.comparisons_list.item(row)
        if item is None:
            return
        comparison = self.commissioning.get_comparison(
            self.document_id, item.data(Qt.ItemDataRole.UserRole)
        )
        if comparison is None:
            return
        if comparison.status == 'incomparable':
            self.comparison_overall.setText(
                '比較不能: '
                + ' / '.join(comparison.incompatibility_reasons)
            )
            self.deltas_table.setRowCount(0)
            return
        self.comparison_overall.setText(
            f'#{comparison.iteration_index} '
            f'{_DIRECTION_LABELS[comparison.overall]}'
        )
        self.deltas_table.setRowCount(len(comparison.rows))
        for r, delta in enumerate(comparison.rows):
            self.deltas_table.setItem(
                r, 0, QTableWidgetItem(delta.label)
            )
            self.deltas_table.setItem(
                r,
                1,
                QTableWidgetItem(
                    '—' if delta.before is None else f'{delta.before:.3f}'
                ),
            )
            self.deltas_table.setItem(
                r,
                2,
                QTableWidgetItem(
                    '—' if delta.after is None else f'{delta.after:.3f}'
                ),
            )
            self.deltas_table.setItem(
                r,
                3,
                QTableWidgetItem(
                    '—' if delta.delta is None else f'{delta.delta:+.3f}'
                ),
            )
            self.deltas_table.setItem(
                r,
                4,
                QTableWidgetItem(_DIRECTION_LABELS[delta.direction]),
            )

    def _run_comparison(self) -> None:
        session = self._current_session
        if session is None:
            self._notice('先にセッションを作成してください', error=True)
            return
        adjustments = self.commissioning.list_adjustments(
            self.document_id, session.session_id
        )
        candidate = next(
            (
                a
                for a in reversed(adjustments)
                if a.followup_measurement_set_id is not None
                and not self._comparison_exists(a.iteration_index)
            ),
            None,
        )
        if candidate is None:
            self._notice(
                '再測定つきの調整記録がありません — '
                '「対策」ページで調整と再測定セットを記録してください',
                error=True,
            )
            return
        before = self._measurement_sets.get(
            candidate.before_measurement_set_id
        )
        after = self._measurement_sets.get(
            candidate.followup_measurement_set_id
        )
        target = self._session_target()
        if before is None or after is None:
            self._notice('調整に紐付く測定セットが見つかりません', error=True)
            return
        if target is None:
            self._notice('色ターゲットが未設定です', error=True)
            return
        try:
            comparison = compare_video_measurements(
                session=session,
                before_set=before,
                after_set=after,
                target=target,
                iteration_index=candidate.iteration_index,
                metric_family=self.metric_combo.currentData(),
                compared_at_utc=_utc_now(),
            )
            self.commissioning.save_comparison(
                comparison, self.document_id
            )
        except ValueError as exc:
            warn_user(self, 'ビフォーアフター比較', exc)
            return
        self._notice(
            f'比較 {comparison.comparison_id}: '
            + (
                _DIRECTION_LABELS[comparison.overall]
                if comparison.status == 'comparable'
                else '比較不能'
            )
        )
        self.refresh()

    def _comparison_exists(self, iteration_index: int) -> bool:
        return any(
            c.iteration_index == iteration_index
            and c.status == 'comparable'
            for c in self._session_comparisons()
        )

    # ------------------------------------------------------------------
    # Report page — the persisted evidence chain

    def _build_report_page(self) -> None:
        page = QWidget(self)
        scroll = QScrollArea(self.pages)
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 12, 24, 24)
        layout.setSpacing(10)

        status_row = QHBoxLayout()
        self.report_status_label = QLabel('セッション未選択', page)
        set_typography_role(
            self.report_status_label, TypographyRole.SECTION_TITLE
        )
        status_row.addWidget(self.report_status_label, 1)
        complete_button = QPushButton('セッションを完了', page)
        complete_button.setObjectName('videoSessionComplete')
        complete_button.clicked.connect(
            lambda: self._transition_session('completed')
        )
        status_row.addWidget(complete_button)
        abandon_button = QPushButton('セッションを中止', page)
        abandon_button.setObjectName('videoSessionAbandon')
        abandon_button.clicked.connect(
            lambda: self._transition_session('abandoned')
        )
        status_row.addWidget(abandon_button)
        layout.addLayout(status_row)

        self.report_summary = QLabel('', page)
        self.report_summary.setObjectName('videoReportSummary')
        self.report_summary.setWordWrap(True)
        layout.addWidget(self.report_summary)

        history_group = QGroupBox('証拠チェーン', page)
        history_layout = QVBoxLayout(history_group)
        self.evidence_list = QListWidget(history_group)
        self.evidence_list.setObjectName('videoEvidenceList')
        history_layout.addWidget(self.evidence_list)
        layout.addWidget(history_group, 1)
        self.pages.addWidget(scroll)

    def _refresh_report_page(self) -> None:
        session = self._current_session
        self.evidence_list.clear()
        if session is None:
            self.report_status_label.setText('セッション未作成')
            self.report_summary.setText('')
            return
        state = self.commissioning.journey_state(
            self.document_id, session.session_id
        )
        status_label = SESSION_STATUS_LABELS[state.current_status]
        readiness_label = (
            READINESS_STATE_LABELS[state.readiness_state]
            if state.readiness_state is not None
            else '未評価'
        )
        self.report_status_label.setText(
            f'{session.session_id} — {status_label}'
        )
        self.report_summary.setText(
            f'対象: {session.surface_entity_id} '
            f'({SURFACE_KIND_LABELS[session.surface_kind]}) / '
            f'モード: {MODE_LABELS[session.mode]} / '
            f'準備状況: {readiness_label} / '
            f'測定 {len(state.measurement_set_ids)} 件 / '
            f'診断 {len(state.diagnosis_ids)} 件 / '
            f'調整 {len(state.adjustment_ids)} 件 / '
            f'比較 {len(state.comparison_ids)} 件'
        )
        entries = [f'セッション {session.session_id} 作成']
        for batch_id in state.import_batch_ids:
            batch = self.commissioning.get_import_batch(
                self.document_id, batch_id
            )
            if batch is not None:
                entries.append(
                    f'取り込み {batch.file_name} '
                    f'({VIDEO_IMPORT_FORMAT_LABELS[batch.format_id]}) → '
                    f'{batch.measurement_set_id}'
                )
        for diagnosis_id in state.diagnosis_ids:
            diagnosis = self._diagnoses.get(diagnosis_id)
            verdict = (
                _VERDICT_LABELS.get(
                    diagnosis.overall, diagnosis.overall
                )
                if diagnosis is not None
                else '?'
            )
            entries.append(f'診断 {diagnosis_id}: {verdict}')
        for proposal in self._proposals():
            entries.append(
                f'対策案 {proposal.proposal_id}: '
                f'{len(proposal.actions)} 件'
            )
        for adjustment_id in state.adjustment_ids:
            entries.append(f'調整記録 {adjustment_id}')
        for comparison_id in state.comparison_ids:
            comparison = self.commissioning.get_comparison(
                self.document_id, comparison_id
            )
            if comparison is not None:
                direction = (
                    _DIRECTION_LABELS[comparison.overall]
                    if comparison.status == 'comparable'
                    else '比較不能'
                )
                entries.append(f'比較 {comparison_id}: {direction}')
        for event in self.commissioning.list_status_events(
            self.document_id, session.session_id
        ):
            entries.append(
                f'状態 {SESSION_STATUS_LABELS[event.from_status]} → '
                f'{SESSION_STATUS_LABELS[event.to_status]}'
            )
        for entry in entries:
            QListWidgetItem(entry, self.evidence_list)

    def _transition_session(
        self, to_status: str
    ) -> None:
        session = self._current_session
        if session is None:
            return
        answer = QMessageBox.question(
            self,
            'セッションの状態遷移',
            f'このセッションを「{SESSION_STATUS_LABELS[to_status]}」にします。',
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.commissioning.record_status_event(
                self.document_id,
                session.session_id,
                to_status,
            )
        except (
            VideoCommissioningConflictError,
            VideoCommissioningIntegrityError,
        ) as exc:
            warn_user(self, 'セッションの状態遷移', exc)
            return
        self._notice(f'セッションを{SESSION_STATUS_LABELS[to_status]}にしました')
        self.refresh()

def build_video_commissioning_mount(
    repository: SceneRepository,
    document_id: str,
    navigate=None,
):
    from .workflow_shell import WorkspaceMount

    page = VideoCommissioningWorkspace(
        repository, document_id, navigate=navigate
    )

    def activate() -> None:
        page.refresh()

    return WorkspaceMount.from_widget(
        page,
        on_activate=activate,
        on_context_changed=page.set_context,
    )


__all__ = [
    'VideoCommissioningWorkspace',
    'build_video_commissioning_mount',
]
