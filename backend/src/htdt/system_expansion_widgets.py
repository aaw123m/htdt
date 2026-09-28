from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_scene import is_unassigned_speaker_role
from .cad_topology_search import PlacementAngleAxis
from .native_worker import WORKER_CANCELLED, NativeWorkerPool
from .system_expansion_workflow import (
    MeasurementPlanOptions,
    ProposalEquipmentChange,
    ProposalLinkInput,
    ProposalSpeakerInput,
    SystemExpansionWorkflowService,
)
from .ui_theme import (
    ControlSize,
    SemanticState,
    SurfaceRole,
    TypographyRole,
    set_control_size,
    set_primary_action,
    set_semantic_state,
    set_surface_role,
    set_typography_role,
)
from .user_facing_error import warn_user
from .user_facing_error import operation_error_message


# Inspector suggestion list only — not a persisted enum. Custom roles remain
# free-form text and are stored verbatim. Kept aligned with the Room inspector
# role picker in room_workspace.SPEAKER_ROLE_SUGGESTIONS.
PROPOSED_ROLE_SUGGESTIONS = (
    "FL",
    "C",
    "FR",
    "SL",
    "SR",
    "SBL",
    "SBR",
    "SUB",
    "TFL",
    "TFR",
    "TML",
    "TMR",
    "TRL",
    "TRR",
)

# UI-facing labels for the existing O100B LinkRelation authority. "pair_mirror"
# is a presentation shortcut that expands to the canonical
# mirror_x + equal_y + equal_z rule set; no positions are synthesized here.
_LINK_RELATION_CHOICES: tuple[tuple[str, str], ...] = (
    ("pair_mirror", "左右ミラーペア (Xミラー + Y/Z同一)"),
    ("mirror_x", "X軸ミラー"),
    ("equal_x", "X座標を同一"),
    ("equal_y", "Y座標を同一"),
    ("equal_z", "高さZを同一"),
    ("equal_delta_x", "X変位を同一"),
    ("equal_delta_y", "Y変位を同一"),
    ("equal_delta_z", "高さZ変位を同一"),
)
_MIRROR_RELATIONS = {"pair_mirror", "mirror_x"}


def _metric_field(*, minimum: float = -1000.0) -> QDoubleSpinBox:
    field = QDoubleSpinBox()
    field.setRange(minimum, 1000.0)
    field.setDecimals(3)
    field.setSingleStep(0.1)
    return field


def _degree_field(minimum: float = -180.0, maximum: float = 180.0) -> QDoubleSpinBox:
    field = QDoubleSpinBox()
    field.setRange(minimum, maximum)
    field.setDecimals(1)
    field.setSingleStep(1.0)
    return field


def _field_pair(first: QWidget, second: QWidget) -> QWidget:
    host = QWidget()
    row = QHBoxLayout(host)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(4)
    row.addWidget(first, 1)
    row.addWidget(second, 1)
    return host


