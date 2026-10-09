from __future__ import annotations

from math import hypot
from typing import TYPE_CHECKING
from uuid import uuid4

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .cad_display_units import (
    LengthDisplayPolicy,
    display_length_policy,
    format_length_m,
    si_to_display,
)
from .cad_document import EditStateError
from .cad_room_authoring import (
    AUTHORING_KIND_LABELS,
    validate_room_authoring_model,
)
from .cad_scene import (
    AdjacentRegionSpec,
    PartialHeightWallSpec,
    RiserSpec,
    RoomAuthoringModel,
    SlopedCeilingSpec,
    SoffitSpec,
    room_vertices,
)
from .cad_wall_models import WallConstraintBinding, WallOpening
from .cad_walls import (
    WallTopologyError,
    add_constraint_binding,
    add_opening,
    delete_constraint_binding,
    update_constraint_binding,
    wall_length,
)
from .length_spinbox import MetricSpinBox
from .room_geometry_preview import (
    GeometryChangePreview,
    preview_opening_delete,
    preview_opening_update,
    preview_vertex_delete,
    preview_wall_delete,
    preview_wall_merge,
    preview_wall_thickness,
)
from .room_workspace import InspectorSection
from .ui_theme import (
    SemanticState,
    SurfaceRole,
    TypographyRole,
    set_semantic_state,
    set_surface_role,
    set_typography_role,
)
from .user_facing_error import operation_error_message

if TYPE_CHECKING:
    from .room_geometry_input import RoomGeometryInputController


def _spin_value_changed(field: MetricSpinBox, stored_m: float) -> bool:
    """Whether the field holds a user-meaningful change from ``stored_m``.

    The displayed value is quantized to the field's decimals in the current
    display unit, so a stored SI value with finer precision (sketched
    floats, imported documents) would otherwise commit display-rounding
    noise as a silent edit on focus-out. The tolerance is half a display
    quantum converted back into metres.
    """
    scale = MetricSpinBox.UNIT_SCALES[field.display_unit()]
    tolerance_m = (0.5 * 10 ** -field.decimals()) / scale
    return abs(field.value_m() - stored_m) > tolerance_m


