from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, cast
from uuid import uuid4

from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .cad_document import EditStateError, EditorViewState
from .cad_repository import RecoverySnapshot, SceneRepository
from .cad_scene import (
    F1_DOCUMENT_ID,
    Offset3,
    Position3,
    SceneDocument,
    SceneEntity,
    Size3,
    make_empty_scene,
    make_f1_scene,
    quaternion_from_euler_deg,
)
from .room_viewport import RoomOverlayState, RoomViewport3D
from .theater_document import TheaterWorkingDocument
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
from .workflow_shell import WorkspaceMount
from .system_expansion_workflow import SystemExpansionWorkflowService
from .system_expansion_widgets import SystemExpansionRoomPanel


ROOM_CONTEXT_IDS = ("geometry", "objects", "placement", "acoustics")


class RoomViewportPort(Protocol):
    entitySelected: object

    def render_document(
        self,
        document: SceneDocument,
        *,
        selected_id: str | None,
        overlays: RoomOverlayState,
        reset_camera: bool = False,
    ) -> None: ...

    def fit_scene(self) -> None: ...

    def focus_entity(self, entity_id: str) -> None: ...

    def render_proposed_entities(
        self,
        entities: tuple[SceneEntity, ...],
        *,
        selected_id: str | None = None,
    ) -> None: ...

    def close(self) -> bool: ...


ViewportFactory = Callable[[QWidget | None], QWidget]