class _ProposalSpeakerRow(QFrame):
    """One add-speaker row inside the topology proposal builder."""

    removeRequested = Signal(object)
    roleEdited = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        set_surface_role(self, SurfaceRole.BASE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        self.role_combo = QComboBox()
        self.role_combo.setEditable(True)
        self.role_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.role_combo.addItems(PROPOSED_ROLE_SUGGESTIONS)
        self.role_combo.setCurrentIndex(-1)
        role_edit = self.role_combo.lineEdit()
        if role_edit is not None:
            role_edit.setPlaceholderText("役割 (例: SL)")
        self.role_combo.currentTextChanged.connect(
            lambda _text: self.roleEdited.emit()
        )
        self.equipment_combo = QComboBox()
        self.remove_button = QPushButton("削除")
        set_control_size(self.remove_button, ControlSize.COMPACT)
        self.remove_button.setToolTip("この追加スピーカー行を提案から外す")
        self.remove_button.clicked.connect(
            lambda: self.removeRequested.emit(self)
        )
        header.addWidget(self.role_combo, 1)
        header.addWidget(self.equipment_combo, 1)
        header.addWidget(self.remove_button)
        layout.addLayout(header)

        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setContentsMargins(0, 0, 0, 0)
        self.zone_name = QLineEdit("設置エリア")
        self.min_x = _metric_field()
        self.max_x = _metric_field()
        self.min_y = _metric_field()
        self.max_y = _metric_field()
        self.min_z = _metric_field()
        self.max_z = _metric_field()
        form.addRow("設置可能領域", self.zone_name)
        form.addRow("X 最小/最大 m", _field_pair(self.min_x, self.max_x))
        form.addRow("Y 最小/最大 m", _field_pair(self.min_y, self.max_y))
        form.addRow("高さZ 最小/最大 m", _field_pair(self.min_z, self.max_z))
        layout.addLayout(form)

        self.advanced_button = QPushButton("詳細")
        self.advanced_button.setCheckable(True)
        set_control_size(self.advanced_button, ControlSize.COMPACT)
        self.advanced_area = QWidget()
        advanced = QFormLayout(self.advanced_area)
        advanced.setContentsMargins(0, 0, 0, 0)
        self.optional_check = QCheckBox("optional role として扱う")
        self.aim_enabled = QCheckBox("aim yaw 範囲を探索")
        self.aim_min = _degree_field()
        self.aim_max = _degree_field()
        self.aim_min.setValue(-15.0)
        self.aim_max.setValue(15.0)
        self.aim_step = _degree_field(minimum=0.5, maximum=180.0)
        self.aim_step.setValue(5.0)
        advanced.addRow(self.optional_check)
        advanced.addRow(self.aim_enabled)
        advanced.addRow(
            "aim yaw 最小/最大 deg",
            _field_pair(self.aim_min, self.aim_max),
        )
        advanced.addRow("aim yaw 刻み deg", self.aim_step)
        self.advanced_area.hide()
        self.advanced_button.toggled.connect(self.advanced_area.setVisible)
        layout.addWidget(self.advanced_button)
        layout.addWidget(self.advanced_area)

    def role_text(self) -> str:
        return self.role_combo.currentText().strip()

    def set_zone(
        self,
        zone_name: str,
        min_x: float,
        max_x: float,
        min_y: float,
        max_y: float,
        min_z: float,
        max_z: float,
    ) -> None:
        self.zone_name.setText(zone_name)
        self.min_x.setValue(min_x)
        self.max_x.setValue(max_x)
        self.min_y.setValue(min_y)
        self.max_y.setValue(max_y)
        self.min_z.setValue(min_z)
        self.max_z.setValue(max_z)

    def draft(self, step_m: float) -> ProposalSpeakerInput:
        data = self.equipment_combo.currentData()
        angle_axes: tuple[PlacementAngleAxis, ...] = ()
        if self.aim_enabled.isChecked():
            angle_axes = (
                PlacementAngleAxis(
                    parameter="aim_yaw_deg",
                    min_deg=self.aim_min.value(),
                    max_deg=self.aim_max.value(),
                    step_deg=self.aim_step.value(),
                ),
            )
        return ProposalSpeakerInput(
            role_id=self.role_combo.currentText(),
            equipment_sha256="" if data is None else str(data),
            zone_name=self.zone_name.text(),
            min_x_m=self.min_x.value(),
            max_x_m=self.max_x.value(),
            min_y_m=self.min_y.value(),
            max_y_m=self.max_y.value(),
            min_z_m=self.min_z.value(),
            max_z_m=self.max_z.value(),
            step_m=step_m,
            optional_role=self.optional_check.isChecked(),
            angle_axes=angle_axes,
        )


class _ProposalLinkRow(QFrame):
    """One linked-placement rule between two proposed speaker rows."""

    removeRequested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        set_surface_role(self, SurfaceRole.BASE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)

        relation_row = QHBoxLayout()
        relation_row.setContentsMargins(0, 0, 0, 0)
        relation_row.setSpacing(4)
        self.master_combo = QComboBox()
        self.relation_combo = QComboBox()
        for code, label in _LINK_RELATION_CHOICES:
            self.relation_combo.addItem(label, code)
        self.slave_combo = QComboBox()
        relation_row.addWidget(self.master_combo, 1)
        relation_row.addWidget(self.relation_combo, 2)
        relation_row.addWidget(self.slave_combo, 1)
        layout.addLayout(relation_row)

        option_row = QHBoxLayout()
        option_row.setContentsMargins(0, 0, 0, 0)
        option_row.setSpacing(4)
        self.mirror_center = QCheckBox("部屋中央でミラー")
        self.mirror_center.setChecked(True)
        self.mirror_axis = QDoubleSpinBox()
        self.mirror_axis.setRange(-1000.0, 1000.0)
        self.mirror_axis.setDecimals(3)
        self.mirror_axis.setSingleStep(0.1)
        self.mirror_axis.setSuffix(" m")
        self.mirror_axis.setEnabled(False)
        self.mirror_center.toggled.connect(
            lambda checked: self.mirror_axis.setEnabled(not checked)
        )
        self.remove_button = QPushButton("削除")
        set_control_size(self.remove_button, ControlSize.COMPACT)
        self.remove_button.clicked.connect(
            lambda: self.removeRequested.emit(self)
        )
        option_row.addWidget(self.mirror_center)
        option_row.addWidget(self.mirror_axis)
        option_row.addStretch(1)
        option_row.addWidget(self.remove_button)
        layout.addLayout(option_row)

        self.relation_combo.currentIndexChanged.connect(self._sync_relation)
        self._sync_relation()

    def _sync_relation(self) -> None:
        mirrored = self.relation_combo.currentData() in _MIRROR_RELATIONS
        self.mirror_center.setVisible(mirrored)
        self.mirror_axis.setVisible(mirrored)

    def set_rows(self, rows: list[_ProposalSpeakerRow]) -> None:
        for combo in (self.master_combo, self.slave_combo):
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for index, row in enumerate(rows):
                combo.addItem(row.role_text() or f"行 {index + 1}", row)
            if current in rows:
                combo.setCurrentIndex(rows.index(current))
            combo.blockSignals(False)

    def rules(self) -> tuple[ProposalLinkInput, ...]:
        master_row = self.master_combo.currentData()
        slave_row = self.slave_combo.currentData()
        if master_row is None or slave_row is None:
            return ()
        master = master_row.role_text()
        slave = slave_row.role_text()
        code = str(self.relation_combo.currentData())
        mirror_axis = (
            None if self.mirror_center.isChecked() else self.mirror_axis.value()
        )
        if code == "pair_mirror":
            return (
                ProposalLinkInput(
                    master_role_id=master,
                    slave_role_id=slave,
                    relation="mirror_x",
                    mirror_axis_x_m=mirror_axis,
                ),
                ProposalLinkInput(
                    master_role_id=master,
                    slave_role_id=slave,
                    relation="equal_y",
                ),
                ProposalLinkInput(
                    master_role_id=master,
                    slave_role_id=slave,
                    relation="equal_z",
                ),
            )
        return (
            ProposalLinkInput(
                master_role_id=master,
                slave_role_id=slave,
                relation=code,  # type: ignore[arg-type]
                mirror_axis_x_m=(
                    mirror_axis if code == "mirror_x" else None
                ),
            ),
        )


class _VariantSelector(QWidget):
    changed = Signal(str)

    def __init__(self, service: SystemExpansionWorkflowService, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.combo = QComboBox()
        self.combo.setAccessibleName("システムバリアント提案")
        self.refresh_button = QPushButton("更新")
        set_control_size(self.refresh_button, ControlSize.COMPACT)
        row.addWidget(QLabel("提案"))
        row.addWidget(self.combo, 1)
        row.addWidget(self.refresh_button)
        self.refresh_button.clicked.connect(self.refresh)
        self.combo.currentIndexChanged.connect(self._emit)
        self.refresh()

    def refresh(self) -> None:
        selected = self.current_variant_id()
        self.combo.blockSignals(True)
        self.combo.clear()
        for variant in self.service.variants():
            # Human-readable name is the primary UI identity; internal IDs stay in userData.
            self.combo.addItem(variant.name, variant.variant_id)
        if selected is not None:
            index = self.combo.findData(selected)
            if index >= 0:
                self.combo.setCurrentIndex(index)
        self.combo.blockSignals(False)
        self._emit()

    def current_variant_id(self) -> str | None:
        value = self.combo.currentData()
        return None if value is None else str(value)

    def select_variant(self, variant_id: str) -> bool:
        index = self.combo.findData(variant_id)
        if index < 0:
            return False
        self.combo.setCurrentIndex(index)
        return True

    def _emit(self, *_args) -> None:
        variant_id = self.current_variant_id()
        if variant_id is not None:
            self.changed.emit(variant_id)


class SystemExpansionRoomPanel(QFrame):
    """Room / speaker-placement presentation for proposed SystemVariants."""

    variantChanged = Signal(str)
    ghostEntityRequested = Signal(str)

    def __init__(
        self,
        service: SystemExpansionWorkflowService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self._pool = NativeWorkerPool(self)
        self._equipment_available = False
        self.setMinimumWidth(0)
        self.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        set_surface_role(self, SurfaceRole.RAISED)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("システム拡張")
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)
        intro = QLabel(
            "現在の構成と提案を分けて確認します。提案は未設置として表示し、"
            "現在の部屋には反映しません。"
        )
        intro.setWordWrap(True)
        set_typography_role(intro, TypographyRole.SECONDARY)
        layout.addWidget(intro)

        self.current_label = QLabel()
        self.current_label.setWordWrap(True)
        layout.addWidget(self.current_label)

        author = QFrame()
        set_surface_role(author, SurfaceRole.BASE)
        author_layout = QVBoxLayout(author)
        author_layout.setContentsMargins(0, 6, 0, 8)
        author_layout.setSpacing(8)
        author_title = QLabel("トポロジー提案ビルダー")
        set_typography_role(author_title, TypographyRole.SECTION_TITLE)
        author_layout.addWidget(author_title)
        author_note = QLabel(
            "1つの提案に複数の追加スピーカー・既存スピーカーの削除・機器変更と"
            "左右連動ルールをまとめ、1つのSystemVariantとして配置候補を作成します。"
            "内部IDやSHAの入力は不要です。"
        )
        author_note.setWordWrap(True)
        set_typography_role(author_note, TypographyRole.SECONDARY)
        author_layout.addWidget(author_note)

        self.proposal_name = QLineEdit()
        self.proposal_name.setPlaceholderText("例: 5.0.2 A")
        self.zone_step = _metric_field(minimum=0.01)
        self.zone_step.setValue(0.25)

        proposal_form = QFormLayout()
        proposal_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        proposal_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        proposal_form.addRow("提案名", self.proposal_name)
        proposal_form.addRow("探索刻み m", self.zone_step)
        author_layout.addLayout(proposal_form)

        speaker_header = QHBoxLayout()
        speaker_header.setContentsMargins(0, 0, 0, 0)
        speaker_header.setSpacing(4)
        speaker_title = QLabel("追加スピーカー / チャンネル")
        speaker_header.addWidget(speaker_title, 1)
        self.add_speaker_button = QPushButton("＋ 行を追加")
        set_control_size(self.add_speaker_button, ControlSize.COMPACT)
        self.add_speaker_button.setToolTip("追加スピーカー行を1つ追加")
        self.add_speaker_button.clicked.connect(
            lambda _checked=False: self._add_speaker_row()
        )
        self.add_pair_button = QPushButton("＋ サラウンドペア")
        set_control_size(self.add_pair_button, ControlSize.COMPACT)
        self.add_pair_button.setToolTip(
            "SL/SRの2行と左右ミラー連動をまとめて追加"
        )
        self.add_pair_button.clicked.connect(self._add_surround_pair)
        speaker_header.addWidget(self.add_speaker_button)
        speaker_header.addWidget(self.add_pair_button)
        author_layout.addLayout(speaker_header)

        self.speaker_rows_host = QWidget()
        self.speaker_rows_layout = QVBoxLayout(self.speaker_rows_host)
        self.speaker_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.speaker_rows_layout.setSpacing(6)
        author_layout.addWidget(self.speaker_rows_host)

        self.link_title = QLabel("連動ルール (追加スピーカー同士)")
        self.link_title.setWordWrap(True)
        author_layout.addWidget(self.link_title)
        self.link_rows_host = QWidget()
        self.link_rows_layout = QVBoxLayout(self.link_rows_host)
        self.link_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.link_rows_layout.setSpacing(6)
        author_layout.addWidget(self.link_rows_host)
        self.add_link_button = QPushButton("＋ 連動ルールを追加")
        set_control_size(self.add_link_button, ControlSize.COMPACT)
        self.add_link_button.setToolTip(
            "追加スピーカー同士の連動 (ミラー / 同一座標 / 同一変位) を追加"
        )
        self.add_link_button.clicked.connect(
            lambda _checked=False: self._add_link_row()
        )
        author_layout.addWidget(self.add_link_button)

        self.existing_button = QPushButton("既存スピーカーの削除 / 機器変更")
        self.existing_button.setCheckable(True)
        set_control_size(self.existing_button, ControlSize.COMPACT)
        self.existing_area = QWidget()
        self.existing_layout = QVBoxLayout(self.existing_area)
        self.existing_layout.setContentsMargins(0, 0, 0, 0)
        self.existing_layout.setSpacing(4)
        self._existing_rows: list[tuple[str, QCheckBox, QComboBox]] = []
        self.existing_area.hide()
        self.existing_button.toggled.connect(self.existing_area.setVisible)
        author_layout.addWidget(self.existing_button)
        author_layout.addWidget(self.existing_area)

        create_row = QHBoxLayout()
        create_row.setContentsMargins(0, 0, 0, 0)
        self.create_proposal_button = QPushButton("提案を作成")
        self.create_proposal_button.setToolTip(
            "提案と配置候補を既存のO100B探索authorityで作成"
        )
        set_primary_action(self.create_proposal_button)
        self.create_proposal_button.clicked.connect(self._create_proposal)
        create_row.addWidget(self.create_proposal_button)
        self.cancel_proposal_button = QPushButton("作成を中止")
        self.cancel_proposal_button.setEnabled(False)
        self.cancel_proposal_button.clicked.connect(self._cancel_proposal)
        create_row.addWidget(self.cancel_proposal_button)
        create_row.addStretch(1)
        author_layout.addLayout(create_row)
        library_row = QHBoxLayout()
        library_row.setContentsMargins(0, 0, 0, 0)
        library_row.setSpacing(6)
        self.library_button = QPushButton("機器・音源ライブラリ…")
        self.library_button.setToolTip(
            "機器定義の作成・新バージョン発行・指向性インポート"
        )
        self.library_button.clicked.connect(self._open_equipment_library)
        library_row.addWidget(self.library_button)
        self.playback_button = QPushButton("再生チェーン / ヘッドルーム…")
        self.playback_button.setToolTip(
            "アンプ出力能力・スピーカー負荷・ルーティングの作成と評価"
        )
        self.playback_button.clicked.connect(self._open_playback_chain)
        library_row.addWidget(self.playback_button)
        library_row.addStretch(1)
        author_layout.addLayout(library_row)
        self.authoring_status = QLabel()
        self.authoring_status.setWordWrap(True)
        author_layout.addWidget(self.authoring_status)
        layout.addWidget(author)

        self.selector = _VariantSelector(service)
        self.selector.changed.connect(self._variant_changed)
        layout.addWidget(self.selector)

        self.lifecycle_badge = QLabel("提案")
        set_typography_role(self.lifecycle_badge, TypographyRole.BODY)
        self.lifecycle_badge.setContentsMargins(8, 4, 8, 4)
        layout.addWidget(self.lifecycle_badge)
        self.lifecycle_label = QLabel("提案がありません")
        self.lifecycle_label.setWordWrap(True)
        set_typography_role(self.lifecycle_label, TypographyRole.SECONDARY)
        layout.addWidget(self.lifecycle_label)

        self.entity_tree = QTreeWidget()
        self.entity_tree.setMinimumWidth(0)
        self.entity_tree.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Expanding,
        )
        self.entity_tree.setHeaderLabels(
            ["追加スピーカー / チャンネル", "役割", "状態", "機器", "設置可能領域", "理由"]
        )
        header = self.entity_tree.header()
        header.setMinimumSectionSize(0)
        for index in range(self.entity_tree.columnCount()):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.Stretch)
        self.entity_tree.itemSelectionChanged.connect(self._entity_selected)
        layout.addWidget(self.entity_tree, 1)

        self.advanced_button = QPushButton("詳細を表示")
        self.advanced_button.setCheckable(True)
        set_control_size(self.advanced_button, ControlSize.COMPACT)
        self.advanced_label = QLabel()
        self.advanced_label.setWordWrap(True)
        self.advanced_label.hide()
        self.advanced_button.toggled.connect(self._toggle_advanced)
        layout.addWidget(self.advanced_button)
        layout.addWidget(self.advanced_label)
        # The happy path stays a single-speaker row; more rows, linked rules,
        # and removals are progressive disclosure on top of it.
        self._add_speaker_row()
        self.refresh()

    def _speaker_rows(self) -> list[_ProposalSpeakerRow]:
        rows: list[_ProposalSpeakerRow] = []
        for index in range(self.speaker_rows_layout.count()):
            widget = self.speaker_rows_layout.itemAt(index).widget()
            if isinstance(widget, _ProposalSpeakerRow):
                rows.append(widget)
        return rows

    def _link_rows(self) -> list[_ProposalLinkRow]:
        rows: list[_ProposalLinkRow] = []
        for index in range(self.link_rows_layout.count()):
            widget = self.link_rows_layout.itemAt(index).widget()
            if isinstance(widget, _ProposalLinkRow):
                rows.append(widget)
        return rows

    def _default_zone(self) -> tuple[str, float, float, float, float, float, float] | None:
        latest = self.service.scene_repository.current_head(self.service.document_id)
        if latest is None or latest.document.room is None:
            return None
        min_x, min_y, max_x, max_y = latest.document.room.bounds_m
        z_m = min(latest.document.room.height_m, 1.2)
        return ("設置エリア", min_x, max_x, min_y, max_y, z_m, z_m)

    def _add_speaker_row(
        self,
        *,
        role: str = "",
        zone: tuple[str, float, float, float, float, float, float] | None = None,
    ) -> _ProposalSpeakerRow:
        row = _ProposalSpeakerRow()
        if role:
            row.role_combo.setCurrentText(role)
        if zone is None:
            zone = self._default_zone()
        if zone is not None:
            row.set_zone(*zone)
        row.removeRequested.connect(self._remove_speaker_row)
        row.roleEdited.connect(self._refresh_link_rows)
        self.speaker_rows_layout.addWidget(row)
        self._refresh_equipment()
        self._refresh_link_rows()
        return row

    def _remove_speaker_row(self, row: _ProposalSpeakerRow) -> None:
        self.speaker_rows_layout.removeWidget(row)
        row.deleteLater()
        self._refresh_link_rows()

    def _add_surround_pair(self) -> None:
        # Blank untouched rows only get in the way of the pair shortcut; a row
        # with no role would fail validation anyway.
        for row in self._speaker_rows():
            if not row.role_text():
                self._remove_speaker_row(row)
        zone = self._default_zone()
        left_zone = right_zone = None
        if zone is not None:
            _name, min_x, max_x, min_y, max_y, min_z, max_z = zone
            width = max_x - min_x
            depth = max_y - min_y
            left_zone = (
                "左側面 (SL)",
                min_x,
                min_x + width * 0.3,
                min_y + depth * 0.45,
                max_y,
                min_z,
                max_z,
            )
            right_zone = (
                "右側面 (SR)",
                max_x - width * 0.3,
                max_x,
                min_y + depth * 0.45,
                max_y,
                min_z,
                max_z,
            )
        left = self._add_speaker_row(role="SL", zone=left_zone)
        right = self._add_speaker_row(role="SR", zone=right_zone)
        link = self._add_link_row()
        master_index = link.master_combo.findText("SL")
        if master_index < 0:
            master_index = self._speaker_rows().index(left)
        slave_index = link.slave_combo.findText("SR")
        if slave_index < 0:
            slave_index = self._speaker_rows().index(right)
        link.master_combo.setCurrentIndex(master_index)
        link.slave_combo.setCurrentIndex(slave_index)
        mirror_index = link.relation_combo.findData("pair_mirror")
        if mirror_index >= 0:
            link.relation_combo.setCurrentIndex(mirror_index)

    def _add_link_row(self) -> _ProposalLinkRow:
        row = _ProposalLinkRow()
        row.set_rows(self._speaker_rows())
        row.removeRequested.connect(self._remove_link_row)
        self.link_rows_layout.addWidget(row)
        return row

    def _remove_link_row(self, row: _ProposalLinkRow) -> None:
        self.link_rows_layout.removeWidget(row)
        row.deleteLater()

    def _refresh_link_rows(self) -> None:
        rows = self._speaker_rows()
        for link in self._link_rows():
            link.set_rows(rows)
        multi = len(rows) >= 2
        self.link_title.setVisible(multi)
        self.link_rows_host.setVisible(multi)
        self.add_link_button.setVisible(multi)

    def _refresh_equipment(self) -> None:
        choices = self.service.equipment_choices()
        for row in self._speaker_rows():
            selected = row.equipment_combo.currentData()
            row.equipment_combo.blockSignals(True)
            row.equipment_combo.clear()
            for semantic_sha256, label in choices:
                row.equipment_combo.addItem(label, semantic_sha256)
            if selected is not None:
                index = row.equipment_combo.findData(selected)
                if index >= 0:
                    row.equipment_combo.setCurrentIndex(index)
            row.equipment_combo.blockSignals(False)
        for _role, _remove, combo in self._existing_rows:
            selected = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("機器変更なし", None)
            for semantic_sha256, label in choices:
                combo.addItem(label, semantic_sha256)
            if selected is not None:
                index = combo.findData(selected)
                if index >= 0:
                    combo.setCurrentIndex(index)
            combo.blockSignals(False)
        self._equipment_available = bool(choices)
        self._refresh_run_state()
        if not choices:
            self.authoring_status.setText(
                "機器 / 音源モデルがありません。先に機器定義を登録してください。"
            )

    def _refresh_existing_ops(self) -> None:
        while self.existing_layout.count():
            item = self.existing_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._existing_rows = []
        latest = self.service.scene_repository.current_head(self.service.document_id)
        if latest is None:
            return
        choices = self.service.equipment_choices()
        for entity in latest.document.entities:
            if entity.kind != "speaker":
                continue
            role = entity.speaker_role or ""
            label_role = "未設定" if is_unassigned_speaker_role(role) else role
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(4)
            name = QLabel(f"{entity.name} / {label_role}")
            name.setToolTip(entity.entity_id)
            remove_check = QCheckBox("削除")
            equipment = QComboBox()
            equipment.addItem("機器変更なし", None)
            for semantic_sha256, label in choices:
                equipment.addItem(label, semantic_sha256)
            remove_check.toggled.connect(
                lambda checked, combo=equipment: combo.setEnabled(not checked)
            )
            line.addWidget(name, 1)
            line.addWidget(remove_check)
            line.addWidget(equipment, 1)
            self.existing_layout.addWidget(row)
            self._existing_rows.append((role, remove_check, equipment))

    def _existing_ops(
        self,
    ) -> tuple[list[str], list[ProposalEquipmentChange]]:
        removes: list[str] = []
        overrides: list[ProposalEquipmentChange] = []
        for role, remove_check, equipment in self._existing_rows:
            if remove_check.isChecked():
                removes.append(role)
            elif equipment.currentData() is not None:
                overrides.append(
                    ProposalEquipmentChange(
                        role_id=role,
                        equipment_sha256=str(equipment.currentData()),
                    )
                )
        return removes, overrides

    def _initialize_zone_from_room(self) -> None:
        zone = self._default_zone()
        if zone is None:
            return
        for row in self._speaker_rows():
            if (
                row.min_x.value() == 0.0
                and row.max_x.value() == 0.0
                and row.min_y.value() == 0.0
                and row.max_y.value() == 0.0
                and row.min_z.value() == 0.0
                and row.max_z.value() == 0.0
            ):
                row.set_zone(*zone)

    def _create_proposal(self) -> None:
        rows = self._speaker_rows()
        if not rows:
            self.authoring_status.setText(
                "追加するスピーカー行を1つ以上作成してください。"
            )
            return
        drafts = [row.draft(self.zone_step.value()) for row in rows]
        links: list[ProposalLinkInput] = []
        for link_row in self._link_rows():
            links.extend(link_row.rules())
        removes, overrides = self._existing_ops()
        proposal_name = self.proposal_name.text()
        self.authoring_status.setText("配置候補を生成しています…")
        self._refresh_run_state()
        self._pool.start(
            'create_topology_proposal',
            lambda cancel_event: self.service.create_topology_proposal(
                proposal_name=proposal_name,
                speakers=drafts,
                linked_rules=links,
                remove_role_ids=removes,
                equipment_overrides=overrides,
                max_returned_candidates=24,
                is_cancelled=cancel_event.is_set,
            ),
            self._proposal_completed,
            on_finished=lambda _key: self._refresh_run_state(),
        )

    def _proposal_completed(self, key, result, error) -> None:
        if error == WORKER_CANCELLED:
            self.authoring_status.setText(
                "提案の作成を中止しました"
            )
            return
        if error is not None:
            self.authoring_status.setText(
                f"作成できません: {operation_error_message(error)}"
            )
            return
        self.authoring_status.setText(
            f"提案を保存しました。配置候補 {len(result.candidate_variant_ids)} 件を"
            f"保存（feasible {result.feasible_candidate_count} 件）。"
        )
        self.selector.refresh()
        self.selector.select_variant(result.template_variant_id)

    def _cancel_proposal(self) -> None:
        self._pool.cancel('create_topology_proposal')

    def is_running(self) -> bool:
        return self._pool.active_count > 0

    def dispose(self) -> None:
        self._pool.shutdown()

    def _refresh_run_state(self) -> None:
        running = self.is_running()
        self.create_proposal_button.setEnabled(
            self._equipment_available and not running
        )
        self.cancel_proposal_button.setEnabled(running)

    def refresh(self) -> None:
        self._refresh_equipment()
        self._initialize_zone_from_room()
        self._refresh_existing_ops()
        self._refresh_link_rows()
        latest = self.service.scene_repository.current_head(self.service.document_id)
        if latest is None:
            self.current_label.setText("現在構成: SceneRevisionがありません")
        else:
            speakers = [
                entity for entity in latest.document.entities
                if entity.kind == "speaker"
            ]
            roles = [
                (
                    item.speaker_role
                    if item.speaker_role
                    and not is_unassigned_speaker_role(item.speaker_role)
                    else "未設定"
                )
                for item in speakers
            ]
            summary = " / ".join(roles) if roles else "speakerなし"
            self.current_label.setText(f"現在構成: {summary}")
        self.selector.refresh()
        current = self.selector.current_variant_id()
        if current is not None:
            self._show_variant(current)
        else:
            self.entity_tree.clear()
            self.lifecycle_label.setText("保存済みのSystemVariant提案がありません。")

    def current_variant_id(self) -> str | None:
        return self.selector.current_variant_id()

    def _open_equipment_library(self) -> None:
        from .equipment_library import (
            EquipmentLibraryDialog,
            EquipmentLibraryService,
        )

        service = EquipmentLibraryService(
            self.service.scene_repository,
            self.service.variant_repository,
        )
        dialog = EquipmentLibraryDialog(service, parent=self)
        dialog.definitionsChanged.connect(self._refresh_equipment)
        dialog.exec()
        self._refresh_equipment()

    def _open_playback_chain(self) -> None:
        from .playback_chain_widgets import (
            PlaybackChainDialog,
            PlaybackChainService,
        )

        service = PlaybackChainService(
            self.service.scene_repository,
            self.service.document_id,
        )
        dialog = PlaybackChainDialog(service, parent=self)
        dialog.exec()

    def _variant_changed(self, variant_id: str) -> None:
        self._show_variant(variant_id)
        self.variantChanged.emit(variant_id)

    def _show_variant(self, variant_id: str) -> None:
        view = self.service.variant_presentation(variant_id)
        self.lifecycle_badge.setText(view.lifecycle.label)
        set_semantic_state(
            self.lifecycle_badge,
            {
                "current": SemanticState.SELECTED,
                "proposed": SemanticState.WARNING,
                "as_built": SemanticState.SUCCESS,
                "measured": SemanticState.SUCCESS,
            }[view.lifecycle.state],
        )
        state = view.lifecycle.validation_label
        if view.stale and view.stale_reason:
            state += f" / 要再評価: {view.stale_reason}"
        self.lifecycle_label.setText(state)
        self.entity_tree.clear()
        for entity in view.entities:
            row = QTreeWidgetItem(
                [
                    entity.name,
                    entity.role or "未設定",
                    entity.lifecycle_label,
                    entity.equipment,
                    entity.install_zone,
                    entity.reason or "",
                ]
            )
            # Selection metadata is not rendered as a standard field.
            ghost = next(
                (
                    item for item in self.service.ghost_preview(variant_id)
                    if item.name == entity.name and item.speaker_role == entity.role
                ),
                None,
            )
            if ghost is not None:
                row.setData(0, 0x0100, ghost.entity_id)
            self.entity_tree.addTopLevelItem(row)
        p = view.advanced
        self.advanced_label.setText(
            "\n".join(
                (
                    f"variant_id: {p.variant_id}",
                    f"variant_sha256: {p.variant_sha256}",
                    f"baseline_revision_id: {p.baseline_revision_id}",
                    f"baseline_content_hash: {p.baseline_content_hash}",
                    f"schema_version: {p.schema_version}",
                    f"authority_version: {p.authority_version}",
                    "リポジトリ/出典キー: " + (", ".join(p.repository_keys) or "なし"),
                )
            )
        )

    def _entity_selected(self) -> None:
        rows = self.entity_tree.selectedItems()
        if not rows:
            return
        entity_id = rows[0].data(0, 0x0100)
        if entity_id:
            self.ghostEntityRequested.emit(str(entity_id))

    def _toggle_advanced(self, checked: bool) -> None:
        self.advanced_button.setText("詳細を隠す" if checked else "詳細を表示")
        self.advanced_label.setVisible(checked)