class RoomGeometryPanel(QFrame):
    """Context-only geometry inspector backed by existing N30a/N30b authority."""

    def __init__(
        self,
        geometry: RoomGeometryInputController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.geometry = geometry
        self.controller = geometry.workspace.controller
        self._length_policy = display_length_policy('m')
        self._pending_preview: GeometryChangePreview | None = None
        self._last_target_key: str | None = None
        # Issue #982/#936: lightweight interaction log for the
        # 「壁選択→開口編集→Undo」 operation-count / error-rate measurement.
        self.operation_events: list[tuple[str, str]] = []
        self.setObjectName("roomGeometryPanel")
        self.setMinimumWidth(248)
        self.setMaximumWidth(560)
        set_surface_role(self, SurfaceRole.RAISED)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        title = QLabel("部屋形状")
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        root.addWidget(title)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        set_typography_role(self.summary, TypographyRole.SECONDARY)
        root.addWidget(self.summary)

        mode_row = QHBoxLayout()
        self.edit_button = QPushButton("形状編集")
        self.edit_button.setToolTip(
            "編集モードに入り、3D上で頂点・辺・壁を選んで調整できるようにします"
        )
        self.finish_button = QPushButton("編集終了")
        self.finish_button.setToolTip(
            "形状編集モードを終了します · ここまでの変更は残ります"
            "（直前の変更を戻すには「元に戻す」）"
        )
        self.edit_button.clicked.connect(self.geometry.start_edit)
        self.finish_button.clicked.connect(self.geometry.cancel)
        mode_row.addWidget(self.edit_button)
        mode_row.addWidget(self.finish_button)
        root.addLayout(mode_row)

        room_form = QFormLayout()
        self.height = self._metric_field(0.1, 20.0)
        self.height.setToolTip("床から天井までの高さ · すべての壁に共通です")
        self.height.editingFinished.connect(self._height_edited)
        room_form.addRow("天井高", self.height)
        height_label = room_form.labelForField(self.height)
        if height_label is not None:
            height_label.setToolTip(self.height.toolTip())
        self.height_caption = self._caption_row(room_form)
        root.addLayout(room_form)

        self.selection_title = QLabel("選択: なし")
        set_typography_role(self.selection_title, TypographyRole.BODY)
        root.addWidget(self.selection_title)

        self.selection_context = QLabel()
        self.selection_context.setWordWrap(True)
        set_typography_role(self.selection_context, TypographyRole.SECONDARY)
        root.addWidget(self.selection_context)

        # Issue #982: armed pre-apply preview block. Shown only while a
        # dangerous edit is armed; 適用/取り消し here are preview actions,
        # visually distinct from 「編集終了」 (mode exit, keeps changes) and
        # from Undo (history roll-back).
        self.preview_host = QFrame()
        self.preview_host.setObjectName("geometryChangePreview")
        set_surface_role(self.preview_host, SurfaceRole.OVERLAY)
        preview_layout = QVBoxLayout(self.preview_host)
        preview_layout.setContentsMargins(8, 8, 8, 8)
        preview_layout.setSpacing(6)
        self.preview_title = QLabel()
        self.preview_title.setWordWrap(True)
        set_typography_role(self.preview_title, TypographyRole.BODY)
        preview_layout.addWidget(self.preview_title)
        self.preview_lines = QLabel()
        self.preview_lines.setWordWrap(True)
        set_typography_role(self.preview_lines, TypographyRole.SECONDARY)
        preview_layout.addWidget(self.preview_lines)
        preview_actions = QHBoxLayout()
        self.preview_apply_button = QPushButton("この内容で適用")
        self.preview_apply_button.setToolTip(
            "プレビューの内容を確定し、1回の「元に戻す」で取り消せる形で反映します"
        )
        self.preview_cancel_button = QPushButton("変更を取り消す")
        self.preview_cancel_button.setToolTip(
            "プレビューを破棄します · 部屋は変更されません（編集モードは続きます）"
        )
        self.preview_apply_button.clicked.connect(self._apply_pending_preview)
        self.preview_cancel_button.clicked.connect(self._cancel_pending_preview_clicked)
        preview_actions.addWidget(self.preview_apply_button)
        preview_actions.addWidget(self.preview_cancel_button)
        preview_layout.addLayout(preview_actions)
        self.preview_host.setVisible(False)
        root.addWidget(self.preview_host)

        # Esc while a preview is armed discards the preview (not edit mode).
        # The shortcut stays disabled while nothing is armed so the
        # workspace-level Esc (edit-mode cancel) never goes ambiguous.
        self._preview_escape = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._preview_escape.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._preview_escape.setEnabled(False)
        self._preview_escape.activated.connect(self._cancel_pending_preview_clicked)

        self.vertex_section = InspectorSection("頂点", self, collapsible=True, expanded=False)
        self.vertex_host = QWidget()
        vertex_layout = QFormLayout(self.vertex_host)
        vertex_layout.setContentsMargins(0, 0, 0, 0)
        self.vertex_x = self._metric_field(-1000.0, 1000.0)
        self.vertex_y = self._metric_field(-1000.0, 1000.0)
        vertex_hint = "選択中の頂点の部屋座標 · +X=部屋右、+Y=部屋奥"
        self.vertex_x.setToolTip(vertex_hint)
        self.vertex_y.setToolTip(vertex_hint)
        self.vertex_x.editingFinished.connect(self._vertex_edited)
        self.vertex_y.editingFinished.connect(self._vertex_edited)
        vertex_layout.addRow("頂点 X", self.vertex_x)
        vertex_layout.addRow("頂点 Y", self.vertex_y)
        for field in (self.vertex_x, self.vertex_y):
            label = vertex_layout.labelForField(field)
            if label is not None:
                label.setToolTip(vertex_hint)
        self.delete_vertex_button = QPushButton("頂点を削除")
        self.delete_vertex_button.setToolTip(
            "選択中の頂点を削除します（両隣の頂点が直線で結ばれます）"
        )
        self.delete_vertex_button.clicked.connect(self._delete_vertex)
        vertex_layout.addRow("", self.delete_vertex_button)
        self.vertex_caption = self._caption_row(vertex_layout)
        self.vertex_section.body_layout.addWidget(self.vertex_host)
        root.addWidget(self.vertex_section)

        self.edge_section = InspectorSection("辺", self, collapsible=True, expanded=False)
        self.edge_host = QWidget()
        edge_layout = QFormLayout(self.edge_host)
        edge_layout.setContentsMargins(0, 0, 0, 0)
        self.edge_length = self._metric_field(0.001, 1000.0)
        edge_hint = "選択中の辺の長さ · 変更すると終点側の頂点が移動します"
        self.edge_length.setToolTip(edge_hint)
        self.edge_length.editingFinished.connect(self._edge_length_edited)
        edge_layout.addRow("辺の長さ", self.edge_length)
        edge_label = edge_layout.labelForField(self.edge_length)
        if edge_label is not None:
            edge_label.setToolTip(edge_hint)
        self.split_offset = self._metric_field(0.001, 1000.0)
        split_offset_hint = "辺の始点からの分割位置 · 中点以外に頂点を挿入したい場合に変更"
        self.split_offset.setToolTip(split_offset_hint)
        edge_layout.addRow("分割位置", self.split_offset)
        split_offset_label = edge_layout.labelForField(self.split_offset)
        if split_offset_label is not None:
            split_offset_label.setToolTip(split_offset_hint)
        edge_actions = QHBoxLayout()
        self.insert_midpoint_button = QPushButton("中点に頂点追加")
        self.insert_midpoint_button.setToolTip(
            "選択中の辺の中点に新しい頂点を挿入します（L字・凹凸を作れます）"
        )
        self.insert_midpoint_button.clicked.connect(self._insert_midpoint)
        self.insert_at_offset_button = QPushButton("指定位置に頂点追加")
        self.insert_at_offset_button.setToolTip(
            "「分割位置」で指定した地点に新しい頂点を挿入します"
        )
        self.insert_at_offset_button.clicked.connect(self._insert_at_offset)
        self.ensure_walls_button = QPushButton("壁編集を有効化")
        self.ensure_walls_button.setToolTip(
            "辺から壁オブジェクトを生成し、厚さ・開口を編集できるようにします"
        )
        self.ensure_walls_button.clicked.connect(self._ensure_topology)
        edge_actions.addWidget(self.insert_midpoint_button)
        edge_actions.addWidget(self.ensure_walls_button)
        edge_layout.addRow("", edge_actions)
        offset_actions = QHBoxLayout()
        offset_actions.addWidget(self.insert_at_offset_button)
        edge_layout.addRow("", offset_actions)
        self.edge_length_caption = self._caption_row(edge_layout)
        self.split_offset_caption = self._caption_row(edge_layout)
        self.edge_section.body_layout.addWidget(self.edge_host)
        root.addWidget(self.edge_section)

        self.wall_section = InspectorSection("壁", self, collapsible=True, expanded=False)
        self.wall_section.setToolTip("部屋の外周を構成する壁 · 「形状編集」モードで辺/壁を選ぶと編集できます")

        self.wall_hint = QLabel(
            "「形状編集」モードで3D上の辺・壁を選ぶと、ここに厚さ・結合/削除・クリアランス参照の設定が表示されます"
        )
        self.wall_hint.setWordWrap(True)
        set_typography_role(self.wall_hint, TypographyRole.SECONDARY)
        self.wall_section.body_layout.addWidget(self.wall_hint)

        self.wall_host = QWidget()
        wall_form = QFormLayout(self.wall_host)
        wall_form.setContentsMargins(0, 0, 0, 0)
        self.wall_id = QLabel("—")
        self.wall_id.setToolTip("選択中の壁の番号（部屋の外周を時計回りに採番）")
        self.wall_length = QLabel("—")
        self.wall_length.setToolTip("選択中の壁の長さ · 読み取り専用")
        self.wall_thickness = self._metric_field(0.001, 5.0)
        wall_thickness_hint = "壁の厚さ · 遮音・構造の表現に使われます"
        self.wall_thickness.setToolTip(wall_thickness_hint)
        self.wall_thickness.editingFinished.connect(self._wall_thickness_edited)
        wall_form.addRow("壁", self.wall_id)
        wall_form.addRow("長さ", self.wall_length)
        wall_form.addRow("厚さ", self.wall_thickness)
        self.wall_thickness_caption = self._caption_row(wall_form)
        thickness_label = wall_form.labelForField(self.wall_thickness)
        if thickness_label is not None:
            thickness_label.setToolTip(wall_thickness_hint)
        wall_actions = QHBoxLayout()
        self.merge_wall_button = QPushButton("次の壁と結合")
        self._merge_wall_tooltips = (
            "この壁と次（時計回り）の壁を1本にまとめます",
            "この壁と先頭の壁を1本にまとめます（境目をまたぐ結合）",
        )
        self.merge_wall_button.setToolTip(self._merge_wall_tooltips[0])
        self.delete_wall_button = QPushButton("壁を削除")
        self.delete_wall_button.setToolTip("選択中の壁を削除します")
        self.merge_wall_button.clicked.connect(self._merge_wall)
        self.delete_wall_button.clicked.connect(self._delete_wall)
        wall_actions.addWidget(self.merge_wall_button)
        wall_actions.addWidget(self.delete_wall_button)
        wall_form.addRow("", wall_actions)
        clearance_hint = (
            "選択中の壁に対するクリアランス参照（自動配置との境界条件）"
        )
        self.wall_clearance_count = QLabel("—")
        self.wall_clearance_count.setToolTip("この壁に紐づくクリアランス参照の件数")
        self.binding_selector = QComboBox()
        self.binding_selector.setToolTip(
            "編集するクリアランス参照を選択 ·「新規 / 未選択」では追加モードになります"
        )
        self.binding_selector.currentIndexChanged.connect(self._binding_selected)
        self.clearance_value = self._metric_field(0.0, 10.0)
        self.clearance_value.set_value_m(0.30)
        self.clearance_value.setToolTip("クリアランスの距離")
        self.add_clearance_button = QPushButton("追加")
        self.add_clearance_button.setToolTip(
            "選択中の壁へクリアランス参照を追加します"
        )
        self.apply_clearance_button = QPushButton("適用")
        self.apply_clearance_button.setToolTip(
            "選択中のクリアランス参照に入力値を反映します"
        )
        self.delete_clearance_button = QPushButton("削除")
        self.delete_clearance_button.setToolTip(
            "選択中のクリアランス参照を削除します"
        )
        self.add_clearance_button.clicked.connect(self._add_clearance_binding)
        self.apply_clearance_button.clicked.connect(self._apply_clearance_binding)
        self.delete_clearance_button.clicked.connect(self._delete_clearance_binding)
        clearance_actions = QHBoxLayout()
        clearance_actions.addWidget(self.add_clearance_button)
        clearance_actions.addWidget(self.apply_clearance_button)
        clearance_actions.addWidget(self.delete_clearance_button)
        wall_form.addRow("クリアランス参照", self.wall_clearance_count)
        wall_form.addRow("参照の選択", self.binding_selector)
        wall_form.addRow("クリアランス", self.clearance_value)
        wall_form.addRow("", clearance_actions)
        clearance_label = wall_form.labelForField(self.wall_clearance_count)
        if clearance_label is not None:
            clearance_label.setToolTip(clearance_hint)
        self.clearance_caption = self._caption_row(wall_form)
        for field, hint in (
            (self.binding_selector, self.binding_selector.toolTip()),
            (self.clearance_value, self.clearance_value.toolTip()),
        ):
            label = wall_form.labelForField(field)
            if label is not None:
                label.setToolTip(hint)
        self.wall_section.body_layout.addWidget(self.wall_host)
        root.addWidget(self.wall_section)

        self.opening_section = InspectorSection("開口", self, collapsible=True, expanded=False)
        self.opening_section.setToolTip("壁の開いた部分（ドア・窓・通路）")

        self.opening_hint = QLabel(
            "壁を選択すると開口の追加・編集ができます"
        )
        self.opening_hint.setWordWrap(True)
        set_typography_role(self.opening_hint, TypographyRole.SECONDARY)
        self.opening_section.body_layout.addWidget(self.opening_hint)

        self.opening_host = QWidget()
        opening_form = QFormLayout(self.opening_host)
        opening_form.setContentsMargins(0, 0, 0, 0)
        self.opening_selector = QComboBox()
        self.opening_selector.setToolTip(
            "編集する開口を選択 ·「新規 / 未選択」の状態で「追加」を押すと新しい開口を作成します"
        )
        self.opening_selector.currentIndexChanged.connect(self._opening_selected)
        self.opening_kind = QComboBox()
        for value, label in (
            ("door", "ドア"),
            ("window", "窓"),
            ("passage", "通路"),
            ("other", "その他"),
        ):
            self.opening_kind.addItem(label, value)
        self.opening_kind.setToolTip(
            "開口の種類 · 通路は常に開放扱い、ドア・窓は閉じた開口として音響計算されます"
        )
        self.opening_offset = self._metric_field(0.0, 1000.0)
        self.opening_width = self._metric_field(0.001, 1000.0)
        self.opening_sill = self._metric_field(0.0, 20.0)
        self.opening_height = self._metric_field(0.001, 20.0)
        self.opening_open = QCheckBox("開放として扱う")
        opening_hints = {
            "開口": "編集する開口を選択 ·「新規 / 未選択」では追加モードになります",
            "種類": self.opening_kind.toolTip(),
            "開始位置": "壁の始点から開口の開始端までの距離",
            "幅": "開口の幅",
            "床から": "床から開口下端までの高さ · 窓の場合は腰壁の高さ",
            "高さ": "開口の高さ",
        }
        self.opening_offset.setToolTip(opening_hints["開始位置"])
        self.opening_width.setToolTip(opening_hints["幅"])
        self.opening_sill.setToolTip(opening_hints["床から"])
        self.opening_height.setToolTip(opening_hints["高さ"])
        self.opening_open.setToolTip(
            "扉・仕切りのない常時開いた開口として扱います（通路と同じ扱い）"
        )
        opening_form.addRow("開口", self.opening_selector)
        opening_form.addRow("種類", self.opening_kind)
        opening_form.addRow("開始位置", self.opening_offset)
        self.opening_offset_caption = self._caption_row(opening_form)
        opening_form.addRow("幅", self.opening_width)
        self.opening_width_caption = self._caption_row(opening_form)
        opening_form.addRow("床から", self.opening_sill)
        self.opening_sill_caption = self._caption_row(opening_form)
        opening_form.addRow("高さ", self.opening_height)
        self.opening_height_caption = self._caption_row(opening_form)
        opening_form.addRow("", self.opening_open)
        for field, hint in (
            (self.opening_selector, opening_hints["開口"]),
            (self.opening_kind, opening_hints["種類"]),
            (self.opening_offset, opening_hints["開始位置"]),
            (self.opening_width, opening_hints["幅"]),
            (self.opening_sill, opening_hints["床から"]),
            (self.opening_height, opening_hints["高さ"]),
        ):
            label = opening_form.labelForField(field)
            if label is not None:
                label.setToolTip(hint)

        opening_actions = QHBoxLayout()
        self.add_opening_button = QPushButton("追加")
        self.add_opening_button.setToolTip(
            "入力した種類・寸法でこの壁に新しい開口を作成します"
        )
        self.apply_opening_button = QPushButton("適用")
        self.apply_opening_button.setToolTip("選択中の開口に入力値を反映します")
        self.delete_opening_button = QPushButton("削除")
        self.delete_opening_button.setToolTip("選択中の開口を削除します")
        self.add_opening_button.clicked.connect(self._add_opening)
        self.apply_opening_button.clicked.connect(self._apply_opening)
        self.delete_opening_button.clicked.connect(self._delete_opening)
        opening_actions.addWidget(self.add_opening_button)
        opening_actions.addWidget(self.apply_opening_button)
        opening_actions.addWidget(self.delete_opening_button)
        opening_form.addRow("", opening_actions)
        self.opening_section.body_layout.addWidget(self.opening_host)
        root.addWidget(self.opening_section)

        # Issue #976: 「高度な形状」 — progressive disclosure for semantic
        # primitives that the flat RoomPrism cannot express. All edits route
        # through RoomAuthoringModel → replace_room_authoring (history, undo).
        self.authoring_toggle = QCheckBox("高度な形状を表示")
        self.authoring_toggle.setToolTip(
            "傾斜天井・下がり天井・段床・腰壁・隣接領域など、平たい直方体では表せない形状を編集します"
        )
        self.authoring_toggle.toggled.connect(self._authoring_toggled)
        root.addWidget(self.authoring_toggle)

        self.authoring_host = QWidget()
        authoring_layout = QVBoxLayout(self.authoring_host)
        authoring_layout.setContentsMargins(0, 0, 0, 0)
        authoring_layout.setSpacing(8)

        self.authoring_kind = QComboBox()
        self.authoring_kind.setToolTip(
            "編集する形状の種類 · 傾斜天井は1部屋につき1面、ほかは複数登録できます"
        )
        for kind in (
            'ceiling',
            'soffit',
            'riser',
            'partial_wall',
            'adjacent_region',
        ):
            self.authoring_kind.addItem(AUTHORING_KIND_LABELS[kind], kind)
        self.authoring_kind.currentIndexChanged.connect(self._authoring_kind_changed)
        kind_form = QFormLayout()
        kind_form.setContentsMargins(0, 0, 0, 0)
        kind_form.addRow("種類", self.authoring_kind)
        kind_label = kind_form.labelForField(self.authoring_kind)
        if kind_label is not None:
            kind_label.setToolTip(self.authoring_kind.toolTip())
        authoring_layout.addLayout(kind_form)

        self.authoring_selector = QComboBox()
        self.authoring_selector.setToolTip(
            "編集する形状を選択 ·「新規 / 未選択」では追加モードになります"
        )
        self.authoring_selector.currentIndexChanged.connect(self._authoring_item_selected)
        item_form = QFormLayout()
        item_form.setContentsMargins(0, 0, 0, 0)
        item_form.addRow("対象", self.authoring_selector)
        item_label = item_form.labelForField(self.authoring_selector)
        if item_label is not None:
            item_label.setToolTip(self.authoring_selector.toolTip())
        authoring_layout.addLayout(item_form)

        # Per-kind inspector hosts — only the active kind is visible.
        self._auth_ceiling_host, self._auth_ceiling_fields = self._build_ceiling_form()
        self._auth_soffit_host, self._auth_soffit_fields = self._build_rect_soffit_form()
        self._auth_riser_host, self._auth_riser_fields = self._build_riser_form()
        self._auth_wall_host, self._auth_wall_fields = self._build_partial_wall_form()
        self._auth_region_host, self._auth_region_fields = self._build_region_form()
        for host in (
            self._auth_ceiling_host,
            self._auth_soffit_host,
            self._auth_riser_host,
            self._auth_wall_host,
            self._auth_region_host,
        ):
            authoring_layout.addWidget(host)

        authoring_actions = QHBoxLayout()
        self.add_authoring_button = QPushButton("追加")
        self.add_authoring_button.setToolTip(
            "入力した寸法で新しい形状を作成します（3Dに薄い色でプレビューされます）"
        )
        self.apply_authoring_button = QPushButton("適用")
        self.apply_authoring_button.setToolTip(
            "選択中の形状に入力値を反映します"
        )
        self.delete_authoring_button = QPushButton("削除")
        self.delete_authoring_button.setToolTip("選択中の形状を削除します")
        self.add_authoring_button.clicked.connect(self._add_authoring)
        self.apply_authoring_button.clicked.connect(self._apply_authoring)
        self.delete_authoring_button.clicked.connect(self._delete_authoring)
        authoring_actions.addWidget(self.add_authoring_button)
        authoring_actions.addWidget(self.apply_authoring_button)
        authoring_actions.addWidget(self.delete_authoring_button)
        authoring_layout.addLayout(authoring_actions)
        self.authoring_host.hide()
        root.addWidget(self.authoring_host)

        self.notice = QLabel()
        self.notice.setWordWrap(True)
        set_typography_role(self.notice, TypographyRole.SECONDARY)
        root.addWidget(self.notice)
        root.addStretch(1)

        geometry.selectionChanged.connect(self.refresh)
        self.refresh()

    def _caption_row(self, form: QFormLayout) -> QLabel:
        """Unit / range / dependency note directly under a field (#982)."""

        caption = QLabel()
        caption.setWordWrap(True)
        set_typography_role(caption, TypographyRole.SECONDARY)
        form.addRow("", caption)
        return caption

    def set_length_policy(self, policy: LengthDisplayPolicy) -> None:
        """Apply the #496 display-unit policy to every length field/label.

        SI metres stay authoritative — the policy only re-skins suffixes,
        decimals and human-readable labels, matching the inspector and
        measure panels (previously metre-only while the rest of the app
        honored the preference).
        """

        self._length_policy = policy
        for field in self._length_fields():
            field.set_display_unit(policy.unit, decimals=policy.decimals)
        self.refresh()

    def _length_fields(self) -> tuple[MetricSpinBox, ...]:
        fields: list[MetricSpinBox] = [
            self.height,
            self.vertex_x,
            self.vertex_y,
            self.edge_length,
            self.split_offset,
            self.wall_thickness,
            self.clearance_value,
            self.opening_offset,
            self.opening_width,
            self.opening_sill,
            self.opening_height,
        ]
        for field_set in (
            self._auth_ceiling_fields,
            self._auth_soffit_fields,
            self._auth_riser_fields,
            self._auth_wall_fields,
            self._auth_region_fields,
        ):
            fields.extend(field_set.values())
        return tuple(fields)

    def _format_m(self, value_m: float) -> str:
        return format_length_m(value_m, self._length_policy)

    def _format_range_m(self, low_m: float, high_m: float) -> str:
        """``low–high`` sharing one unit suffix — keeps range labels compact."""

        policy = self._length_policy
        suffix = format_length_m(0.0, policy).rsplit(' ', 1)[1]
        return (
            f'{si_to_display(low_m, policy.unit):.{policy.decimals}f}–'
            f'{si_to_display(high_m, policy.unit):.{policy.decimals}f} {suffix}'
        )

    # ---- Issue #982: armed pre-apply previews ----

    def _editable_state(self) -> bool:
        return (
            self.geometry.room is not None
            and self.controller.recovery_candidate is None
            and not self.controller.working.has_preview
        )

    def _arm_preview(self, preview: GeometryChangePreview | None) -> None:
        if preview is None:
            return
        if not self._editable_state():
            self.notice.setText("現在の状態では編集できません")
            set_semantic_state(self.notice, SemanticState.ERROR)
            return
        self._pending_preview = preview
        self.operation_events.append(("preview_armed", preview.kind))
        self.preview_title.setText(f"変更プレビュー — {preview.title}")
        body_lines = list(preview.lines)
        for blocker in preview.blockers:
            body_lines.append(f"実行できない理由: {blocker}")
        self.preview_lines.setText("\n".join(body_lines))
        self.preview_apply_button.setEnabled(preview.feasible)
        self.preview_apply_button.setToolTip(
            "プレビューの内容を確定し、1回の「元に戻す」で取り消せる形で反映します"
            if preview.feasible
            else "この変更はブロックされています（上記の影響を先に解消してください）"
        )
        self.preview_host.setVisible(True)
        self.preview_apply_button.setFocus() if preview.feasible else self.preview_cancel_button.setFocus()
        self._scroll_to(self.preview_host)
        self.refresh()

    def _scroll_to(self, widget: QWidget) -> None:
        parent = self.parentWidget()
        while parent is not None and not isinstance(parent, QScrollArea):
            parent = parent.parentWidget()
        if isinstance(parent, QScrollArea):
            parent.ensureWidgetVisible(widget)

    def cancel_pending_preview(self) -> bool:
        """Discard an armed preview; returns True when one was armed.

        Called by the workspace Esc chain before edit-mode cancel so the
        semantics stay layered: Esc first drops the pending change, then
        exits edit mode — never silently both.
        """

        if self._pending_preview is None:
            return False
        self._pending_preview = None
        self.operation_events.append(("preview_cancelled", ""))
        self.preview_host.setVisible(False)
        self.notice.setText("変更を取り消しました（部屋は変わりません）")
        set_semantic_state(self.notice, None)
        self.refresh()
        return True

    def _cancel_pending_preview_clicked(self) -> None:
        self.cancel_pending_preview()

    def _apply_pending_preview(self) -> None:
        preview = self._pending_preview
        if preview is None:
            return
        if not preview.feasible or preview.room is None:
            self.notice.setText("この変更は適用できません")
            set_semantic_state(self.notice, SemanticState.ERROR)
            return
        # Stale-revision rejection: the committed document must still be the
        # one the preview was computed against (revision id + content hash).
        if preview.is_stale(
            self.controller.committed_document,
            self.controller.working.source_revision_id,
        ):
            self._pending_preview = None
            self.preview_host.setVisible(False)
            self.operation_events.append(("preview_stale_rejected", preview.kind))
            self.notice.setText(
                "プレビュー後に部屋が変更されました。変更は適用されませんでした"
            )
            set_semantic_state(self.notice, SemanticState.ERROR)
            self.geometry.workspace.mark_pending_editor_rejected()
            self.refresh()
            return

        def operation() -> bool:
            return self.geometry.apply_geometry_candidate(
                preview.room,
                None if preview.room_only else preview.topology,
                message=f"{preview.title} を適用しました",
            )

        self._pending_preview = None
        self.preview_host.setVisible(False)
        self._run(operation, f"{preview.title} を適用しました")
        if preview.clear_selection:
            self.geometry.clear_selection()
            return
        if preview.select_vertex_id is not None:
            try:
                self.geometry.select_vertex(preview.select_vertex_id)
            except KeyError:
                pass
        elif preview.select_edge_index is not None:
            self.geometry.select_edge(preview.select_edge_index)
        if preview.select_opening_id is not None:
            try:
                self.geometry.select_opening(preview.select_opening_id)
            except KeyError:
                pass
        self.operation_events.append(("preview_applied", preview.kind))

    @staticmethod
    def _metric_field(
        minimum_m: float,
        maximum_m: float,
    ) -> MetricSpinBox:
        return MetricSpinBox(minimum_m=minimum_m, maximum_m=maximum_m)

    def refresh(self) -> None:
        room = self.geometry.room
        editable = self._editable_state()
        armed = self._pending_preview is not None

        # Drop an armed preview whose committed document has moved — the
        # displayed impact would describe a state that no longer exists.
        if armed and self._pending_preview.is_stale(
            self.controller.committed_document,
            self.controller.working.source_revision_id,
        ):
            self._pending_preview = None
            self.preview_host.setVisible(False)
            self.operation_events.append(("preview_stale_dropped", ""))
            self.notice.setText(
                "プレビューの基準となる部屋が変更されました。プレビューを破棄しました"
            )
            set_semantic_state(self.notice, SemanticState.ERROR)
            armed = False

        # Leaving edit mode with a pending change discards it — the preview
        # is never committed implicitly (「編集終了」 keeps only committed
        # changes; an armed preview is not one).
        if armed and self.geometry.mode != "edit":
            self._pending_preview = None
            self.preview_host.setVisible(False)
            self.operation_events.append(("preview_cancelled", "mode_exit"))
            armed = False

        self._preview_escape.setEnabled(armed)
        can_commit = editable and not armed
        self.edit_button.setEnabled(editable and self.geometry.mode == "idle")
        self.finish_button.setEnabled(self.geometry.mode != "idle")
        self.height.setEnabled(can_commit)
        self.height_caption.setText(
            f"有効範囲 {self._format_range_m(0.1, 20.0)} · すべての壁に共通です"
        )

        if room is None:
            self.summary.setText("部屋がありません。まず「部屋を描く」で形状を作成してください。")
            self.selection_context.setText("")
            self._set_target_key("none")
            self.vertex_host.hide()
            self.edge_host.hide()
            self.wall_host.hide()
            self.opening_host.hide()
            self.wall_hint.hide()
            self.opening_hint.hide()
            self.authoring_toggle.setEnabled(False)
            self.authoring_host.hide()
            return

        min_x, min_y, max_x, max_y = room.bounds_m
        self.summary.setText(
            f"{len(room_vertices(room))}頂点 · "
            f"X {self._format_range_m(min_x, max_x)} · "
            f"Y {self._format_range_m(min_y, max_y)}"
        )
        # Issue #976: 高度な形状 disclosure — enabled once a room exists.
        self.authoring_toggle.setEnabled(editable)
        self.authoring_host.setVisible(
            self.authoring_toggle.isChecked() and room is not None
        )
        target = self.geometry.selected_authoring
        if target is not None:
            kind_index = self.authoring_kind.findData(target[0])
            if kind_index >= 0:
                with QSignalBlocker(self.authoring_kind):
                    self.authoring_kind.setCurrentIndex(kind_index)
        if self.authoring_host.isVisible():
            self._populate_authoring_selector()
        with QSignalBlocker(self.height):
            self.height.set_value_m(room.height_m)

        vertex = self.geometry.selected_vertex
        edge_index = self.geometry.selected_edge_index
        topology = self.geometry.topology
        wall = self.geometry.selected_wall
        opening_id = self.geometry.selected_opening_id
        selected_opening = next(
            (
                item
                for item in (topology.openings if topology is not None else ())
                if item.opening_id == opening_id
            ),
            None,
        )
        self.vertex_host.setVisible(vertex is not None)
        self.edge_host.setVisible(edge_index is not None)

        # #982: one target at the top — vertex / edge / wall / opening /
        # authoring — with ID, dimensions and dependencies as context.
        if vertex is not None:
            target_key = "vertex"
            self.selection_title.setText("選択: 頂点")
            self.selection_context.setText(
                f"頂点 {vertex.vertex_id} · 座標 "
                f"({self._format_m(vertex.x_m)}, {self._format_m(vertex.y_m)})"
                + self._edit_state_suffix(editable)
            )
            with QSignalBlocker(self.vertex_x), QSignalBlocker(self.vertex_y):
                self.vertex_x.set_value_m(vertex.x_m)
                self.vertex_y.set_value_m(vertex.y_m)
            self.vertex_x.setEnabled(can_commit)
            self.vertex_y.setEnabled(can_commit)
            self.delete_vertex_button.setEnabled(can_commit)
            self.vertex_caption.setText(
                f"範囲 {self._format_range_m(-1000.0, 1000.0)} · "
                "部屋座標（+X=右、+Y=奥）"
            )
        elif edge_index is not None:
            vertices = tuple(room_vertices(room))
            start = vertices[edge_index % len(vertices)]
            end = vertices[(edge_index + 1) % len(vertices)]
            edge_length_value = hypot(end.x_m - start.x_m, end.y_m - start.y_m)
            if wall is not None and selected_opening is not None:
                target_key = "opening"
                self.selection_title.setText("選択: 開口")
                wall_index = topology.walls.index(wall)
                deps = self._dependency_text(topology, wall.wall_id)
                self.selection_context.setText(
                    f"開口 {selected_opening.opening_id}"
                    f"（{self._kind_label(selected_opening.kind)}）· "
                    f"壁 {wall_index + 1}（{wall.wall_id}）上 · " + deps
                    + self._edit_state_suffix(editable)
                )
            elif wall is not None:
                target_key = "wall"
                self.selection_title.setText("選択: 壁")
                wall_index = topology.walls.index(wall)
                deps = self._dependency_text(topology, wall.wall_id)
                self.selection_context.setText(
                    f"壁 {wall_index + 1}（{wall.wall_id}）· "
                    f"長さ {self._format_m(wall_length(room, wall))} · " + deps
                    + self._edit_state_suffix(editable)
                )
            else:
                target_key = "edge"
                self.selection_title.setText("選択: 辺")
                self.selection_context.setText(
                    f"辺 {edge_index % len(vertices) + 1} · "
                    f"長さ {self._format_m(edge_length_value)}"
                    + (" · 壁トポロジなし" if topology is None else "")
                    + self._edit_state_suffix(editable)
                )
            with QSignalBlocker(self.edge_length):
                self.edge_length.set_value_m(edge_length_value)
            self.edge_length.setEnabled(can_commit)
            self.edge_length_caption.setText(
                f"有効範囲 {self._format_range_m(0.001, 1000.0)} · "
                "変更すると終点側の頂点が移動します"
            )
            with QSignalBlocker(self.split_offset):
                self.split_offset.set_maximum_m(
                    max(edge_length_value - 0.001, 0.001)
                )
                self.split_offset.set_value_m(
                    min(
                        edge_length_value * 0.5,
                        max(edge_length_value - 0.001, 0.001),
                    )
                )
            self.split_offset.setEnabled(can_commit)
            self.split_offset_caption.setText(
                f"有効範囲 {self._format_range_m(0.0, max(edge_length_value - 0.001, 0.001))}"
                " · この辺の始点からの距離"
            )
            self.insert_midpoint_button.setEnabled(can_commit)
            self.insert_at_offset_button.setEnabled(can_commit)
        elif self.geometry.selected_authoring is not None:
            kind, primitive_id = self.geometry.selected_authoring
            target_key = "authoring"
            self.selection_title.setText(
                f"選択: {AUTHORING_KIND_LABELS.get(kind, kind)}「{primitive_id}」"
            )
            self.selection_context.setText(
                f"種類 {AUTHORING_KIND_LABELS.get(kind, kind)} · "
                f"ID {primitive_id}" + self._edit_state_suffix(editable)
            )
            self.vertex_host.hide()
            self.edge_host.hide()
        else:
            target_key = "none"
            self.selection_title.setText("選択: なし")
            self.selection_context.setText(
                "3Dで頂点・壁・開口を選ぶと、対応する編集フォームが開きます"
            )
            self.vertex_host.hide()
            self.edge_host.hide()
        self._set_target_key(target_key)

        self.ensure_walls_button.setVisible(topology is None)
        self.ensure_walls_button.setEnabled(can_commit)
        wall_ready = can_commit and wall is not None and topology is not None
        self.wall_host.setVisible(wall is not None)
        self.opening_host.setVisible(wall is not None)
        self.wall_hint.setVisible(wall is None)
        self.opening_hint.setVisible(wall is None)

        if wall is None or topology is None:
            self.clearance_caption.setText("")
            for caption in (
                self.opening_offset_caption,
                self.opening_width_caption,
                self.opening_sill_caption,
                self.opening_height_caption,
            ):
                caption.setText("")
            return

        wall_index = topology.walls.index(wall)
        self.wall_id.setText(f"壁 {wall_index + 1}")
        self.wall_id.setToolTip(f"選択中の壁のID: {wall.wall_id}")
        self.wall_length.setText(self._format_m(wall_length(room, wall)))
        with QSignalBlocker(self.wall_thickness):
            self.wall_thickness.set_value_m(wall.thickness_m)
        self.wall_thickness.setEnabled(wall_ready)
        self.wall_thickness_caption.setText(
            f"範囲 {self._format_range_m(0.001, 5.0)} · "
            "結合/削除は隣接壁と同一の厚さが前提です · 変更はプレビュー確認後に適用"
        )
        is_last_wall = wall_index == len(topology.walls) - 1
        self.merge_wall_button.setText(
            "先頭の壁と結合" if is_last_wall else "次の壁と結合"
        )
        self.merge_wall_button.setToolTip(
            self._merge_wall_tooltips[1 if is_last_wall else 0]
            + " · 実行前に影響プレビューを表示します"
        )
        self.merge_wall_button.setEnabled(wall_ready)
        self.delete_wall_button.setEnabled(wall_ready)
        self.delete_wall_button.setToolTip(
            "選択中の壁を削除します · 実行前に影響プレビューを表示します"
        )

        bindings = tuple(
            item
            for item in topology.constraint_bindings
            if wall.wall_id in item.wall_ids
        )
        self.wall_clearance_count.setText(f"{len(bindings)} 件")
        self.wall_clearance_count.setToolTip(
            "この壁に紐づくクリアランス参照"
            + (
                "（"
                + ", ".join(self._format_m(item.clearance_m) for item in bindings)
                + "）"
                if bindings
                else "はありません"
            )
        )
        previous_binding = self.binding_selector.currentData()
        with QSignalBlocker(self.binding_selector):
            self.binding_selector.clear()
            self.binding_selector.addItem("新規 / 未選択", None)
            for item in bindings:
                label = self._format_m(item.clearance_m)
                if len(item.wall_ids) > 1:
                    label += f"（{len(item.wall_ids)}壁）"
                self.binding_selector.addItem(label, item.binding_id)
            if previous_binding is not None:
                index = self.binding_selector.findData(previous_binding)
                if index >= 0:
                    self.binding_selector.setCurrentIndex(index)
        self.binding_selector.setEnabled(wall_ready)
        self.clearance_value.setEnabled(wall_ready)
        self.add_clearance_button.setEnabled(wall_ready)
        self._binding_selected()

        previous = self.opening_selector.currentData()
        openings = tuple(item for item in topology.openings if item.wall_id == wall.wall_id)
        with QSignalBlocker(self.opening_selector):
            self.opening_selector.clear()
            self.opening_selector.addItem("新規 / 未選択", None)
            for item in openings:
                self.opening_selector.addItem(
                    f"{self._kind_label(item.kind)} · {self._format_m(item.offset_m)}",
                    item.opening_id,
                )
            # #982: a 3D-picked opening (selected_opening_id) wins over the
            # previous row so inspector and 3D show the same target.
            desired = self.geometry.selected_opening_id or previous
            if desired is not None:
                index = self.opening_selector.findData(desired)
                if index >= 0:
                    self.opening_selector.setCurrentIndex(index)
        self.add_opening_button.setEnabled(wall_ready)
        self._opening_selected()

    @staticmethod
    def _kind_label(kind: str) -> str:
        return {
            "door": "ドア",
            "window": "窓",
            "passage": "通路",
            "other": "その他",
        }.get(kind, kind)

    def _selected_opening(self) -> WallOpening | None:
        topology = self.geometry.topology
        opening_id = self.opening_selector.currentData()
        if topology is None or not isinstance(opening_id, str):
            return None
        return next(
            (item for item in topology.openings if item.opening_id == opening_id),
            None,
        )

    def _opening_selected(self) -> None:
        opening = self._selected_opening()
        can_commit = self._editable_state() and self._pending_preview is None
        enabled = opening is not None and can_commit
        self.opening_kind.setEnabled(self.add_opening_button.isEnabled())
        for field in (
            self.opening_offset,
            self.opening_width,
            self.opening_sill,
            self.opening_height,
            self.opening_open,
        ):
            field.setEnabled(enabled)
        self.apply_opening_button.setEnabled(enabled)
        self.delete_opening_button.setEnabled(enabled)
        # #982: inspector→3D shared selection — highlight the same opening.
        opening_id = opening.opening_id if opening is not None else None
        if opening_id != self.geometry.selected_opening_id:
            try:
                self.geometry.select_opening(opening_id)
            except KeyError:
                pass
        room = self.geometry.room
        topology = self.geometry.topology
        wall = self.geometry.selected_wall
        if opening is None or room is None or topology is None or wall is None:
            self.opening_offset_caption.setText("")
            self.opening_width_caption.setText("")
            self.opening_sill_caption.setText("")
            self.opening_height_caption.setText("")
            return
        # #982: dependent bounds sit with the fields — the user sees the
        # valid span before ever starting a commit.
        length = wall_length(room, wall)
        self.opening_offset_caption.setText(
            f"有効: 0–{self._format_m(max(length - self.opening_width.value_m(), 0.0))}"
            f"（壁長 {self._format_m(length)} − 幅）"
        )
        self.opening_width_caption.setText(
            f"有効: ≤ {self._format_m(max(length - self.opening_offset.value_m(), 0.0))}"
            "（壁長 − 開始位置）"
        )
        self.opening_sill_caption.setText(
            f"有効: 0–{self._format_m(max(room.height_m - self.opening_height.value_m(), 0.0))}"
            f"（天井 {self._format_m(room.height_m)} − 高さ）"
        )
        self.opening_height_caption.setText(
            f"有効: ≤ {self._format_m(max(room.height_m - self.opening_sill.value_m(), 0.0))}"
            "（天井 − 床から）"
        )
        blockers = [
            QSignalBlocker(self.opening_kind),
            QSignalBlocker(self.opening_offset),
            QSignalBlocker(self.opening_width),
            QSignalBlocker(self.opening_sill),
            QSignalBlocker(self.opening_height),
            QSignalBlocker(self.opening_open),
        ]
        try:
            kind_index = self.opening_kind.findData(opening.kind)
            if kind_index >= 0:
                self.opening_kind.setCurrentIndex(kind_index)
            self.opening_offset.set_value_m(opening.offset_m)
            self.opening_width.set_value_m(opening.width_m)
            self.opening_sill.set_value_m(opening.sill_m)
            self.opening_height.set_value_m(opening.height_m)
            self.opening_open.setChecked(opening.is_open)
        finally:
            del blockers

    def _run(self, operation, success: str) -> None:
        try:
            changed = bool(operation())
        except (EditStateError, ValueError, WallTopologyError) as exc:
            self.geometry.workspace.mark_pending_editor_rejected()
            self.notice.setText(operation_error_message(exc))
            set_semantic_state(self.notice, SemanticState.ERROR)
            self.refresh()
            return
        if changed:
            self.notice.setText(success)
            set_semantic_state(self.notice, SemanticState.SUCCESS)
            self.operation_events.append(("commit", success))
        else:
            self.notice.setText("変更はありません")
            set_semantic_state(self.notice, None)
        self.geometry.workspace.refresh()
        self.refresh()

    def _height_edited(self) -> None:
        room = self.geometry.room
        if room is None:
            return
        if not _spin_value_changed(self.height, room.height_m):
            self.refresh()
            return
        self._run(
            lambda: self.geometry.set_room_height(self.height.value_m()),
            "天井高を更新しました",
        )

    def _vertex_edited(self) -> None:
        selected = self.geometry.selected_vertex
        if selected is None:
            return
        if not _spin_value_changed(
            self.vertex_x, selected.x_m
        ) and not _spin_value_changed(self.vertex_y, selected.y_m):
            self.refresh()
            return
        self._run(
            lambda: self.geometry.set_selected_vertex_coordinates(
                x_m=self.vertex_x.value_m(),
                y_m=self.vertex_y.value_m(),
            ),
            "頂点座標を更新しました",
        )

    def _edge_length_edited(self) -> None:
        room = self.geometry.room
        if room is None or self.geometry.selected_edge_index is None:
            return
        vertices = list(room_vertices(room))
        index = self.geometry.selected_edge_index % len(vertices)
        start = vertices[index]
        end = vertices[(index + 1) % len(vertices)]
        current_length = hypot(end.x_m - start.x_m, end.y_m - start.y_m)
        if not _spin_value_changed(self.edge_length, current_length):
            self.refresh()
            return
        self._run(
            lambda: self.geometry.set_selected_edge_length(
                self.edge_length.value_m()
            ),
            "辺の長さを更新しました",
        )

    def _insert_midpoint(self) -> None:
        self._run(self.geometry.insert_selected_edge_midpoint, "頂点を追加しました")

    def _insert_at_offset(self) -> None:
        self._run(
            lambda: self.geometry.insert_selected_edge_vertex(
                self.split_offset.value_m()
            ),
            "頂点を追加しました",
        )

    def _delete_vertex(self) -> None:
        room = self.geometry.room
        vertex = self.geometry.selected_vertex
        if room is None or vertex is None:
            return
        self._arm_preview(
            preview_vertex_delete(
                room,
                self.geometry.topology,
                vertex.vertex_id,
                document=self.controller.committed_document,
                source_revision_id=self.controller.working.source_revision_id,
                authoring=self.geometry.authoring,
                fmt=self._format_m,
            )
        )

    def _ensure_topology(self) -> None:
        self._run(self.geometry.ensure_wall_topology, "壁編集を有効にしました")

    def _merge_wall(self) -> None:
        room = self.geometry.room
        topology = self.geometry.topology
        wall = self.geometry.selected_wall
        if room is None or topology is None or wall is None:
            return
        self._arm_preview(
            preview_wall_merge(
                room,
                topology,
                wall.wall_id,
                document=self.controller.committed_document,
                source_revision_id=self.controller.working.source_revision_id,
                authoring=self.geometry.authoring,
                fmt=self._format_m,
            )
        )

    def _delete_wall(self) -> None:
        room = self.geometry.room
        topology = self.geometry.topology
        wall = self.geometry.selected_wall
        if room is None or topology is None or wall is None:
            return
        self._arm_preview(
            preview_wall_delete(
                room,
                topology,
                wall.wall_id,
                document=self.controller.committed_document,
                source_revision_id=self.controller.working.source_revision_id,
                authoring=self.geometry.authoring,
                fmt=self._format_m,
            )
        )

    def _selected_binding(self) -> WallConstraintBinding | None:
        topology = self.geometry.topology
        binding_id = self.binding_selector.currentData()
        if topology is None or not isinstance(binding_id, str):
            return None
        return next(
            (
                item
                for item in topology.constraint_bindings
                if item.binding_id == binding_id
            ),
            None,
        )

    def _binding_selected(self) -> None:
        binding = self._selected_binding()
        enabled = (
            binding is not None
            and self._editable_state()
            and self._pending_preview is None
        )
        self.apply_clearance_button.setEnabled(enabled)
        self.delete_clearance_button.setEnabled(enabled)
        if binding is None:
            return
        with QSignalBlocker(self.clearance_value):
            self.clearance_value.set_value_m(binding.clearance_m)

    def _apply_clearance_binding(self) -> None:
        room = self.geometry.room
        topology = self.geometry.topology
        current = self._selected_binding()
        if room is None or topology is None or current is None:
            return
        replacement = WallConstraintBinding(
            binding_id=current.binding_id,
            wall_ids=current.wall_ids,
            clearance_m=float(self.clearance_value.value_m()),
        )

        def operation() -> bool:
            candidate = update_constraint_binding(room, topology, replacement)
            return self.controller.replace_room_topology(room, candidate)

        self._run(operation, "クリアランス参照を更新しました")

    def _delete_clearance_binding(self) -> None:
        room = self.geometry.room
        topology = self.geometry.topology
        current = self._selected_binding()
        if room is None or topology is None or current is None:
            return

        def operation() -> bool:
            candidate = delete_constraint_binding(room, topology, current.binding_id)
            return self.controller.replace_room_topology(room, candidate)

        self._run(operation, "クリアランス参照を削除しました")

    def _add_clearance_binding(self) -> None:
        room = self.geometry.room
        topology = self.geometry.topology
        wall = self.geometry.selected_wall
        if room is None or topology is None or wall is None:
            return
        binding = WallConstraintBinding(
            binding_id=f"clearance-{uuid4().hex[:10]}",
            wall_ids=(wall.wall_id,),
            clearance_m=float(self.clearance_value.value_m()),
        )

        def operation() -> bool:
            candidate = add_constraint_binding(room, topology, binding)
            return self.controller.replace_room_topology(room, candidate)

        self._run(operation, "クリアランス参照を追加しました")

    def _wall_thickness_edited(self) -> None:
        room = self.geometry.room
        topology = self.geometry.topology
        wall = self.geometry.selected_wall
        if room is None or topology is None or wall is None:
            return
        if not _spin_value_changed(self.wall_thickness, wall.thickness_m):
            self.refresh()
            return
        self._arm_preview(
            preview_wall_thickness(
                room,
                topology,
                wall.wall_id,
                self.wall_thickness.value_m(),
                document=self.controller.committed_document,
                source_revision_id=self.controller.working.source_revision_id,
                authoring=self.geometry.authoring,
                fmt=self._format_m,
            )
        )

    def _add_opening(self) -> None:
        room = self.geometry.room
        topology = self.geometry.topology
        wall = self.geometry.selected_wall
        if room is None or wall is None:
            return
        if topology is None:
            self.geometry.ensure_wall_topology()
            topology = self.geometry.topology
            wall = self.geometry.selected_wall
        if topology is None or wall is None:
            return
        length = wall_length(room, wall)
        if length <= 0.12:
            self.notice.setText("壁が短すぎるため開口を追加できません")
            set_semantic_state(self.notice, SemanticState.ERROR)
            return
        width = min(0.9, max(0.10, length - 0.10))
        kind = str(self.opening_kind.currentData() or "door")
        opening = WallOpening(
            opening_id=f"opening-{uuid4().hex[:10]}",
            wall_id=wall.wall_id,
            offset_m=max((length - width) * 0.5, 0.0),
            width_m=width,
            sill_m=0.8 if kind == "window" else 0.0,
            height_m=min(1.0 if kind == "window" else 2.0, room.height_m),
            kind=kind,
            is_open=kind == "passage",
        )

        def operation() -> bool:
            candidate = add_opening(room, topology, opening)
            return self.controller.replace_room_topology(room, candidate)

        self._run(operation, "開口を追加しました")
        index = self.opening_selector.findData(opening.opening_id)
        if index >= 0:
            self.opening_selector.setCurrentIndex(index)

    def _apply_opening(self) -> None:
        room = self.geometry.room
        topology = self.geometry.topology
        wall = self.geometry.selected_wall
        current = self._selected_opening()
        if room is None or topology is None or wall is None or current is None:
            return
        # #982: reject the commit before it starts when the entered values
        # cannot fit the wall / room — the captions show the same bounds.
        length = wall_length(room, wall)
        if self.opening_offset.value_m() + self.opening_width.value_m() > length + 1e-9:
            self.operation_events.append(("commit_rejected", "opening_span"))
            self.notice.setText(
                f"開始位置＋幅が壁長を超えています（壁長 {self._format_m(length)}）"
            )
            set_semantic_state(self.notice, SemanticState.ERROR)
            return
        if self.opening_sill.value_m() + self.opening_height.value_m() > room.height_m + 1e-9:
            self.operation_events.append(("commit_rejected", "opening_height"))
            self.notice.setText(
                f"床から＋高さが天井高を超えています（天井 {self._format_m(room.height_m)}）"
            )
            set_semantic_state(self.notice, SemanticState.ERROR)
            return
        replacement = WallOpening(
            opening_id=current.opening_id,
            wall_id=wall.wall_id,
            offset_m=self.opening_offset.value_m(),
            width_m=self.opening_width.value_m(),
            sill_m=self.opening_sill.value_m(),
            height_m=self.opening_height.value_m(),
            kind=str(self.opening_kind.currentData() or current.kind),
            is_open=self.opening_open.isChecked(),
        )
        self._arm_preview(
            preview_opening_update(
                room,
                topology,
                replacement,
                document=self.controller.committed_document,
                source_revision_id=self.controller.working.source_revision_id,
                authoring=self.geometry.authoring,
                fmt=self._format_m,
            )
        )

    def _delete_opening(self) -> None:
        room = self.geometry.room
        topology = self.geometry.topology
        current = self._selected_opening()
        if room is None or topology is None or current is None:
            return
        self._arm_preview(
            preview_opening_delete(
                room,
                topology,
                current.opening_id,
                document=self.controller.committed_document,
                source_revision_id=self.controller.working.source_revision_id,
                authoring=self.geometry.authoring,
                fmt=self._format_m,
            )
        )

    # ---- Issue #982: target-keyed sections, context and focus ----

    def _edit_state_suffix(self, editable: bool) -> str:
        if editable:
            return " · 編集可"
        if self.controller.recovery_candidate is not None:
            return " · 編集不可（復旧データを処理してください）"
        if self.controller.working.has_preview:
            return " · 編集不可（確定待ちのプレビューがあります）"
        return ""

    def _dependency_text(self, topology, wall_id: str) -> str:
        openings = sum(
            1 for item in topology.openings if item.wall_id == wall_id
        )
        bindings = sum(
            1
            for item in topology.constraint_bindings
            if wall_id in item.wall_ids
        )
        return f"開口 {openings} 件 · クリアランス {bindings} 件"

    def _set_target_key(self, target_key: str) -> None:
        """Expand the target's section, collapse the rest, focus its field."""

        if target_key == self._last_target_key:
            return
        self._last_target_key = target_key
        expanded = {
            "vertex": (True, False, False, False),
            "edge": (False, True, False, False),
            # A selected wall IS the boundary edge — keep the edge form open
            # alongside the wall form; the opening form waits for an opening.
            "wall": (False, True, True, False),
            "opening": (False, True, True, True),
            "authoring": (False, False, False, False),
            "none": (False, False, False, False),
        }.get(target_key, (False, False, False, False))
        for section, flag in zip(
            (
                self.vertex_section,
                self.edge_section,
                self.wall_section,
                self.opening_section,
            ),
            expanded,
        ):
            section.header.setChecked(flag)
        # #982: 3D pick → inspector focus — scroll to and focus the field
        # that edits the picked target.
        focus_widget = {
            "vertex": self.vertex_x,
            "edge": self.edge_length,
            "wall": self.wall_thickness,
            "opening": self.opening_selector,
        }.get(target_key)
        if focus_widget is not None:
            self._scroll_to(focus_widget)
            focus_widget.setFocus(Qt.FocusReason.OtherFocusReason)

    # ---- Issue #976: 高度な形状 (semantic authoring primitives) ----

    def _auth_form_host(self, rows, hints) -> tuple[QWidget, dict[str, MetricSpinBox]]:
        """Build one per-kind inspector host; ``rows`` is [(key, label, min, max)]."""

        host = QWidget()
        form = QFormLayout(host)
        form.setContentsMargins(0, 0, 0, 0)
        fields: dict[str, MetricSpinBox] = {}
        for key, label, minimum, maximum, hint in rows:
            field = self._metric_field(minimum, maximum)
            field.setToolTip(hint)
            field.valueChanged.connect(self._authoring_fields_changed)
            form.addRow(label, field)
            field_label = form.labelForField(field)
            if field_label is not None:
                field_label.setToolTip(hint)
            fields[key] = field
        return host, fields

    def _build_ceiling_form(self):
        host = QWidget()
        form = QFormLayout(host)
        form.setContentsMargins(0, 0, 0, 0)
        self._auth_ceiling_dir = QComboBox()
        dir_hint = "天井が高くなる方向 · 部屋の平面図で+X=右、+Y=奥"
        self._auth_ceiling_dir.setToolTip(dir_hint)
        for value, label in (
            ('x+', '+X（右側が高い）'),
            ('x-', '-X（左側が高い）'),
            ('y+', '+Y（奥側が高い）'),
            ('y-', '-Y（手前側が高い）'),
        ):
            self._auth_ceiling_dir.addItem(label, value)
        self._auth_ceiling_dir.currentIndexChanged.connect(self._authoring_fields_changed)
        form.addRow('傾斜方向', self._auth_ceiling_dir)
        dir_label = form.labelForField(self._auth_ceiling_dir)
        if dir_label is not None:
            dir_label.setToolTip(dir_hint)
        fields: dict[str, MetricSpinBox] = {}
        for key, label, minimum, maximum, hint in (
            ('low', '最低点', 0.1, 20.0, "傾斜の低い側の天井高"),
            ('high', '最高点', 0.1, 20.0, "傾斜の高い側の天井高 · 最低点以上が必要です"),
        ):
            field = self._metric_field(minimum, maximum)
            field.setToolTip(hint)
            field.valueChanged.connect(self._authoring_fields_changed)
            form.addRow(label, field)
            field_label = form.labelForField(field)
            if field_label is not None:
                field_label.setToolTip(hint)
            fields[key] = field
        return host, fields

    def _build_rect_soffit_form(self):
        rect_hint = "部屋座標での下がり天井の平面範囲（X1<X2、Y1<Y2）"
        return self._auth_form_host(
            (
                ('x1', 'X1', -1000.0, 1000.0, rect_hint),
                ('y1', 'Y1', -1000.0, 1000.0, rect_hint),
                ('x2', 'X2', -1000.0, 1000.0, rect_hint),
                ('y2', 'Y2', -1000.0, 1000.0, rect_hint),
                ('drop', '下がり量', 0.001, 20.0, "天井面からの下がり深さ · 傾斜天井では最も低い点を基準にします"),
            ),
            {},
        )

    def _build_riser_form(self):
        rect_hint = "部屋座標での段床の平面範囲（X1<X2、Y1<Y2）"
        return self._auth_form_host(
            (
                ('x1', 'X1', -1000.0, 1000.0, rect_hint),
                ('y1', 'Y1', -1000.0, 1000.0, rect_hint),
                ('x2', 'X2', -1000.0, 1000.0, rect_hint),
                ('y2', 'Y2', -1000.0, 1000.0, rect_hint),
                ('height', '高さ', 0.001, 20.0, "床面からの段差の高さ"),
            ),
            {},
        )

    def _build_partial_wall_form(self):
        line_hint = "腰壁の中心線の端点（部屋座標）"
        return self._auth_form_host(
            (
                ('x1', '始点X', -1000.0, 1000.0, line_hint),
                ('y1', '始点Y', -1000.0, 1000.0, line_hint),
                ('x2', '終点X', -1000.0, 1000.0, line_hint),
                ('y2', '終点Y', -1000.0, 1000.0, line_hint),
                ('base', '下端', 0.0, 20.0, "床から腰壁下端までの高さ"),
                ('height', '高さ', 0.001, 20.0, "下端からの壁の高さ"),
                ('thickness', '厚み', 0.001, 5.0, "腰壁の厚み"),
            ),
            {},
        )

    def _build_region_form(self):
        host = QWidget()
        form = QFormLayout(host)
        form.setContentsMargins(0, 0, 0, 0)
        fields: dict[str, MetricSpinBox] = {}
        self._auth_region_edge = QComboBox()
        self._auth_region_edge.setToolTip(
            "部屋の外周のどの辺と隣接領域が接するか · 辺の番号は3Dビューの頂点順です"
        )
        self._auth_region_edge.currentIndexChanged.connect(self._authoring_fields_changed)
        form.addRow("共有辺", self._auth_region_edge)
        edge_label = form.labelForField(self._auth_region_edge)
        if edge_label is not None:
            edge_label.setToolTip(self._auth_region_edge.toolTip())
        for key, label, minimum, maximum, hint in (
            ('depth', '奥行き', 0.001, 1000.0, "共有辺から外側への領域の深さ"),
            ('height', '高さ', 0.001, 20.0, "隣接領域の天井高"),
            ('open_offset', '開口位置', 0.0, 1000.0, "共有辺の始点から開口の開始端までの距離"),
            ('open_width', '開口幅', 0.001, 1000.0, "共有辺に沿った接続開口の幅"),
            ('open_height', '開口高さ', 0.001, 20.0, "床から測った接続開口の高さ"),
        ):
            field = self._metric_field(minimum, maximum)
            field.setToolTip(hint)
            field.valueChanged.connect(self._authoring_fields_changed)
            form.addRow(label, field)
            field_label = form.labelForField(field)
            if field_label is not None:
                field_label.setToolTip(hint)
            fields[key] = field
        return host, fields

    def _current_authoring_kind(self) -> str:
        return str(self.authoring_kind.currentData() or 'ceiling')

    def _authoring_items(self, kind: str) -> tuple:
        model = self.geometry.authoring
        if model is None:
            return ()
        if kind == 'ceiling':
            return (model.ceiling,) if model.ceiling is not None else ()
        key = {
            'soffit': 'soffits',
            'riser': 'risers',
            'partial_wall': 'partial_walls',
            'adjacent_region': 'adjacent_regions',
        }[kind]
        return tuple(getattr(model, key))

    @staticmethod
    def _authoring_item_id(kind: str, item) -> str:
        if kind == 'ceiling':
            return 'ceiling'
        return getattr(
            item,
            {
                'soffit': 'soffit_id',
                'riser': 'riser_id',
                'partial_wall': 'wall_id',
                'adjacent_region': 'region_id',
            }[kind],
        )

    def _authoring_model_or_new(self) -> RoomAuthoringModel | None:
        room = self.geometry.room
        if room is None:
            return None
        model = self.geometry.authoring
        if model is None:
            return RoomAuthoringModel(room=room)
        return model

    def _authoring_candidate(
        self, kind: str, spec, *, remove_id: str | None = None
    ) -> RoomAuthoringModel | None:
        """Spec-level add/replace/remove on a copy of the committed model."""

        model = self._authoring_model_or_new()
        if model is None:
            return None
        if kind == 'ceiling':
            update = {'ceiling': spec}
        else:
            key = {
                'soffit': 'soffits',
                'riser': 'risers',
                'partial_wall': 'partial_walls',
                'adjacent_region': 'adjacent_regions',
            }[kind]
            items = list(getattr(model, key))
            if remove_id is not None:
                items = [
                    item
                    for item in items
                    if self._authoring_item_id(kind, item) != remove_id
                ]
            elif spec is not None:
                spec_id = self._authoring_item_id(kind, spec)
                items = [
                    spec if self._authoring_item_id(kind, item) == spec_id else item
                    for item in items
                ]
                if all(self._authoring_item_id(kind, item) != spec_id for item in items):
                    items.append(spec)
            update = {key: tuple(items)}
        return model.model_copy(update=update, deep=True)

    def _authoring_spec_from_fields(self, kind: str, item_id: str):
        if kind == 'ceiling':
            f = self._auth_ceiling_fields
            return SlopedCeilingSpec(
                slope_direction=str(self._auth_ceiling_dir.currentData() or 'x+'),
                low_height_m=f['low'].value_m(),
                high_height_m=f['high'].value_m(),
            )
        if kind == 'soffit':
            f = self._auth_soffit_fields
            return SoffitSpec(
                soffit_id=item_id,
                min_x_m=min(f['x1'].value_m(), f['x2'].value_m()),
                min_y_m=min(f['y1'].value_m(), f['y2'].value_m()),
                max_x_m=max(f['x1'].value_m(), f['x2'].value_m()),
                max_y_m=max(f['y1'].value_m(), f['y2'].value_m()),
                drop_m=f['drop'].value_m(),
            )
        if kind == 'riser':
            f = self._auth_riser_fields
            return RiserSpec(
                riser_id=item_id,
                min_x_m=min(f['x1'].value_m(), f['x2'].value_m()),
                min_y_m=min(f['y1'].value_m(), f['y2'].value_m()),
                max_x_m=max(f['x1'].value_m(), f['x2'].value_m()),
                max_y_m=max(f['y1'].value_m(), f['y2'].value_m()),
                height_m=f['height'].value_m(),
            )
        if kind == 'partial_wall':
            f = self._auth_wall_fields
            return PartialHeightWallSpec(
                wall_id=item_id,
                x1_m=f['x1'].value_m(),
                y1_m=f['y1'].value_m(),
                x2_m=f['x2'].value_m(),
                y2_m=f['y2'].value_m(),
                thickness_m=f['thickness'].value_m(),
                height_m=f['height'].value_m(),
                base_height_m=f['base'].value_m(),
            )
        f = self._auth_region_fields
        return AdjacentRegionSpec(
            region_id=item_id,
            shared_edge_index=int(self._auth_region_edge.currentData() or 0),
            outward_depth_m=f['depth'].value_m(),
            opening=(
                f['open_offset'].value_m(),
                f['open_width'].value_m(),
                f['open_height'].value_m(),
            ),
            ceiling_height_m=f['height'].value_m(),
        )

    def _selected_authoring_ref(self) -> tuple[str, str] | None:
        kind = self._current_authoring_kind()
        item_id = self.authoring_selector.currentData()
        if not isinstance(item_id, str):
            return None
        return (kind, item_id)

    def _authoring_toggled(self, checked: bool) -> None:
        self.authoring_host.setVisible(
            checked and self.geometry.room is not None
        )
        if not checked:
            self.geometry.set_authoring_preview(None)
        self.refresh()

    def _authoring_kind_changed(self) -> None:
        self._populate_authoring_selector()

    def _authoring_item_selected(self) -> None:
        ref = self._selected_authoring_ref()
        if (
            ref is not None
            and ref != self.geometry.selected_authoring
            and self.geometry.authoring is not None
        ):
            try:
                self.geometry.select_authoring_primitive(ref)
            except KeyError:
                pass
        self._populate_authoring_fields()
        self._authoring_fields_changed()

    def _authoring_fields_changed(self) -> None:
        """Ghost-preview the field state in 3D before any commit (#976)."""

        if self.authoring_host.isHidden():
            return
        kind = self._current_authoring_kind()
        ref = self._selected_authoring_ref()
        spec = self._authoring_spec_from_fields(
            kind, ref[1] if ref is not None else f'{kind}-preview'
        )
        candidate = self._authoring_candidate(kind, spec)
        try:
            self.geometry.set_authoring_preview(candidate)
        except (RuntimeError, AttributeError):
            pass

    def _populate_authoring_selector(self) -> None:
        kind = self._current_authoring_kind()
        hosts = {
            'ceiling': self._auth_ceiling_host,
            'soffit': self._auth_soffit_host,
            'riser': self._auth_riser_host,
            'partial_wall': self._auth_wall_host,
            'adjacent_region': self._auth_region_host,
        }
        for key, host in hosts.items():
            host.setVisible(key == kind)
        room = self.geometry.room
        if kind == 'adjacent_region' and room is not None:
            vertices = room_vertices(room)
            previous_edge = self._auth_region_edge.currentData()
            with QSignalBlocker(self._auth_region_edge):
                self._auth_region_edge.clear()
                for index, start in enumerate(vertices):
                    end = vertices[(index + 1) % len(vertices)]
                    self._auth_region_edge.addItem(
                        f"辺 {index + 1}（{self._format_m(hypot(end.x_m - start.x_m, end.y_m - start.y_m))}）",
                        index,
                    )
                if previous_edge is not None:
                    index = self._auth_region_edge.findData(previous_edge)
                    if index >= 0:
                        self._auth_region_edge.setCurrentIndex(index)
        previous = self.authoring_selector.currentData()
        with QSignalBlocker(self.authoring_selector):
            self.authoring_selector.clear()
            self.authoring_selector.addItem("新規 / 未選択", None)
            for item in self._authoring_items(kind):
                self.authoring_selector.addItem(
                    self._authoring_item_id(kind, item),
                    self._authoring_item_id(kind, item),
                )
            target = self.geometry.selected_authoring
            if target is not None and target[0] == kind:
                index = self.authoring_selector.findData(target[1])
                if index >= 0:
                    self.authoring_selector.setCurrentIndex(index)
            elif previous is not None:
                index = self.authoring_selector.findData(previous)
                if index >= 0:
                    self.authoring_selector.setCurrentIndex(index)
        self._populate_authoring_fields()

    def _populate_authoring_fields(self) -> None:
        kind = self._current_authoring_kind()
        ref = self._selected_authoring_ref()
        item = None
        if ref is not None:
            item = next(
                (
                    entry
                    for entry in self._authoring_items(kind)
                    if self._authoring_item_id(kind, entry) == ref[1]
                ),
                None,
            )
        enabled = item is not None and self._pending_preview is None
        self.apply_authoring_button.setEnabled(enabled)
        self.delete_authoring_button.setEnabled(enabled)
        room = self.geometry.room
        if room is None:
            return
        blockers: list[QSignalBlocker] = []

        def _set(fields: dict[str, MetricSpinBox], values: dict[str, float]) -> None:
            for key, field in fields.items():
                blockers.append(QSignalBlocker(field))
                if key in values:
                    field.set_value_m(values[key])

        room_min_x, room_min_y, room_max_x, room_max_y = (
            room.bounds_m if room is not None else (0.0, 0.0, 4.0, 4.0)
        )
        try:
            if kind == 'ceiling':
                _set(
                    self._auth_ceiling_fields,
                    {
                        'low': item.low_height_m if item is not None else room.height_m * 0.85,
                        'high': item.high_height_m if item is not None else room.height_m,
                    },
                )
                if item is not None:
                    blockers.append(QSignalBlocker(self._auth_ceiling_dir))
                    index = self._auth_ceiling_dir.findData(item.slope_direction)
                    if index >= 0:
                        self._auth_ceiling_dir.setCurrentIndex(index)
            elif kind == 'soffit':
                defaults = {
                    'x1': room_min_x + (room_max_x - room_min_x) * 0.25,
                    'y1': room_min_y + (room_max_y - room_min_y) * 0.25,
                    'x2': room_min_x + (room_max_x - room_min_x) * 0.75,
                    'y2': room_min_y + (room_max_y - room_min_y) * 0.75,
                    'drop': 0.3,
                }
                _set(
                    self._auth_soffit_fields,
                    defaults
                    if item is None
                    else {
                        'x1': item.min_x_m, 'y1': item.min_y_m,
                        'x2': item.max_x_m, 'y2': item.max_y_m,
                        'drop': item.drop_m,
                    },
                )
            elif kind == 'riser':
                defaults = {
                    'x1': room_min_x + (room_max_x - room_min_x) * 0.25,
                    'y1': room_min_y + (room_max_y - room_min_y) * 0.25,
                    'x2': room_min_x + (room_max_x - room_min_x) * 0.75,
                    'y2': room_min_y + (room_max_y - room_min_y) * 0.75,
                    'height': 0.15,
                }
                _set(
                    self._auth_riser_fields,
                    defaults
                    if item is None
                    else {
                        'x1': item.min_x_m, 'y1': item.min_y_m,
                        'x2': item.max_x_m, 'y2': item.max_y_m,
                        'height': item.height_m,
                    },
                )
            elif kind == 'partial_wall':
                mid_x = (room_min_x + room_max_x) * 0.5
                defaults = {
                    'x1': mid_x, 'y1': room_min_y + (room_max_y - room_min_y) * 0.25,
                    'x2': mid_x, 'y2': room_min_y + (room_max_y - room_min_y) * 0.75,
                    'base': 0.0, 'height': 1.0, 'thickness': 0.1,
                }
                _set(
                    self._auth_wall_fields,
                    defaults
                    if item is None
                    else {
                        'x1': item.x1_m, 'y1': item.y1_m,
                        'x2': item.x2_m, 'y2': item.y2_m,
                        'base': item.base_height_m, 'height': item.height_m,
                        'thickness': item.thickness_m,
                    },
                )
            else:
                _set(
                    self._auth_region_fields,
                    {
                        'depth': item.outward_depth_m if item is not None else 3.0,
                        'height': (item.ceiling_height_m if item is not None and item.ceiling_height_m is not None else room.height_m),
                        'open_offset': item.opening[0] if item is not None else 0.0,
                        'open_width': item.opening[1] if item is not None else 2.0,
                        'open_height': item.opening[2] if item is not None else min(2.0, room.height_m),
                    },
                )
                if item is not None:
                    blockers.append(QSignalBlocker(self._auth_region_edge))
                    index = self._auth_region_edge.findData(item.shared_edge_index)
                    if index >= 0:
                        self._auth_region_edge.setCurrentIndex(index)
        finally:
            del blockers

    def _commit_authoring(self, candidate: RoomAuthoringModel | None, success: str) -> None:
        if candidate is None:
            return
        self.geometry.set_authoring_preview(None)

        committed_ok: list[bool] = []

        def operation() -> bool:
            changed = bool(self.controller.replace_room_authoring(candidate))
            committed_ok.append(changed)
            return changed

        self._run(operation, success)
        if committed_ok != [True]:
            return  # rejected or no-change — keep _run's notice
        issues = validate_room_authoring_model(
            candidate, wall_topology=self.geometry.topology
        )
        warnings = [issue for issue in issues if issue.severity == 'warning']
        if warnings:
            self.notice.setText(
                success + '（注意: ' + ' / '.join(w.message for w in warnings[:3]) + '）'
            )
            set_semantic_state(self.notice, SemanticState.WARNING)

    def _add_authoring(self) -> None:
        room = self.geometry.room
        if room is None:
            return
        kind = self._current_authoring_kind()
        item_id = 'ceiling' if kind == 'ceiling' else f'{kind}-{uuid4().hex[:8]}'
        spec = self._authoring_spec_from_fields(kind, item_id)
        candidate = self._authoring_candidate(kind, spec)
        if candidate is None:
            return
        label = AUTHORING_KIND_LABELS[kind]
        self._commit_authoring(candidate, f'{label}を追加しました')
        index = self.authoring_selector.findData(item_id)
        if index >= 0:
            self.authoring_selector.setCurrentIndex(index)
            self._authoring_item_selected()

    def _apply_authoring(self) -> None:
        ref = self._selected_authoring_ref()
        if ref is None:
            return
        kind, item_id = ref
        spec = self._authoring_spec_from_fields(kind, item_id)
        candidate = self._authoring_candidate(kind, spec)
        if candidate is None:
            return
        self._commit_authoring(candidate, f'{AUTHORING_KIND_LABELS[kind]}を更新しました')

    def _delete_authoring(self) -> None:
        ref = self._selected_authoring_ref()
        if ref is None:
            return
        kind, item_id = ref
        candidate = self._authoring_candidate(kind, None, remove_id=item_id)
        if candidate is None:
            return
        if self.geometry.selected_authoring == ref:
            try:
                self.geometry.select_authoring_primitive(None)
            except KeyError:
                pass
        self._commit_authoring(candidate, f'{AUTHORING_KIND_LABELS[kind]}を削除しました')


__all__ = ["RoomGeometryPanel"]