class RoomWorkspaceController:
    """UX120 application boundary over existing Scene/WorkingDocument authorities."""

    def __init__(self, repository: SceneRepository, document_id: str) -> None:
        self.repository = repository
        self.document_id = document_id
        self.working: TheaterWorkingDocument
        self.view_state = EditorViewState()
        self.recovery_candidate: RecoverySnapshot | None = None
        self._load_latest_or_seed()

    @property
    def document(self) -> SceneDocument:
        """Current presentation snapshot, including an active transform preview."""
        return self.working.document

    @property
    def committed_document(self) -> SceneDocument:
        return self.working.committed_document

    @property
    def selected_id(self) -> str | None:
        return self.view_state.selected_id

    @property
    def is_dirty(self) -> bool:
        return self.working.is_dirty

    @property
    def can_edit(self) -> bool:
        return (
            self.recovery_candidate is None
            and self.document.room is not None
            and not self.working.has_preview
        )

    def _load_latest_or_seed(self) -> None:
        revision = self.repository.latest(self.document_id)
        if revision is None:
            seed = (
                make_f1_scene()
                if self.document_id == F1_DOCUMENT_ID
                else make_empty_scene(self.document_id)
            )
            revision = self.repository.save(seed, parent_revision_id=None).revision
        self.working = TheaterWorkingDocument(
            revision.document,
            source_revision_id=revision.revision_id,
            saved_content_hash=revision.content_hash,
        )
        record = self.repository.view_state(self.document_id)
        if record is not None:
            self.view_state = EditorViewState(
                selected_id=record.selected_id,
                selected_ids=list(record.selected_ids),
                hidden_ids=set(record.hidden_ids),
                locked_ids=set(record.locked_ids),
            )
            self.view_state.sanitize(revision.document)
        self.recovery_candidate = self.repository.recovery(self.document_id)

    def reload_if_clean(self) -> bool:
        if self.working.is_dirty or self.working.has_preview or self.recovery_candidate is not None:
            return False
        revision = self.repository.latest(self.document_id)
        if revision is None or revision.revision_id == self.working.source_revision_id:
            return False
        self.working = TheaterWorkingDocument(
            revision.document,
            source_revision_id=revision.revision_id,
            saved_content_hash=revision.content_hash,
        )
        self.view_state.sanitize(revision.document)
        self.recovery_candidate = self.repository.recovery(self.document_id)
        return True

    def before_deactivate(self) -> tuple[bool, str | None]:
        if self.working.has_preview:
            return False, "操作中のプレビューを確定またはキャンセルしてから画面を切り替えてください"
        if self.working.is_dirty:
            return False, "未保存の変更を保存または元に戻してから画面を切り替えてください"
        if self.recovery_candidate is not None:
            return False, "復旧データを復元または破棄してから画面を切り替えてください"
        return True, None

    def set_selection(self, entity_id: str | None) -> None:
        if entity_id is None:
            self.view_state.set_selection(())
        else:
            self.document.entity(entity_id)
            self.view_state.set_selection((entity_id,), primary_id=entity_id)
        self._persist_view_state()

    def save(self) -> bool:
        if self.recovery_candidate is not None:
            raise EditStateError("復旧可能な下書きを処理してから保存してください")
        if self.working.has_preview:
            raise EditStateError("操作中のプレビューを確定またはキャンセルしてから保存してください")
        result = self.repository.save(
            self.committed_document,
            parent_revision_id=self.working.source_revision_id,
        )
        self.working.mark_saved(
            result.revision.revision_id,
            result.revision.content_hash,
        )
        self.repository.clear_recovery(self.document_id)
        return bool(result.created)

    def undo(self) -> bool:
        if self.recovery_candidate is not None:
            return False
        changed = self.working.undo()
        if changed:
            self.view_state.sanitize(self.document)
            self._sync_recovery()
            self._persist_view_state()
        return changed

    def redo(self) -> bool:
        if self.recovery_candidate is not None:
            return False
        changed = self.working.redo()
        if changed:
            self.view_state.sanitize(self.document)
            self._sync_recovery()
            self._persist_view_state()
        return changed

    def add_object(self, kind: str) -> SceneEntity:
        if not self.can_edit:
            raise EditStateError("オブジェクト追加には完成した部屋と編集可能な下書きが必要です")
        entity = self._make_object(kind)
        if not self.working.add_entity(entity):
            raise EditStateError("オブジェクトを追加できませんでした")
        self.view_state.set_selection((entity.entity_id,), primary_id=entity.entity_id)
        self._sync_recovery()
        self._persist_view_state()
        return entity

    def replace_room(self, room) -> bool:
        if self.recovery_candidate is not None:
            raise EditStateError("復旧データを処理してから部屋形状を編集してください")
        changed = self.working.replace_room(room)
        if changed:
            self._sync_recovery()
        return changed

    def replace_room_topology(self, room, topology) -> bool:
        if self.recovery_candidate is not None:
            raise EditStateError("復旧データを処理してから壁・開口を編集してください")
        changed = self.working.replace_room_topology(room, topology)
        if changed:
            self._sync_recovery()
        return changed

    def update_selected(
        self,
        *,
        name: str,
        position: Position3,
        size_m: Size3 | None,
        speaker_role: str | None,
    ) -> bool:
        entity_id = self.selected_id
        if entity_id is None:
            return False
        if not self.can_edit:
            raise EditStateError("現在の状態では選択項目を編集できません")
        if self.view_state.is_locked(entity_id):
            raise EditStateError("ロック中のオブジェクトは編集できません")
        entity = self.document.entity(entity_id)
        updates: dict[str, object] = {
            "name": name.strip() or entity.name,
            "position": position,
        }
        if entity.size_m is not None:
            if size_m is None:
                raise EditStateError("物理オブジェクトには寸法が必要です")
            updates["size_m"] = size_m
        if entity.kind == "speaker":
            role = (speaker_role or "").strip()
            if not role:
                raise EditStateError("スピーカーの役割を入力してください")
            updates["speaker_role"] = role
        changed = self.working.update_entity(entity_id, **updates)
        if changed:
            self._sync_recovery()
        return changed

    def recover_draft(self) -> bool:
        recovery = self.recovery_candidate
        if recovery is None:
            return False
        source_id = recovery.source_revision_id
        source = self.repository.get(source_id) if source_id is not None else None
        if source is None:
            raise EditStateError("復旧元のrevisionが見つかりません")
        self.working = TheaterWorkingDocument(
            recovery.document,
            source_revision_id=source.revision_id,
            saved_content_hash=source.content_hash,
        )
        self.view_state.sanitize(self.working.committed_document)
        self.recovery_candidate = None
        self._persist_view_state()
        return True

    def discard_recovery(self) -> bool:
        if self.recovery_candidate is None:
            return False
        self.repository.clear_recovery(self.document_id)
        self.recovery_candidate = None
        self._load_latest_or_seed()
        return True

    def close(self) -> None:
        if self.working.has_preview:
            self.working.cancel_preview()
        self._sync_recovery()
        self._persist_view_state()

    def _sync_recovery(self) -> None:
        if self.working.has_preview or self.recovery_candidate is not None:
            return
        if self.working.is_dirty:
            self.repository.save_recovery(
                self.document,
                source_revision_id=self.working.source_revision_id,
            )
        else:
            self.repository.clear_recovery(self.document_id)

    def _persist_view_state(self) -> None:
        self.repository.save_view_state(
            self.document_id,
            selected_id=self.view_state.selected_id,
            selected_ids=self.view_state.selection,
            hidden_ids=self.view_state.hidden_ids,
            locked_ids=self.view_state.locked_ids,
        )

    def _default_position(self, kind: str, size: Size3 | None = None) -> Position3:
        room = self.document.room
        if room is None:
            raise EditStateError("部屋を作成してからオブジェクトを追加してください")
        min_x, min_y, max_x, max_y = room.bounds_m
        width = max_x - min_x
        depth = max_y - min_y
        center_x = min_x + width * 0.5
        center_y = min_y + depth * 0.55
        if kind == "screen":
            return Position3(
                x_m=center_x,
                y_m=min_y + min(depth * 0.04, 0.15),
                z_m=min(max(room.height_m * 0.55, 0.5), max(room.height_m - 0.1, 0.1)),
            )
        half_height = size.z_m * 0.5 if size is not None else 0.08
        z_m = max(half_height, min(1.0, room.height_m - half_height - 0.05))
        return Position3(x_m=center_x, y_m=center_y, z_m=z_m)

    def _make_object(self, kind: str) -> SceneEntity:
        token = uuid4().hex[:10]
        if kind == "speaker":
            size = Size3(x_m=0.24, y_m=0.28, z_m=0.42)
            return SceneEntity(
                entity_id=f"speaker-{token}",
                kind="speaker",
                name="スピーカー",
                speaker_role="SPK",
                position=self._default_position(kind, size),
                size_m=size,
                acoustic_reference_offset_m=Offset3(y_m=size.y_m * 0.5),
            )
        if kind == "seat":
            size = Size3(x_m=0.70, y_m=0.80, z_m=0.90)
            return SceneEntity(
                entity_id=f"seat-{token}",
                kind="seat",
                name="座席",
                position=self._default_position(kind, size),
                orientation=quaternion_from_euler_deg(
                    yaw_deg=180.0,
                    pitch_deg=0.0,
                    roll_deg=0.0,
                ),
                size_m=size,
                acoustic_reference_offset_m=Offset3(z_m=0.65),
            )
        if kind == "screen":
            size = Size3(x_m=2.60, y_m=0.04, z_m=1.46)
            return SceneEntity(
                entity_id=f"screen-{token}",
                kind="screen",
                name="スクリーン",
                position=self._default_position(kind, size),
                size_m=size,
            )
        if kind == "furniture":
            size = Size3(x_m=1.20, y_m=0.55, z_m=0.90)
            return SceneEntity(
                entity_id=f"furniture-{token}",
                kind="furniture",
                name="家具",
                position=self._default_position(kind, size),
                size_m=size,
            )
        if kind == "av_equipment":
            size = Size3(x_m=0.60, y_m=0.55, z_m=1.30)
            return SceneEntity(
                entity_id=f"av-{token}",
                kind="av_equipment",
                name="AV機器",
                position=self._default_position(kind, size),
                size_m=size,
            )
        if kind == "measurement_point":
            return SceneEntity(
                entity_id=f"point-{token}",
                kind="measurement_point",
                name="測定点",
                position=self._default_position(kind),
            )
        raise ValueError(f"unsupported object kind: {kind}")