class SystemExpansionOptimizePanel(QFrame):
    robustnessRequested = Signal(str)
    applied = Signal(str)

    def __init__(
        self,
        service: SystemExpansionWorkflowService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self._pool = NativeWorkerPool(self)
        set_surface_role(self, SurfaceRole.RAISED)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("SystemVariant 比較")
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)
        note = QLabel(
            "coverage / SPL・headroom / Standards / objectiveを独立表示します。"
            "比較できない値は順位付けせず、理由を表示します。"
        )
        note.setWordWrap(True)
        set_typography_role(note, TypographyRole.SECONDARY)
        layout.addWidget(note)

        self.selector = _VariantSelector(service)
        layout.addWidget(self.selector)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(
            [
                "candidate",
                "状態",
                "coverage",
                "SPL/headroom",
                "規格",
                "比較",
                "Pareto",
                "理由",
            ]
        )
        layout.addWidget(self.tree, 1)

        self.advanced_button = QPushButton("詳細を表示")
        self.advanced_button.setCheckable(True)
        set_control_size(self.advanced_button, ControlSize.COMPACT)
        self.advanced_label = QLabel()
        self.advanced_label.setWordWrap(True)
        self.advanced_label.hide()
        self.advanced_button.toggled.connect(self._toggle_advanced)
        layout.addWidget(self.advanced_button)
        layout.addWidget(self.advanced_label)

        actions = QHBoxLayout()
        self.evaluate_button = QPushButton("提案を評価 / 比較を更新")
        self.cancel_button = QPushButton("評価を中止")
        self.cancel_button.setEnabled(False)
        self.robustness_button = QPushButton("ばらつき耐性を確認")
        self.apply_button = QPushButton("この提案を適用…")
        set_primary_action(self.apply_button)
        actions.addWidget(self.evaluate_button)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.robustness_button)
        actions.addWidget(self.apply_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.evaluate_button.clicked.connect(self._evaluate)
        self.cancel_button.clicked.connect(
            lambda: self._pool.cancel('evaluate_proposals')
        )
        self.robustness_button.clicked.connect(self._robustness)
        self.apply_button.clicked.connect(self._apply)
        self.selector.changed.connect(lambda _variant_id: self.refresh())
        self.refresh()

    def refresh(self) -> None:
        view = self.service.comparison()
        self.tree.clear()
        if view is None:
            self.summary.setText(
                "no eligible comparison: 保存済みTopologyComparisonEvaluationがありません。"
            )
            return
        text = view.name
        if view.authority_stale and view.stale_reason:
            text += f" / 要再評価: {view.stale_reason}"
        self.summary.setText(text)
        for variant in view.variants:
            row = QTreeWidgetItem(
                [
                    variant.name,
                    variant.lifecycle_label,
                    variant.coverage,
                    variant.spl_headroom,
                    variant.standards,
                    variant.eligibility_label,
                    variant.pareto_state,
                    variant.blocked_reason or "",
                ]
            )
            row.setData(0, 0x0100, variant.variant_id)
            if variant.blocked_reason:
                row.setToolTip(7, variant.blocked_reason)
            for metric in variant.objectives:
                child = QTreeWidgetItem(
                    [
                        metric.objective_id,
                        "",
                        metric.value_text,
                        metric.direction_label,
                        "",
                        "比較可能" if metric.eligible else "利用不可",
                        metric.reason or "",
                        metric.reason or "",
                    ]
                )
                row.addChild(child)
            self.tree.addTopLevelItem(row)
        self.tree.expandAll()

    def _toggle_advanced(self, checked: bool) -> None:
        self.advanced_button.setText("詳細を隠す" if checked else "詳細を表示")
        self.advanced_label.setVisible(checked)
        if not checked:
            return
        variant_id = self.selector.current_variant_id()
        if variant_id is None:
            self.advanced_label.setText("選択中のSystemVariantがありません。")
            return
        try:
            p = self.service.variant_presentation(variant_id).advanced
        except KeyError:
            self.advanced_label.setText("provenanceを再解決できません。")
            return
        self.advanced_label.setText(
            "\n".join(
                (
                    f"variant_id: {p.variant_id}",
                    f"variant_sha256: {p.variant_sha256}",
                    f"baseline_revision_id: {p.baseline_revision_id}",
                    f"baseline_content_hash: {p.baseline_content_hash}",
                    f"schema_version: {p.schema_version}",
                    f"authority_version: {p.authority_version}",
                    "リポジトリ/出典キー: "
                    + (", ".join(p.repository_keys) or "なし"),
                )
            )
        )

    def _evaluate(self) -> None:
        variant_ids = [
            variant.variant_id for variant in self.service.variants()
        ]
        if not variant_ids:
            self.summary.setText("評価対象の提案がありません。")
            return
        self.summary.setText(
            f"{len(variant_ids) + 1} 候補の比較評価を実行しています…"
        )
        self._refresh_run_state()
        self._pool.start(
            'evaluate_proposals',
            lambda cancel_event: self.service.evaluate_proposals(
                variant_ids,
                include_current=True,
                is_cancelled=cancel_event.is_set,
            ),
            self._evaluation_completed,
            on_finished=lambda _key: self._refresh_run_state(),
        )

    def _evaluation_completed(self, key, result, error) -> None:
        if error == WORKER_CANCELLED:
            self.summary.setText(
                "評価を中止しました · 完了した候補の証跡は保持されています"
            )
            self.refresh()
            return
        if error is not None:
            self.summary.setText(
                f"評価できません: {operation_error_message(error)}"
            )
            return
        execution = result
        evaluated = sum(
            1
            for candidate in execution.candidates
            if candidate.bundle is not None
        )
        self.summary.setText(
            f"{execution.spec.name}: {evaluated}/"
            f"{len(execution.candidates)} 候補の証跡を永続化しました。"
        )
        self.refresh()

    def is_running(self) -> bool:
        return self._pool.active_count > 0

    def dispose(self) -> None:
        self._pool.shutdown()

    def _refresh_run_state(self) -> None:
        running = self.is_running()
        self.evaluate_button.setEnabled(not running)
        self.apply_button.setEnabled(not running)
        self.robustness_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)

    def _robustness(self) -> None:
        variant_id = self.selector.current_variant_id()
        if variant_id is None:
            self.summary.setText("提案を選択してください。")
            return
        target = self.service.robustness_target(variant_id)
        if not target.available:
            self.summary.setText(target.reason or "ばらつきevidenceを利用できません。")
            return
        self.robustnessRequested.emit(variant_id)

    def _apply(self) -> None:
        variant_id = self.selector.current_variant_id()
        if variant_id is None:
            self.summary.setText("提案を選択してください。")
            return
        preview = self.service.apply_preview(variant_id)
        if preview.stale:
            self.summary.setText(preview.stale_reason or "古い提案")
            return
        change_text = "\n".join(preview.change_lines) or "差分なし"
        message = (
            f"{preview.name}\n\n{change_text}\n\n"
            "baselineを上書きせず、新しいSceneRevisionを作成します。"
            "この操作だけでは「設置済み」にはなりません。"
        )
        answer = QMessageBox.question(
            self,
            "提案を適用",
            message,
            QMessageBox.StandardButton.Apply | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Apply:
            return
        try:
            application = self.service.apply(variant_id)
        except ValueError as exc:
            self.summary.setText(f"適用できません: {operation_error_message(exc)}")
            return
        self.summary.setText(
            "提案を新しいSceneRevisionへ適用しました。"
            "As-builtは実設置確認後に別途記録してください。"
        )
        self.applied.emit(application.applied_revision_id)
        self.selector.refresh()
        self.refresh()


class SystemExpansionRobustnessPanel(QFrame):
    """Read-only O100F proposal robustness surface mounted inside O90D."""

    def __init__(
        self,
        service: SystemExpansionWorkflowService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self._variant_id: str | None = None
        set_surface_role(self, SurfaceRole.RAISED)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("SystemVariant 提案のばらつき耐性")
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)
        note = QLabel(
            "O100Fの保存済みproposal robustness authorityを、この既存ばらつき耐性"
            "workspace内で読み取り専用表示します。通常のO90 CadCandidateへ偽装しません。"
        )
        note.setWordWrap(True)
        set_typography_role(note, TypographyRole.SECONDARY)
        layout.addWidget(note)

        self.summary = QLabel("比較画面から提案を選択してください。")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(
            [
                "objective",
                "向き",
                "nominal",
                "評価サンプル内の不利側",
                "局所感度",
                "確率",
            ]
        )
        self.tree.setMinimumHeight(150)
        layout.addWidget(self.tree)

        self.advanced_button = QPushButton("詳細を表示")
        self.advanced_button.setCheckable(True)
        set_control_size(self.advanced_button, ControlSize.COMPACT)
        self.advanced_label = QLabel()
        self.advanced_label.setWordWrap(True)
        self.advanced_label.hide()
        self.advanced_button.toggled.connect(self._toggle_advanced)
        layout.addWidget(self.advanced_button)
        layout.addWidget(self.advanced_label)

    def select_variant(self, variant_id: str) -> None:
        self._variant_id = variant_id
        self.refresh()

    def refresh(self) -> None:
        self.tree.clear()
        if self._variant_id is None:
            self.summary.setText("比較画面から提案を選択してください。")
            self.advanced_label.clear()
            return
        try:
            view = self.service.proposal_robustness_presentation(self._variant_id)
        except (KeyError, ValueError) as exc:
            self.summary.setText(f"ばらつきauthorityを表示できません: {operation_error_message(exc)}")
            self.advanced_label.clear()
            return
        if view is None:
            target = self.service.robustness_target(self._variant_id)
            self.summary.setText(
                target.reason or "保存済みproposal robustness authorityがありません。"
            )
            self.advanced_label.clear()
            return

        state = (
            "現在のbaselineと一致"
            if view.current
            else f"要再評価: {view.stale_reason}"
        )
        self.summary.setText(
            f"{view.variant_name} / {view.sampling_label} / {state} / "
            f"サンプル {view.sample_count} "
            f"(実現可能 {view.feasible_count}, 非実現可能 {view.infeasible_count}, "
            f"失敗 {view.failed_count}, 未評価 {view.unscored_count})"
        )
        for objective in view.objectives:
            self.tree.addTopLevelItem(
                QTreeWidgetItem(
                    [
                        objective.objective_id,
                        objective.direction_label,
                        objective.nominal_text,
                        objective.sampled_adverse_text,
                        objective.sensitivity_text,
                        objective.probability_text,
                    ]
                )
            )
        if not view.objectives:
            self.tree.addTopLevelItem(
                QTreeWidgetItem(
                    ["評価結果なし", "", "", "", "", ""]
                )
            )
        self.advanced_label.setText(
            "\n".join(
                (
                    f"robustness_spec_id: {view.robustness_spec_id}",
                    f"robustness_spec_sha256: {view.robustness_spec_sha256}",
                    f"candidate_variant_sha256: {view.candidate_variant_sha256}",
                )
            )
        )

    def _toggle_advanced(self, checked: bool) -> None:
        self.advanced_button.setText("詳細を隠す" if checked else "詳細を表示")
        self.advanced_label.setVisible(checked)


# Human labels for the existing MeasurementCapabilityClaim authority.
_MEASUREMENT_OBSERVABLES: tuple[tuple[str, str], ...] = (
    ("magnitude_response", "magnitude応答"),
    ("phase_response", "phase応答"),
    ("arrival_time", "到達時間"),
    ("decay", "減衰"),
    ("common_timing", "共通タイミング"),
    ("calibrated_response", "校正済み応答"),
    ("repeatability", "再現性"),
    ("polarity", "極性"),
)


class _MeasurementPlanDialog(QDialog):
    """Target picker for one SystemVariant-specific measurement plan."""

    def __init__(
        self,
        options: MeasurementPlanOptions,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("SystemVariant 測定計画")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.point_combo = QComboBox()
        for point in options.measurement_points:
            self.point_combo.addItem(
                f"{point.name}  {point.position_text}", point.entity_id
            )
        form.addRow("測定点", self.point_combo)

        self.sources = QListWidget()
        self.sources.setSelectionMode(
            QAbstractItemView.SelectionMode.MultiSelection
        )
        for source in options.sources:
            item = QListWidgetItem(
                f"{source.name}" + (f" ({source.role})" if source.role else "")
            )
            item.setData(0x0100, source.entity_id)
            self.sources.addItem(item)
        self.sources.setMinimumHeight(90)
        form.addRow("音源", self.sources)

        self.role_combo = QComboBox()
        self.role_combo.setEditable(True)
        roles = sorted(
            {source.role for source in options.sources if source.role}
        )
        self.role_combo.addItems(roles)
        form.addRow("チャンネル役割", self.role_combo)

        self.observable_combo = QComboBox()
        for value, label in _MEASUREMENT_OBSERVABLES:
            self.observable_combo.addItem(label, value)
        form.addRow("測定量", self.observable_combo)

        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 8)
        self.count_spin.setValue(1)
        form.addRow("必要測定回数", self.count_spin)

        self.repeatability_check = QCheckBox("再現性を要求する")
        form.addRow(self.repeatability_check)
        self.context_check = QCheckBox("acquisition contextを要求する")
        self.context_check.setChecked(True)
        form.addRow(self.context_check)
        self.purpose_edit = QLineEdit("設置済み構成の実測検証")
        form.addRow("目的", self.purpose_edit)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_source_ids(self) -> list[str]:
        return [
            item.data(0x0100) for item in self.sources.selectedItems()
        ]


class SystemExpansionMeasurementPanel(QFrame):
    openMeasurementsRequested = Signal()

    def __init__(
        self,
        service: SystemExpansionWorkflowService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        set_surface_role(self, SurfaceRole.RAISED)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("SystemVariant lifecycle / 実測")
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)
        note = QLabel(
            "generic N60 measurementの存在だけでは実測済みに昇格しません。"
            "SystemVariant固有plan/campaign/completionとvalidationを区別します。"
        )
        note.setWordWrap(True)
        set_typography_role(note, TypographyRole.SECONDARY)
        layout.addWidget(note)
        self.selector = _VariantSelector(service)
        layout.addWidget(self.selector)
        self.lifecycle_badge = QLabel()
        self.lifecycle_badge.setContentsMargins(8, 4, 8, 4)
        layout.addWidget(self.lifecycle_badge)
        self.state = QLabel()
        self.state.setWordWrap(True)
        layout.addWidget(self.state)

        actions = QHBoxLayout()
        self.as_built_button = QPushButton("実設置を記録…")
        self.plan_button = QPushButton("この構成の測定計画を作成…")
        self.campaign_button = QPushButton("キャンペーンを事前登録")
        self.open_measurements_button = QPushButton("測定を開く")
        for button in (
            self.as_built_button,
            self.plan_button,
            self.campaign_button,
            self.open_measurements_button,
        ):
            set_control_size(button, ControlSize.COMPACT)
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.targets_label = QLabel("事前登録済みtarget")
        set_typography_role(self.targets_label, TypographyRole.SECONDARY)
        layout.addWidget(self.targets_label)
        self.targets = QTreeWidget()
        self.targets.setHeaderLabels(
            ["target", "測定点", "役割", "期待", "記録済み"]
        )
        self.targets.setMinimumHeight(90)
        layout.addWidget(self.targets)

        self.as_built_button.clicked.connect(self._record_as_built)
        self.plan_button.clicked.connect(self._create_plan)
        self.campaign_button.clicked.connect(self._preregister_campaign)
        self.open_measurements_button.clicked.connect(
            self.openMeasurementsRequested.emit
        )
        self.selector.changed.connect(lambda _variant_id: self.refresh())
        self.refresh()

    def refresh(self) -> None:
        self.targets.clear()
        variant_id = self.selector.current_variant_id()
        if variant_id is None:
            self.lifecycle_badge.setText("")
            self.state.setText("SystemVariant提案がありません。")
            self.as_built_button.setEnabled(False)
            self.plan_button.setEnabled(False)
            self.campaign_button.setEnabled(False)
            self.open_measurements_button.setEnabled(False)
            return
        lifecycle = self.service.lifecycle(variant_id)
        self.lifecycle_badge.setText(lifecycle.label)
        set_semantic_state(
            self.lifecycle_badge,
            {
                "current": SemanticState.SELECTED,
                "proposed": SemanticState.WARNING,
                "as_built": SemanticState.SUCCESS,
                "measured": SemanticState.SUCCESS,
            }[lifecycle.state],
        )
        view = self.service.measurement(variant_id)
        self.state.setText(
            f"{view.state_label} / {view.validation_label}\n{view.detail}",
        )
        self.as_built_button.setEnabled(
            self.service.as_built_preview(variant_id).ready
        )
        self.plan_button.setEnabled(
            self.service.measurement_plan_options(variant_id).ready
        )
        self.campaign_button.setEnabled(view.state == "planned")
        self.open_measurements_button.setEnabled(
            view.state
            in {
                "campaign_preregistered",
                "evidence_incomplete",
                "validation_pending",
                "validated",
            }
        )
        for target in self.service.pending_measurement_targets(variant_id):
            row = QTreeWidgetItem(
                [
                    target.target_id,
                    target.measurement_point_entity_id,
                    target.channel_role,
                    str(target.expected_measurement_count),
                    str(target.recorded_evidence_count),
                ]
            )
            if target.pending:
                row.setToolTip(0, "未完了: 追加の実測根拠が必要です。")
            self.targets.addTopLevelItem(row)

    def _record_as_built(self) -> None:
        variant_id = self.selector.current_variant_id()
        if variant_id is None:
            return
        preview = self.service.as_built_preview(variant_id)
        if not preview.ready:
            QMessageBox.warning(
                self,
                "実設置を記録",
                "\n".join(
                    item
                    for item in (preview.reason, *preview.blocking)
                    if item
                )
                or "設置済み記録の前提条件を満たしていません。",
            )
            return
        lines = [
            f"{diff.name} ({diff.role or '役割なし'})"
            f"\n  提案: {diff.proposed_pose}"
            f"\n  実設置: {diff.actual_pose}"
            + ("\n  → 位置が異なります" if diff.moved else "")
            for diff in preview.diffs
        ]
        message = (
            "実設置確認後に設置済み記録を保存します。"
            "提案位置との差を確認してください。\n\n"
            + "\n".join(lines)
            + "\n\nこの記録だけでは実測済み・検証済みにはなりません。"
        )
        answer = QMessageBox.question(
            self,
            "実設置を記録",
            message,
            QMessageBox.StandardButton.Apply
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Apply:
            return
        confirmed_by, ok = QInputDialog.getText(
            self, "実設置を記録", "記録者名"
        )
        if not ok or not confirmed_by.strip():
            return
        try:
            self.service.record_as_built(variant_id, confirmed_by=confirmed_by)
        except ValueError as exc:
            warn_user(self, "実設置を記録できませんでした", exc)
            return
        self.refresh()

    def _create_plan(self) -> None:
        variant_id = self.selector.current_variant_id()
        if variant_id is None:
            return
        options = self.service.measurement_plan_options(variant_id)
        if not options.ready:
            QMessageBox.warning(
                self,
                "測定計画",
                options.reason or "測定計画を作成できません。",
            )
            return
        if not options.measurement_points:
            QMessageBox.warning(
                self,
                "測定計画",
                "設置済み状態に測定点がありません。"
                "部屋に受音点を追加してから再度記録してください。",
            )
            return
        dialog = _MeasurementPlanDialog(options, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.service.create_measurement_plan(
                variant_id,
                measurement_point_entity_id=dialog.point_combo.currentData(),
                source_entity_ids=dialog.selected_source_ids(),
                channel_role=dialog.role_combo.currentText(),
                observable=dialog.observable_combo.currentData(),
                expected_measurement_count=dialog.count_spin.value(),
                require_acquisition_context=dialog.context_check.isChecked(),
                repeatability_required=(
                    dialog.repeatability_check.isChecked()
                ),
                purpose=dialog.purpose_edit.text(),
            )
        except ValueError as exc:
            warn_user(self, "測定計画を作成できませんでした", exc)
            return
        self.refresh()

    def _preregister_campaign(self) -> None:
        variant_id = self.selector.current_variant_id()
        if variant_id is None:
            return
        purpose, ok = QInputDialog.getText(
            self,
            "キャンペーンを事前登録",
            "campaignの目的",
            text="設置済みSystemVariantの実測キャンペーン",
        )
        if not ok:
            return
        try:
            self.service.preregister_measurement_campaign(
                variant_id, purpose=purpose
            )
        except ValueError as exc:
            QMessageBox.warning(
                self, "キャンペーンを事前登録", str(exc)
            )
            return
        self.refresh()
