from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .cad_document import EditStateError, EditorViewState
from .cad_objects import (
    aim_target_entities,
    aim_yaw_pitch_deg,
    direction_from_yaw_pitch_deg,
    orientation_aligning_forward,
    speaker_aim_replacements,
)
from .cad_orientation_constraints import entity_collision_geometry_authority
from .cad_repository import RecoverySnapshot, SceneRepository
from .cad_scene import (
    F1_DOCUMENT_ID,
    PHYSICAL_ENTITY_KINDS,
    BodyGeometryKind,
    BodyMeshAsset,
    BodyMeshTriangle,
    BodyMeshVertex,
    EntityBodyGeometry,
    FootprintVertex,
    Offset3,
    Position3,
    Quaternion4,
    SceneDocument,
    SceneEntity,
    Size3,
    is_unassigned_speaker_role,
    make_empty_scene,
    make_f1_scene,
    next_unassigned_speaker_role,
    quaternion_from_euler_deg,
    quaternion_to_euler_deg,
)
from .raw_mesh import RawMeshImportError, import_raw_visual_mesh
from .command_palette import flush_focused_text_editor, focused_text_editor
from .prediction_interpretation import PredictionSpatialLink
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
from .standards_workspace import StandardsCriterionPanel


ROOM_CONTEXT_IDS = ("geometry", "objects", "placement", "acoustics")

SPEAKER_ROLE_UNASSIGNED_LABEL = "未設定"

# Sentinel for "caller did not pass body_geometry" — distinct from an explicit
# None, which downgrades the entity back to the plain box envelope.
_UNSET: object = object()

BODY_SHAPE_ITEMS: tuple[tuple[BodyGeometryKind, str], ...] = (
    ("box", "直方体（包絡）"),
    ("cylinder", "円柱"),
    ("extruded_polygon", "多角形フットプリント"),
    ("mesh_asset", "メッシュアセット"),
)


def parse_footprint_vertices(text: str) -> tuple[FootprintVertex, ...]:
    """Parse ``x,y; x,y; …`` entity-local footprint text into vertices."""

    vertices: list[FootprintVertex] = []
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.replace(",", " ").split()
        if len(parts) != 2:
            raise ValueError("フットプリントは「x,y; x,y; …」（物体ローカル m）で入力してください")
        try:
            x_m, y_m = float(parts[0]), float(parts[1])
        except ValueError as exc:
            raise ValueError("フットプリント頂点は数値で入力してください") from exc
        vertices.append(FootprintVertex(x_m=x_m, y_m=y_m))
    if len(vertices) < 3:
        raise ValueError("フットプリントには3頂点以上が必要です")
    return tuple(vertices)


def format_footprint_vertices(vertices: tuple[FootprintVertex, ...]) -> str:
    return "; ".join(f"{vertex.x_m:g},{vertex.y_m:g}" for vertex in vertices)