class ObjectPalette(QFrame):
    addRequested = Signal(str)

    ITEMS = (
        ("speaker", "スピーカー"),
        ("seat", "座席"),
        ("screen", "スクリーン"),
        ("furniture", "家具"),
        ("av_equipment", "AV機器"),
        ("measurement_point", "測定点"),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("roomObjectPalette")
        set_surface_role(self, SurfaceRole.RAISED)
        self.setMinimumWidth(140)
        self.setMaximumWidth(176)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)
        title = QLabel("追加")
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)
        subtitle = QLabel("部屋へ配置する項目")
        set_typography_role(subtitle, TypographyRole.SECONDARY)
        layout.addWidget(subtitle)
        for kind, label in self.ITEMS:
            button = QPushButton(label)
            button.setProperty("objectKind", kind)
            set_control_size(button, ControlSize.STANDARD)
            button.clicked.connect(
                lambda checked=False, object_kind=kind: self.addRequested.emit(object_kind)
            )
            layout.addWidget(button)
        layout.addStretch(1)


class SelectionInspector(QFrame):
    editCommitted = Signal()

    KIND_LABELS = {
        "speaker": "スピーカー",
        "seat": "座席",
        "screen": "スクリーン",
        "furniture": "家具",
        "av_equipment": "AV機器",
        "measurement_point": "測定点",
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("roomSelectionInspector")
        self.setMinimumWidth(248)
        self.setMaximumWidth(320)
        set_surface_role(self, SurfaceRole.RAISED)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel("選択項目")
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)
        self.empty_label = QLabel("3Dビューで項目を選択してください")
        self.empty_label.setWordWrap(True)
        set_typography_role(self.empty_label, TypographyRole.SECONDARY)
        layout.addWidget(self.empty_label)

        form_host = QWidget()
        self.form = QFormLayout(form_host)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.kind_label = QLabel("—")
        self.name_field = QLineEdit()
        self.role_field = QLineEdit()
        self.form.addRow("種類", self.kind_label)
        self.form.addRow("名前", self.name_field)
        self.form.addRow("役割", self.role_field)

        self.position_fields: dict[str, QDoubleSpinBox] = {}
        for axis in ("X", "Y", "Z"):
            field = self._metric_field()
            self.position_fields[axis] = field
            self.form.addRow(f"位置 {axis}", field)

        self.size_fields: dict[str, QDoubleSpinBox] = {}
        for axis in ("X", "Y", "Z"):
            field = self._metric_field(minimum=0.001)
            self.size_fields[axis] = field
            self.form.addRow(f"寸法 {axis}", field)

        self.name_field.editingFinished.connect(self.editCommitted.emit)
        self.role_field.editingFinished.connect(self.editCommitted.emit)
        for field in (*self.position_fields.values(), *self.size_fields.values()):
            field.editingFinished.connect(self.editCommitted.emit)

        layout.addWidget(form_host)
        layout.addStretch(1)
        self.form_host = form_host
        self.set_entity(None, editable=False)

    @staticmethod
    def _metric_field(*, minimum: float = -1000.0) -> QDoubleSpinBox:
        field = QDoubleSpinBox()
        field.setRange(minimum, 1000.0)
        field.setDecimals(4)
        field.setSingleStep(0.01)
        field.setSuffix(" m")
        field.setKeyboardTracking(False)
        return field

    def set_entity(self, entity: SceneEntity | None, *, editable: bool) -> None:
        self.empty_label.setVisible(entity is None)
        self.form_host.setVisible(entity is not None)
        if entity is None:
            return
        blockers = [
            QSignalBlocker(self.name_field),
            QSignalBlocker(self.role_field),
            *(QSignalBlocker(field) for field in self.position_fields.values()),
            *(QSignalBlocker(field) for field in self.size_fields.values()),
        ]
        try:
            self.kind_label.setText(self.KIND_LABELS.get(entity.kind, entity.kind))
            self.name_field.setText(entity.name)
            self.role_field.setText(entity.speaker_role or "")
            self.role_field.setVisible(entity.kind == "speaker")
            self.name_field.setEnabled(editable)
            self.role_field.setEnabled(editable and entity.kind == "speaker")
            for field, value in zip(
                self.position_fields.values(),
                (entity.position.x_m, entity.position.y_m, entity.position.z_m),
                strict=True,
            ):
                field.setValue(value)
                field.setEnabled(editable)
            if entity.size_m is None:
                for field in self.size_fields.values():
                    field.setValue(0.0)
                    field.setEnabled(False)
            else:
                for field, value in zip(
                    self.size_fields.values(),
                    (entity.size_m.x_m, entity.size_m.y_m, entity.size_m.z_m),
                    strict=True,
                ):
                    field.setValue(value)
                    field.setEnabled(editable)
        finally:
            del blockers

    def values(self, entity: SceneEntity) -> tuple[str, Position3, Size3 | None, str | None]:
        position = Position3(
            x_m=self.position_fields["X"].value(),
            y_m=self.position_fields["Y"].value(),
            z_m=self.position_fields["Z"].value(),
        )
        size = None
        if entity.size_m is not None:
            size = Size3(
                x_m=self.size_fields["X"].value(),
                y_m=self.size_fields["Y"].value(),
                z_m=self.size_fields["Z"].value(),
            )
        role = self.role_field.text() if entity.kind == "speaker" else None
        return self.name_field.text(), position, size, role


class ContextToolStrip(QFrame):
    toolRequested = Signal(str)

    DEFINITIONS = {
        "geometry": (("draw-room", "部屋を描く"), ("edit-room", "形状を編集")),
        "objects": (("show-palette", "オブジェクト追加"),),
        "placement": (("focus-selection", "選択へ移動"), ("fit-scene", "全体表示")),
        "acoustics": (("toggle-acoustics", "音響表示"), ("fit-scene", "全体表示")),
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        set_surface_role(self, SurfaceRole.RAISED)
        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {}
        for context_id in ROOM_CONTEXT_IDS:
            page = QWidget()
            row = QHBoxLayout(page)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            for tool_id, label in self.DEFINITIONS[context_id]:
                button = QPushButton(label)
                set_control_size(button, ControlSize.COMPACT)
                button.clicked.connect(
                    lambda checked=False, target=tool_id: self.toolRequested.emit(target)
                )
                row.addWidget(button)
            row.addStretch(1)
            self.pages[context_id] = page
            self.stack.addWidget(page)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.addWidget(self.stack)
        self.set_context("geometry")

    def set_context(self, context_id: str) -> None:
        if context_id not in self.pages:
            raise ValueError(f"unknown Room context: {context_id}")
        self.stack.setCurrentWidget(self.pages[context_id])


class OverlayControls(QFrame):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        set_surface_role(self, SurfaceRole.OVERLAY)
        self._compact = False
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(10, 6, 10, 6)
        self._layout.setSpacing(10)
        self.grid = QCheckBox("グリッド")
        self.labels = QCheckBox("ラベル")
        self.acoustics = QCheckBox("音響")
        self.focus = QCheckBox("選択に集中")
        self.grid.setChecked(True)
        for toggle in (self.grid, self.labels, self.acoustics, self.focus):
            toggle.toggled.connect(lambda checked=False: self.changed.emit())
            self._layout.addWidget(toggle)

        self.more_button = QPushButton("表示…")
        set_control_size(self.more_button, ControlSize.COMPACT)
        self.more_menu = QMenu(self.more_button)
        self.labels_action = self.more_menu.addAction("ラベル")
        self.labels_action.setCheckable(True)
        self.focus_action = self.more_menu.addAction("選択に集中")
        self.focus_action.setCheckable(True)
        self.labels_action.toggled.connect(self.labels.setChecked)
        self.focus_action.toggled.connect(self.focus.setChecked)
        self.labels.toggled.connect(self.labels_action.setChecked)
        self.focus.toggled.connect(self.focus_action.setChecked)
        self.more_button.setMenu(self.more_menu)
        self.more_button.hide()
        self._layout.addWidget(self.more_button)
        self._layout.addStretch(1)

    @property
    def is_compact(self) -> bool:
        return self._compact

    def set_compact(self, compact: bool) -> None:
        compact = bool(compact)
        if self._compact == compact:
            return
        self._compact = compact
        self.labels.setVisible(not compact)
        self.focus.setVisible(not compact)
        self.more_button.setVisible(compact)
        self._layout.setContentsMargins(
            6 if compact else 10,
            4 if compact else 6,
            6 if compact else 10,
            4 if compact else 6,
        )
        self._layout.setSpacing(4 if compact else 10)

    def state(self) -> RoomOverlayState:
        return RoomOverlayState(
            grid=self.grid.isChecked(),
            labels=self.labels.isChecked(),
            acoustics=self.acoustics.isChecked(),
            focus_selection=self.focus.isChecked(),
        )


class RecoveryBanner(QFrame):
    recoverRequested = Signal()
    discardRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        set_surface_role(self, SurfaceRole.OVERLAY)
        set_semantic_state(self, SemanticState.WARNING)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        message = QLabel("保存前の下書きがあります。復旧するか破棄してから編集してください。")
        message.setWordWrap(True)
        layout.addWidget(message, 1)
        recover = QPushButton("復旧")
        discard = QPushButton("破棄")
        set_primary_action(recover)
        recover.clicked.connect(lambda checked=False: self.recoverRequested.emit())
        discard.clicked.connect(lambda checked=False: self.discardRequested.emit())
        layout.addWidget(recover)
        layout.addWidget(discard)


class RoomWorkspace(QWidget):
    """Viewport-centric UX120 Room workspace with no legacy dock composition."""

    toolRequested = Signal(str)

    def __init__(
        self,
        repository: SceneRepository,
        document_id: str,
        parent: QWidget | None = None,
        *,
        viewport_factory: ViewportFactory | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("roomWorkspace")
        set_surface_role(self, SurfaceRole.BASE)
        self.controller = RoomWorkspaceController(repository, document_id)
        self.current_context = "geometry"
        self.active_axis_constraint: str | None = None
        self.geometry_input = None
        self.transform_input = None
        self.geometry_panel: QWidget | None = None
        self.acoustics_panel: QWidget | None = None
        self.prediction_results: tuple = ()
        self._responsive_compact = False
        self._palette_user_open = False
        self._viewport_factory = viewport_factory or (lambda owner: RoomViewport3D(owner))
        self.system_expansion = SystemExpansionWorkflowService(repository, document_id)
        self._proposed_variant_id: str | None = None
        self._proposed_selected_id: str | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.recovery_banner = RecoveryBanner()
        self.recovery_banner.recoverRequested.connect(self._recover)
        self.recovery_banner.discardRequested.connect(self._discard_recovery)
        root.addWidget(self.recovery_banner)

        self.tools = ContextToolStrip()
        self.tools.toolRequested.connect(self._tool_requested)
        root.addWidget(self.tools)

        content = QHBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)

        self.object_palette = ObjectPalette()
        self.object_palette.addRequested.connect(self._add_object)
        content.addWidget(self.object_palette)

        viewport_column = QWidget()
        viewport_column.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        viewport_layout = QVBoxLayout(viewport_column)
        viewport_layout.setContentsMargins(0, 0, 0, 0)
        viewport_layout.setSpacing(0)
        self.overlay_controls = OverlayControls()
        self.overlay_controls.changed.connect(self._render)
        viewport_layout.addWidget(self.overlay_controls)

        viewport_widget = self._viewport_factory(viewport_column)
        self.viewport = cast(RoomViewportPort, viewport_widget)
        selected_signal = getattr(viewport_widget, "entitySelected", None)
        if selected_signal is not None and hasattr(selected_signal, "connect"):
            selected_signal.connect(self.select_entity)
        proposed_signal = getattr(viewport_widget, "proposedEntitySelected", None)
        if proposed_signal is not None and hasattr(proposed_signal, "connect"):
            proposed_signal.connect(self._proposal_entity_selected)
        viewport_layout.addWidget(viewport_widget, 1)
        content.addWidget(viewport_column, 1)

        self.inspector = SelectionInspector()
        self.inspector.editCommitted.connect(self._commit_inspector)
        self.right_stack = QStackedWidget()
        self.right_stack.setMinimumWidth(248)
        self.right_stack.setMaximumWidth(320)
        self.right_stack.addWidget(self.inspector)
        self.system_expansion_panel = SystemExpansionRoomPanel(self.system_expansion)
        self.system_expansion_panel.variantChanged.connect(self._proposal_variant_changed)
        self.system_expansion_panel.ghostEntityRequested.connect(
            self._proposal_entity_selected
        )
        self.right_stack.addWidget(self.system_expansion_panel)
        self.right_stack.setCurrentWidget(self.inspector)
        content.addWidget(self.right_stack)
        root.addLayout(content, 1)

        self.status = QLabel()
        self.status.setContentsMargins(12, 6, 12, 6)
        set_surface_role(self.status, SurfaceRole.RAISED)
        set_typography_role(self.status, TypographyRole.SECONDARY)
        root.addWidget(self.status)

        self.set_context("geometry")
        self._refresh(reset_camera=True)

    @property
    def is_dirty(self) -> bool:
        return self.controller.is_dirty

    def activate(self) -> None:
        changed = self.controller.reload_if_clean()
        self._refresh(reset_camera=changed)

    def before_deactivate(self) -> tuple[bool, str | None]:
        if self.geometry_input is not None and self.geometry_input.is_active:
            return False, "部屋形状の編集中です。確定またはキャンセルしてから画面を切り替えてください"
        if self.transform_input is not None and self.transform_input.is_active:
            return False, "項目の移動または回転を確定・キャンセルしてから画面を切り替えてください"
        return self.controller.before_deactivate()

    def attach_geometry_input(self, controller) -> None:
        self.geometry_input = controller

    def attach_transform_input(self, controller) -> None:
        self.transform_input = controller

    def attach_geometry_panel(self, panel: QWidget) -> None:
        if self.geometry_panel is not None:
            self.right_stack.removeWidget(self.geometry_panel)
            self.geometry_panel.setParent(None)
        self.geometry_panel = panel
        panel.setParent(self.right_stack)
        self.right_stack.addWidget(panel)
        if self.current_context == "geometry":
            self.right_stack.setCurrentWidget(panel)

    def attach_acoustics_panel(self, panel: QWidget) -> None:
        if self.acoustics_panel is not None:
            self.right_stack.removeWidget(self.acoustics_panel)
            self.acoustics_panel.setParent(None)
        self.acoustics_panel = panel
        panel.setParent(self.right_stack)
        self.right_stack.addWidget(panel)
        if self.current_context == "acoustics":
            self.right_stack.setCurrentWidget(panel)

    def set_prediction_results(self, results: object) -> None:
        self.prediction_results = results if isinstance(results, tuple) else ()
        self._render()

    def refresh(self, *, reset_camera: bool = False) -> None:
        self._refresh(reset_camera=reset_camera)

    def add_object(self, kind: str) -> bool:
        before = self.controller.document
        self._add_object(kind)
        return self.controller.document != before

    def duplicate_selected(self) -> bool:
        entity_id = self.controller.selected_id
        if entity_id is None or not self.controller.can_edit:
            return False
        if self.controller.view_state.is_locked(entity_id):
            return False
        source = self.controller.document.entity(entity_id)
        new_id = f"{source.kind}-{uuid4().hex[:10]}"
        position = Position3(
            x_m=source.position.x_m + 0.10,
            y_m=source.position.y_m + 0.10,
            z_m=source.position.z_m,
        )
        if not self.controller.working.duplicate_entity(
            entity_id,
            new_entity_id=new_id,
            name=f"{source.name} コピー",
            position=position,
        ):
            return False
        self.controller.view_state.set_selection((new_id,), primary_id=new_id)
        self.controller._sync_recovery()
        self.controller._persist_view_state()
        self._refresh()
        self._set_status("選択項目を複製しました")
        return True

    def set_transform_mode(self, mode: str) -> None:
        if mode not in {"move", "rotate"}:
            raise ValueError(mode)
        self.controller.view_state.transform_mode = mode
        self._set_status("移動モード" if mode == "move" else "回転モード")

    def fit_selection(self) -> None:
        entity_id = self.controller.selected_id
        if entity_id is not None:
            self.viewport.focus_entity(entity_id)

    def fit_all(self) -> None:
        self.viewport.fit_scene()

    def cancel_active_operation(self) -> bool:
        if self.transform_input is not None and self.transform_input.is_active:
            return bool(self.transform_input.cancel())
        if self.geometry_input is not None and self.geometry_input.is_active:
            return bool(self.geometry_input.cancel())
        if self.controller.working.has_preview:
            changed = self.controller.working.cancel_preview()
            self._refresh()
            return changed
        return False

    def commit_active_operation(self) -> bool:
        if self.transform_input is not None and self.transform_input.is_active:
            return bool(self.transform_input.commit())
        if self.geometry_input is not None and self.geometry_input.is_active:
            return bool(self.geometry_input.commit())
        if self.controller.working.has_preview:
            changed = self.controller.working.commit_preview()
            if changed:
                self.controller._sync_recovery()
            self._refresh()
            return changed
        return False

    def constrain_axis(self, axis) -> None:
        if self.transform_input is not None and self.transform_input.is_active:
            self.transform_input.set_axis(axis)
            return
        value = getattr(axis, "value", str(axis))
        self.active_axis_constraint = value
        self._set_status(f"{str(value).upper()}軸に拘束")

    def set_context(self, context_id: str) -> None:
        if context_id not in ROOM_CONTEXT_IDS:
            raise ValueError(f"unknown Room context: {context_id}")
        self.current_context = context_id
        self.tools.set_context(context_id)
        self._palette_user_open = False
        self._update_responsive_layout()
        if context_id == "geometry" and self.geometry_panel is not None:
            self.right_stack.setCurrentWidget(self.geometry_panel)
            refresh = getattr(self.geometry_panel, "refresh", None)
            if callable(refresh):
                refresh()
        elif context_id == "placement":
            self.system_expansion_panel.refresh()
            self.right_stack.setCurrentWidget(self.system_expansion_panel)
        elif context_id == "acoustics":
            self.overlay_controls.acoustics.setChecked(True)
            if self.acoustics_panel is not None:
                self.right_stack.setCurrentWidget(self.acoustics_panel)
                refresh = getattr(self.acoustics_panel, "refresh", None)
                if callable(refresh):
                    refresh()
            else:
                self.right_stack.setCurrentWidget(self.inspector)
        else:
            self.right_stack.setCurrentWidget(self.inspector)
        self._render()

    def _proposal_variant_changed(self, variant_id: str) -> None:
        self._proposed_variant_id = variant_id
        self._proposed_selected_id = None
        self._render()

    def _proposal_entity_selected(self, entity_id: object) -> None:
        self._proposed_selected_id = str(entity_id)
        self._render()

    def select_entity(self, entity_id: object) -> None:
        target = str(entity_id) if entity_id is not None else None
        try:
            self.controller.set_selection(target)
        except KeyError:
            return
        self._refresh_inspector()
        if self.acoustics_panel is not None and self.current_context == "acoustics":
            refresh = getattr(self.acoustics_panel, "refresh", None)
            if callable(refresh):
                refresh()
        self._render()

    def save(self) -> bool:
        created = self.controller.save()
        self._refresh()
        self._set_status("保存しました" if created else "変更はありません")
        return created

    def undo(self) -> bool:
        changed = self.controller.undo()
        if changed:
            self._refresh()
            self._set_status("元に戻しました")
        return changed

    def redo(self) -> bool:
        changed = self.controller.redo()
        if changed:
            self._refresh()
            self._set_status("やり直しました")
        return changed

    def _update_responsive_layout(self) -> None:
        width = self.width()
        compact = width < 900
        ultra_compact = width < 720
        self._responsive_compact = compact
        if ultra_compact:
            self._palette_user_open = False
        right_width = 260 if ultra_compact else (280 if compact else 300)
        self.right_stack.setFixedWidth(right_width)
        self.overlay_controls.set_compact(compact)
        show_palette = (
            self.current_context in {"objects", "placement"}
            and (not compact or self._palette_user_open)
        )
        self.object_palette.setVisible(show_palette)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_responsive_layout()

    def _tool_requested(self, tool_id: str) -> None:
        if tool_id == "show-palette":
            if self._responsive_compact:
                self._palette_user_open = not self.object_palette.isVisible()
                self._update_responsive_layout()
            else:
                self.object_palette.setVisible(True)
            return
        if tool_id == "focus-selection":
            if self.controller.selected_id is not None:
                self.viewport.focus_entity(self.controller.selected_id)
            return
        if tool_id == "fit-scene":
            self.viewport.fit_scene()
            return
        if tool_id == "toggle-acoustics":
            self.overlay_controls.acoustics.toggle()
            return
        if tool_id == "draw-room" and self.geometry_input is not None:
            self.geometry_input.start_sketch()
            return
        if tool_id == "edit-room" and self.geometry_input is not None:
            self.geometry_input.start_edit()
            return
        self.toolRequested.emit(tool_id)

    def _add_object(self, kind: str) -> None:
        try:
            entity = self.controller.add_object(kind)
        except (EditStateError, ValueError) as exc:
            self._set_status(str(exc), error=True)
            return
        self._refresh()
        self._set_status(f"{entity.name}を追加しました")

    def _commit_inspector(self) -> None:
        entity_id = self.controller.selected_id
        if entity_id is None:
            return
        entity = self.controller.document.entity(entity_id)
        try:
            name, position, size, role = self.inspector.values(entity)
            changed = self.controller.update_selected(
                name=name,
                position=position,
                size_m=size,
                speaker_role=role,
            )
        except (EditStateError, ValueError) as exc:
            self._refresh_inspector()
            self._set_status(str(exc), error=True)
            return
        if changed:
            self._refresh()
            self._set_status("選択項目を更新しました")

    def _recover(self) -> None:
        try:
            changed = self.controller.recover_draft()
        except EditStateError as exc:
            self._set_status(str(exc), error=True)
            return
        if changed:
            self._refresh(reset_camera=True)
            self._set_status("下書きを復旧しました")

    def _discard_recovery(self) -> None:
        if self.controller.discard_recovery():
            self._refresh(reset_camera=True)
            self._set_status("保存前の下書きを破棄しました")

    def _refresh(self, *, reset_camera: bool = False) -> None:
        self.recovery_banner.setVisible(self.controller.recovery_candidate is not None)
        self._refresh_inspector()
        if self.geometry_panel is not None:
            refresh_geometry = getattr(self.geometry_panel, "refresh", None)
            if callable(refresh_geometry):
                refresh_geometry()
        if self.acoustics_panel is not None and self.current_context == "acoustics":
            refresh_acoustics = getattr(self.acoustics_panel, "refresh", None)
            if callable(refresh_acoustics):
                refresh_acoustics()
        self._render(reset_camera=reset_camera)
        if not self.status.text():
            self._set_status(
                "未保存の変更があります" if self.controller.is_dirty else "保存済み"
            )

    def _refresh_inspector(self) -> None:
        entity = None
        selected_id = self.controller.selected_id
        if selected_id is not None:
            try:
                entity = self.controller.document.entity(selected_id)
            except KeyError:
                self.controller.set_selection(None)
        editable = bool(
            entity is not None
            and self.controller.can_edit
            and not self.controller.view_state.is_locked(entity.entity_id)
        )
        self.inspector.set_entity(entity, editable=editable)

    def _render(self, *, reset_camera: bool = False) -> None:
        overlays = self.overlay_controls.state()
        self.viewport.render_document(
            self.controller.document,
            selected_id=self.controller.selected_id,
            overlays=overlays,
            reset_camera=reset_camera,
        )
        if self.current_context == "placement" and self._proposed_variant_id is not None:
            try:
                proposal_entities = self.system_expansion.ghost_preview(
                    self._proposed_variant_id
                )
            except (KeyError, ValueError):
                proposal_entities = ()
            render_proposals = getattr(self.viewport, "render_proposed_entities", None)
            if callable(render_proposals):
                render_proposals(
                    proposal_entities,
                    selected_id=self._proposed_selected_id,
                )
        if overlays.acoustics and self.prediction_results:
            render_prediction = getattr(self.viewport, "render_prediction_results", None)
            if callable(render_prediction):
                render_prediction(self.prediction_results)

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status.setText(text)
        set_semantic_state(self.status, SemanticState.ERROR if error else None)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.transform_input is not None:
            self.transform_input.dispose()
        if self.geometry_input is not None:
            self.geometry_input.dispose()
        self.controller.close()
        self.viewport.close()
        event.accept()


def build_room_workspace_mount(
    repository: SceneRepository,
    document_id: str,
    *,
    viewport_factory: ViewportFactory | None = None,
) -> WorkspaceMount:
    """Return the UX110 shell mount contract without modifying workflow_shell.py."""

    workspace = RoomWorkspace(
        repository,
        document_id,
        viewport_factory=viewport_factory,
    )
    return WorkspaceMount.from_widget(
        workspace,
        on_activate=workspace.activate,
        before_deactivate=workspace.before_deactivate,
        on_context_changed=workspace.set_context,
        on_entity_requested=workspace.select_entity,
    )


__all__ = [
    "ROOM_CONTEXT_IDS",
    "ContextToolStrip",
    "ObjectPalette",
    "RoomWorkspace",
    "RoomWorkspaceController",
    "SelectionInspector",
    "build_room_workspace_mount",
]