# Inspector suggestions only — not a persisted enum. Custom roles remain
# free-form text and are stored verbatim.
SPEAKER_ROLE_SUGGESTIONS = (
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
        orientation: Quaternion4 | None = None,
        aim_yaw_pitch_deg: tuple[float, float] | None = None,
        body_geometry: EntityBodyGeometry | None | object = _UNSET,
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
        if body_geometry is not _UNSET:
            # None here is an explicit downgrade back to the box envelope.
            updates["body_geometry"] = body_geometry
        if entity.kind == "speaker":
            role = (speaker_role or "").strip()
            if is_unassigned_speaker_role(role):
                # Clearing the role (or picking 未設定) keeps a unique reserved
                # placeholder instead of a plausible-looking fake channel role.
                role = next_unassigned_speaker_role(
                    item.speaker_role
                    for item in self.document.entities
                    if item.kind == "speaker" and item.entity_id != entity_id
                )
            updates["speaker_role"] = role
        if orientation is not None:
            if entity.kind not in PHYSICAL_ENTITY_KINDS:
                raise EditStateError("この種類のオブジェクトには姿勢フィールドがありません")
            # Numeric orientation is an exact pose edit through the same
            # quaternion authority as gizmo rotation; it never touches aim_xyz.
            updates["orientation"] = orientation
        if aim_yaw_pitch_deg is not None:
            if entity.kind != "speaker":
                raise EditStateError("音響方向はスピーカー専用です")
            # Acoustic aim edits never rotate the cabinet; aligning the body is
            # a separate explicit action (align_selected_cabinet_to_aim).
            updates["aim_xyz"] = direction_from_yaw_pitch_deg(
                yaw_deg=aim_yaw_pitch_deg[0],
                pitch_deg=aim_yaw_pitch_deg[1],
            )
        changed = self.working.update_entity(entity_id, **updates)
        if changed:
            self._sync_recovery()
        return changed

    def aim_targets(self) -> tuple[SceneEntity, ...]:
        """Seats and measurement points the selected speaker may be aimed at."""

        return aim_target_entities(self.document)

    def _selected_speaker_for_edit(self) -> SceneEntity:
        entity_id = self.selected_id
        if entity_id is None:
            raise EditStateError("スピーカーを選択してください")
        if not self.can_edit:
            raise EditStateError("現在の状態では選択項目を編集できません")
        if self.view_state.is_locked(entity_id):
            raise EditStateError("ロック中のオブジェクトは編集できません")
        entity = self.document.entity(entity_id)
        if entity.kind != "speaker":
            raise EditStateError("音響方向はスピーカー専用です")
        return entity

    def aim_selected_speaker_at(self, target_id: str) -> bool:
        """Set the selected speaker's acoustic aim toward a seat/measurement point.

        Body orientation is intentionally untouched; one Undo transaction.
        """

        entity = self._selected_speaker_for_edit()
        replacements = speaker_aim_replacements(
            self.document,
            (entity.entity_id,),
            target_id,
        )
        changed = self.working.update_entity(
            entity.entity_id,
            aim_xyz=replacements[0].aim_xyz,
        )
        if changed:
            self._sync_recovery()
        return changed

    def clear_selected_speaker_aim(self) -> bool:
        """Reset the selected speaker's acoustic aim to explicit unknown (None)."""

        entity = self._selected_speaker_for_edit()
        if entity.aim_xyz is None:
            return False
        changed = self.working.update_entity(entity.entity_id, aim_xyz=None)
        if changed:
            self._sync_recovery()
        return changed

    def align_selected_cabinet_to_aim(self) -> bool:
        """Explicit command: rotate the cabinet so its front (+Y) follows the aim.

        The acoustic aim itself is never modified by this action.
        """

        entity = self._selected_speaker_for_edit()
        if entity.aim_xyz is None:
            raise EditStateError("音響方向が未設定のため本体を合わせられません")
        changed = self.working.update_entity(
            entity.entity_id,
            orientation=orientation_aligning_forward(entity.aim_xyz),
        )
        if changed:
            self._sync_recovery()
        return changed

    def attach_mesh_asset(self, entity_id: str, file_path: str | Path) -> SceneEntity:
        """Import a mesh file as the entity's asset-backed body geometry.

        The original bytes land in the project content-addressed blob store;
        the entity persists the parsed local-coordinate mesh plus exact asset
        hash/provenance, so the body is reproducible and auditable.
        """

        if not self.can_edit:
            raise EditStateError("現在の状態ではメッシュを設定できません")
        if self.view_state.is_locked(entity_id):
            raise EditStateError("ロック中のオブジェクトは編集できません")
        entity = self.document.entity(entity_id)
        if entity.size_m is None:
            raise EditStateError("メッシュボディは物理オブジェクトのみに設定できます")
        path = Path(file_path)
        data = path.read_bytes()
        imported = import_raw_visual_mesh(data, source_name=path.name)
        self.repository.store_blob(data)
        geometry = EntityBodyGeometry(
            kind="mesh_asset",
            mesh=BodyMeshAsset(
                asset_sha256=imported.provenance.original_asset_sha256,
                source_name=imported.provenance.source_name,
                asset_format=imported.provenance.asset_format,
                original_size_bytes=imported.provenance.original_size_bytes,
                vertices=tuple(
                    BodyMeshVertex(x_m=vertex.x, y_m=vertex.y, z_m=vertex.z)
                    for vertex in imported.vertices
                ),
                triangles=tuple(
                    BodyMeshTriangle(a=triangle.a, b=triangle.b, c=triangle.c)
                    for triangle in imported.triangles
                ),
            ),
        )
        if not self.working.update_entity(entity_id, body_geometry=geometry):
            raise EditStateError("メッシュボディを設定できませんでした")
        self._sync_recovery()
        return self.document.entity(entity_id)

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
                speaker_role=next_unassigned_speaker_role(
                    entity.speaker_role
                    for entity in self.document.entities
                    if entity.kind == "speaker"
                ),
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
    aimTargetRequested = Signal(str)
    aimClearRequested = Signal()
    alignCabinetRequested = Signal()
    meshImportRequested = Signal()

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
        self.role_field = QComboBox()
        self.role_field.setEditable(True)
        self.role_field.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.role_field.addItem(SPEAKER_ROLE_UNASSIGNED_LABEL)
        self.role_field.addItems(SPEAKER_ROLE_SUGGESTIONS)
        role_edit = self.role_field.lineEdit()
        if role_edit is not None:
            role_edit.setPlaceholderText("役割を選択または入力（例: FL / C / TFL）")
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

        # Issue-464 body geometry authoring: shape picker plus per-kind
        # parameters. ``size_m`` stays the bounding envelope; richer bodies
        # must fit inside it.
        self._entity: SceneEntity | None = None
        self._shape_label = QLabel("形状")
        self.shape_field = QComboBox()
        for shape_kind, shape_label in BODY_SHAPE_ITEMS:
            self.shape_field.addItem(shape_label, userData=shape_kind)
        self.form.addRow(self._shape_label, self.shape_field)

        self._radius_label = QLabel("半径")
        self.radius_field = self._metric_field(minimum=0.001)
        self.form.addRow(self._radius_label, self.radius_field)

        self._footprint_label = QLabel("フットプリント")
        self.footprint_field = QLineEdit()
        self.footprint_field.setPlaceholderText("x,y; x,y; …（物体ローカル m）")
        self.form.addRow(self._footprint_label, self.footprint_field)

        self._mesh_label = QLabel("メッシュ")
        self.mesh_summary = QLabel("未設定")
        self.mesh_summary.setWordWrap(True)
        set_typography_role(self.mesh_summary, TypographyRole.SECONDARY)
        self.mesh_button = QPushButton("メッシュを選択…")
        set_control_size(self.mesh_button, ControlSize.COMPACT)
        mesh_row = QWidget()
        mesh_row_layout = QHBoxLayout(mesh_row)
        mesh_row_layout.setContentsMargins(0, 0, 0, 0)
        mesh_row_layout.setSpacing(6)
        mesh_row_layout.addWidget(self.mesh_summary, 1)
        mesh_row_layout.addWidget(self.mesh_button)
        self.mesh_row = mesh_row
        self.form.addRow(self._mesh_label, mesh_row)

        self.shape_field.activated.connect(lambda _index=-1: self._shape_activated())
        self.radius_field.editingFinished.connect(self.editCommitted.emit)
        self.footprint_field.editingFinished.connect(self.editCommitted.emit)
        self._basis_label = QLabel("衝突・クリアランス")
        self.basis_value = QLabel("—")
        self.basis_value.setWordWrap(True)
        set_typography_role(self.basis_value, TypographyRole.SECONDARY)
        self.form.addRow(self._basis_label, self.basis_value)

        self.mesh_button.clicked.connect(
            lambda checked=False: self.meshImportRequested.emit()
        )

        # Transform — numeric physical orientation (#470). Backed by the exact
        # persisted quaternion; one field commit is one Undo transaction.
        self.orientation_header = QLabel("姿勢")
        set_typography_role(self.orientation_header, TypographyRole.SECTION_TITLE)
        self.orientation_header.setToolTip(
            "本体の向きをヨー・ピッチ・ロール（°）で正確に編集します。"
            "基準姿勢（全て 0°）では本体の正面は +Y（部屋後方）を向きます"
        )
        self.form.addRow(self.orientation_header)
        self.orientation_labels: dict[str, QLabel] = {}
        self.orientation_fields: dict[str, QDoubleSpinBox] = {}
        orientation_tooltips = {
            "Yaw": "Z軸まわりの回転（°）· 0°で正面は+Y（部屋後方）· 正値で正面は−X側へ旋回",
            "Pitch": "本体の前後軸まわりのねじれ（°）· 正面の向きは変わりません",
            "Roll": "X軸まわりの回転（°）· 正面を上（+）/下（−）へ傾けます",
        }
        for axis in ("Yaw", "Pitch", "Roll"):
            label = QLabel(axis)
            field = self._angle_field()
            label.setToolTip(orientation_tooltips[axis])
            field.setToolTip(orientation_tooltips[axis])
            self.orientation_labels[axis] = label
            self.orientation_fields[axis] = field
            self.form.addRow(label, field)
        self._orientation_widgets: tuple[QWidget, ...] = (
            self.orientation_header,
            *self.orientation_labels.values(),
            *self.orientation_fields.values(),
        )

        # Speaker-only acoustic aim block (#470): independent authority from the
        # cabinet pose. Unknown aim is never shown as a zero vector.
        self.aim_section = QWidget()
        aim_layout = QVBoxLayout(self.aim_section)
        aim_layout.setContentsMargins(0, 0, 0, 0)
        aim_layout.setSpacing(6)
        aim_header = QLabel("音響方向")
        set_typography_role(aim_header, TypographyRole.SECTION_TITLE)
        aim_header.setToolTip(
            "スピーカーの音響照準は本体の姿勢とは独立した権威です。"
            "本体を回転させても音響方向は変わりません"
        )
        aim_layout.addWidget(aim_header)
        self.aim_state_label = QLabel("—")
        self.aim_state_label.setWordWrap(True)
        set_typography_role(self.aim_state_label, TypographyRole.SECONDARY)
        aim_layout.addWidget(self.aim_state_label)

        target_row = QHBoxLayout()
        target_label = QLabel("対象")
        target_label.setToolTip("音響照準を向ける座席または測定点の音響基準点")
        target_row.addWidget(target_label)
        self.aim_target_combo = QComboBox()
        target_row.addWidget(self.aim_target_combo, 1)
        self.aim_apply_button = QPushButton("対象へ向ける")
        self.aim_apply_button.setToolTip(
            "選択した座席・測定点の音響基準点へ照準を設定します（本体は回転しません）"
        )
        self.aim_apply_button.clicked.connect(self._emit_aim_target)
        target_row.addWidget(self.aim_apply_button)
        aim_layout.addLayout(target_row)

        self.aim_known_host = QWidget()
        known_layout = QVBoxLayout(self.aim_known_host)
        known_layout.setContentsMargins(0, 0, 0, 0)
        known_layout.setSpacing(6)
        aim_form = QFormLayout()
        aim_form.setContentsMargins(0, 0, 0, 0)
        self.aim_yaw_field = self._angle_field()
        self.aim_yaw_field.setToolTip(
            "音響照準の水平角（°）· 0° = +Y（部屋後方）· 正値 = +X（部屋右）方向"
        )
        self.aim_pitch_field = self._angle_field(minimum=-90.0, maximum=90.0)
        self.aim_pitch_field.setToolTip("音響照準の仰角（°）· 正値 = +Z（上）方向")
        aim_form.addRow("音響Yaw", self.aim_yaw_field)
        aim_form.addRow("音響Pitch", self.aim_pitch_field)
        known_layout.addLayout(aim_form)
        aim_actions = QHBoxLayout()
        self.aim_clear_button = QPushButton("未設定に戻す")
        self.aim_clear_button.setToolTip(
            "音響方向を消去して明示的な「未設定（不明）」に戻します"
        )
        self.aim_clear_button.clicked.connect(
            lambda checked=False: self.aimClearRequested.emit()
        )
        self.aim_align_button = QPushButton("本体を方向に合わせる")
        self.aim_align_button.setToolTip(
            "明示操作: キャビネット正面（+Y）を音響方向へ向けます。音響方向自体は変わりません"
        )
        self.aim_align_button.clicked.connect(
            lambda checked=False: self.alignCabinetRequested.emit()
        )
        aim_actions.addWidget(self.aim_clear_button)
        aim_actions.addWidget(self.aim_align_button)
        known_layout.addLayout(aim_actions)
        aim_layout.addWidget(self.aim_known_host)
        self.form.addRow(self.aim_section)

        self.name_field.editingFinished.connect(self.editCommitted.emit)
        self.role_field.activated.connect(
            lambda _index=-1: self.editCommitted.emit()
        )
        if self.role_field.lineEdit() is not None:
            self.role_field.lineEdit().editingFinished.connect(self.editCommitted.emit)
        for field in (
            *self.position_fields.values(),
            *self.size_fields.values(),
            *self.orientation_fields.values(),
            self.aim_yaw_field,
            self.aim_pitch_field,
        ):
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

    @staticmethod
    def _angle_field(
        *,
        minimum: float = -180.0,
        maximum: float = 180.0,
    ) -> QDoubleSpinBox:
        field = QDoubleSpinBox()
        field.setRange(minimum, maximum)
        field.setDecimals(3)
        field.setSingleStep(1.0)
        field.setSuffix("°")
        field.setKeyboardTracking(False)
        return field

    def _sync_shape_visibility(self, shape_kind: str | None) -> None:
        physical = self._entity is not None and self._entity.size_m is not None
        show = physical and shape_kind is not None
        self._shape_label.setVisible(physical)
        self.shape_field.setVisible(physical)
        self._radius_label.setVisible(bool(show and shape_kind == "cylinder"))
        self.radius_field.setVisible(bool(show and shape_kind == "cylinder"))
        self._footprint_label.setVisible(bool(show and shape_kind == "extruded_polygon"))
        self.footprint_field.setVisible(bool(show and shape_kind == "extruded_polygon"))
        self._mesh_label.setVisible(bool(show and shape_kind == "mesh_asset"))
        self.mesh_row.setVisible(bool(show and shape_kind == "mesh_asset"))
        self._basis_label.setVisible(physical)
        self.basis_value.setVisible(physical)

    def _refresh_basis_label(self) -> None:
        """Show whether clearance/collision consumes the exact authored body.

        The label follows the currently selected shape so users see the basis
        before committing; invalid parameter text degrades to "—".
        """

        entity = self._entity
        if entity is None or entity.size_m is None:
            self.basis_value.setText("—")
            return
        try:
            _name, _pos, _size, _role, _orientation, _aim, body = self.values(entity)
        except ValueError:
            self.basis_value.setText("—")
            return
        candidate = entity.model_copy(update={"body_geometry": body})
        authority = entity_collision_geometry_authority(candidate)
        self.basis_value.setText(
            "実形状（厳密）" if authority == "exact_body_geometry" else "包絡近似"
        )

    def _shape_activated(self) -> None:
        self._sync_shape_visibility(self.shape_field.currentData())
        self._refresh_basis_label()
        self.editCommitted.emit()

    def set_entity(
        self,
        entity: SceneEntity | None,
        *,
        editable: bool,
        aim_targets: tuple[SceneEntity, ...] = (),
    ) -> None:
        self._entity = entity
        self.empty_label.setVisible(entity is None)
        self.form_host.setVisible(entity is not None)
        if entity is None:
            return
        role_edit = self.role_field.lineEdit()
        blockers = [
            QSignalBlocker(self.name_field),
            QSignalBlocker(self.role_field),
            QSignalBlocker(self.shape_field),
            QSignalBlocker(self.radius_field),
            QSignalBlocker(self.footprint_field),
            *(QSignalBlocker(field) for field in self.position_fields.values()),
            *(QSignalBlocker(field) for field in self.size_fields.values()),
            *(QSignalBlocker(field) for field in self.orientation_fields.values()),
            QSignalBlocker(self.aim_yaw_field),
            QSignalBlocker(self.aim_pitch_field),
            QSignalBlocker(self.aim_target_combo),
            *([QSignalBlocker(role_edit)] if role_edit is not None else []),
        ]
        try:
            self.kind_label.setText(self.KIND_LABELS.get(entity.kind, entity.kind))
            self.name_field.setText(entity.name)
            role = entity.speaker_role or ""
            if is_unassigned_speaker_role(role):
                self.role_field.setCurrentIndex(0)
            else:
                index = self.role_field.findText(role)
                if index >= 0:
                    self.role_field.setCurrentIndex(index)
                else:
                    self.role_field.setCurrentIndex(-1)
                    self.role_field.setEditText(role)
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
            # Yaw/Pitch/Roll rows exist only for physical bodies; a measurement
            # point is a reference position without a pose to author.
            physical = entity.kind in PHYSICAL_ENTITY_KINDS
            for widget in self._orientation_widgets:
                widget.setVisible(physical)
            if physical:
                for field, value in zip(
                    self.orientation_fields.values(),
                    quaternion_to_euler_deg(entity.orientation),
                    strict=True,
                ):
                    field.setValue(value)
                    field.setEnabled(editable)
            self.aim_section.setVisible(entity.kind == "speaker")
            if entity.kind == "speaker":
                self._set_aim_state(entity, editable=editable, aim_targets=aim_targets)

            # Body geometry authoring (Issue #464): ``size_m`` stays the
            # bounding envelope; shape fields edit the refined body.
            body = entity.body_geometry
            body_kind = body.kind if body is not None else "box"
            index = self.shape_field.findData(body_kind)
            self.shape_field.setCurrentIndex(index if index >= 0 else 0)
            self.shape_field.setEnabled(editable)
            radius_default = (
                min(entity.size_m.x_m, entity.size_m.y_m) * 0.5
                if entity.size_m is not None
                else 0.5
            )
            if body is not None and body.radius_m is not None:
                radius_default = float(body.radius_m)
            self.radius_field.setValue(radius_default)
            if body is not None and body.footprint_vertices:
                self.footprint_field.setText(
                    format_footprint_vertices(body.footprint_vertices)
                )
            elif entity.size_m is not None:
                half_x = entity.size_m.x_m * 0.5
                half_y = entity.size_m.y_m * 0.5
                self.footprint_field.setText(format_footprint_vertices((
                    FootprintVertex(x_m=-half_x, y_m=-half_y),
                    FootprintVertex(x_m=half_x, y_m=-half_y),
                    FootprintVertex(x_m=half_x, y_m=half_y),
                    FootprintVertex(x_m=-half_x, y_m=half_y),
                )))
            else:
                self.footprint_field.clear()
            self.radius_field.setEnabled(editable)
            self.footprint_field.setEnabled(editable)
            if body is not None and body.mesh is not None:
                self.mesh_summary.setText(
                    f"{body.mesh.source_name} · {body.mesh.asset_format} · "
                    f"{len(body.mesh.vertices)}頂点/{len(body.mesh.triangles)}面 · "
                    f"sha256:{body.mesh.asset_sha256[:12]}…"
                )
            else:
                self.mesh_summary.setText("未設定")
            self.mesh_button.setEnabled(editable and entity.size_m is not None)
            self._sync_shape_visibility(body_kind)
            self._refresh_basis_label()
        finally:
            del blockers

    def _set_aim_state(
        self,
        entity: SceneEntity,
        *,
        editable: bool,
        aim_targets: tuple[SceneEntity, ...],
    ) -> None:
        previous_target = self.aim_target_combo.currentData()
        self.aim_target_combo.clear()
        for target in aim_targets:
            label = f"{target.name} · {self.KIND_LABELS.get(target.kind, target.kind)}"
            self.aim_target_combo.addItem(label, target.entity_id)
        if previous_target is not None:
            index = self.aim_target_combo.findData(previous_target)
            if index >= 0:
                self.aim_target_combo.setCurrentIndex(index)
        has_target = self.aim_target_combo.count() > 0
        self.aim_target_combo.setEnabled(editable and has_target)
        self.aim_apply_button.setEnabled(editable and has_target)

        aim = entity.aim_xyz
        self.aim_known_host.setVisible(aim is not None)
        if aim is None:
            self.aim_state_label.setText(
                "未設定（不明）· 座席・測定点へ向けると確定します"
            )
            return
        yaw_deg, pitch_deg = aim_yaw_pitch_deg(aim)
        self.aim_state_label.setText(
            f"既知 · Yaw {yaw_deg:.3f}° · Pitch {pitch_deg:.3f}°\n"
            f"方向 ({aim.x:.4f}, {aim.y:.4f}, {aim.z:.4f})"
        )
        self.aim_yaw_field.setValue(yaw_deg)
        self.aim_pitch_field.setValue(pitch_deg)
        self.aim_yaw_field.setEnabled(editable)
        self.aim_pitch_field.setEnabled(editable)
        self.aim_clear_button.setEnabled(editable)
        self.aim_align_button.setEnabled(editable)

    def _emit_aim_target(self, checked: bool = False) -> None:
        del checked
        target = self.aim_target_combo.currentData()
        if target is not None:
            self.aimTargetRequested.emit(str(target))

    @staticmethod
    def _edited_angles(
        fields: tuple[QDoubleSpinBox, ...],
        exact: tuple[float, ...],
    ) -> tuple[float, ...] | None:
        """Return edited angle values, or None when the user changed nothing.

        Untouched fields contribute their exact authority values instead of the
        display-rounded spin value, so editing one axis never snaps the others.
        """

        edited: list[float] = []
        changed = False
        for field, component in zip(fields, exact, strict=True):
            value = field.value()
            if abs(value - round(component, field.decimals())) > 1e-9:
                changed = True
                edited.append(value)
            else:
                edited.append(component)
        return tuple(edited) if changed else None

    def _edited_orientation(self, entity: SceneEntity) -> Quaternion4 | None:
        if entity.kind not in PHYSICAL_ENTITY_KINDS:
            return None
        exact = quaternion_to_euler_deg(entity.orientation)
        edited = self._edited_angles(tuple(self.orientation_fields.values()), exact)
        if edited is None:
            return None
        return quaternion_from_euler_deg(
            yaw_deg=edited[0],
            pitch_deg=edited[1],
            roll_deg=edited[2],
        )

    def _edited_aim_angles(self, entity: SceneEntity) -> tuple[float, float] | None:
        if entity.kind != "speaker" or entity.aim_xyz is None:
            return None
        exact = aim_yaw_pitch_deg(entity.aim_xyz)
        return self._edited_angles(
            (self.aim_yaw_field, self.aim_pitch_field),
            exact,
        )

    def values(
        self,
        entity: SceneEntity,
    ) -> tuple[
        str,
        Position3,
        Size3 | None,
        str | None,
        Quaternion4 | None,
        tuple[float, float] | None,
        EntityBodyGeometry | None,
    ]:
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
        role = None
        if entity.kind == "speaker":
            text = self.role_field.currentText().strip()
            role = "" if not text or text == SPEAKER_ROLE_UNASSIGNED_LABEL else text
        body_geometry: EntityBodyGeometry | None = None
        if entity.size_m is not None:
            shape = str(self.shape_field.currentData() or "box")
            if shape == "cylinder":
                body_geometry = EntityBodyGeometry(
                    kind="cylinder",
                    radius_m=self.radius_field.value(),
                )
            elif shape == "extruded_polygon":
                body_geometry = EntityBodyGeometry(
                    kind="extruded_polygon",
                    footprint_vertices=parse_footprint_vertices(
                        self.footprint_field.text()
                    ),
                )
            elif shape == "mesh_asset":
                existing = entity.body_geometry
                if existing is not None and existing.kind == "mesh_asset":
                    body_geometry = existing
                else:
                    raise ValueError(
                        "メッシュボディは「メッシュを選択…」からインポートしてください"
                    )
            # "box" downgrades to no explicit body geometry (legacy envelope).
        return (
            self.name_field.text(),
            position,
            size,
            role,
            self._edited_orientation(entity),
            self._edited_aim_angles(entity),
            body_geometry,
        )


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

        self.navigation_hint = QLabel(
            "操作: 中ボタン=画面移動 / Shift+中ボタン=回転 / "
            "ホイール=拡大縮小 / 右クリック=メニュー"
        )
        self.navigation_hint.setObjectName("cadNavigationHint")
        self.navigation_hint.setWordWrap(True)
        self.navigation_hint.setToolTip(
            "中ボタン: 画面移動\n"
            "Shift + 中ボタン: 視点回転\n"
            "ホイール: 拡大縮小\n"
            "右クリック: 操作メニュー"
        )
        self.navigation_hint.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        set_typography_role(self.navigation_hint, TypographyRole.SECONDARY)
        self._layout.addWidget(self.navigation_hint, 1)

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
        # Spatial link of the selected interpretation finding (Issue #469).
        self.prediction_focus: PredictionSpatialLink | None = None
        self._responsive_compact = False
        self._palette_user_open = False
        self._viewport_factory = viewport_factory or (lambda owner: RoomViewport3D(owner))
        self.system_expansion = SystemExpansionWorkflowService(repository, document_id)
        self._proposed_variant_id: str | None = None
        self._proposed_selected_id: str | None = None
        self._pending_editor_rejected = False

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
        self.inspector.aimTargetRequested.connect(self._aim_target_committed)
        self.inspector.aimClearRequested.connect(self._aim_clear_committed)
        self.inspector.alignCabinetRequested.connect(self._cabinet_align_committed)
        self.inspector.meshImportRequested.connect(self._import_mesh_for_selected)
        self.right_stack = QStackedWidget()
        self.right_stack.setMinimumWidth(248)
        self.right_stack.setMaximumWidth(320)
        self.right_stack.addWidget(self.inspector)
        self.system_expansion_panel = SystemExpansionRoomPanel(self.system_expansion)
        self.system_expansion_panel.variantChanged.connect(self._proposal_variant_changed)
        self.system_expansion_panel.ghostEntityRequested.connect(
            self._proposal_entity_selected
        )
        self.standards_panel = StandardsCriterionPanel(repository, document_id)
        placement_body = QWidget()
        placement_layout = QVBoxLayout(placement_body)
        placement_layout.setContentsMargins(0, 0, 0, 0)
        placement_layout.setSpacing(10)
        placement_layout.addWidget(self.system_expansion_panel)
        placement_layout.addWidget(self.standards_panel)
        placement_layout.addStretch(1)
        self.placement_panel = QScrollArea()
        self.placement_panel.setWidgetResizable(True)
        self.placement_panel.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.placement_panel.setFrameShape(QFrame.Shape.NoFrame)
        self.placement_panel.setWidget(placement_body)
        self.right_stack.addWidget(self.placement_panel)
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
        self.prediction_focus = None
        self._render()

    def set_prediction_focus(self, link: PredictionSpatialLink | None) -> None:
        """Highlight the spatial evidence behind the selected finding."""
        self.prediction_focus = link
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
            self.standards_panel.refresh_targets()
            self.standards_panel.refresh()
            self.right_stack.setCurrentWidget(self.placement_panel)
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

    def has_focused_text_editor(self) -> bool:
        """True while a text/numeric field inside this workspace owns focus."""

        return focused_text_editor(self) is not None

    def commit_pending_editor(self) -> bool:
        """Flush a pending inspector/geometry edit into the WorkingDocument.

        project.save is a GLOBAL shortcut: Ctrl+S arrives while a field still
        owns focus, before ``editingFinished`` commits the visible value. This
        boundary forces that commit (reusing the field's own commit handler) so
        the saved revision reflects what the user sees. Returns False when the
        pending value was rejected, so Save can refuse instead of persisting a
        revision that silently omits the visible edit.
        """

        self._pending_editor_rejected = False
        flush_focused_text_editor(self)
        rejected = self._pending_editor_rejected
        self._pending_editor_rejected = False
        return not rejected

    def mark_pending_editor_rejected(self) -> None:
        """Record that a pending-editor commit was refused during a flush."""

        self._pending_editor_rejected = True

    def save(self) -> bool:
        if not self.commit_pending_editor():
            self._set_status("入力中の値を確定できないため保存できません", error=True)
            return False
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
        hint = " · 役割を選択してください" if entity.kind == "speaker" else ""
        self._set_status(f"{entity.name}を追加しました{hint}")

    def _commit_inspector(self) -> None:
        entity_id = self.controller.selected_id
        if entity_id is None:
            return
        entity = self.controller.document.entity(entity_id)
        try:
            (
                name,
                position,
                size,
                role,
                orientation,
                aim_angles,
                body_geometry,
            ) = self.inspector.values(entity)
            changed = self.controller.update_selected(
                name=name,
                position=position,
                size_m=size,
                speaker_role=role,
                orientation=orientation,
                aim_yaw_pitch_deg=aim_angles,
                body_geometry=body_geometry,
            )
        except (EditStateError, ValueError) as exc:
            self._pending_editor_rejected = True
            self._refresh_inspector()
            self._set_status(str(exc), error=True)
            return
        if changed:
            self._refresh()
            self._set_status("選択項目を更新しました")

    def _aim_target_committed(self, target_id: object) -> None:
        try:
            changed = self.controller.aim_selected_speaker_at(str(target_id))
        except (EditStateError, ValueError) as exc:
            self._refresh_inspector()
            self._set_status(str(exc), error=True)
            return
        if changed:
            self._refresh()
            self._set_status("選択した対象へ音響方向を設定しました")
        else:
            self._set_status("音響方向は変更されませんでした")

    def _aim_clear_committed(self) -> None:
        try:
            changed = self.controller.clear_selected_speaker_aim()
        except (EditStateError, ValueError) as exc:
            self._refresh_inspector()
            self._set_status(str(exc), error=True)
            return
        if changed:
            self._refresh()
            self._set_status("音響方向を未設定に戻しました")
        else:
            self._set_status("音響方向はすでに未設定です")

    def _cabinet_align_committed(self) -> None:
        try:
            changed = self.controller.align_selected_cabinet_to_aim()
        except (EditStateError, ValueError) as exc:
            self._refresh_inspector()
            self._set_status(str(exc), error=True)
            return
        if changed:
            self._refresh()
            self._set_status("本体を音響方向に合わせました")
        else:
            self._set_status("本体の向きは変更されませんでした")

    def import_mesh_for_selected(self, file_path: str | Path) -> bool:
        """Attach an imported mesh as the selected entity's body geometry."""

        entity_id = self.controller.selected_id
        if entity_id is None:
            self._set_status("メッシュを設定するオブジェクトを選択してください", error=True)
            return False
        try:
            entity = self.controller.attach_mesh_asset(entity_id, file_path)
        except (EditStateError, RawMeshImportError, ValueError, OSError) as exc:
            self._pending_editor_rejected = True
            self._set_status(str(exc), error=True)
            return False
        self._refresh()
        self._set_status(f"{entity.name}にメッシュボディを設定しました")
        return True

    def _import_mesh_for_selected(self) -> None:
        file_path, _filter = QFileDialog.getOpenFileName(
            self,
            "ボディメッシュを選択",
            "",
            "メッシュ (*.obj *.glb *.meshbin);;すべてのファイル (*)",
        )
        if not file_path:
            return
        self.import_mesh_for_selected(file_path)

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
        aim_targets = (
            self.controller.aim_targets()
            if entity is not None and entity.kind == "speaker"
            else ()
        )
        self.inspector.set_entity(entity, editable=editable, aim_targets=aim_targets)

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
                render_prediction(
                    self.prediction_results,
                    highlight=self.prediction_focus,
                )

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
