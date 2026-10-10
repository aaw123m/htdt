"""Guided geometry import dialog (Issue #762 / current #35).

Productizes the Issue #167 raw-mesh pipeline — import, deterministic
diagnostics, bounded repair, solver-readiness evaluation — into one guided
surface reachable from the Room workspace. The dialog performs no
persistence: it parses the source asset, runs the canonical diagnostic and
bounded-repair preview, and returns an operator-confirmed
:class:`GeometryImportRequest` that ``RoomWorkspace`` commits through the
controller (declared-authority entity body or ``r120_semantic_geometry``).

Design contract:

- Units are never assumed. Formats that declare a unit (GLB, HTDTMSH1)
  lock the declaration to the format specification; unitless formats
  (OBJ/PLY/STL) require an explicit operator choice, matching
  ``make_mesh_import_authority`` validation.
- Repair is a preview: the bounded plan is applied and re-diagnosed before
  the operator decides to use it, and the exact ``RepairedRawMesh`` +
  ``RepairedRawMeshDiagnosticResult`` pair travels with the request so the
  conversion lineage stays pinned.
- Room-geometry destination converts to ``SemanticAcousticGeometry`` and
  reports the compiler-contract readiness honestly; unresolved conditions
  are shown, never hidden.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt

from .accessible_labels import wire_label_buddies
from .field_tooltips import apply_field_tooltip
from .geometry_import_preview import (
    ImportPreviewScene,
    PreviewDeclaration,
    build_import_preview_scene,
)
from .ingress import read_file_bounded
from .limits import MAX_ATTACHMENT_BYTES
from .mesh_import_authority import format_declared_source_unit
from .raw_mesh import (
    RawVisualMesh,
    diagnose_raw_visual_mesh,
    import_raw_visual_mesh,
)
from .raw_mesh_health import (
    MeshHealthSummary,
    build_mesh_health_summary,
    classify_repair_operation,
    mesh_component_inventory,
)
from .raw_mesh_repair import (
    CorrectConsistentWinding,
    ExactDuplicateVertexConsolidation,
    RawMeshRepairOperation,
    RepairedRawMesh,
    RepairedRawMeshDiagnosticResult,
    RemoveDegenerateFaces,
    RemoveExactDuplicateFaces,
    RemoveUnreferencedVertices,
    ToleranceVertexWeld,
    apply_raw_mesh_repair,
    diagnose_repaired_raw_mesh,
    make_raw_mesh_repair_plan,
    repaired_triangle_ids,
)
from .semantic_geometry import (
    SemanticAcousticGeometry,
    SemanticSurfaceClass,
    SurfaceSemanticAssignment,
    convert_raw_visual_mesh_to_semantic_geometry,
    explicit_identity_source_to_scene_transform,
    make_semantic_geometry_conversion_request,
    raw_triangle_ids,
)
from .ui_theme import (
    ControlSize,
    SemanticState,
    TypographyRole,
    set_control_size,
    set_semantic_state,
    set_typography_role,
)
import logging
from .user_facing_error import operation_error_message

_LOGGER = logging.getLogger(__name__)


GeometryImportDestination = Literal['entity_body', 'room_geometry']


@dataclass(frozen=True)
class GeometryImportRequest:
    """Operator-confirmed import decision returned by the dialog.

    The request is plain data — the Room workspace controller turns it into
    either a declared-authority entity body mesh or a SceneDocument-level
    ``r120_semantic_geometry`` commit.
    """

    destination: GeometryImportDestination
    source_unit: str
    custom_scale_to_meters: float | None
    up_axis: str
    forward_axis: str
    handedness: str
    local_anchor: str
    repaired_mesh: RepairedRawMesh | None
    repaired_diagnostic: RepairedRawMeshDiagnosticResult | None
    surface_assignments: tuple[SurfaceSemanticAssignment, ...]


_UNIT_ITEMS: tuple[tuple[str, str], ...] = (
    ('メートル (m)', 'meters'),
    ('ミリメートル (mm)', 'millimeters'),
    ('センチメートル (cm)', 'centimeters'),
    ('インチ (in)', 'inches'),
    ('フィート (ft)', 'feet'),
    ('カスタム係数…', 'custom'),
)

_AXIS_ITEMS: tuple[tuple[str, str], ...] = (
    ('不明 / そのまま', 'unknown'),
    ('+X', 'x+'),
    ('-X', 'x-'),
    ('+Y', 'y+'),
    ('-Y', 'y-'),
    ('+Z', 'z+'),
    ('-Z', 'z-'),
)

_HANDEDNESS_ITEMS: tuple[tuple[str, str], ...] = (
    ('不明', 'unknown'),
    ('右手系', 'right'),
    ('左手系', 'left'),
)

_ANCHOR_ITEMS: tuple[tuple[str, str], ...] = (
    ('ソース原点', 'source_origin'),
    ('バウンズ中心', 'bounds_center'),
    ('底面中心', 'bottom_center'),
)

_SURFACE_CLASS_ITEMS: tuple[tuple[str, SemanticSurfaceClass | None], ...] = (
    ('部屋の境界面（壁・床・天井）', 'room_boundary'),
    ('オブジェクト表面', 'object_surface'),
    ('割り当てない（不明のまま）', None),
)

# Bounded repair operations offered to the operator, in application order.
# Collapsing operations (consolidation, weld) run before face cleanup so a
# weld can never leave degenerate triangles behind (the repair contract
# requires remove_degenerate_faces after any collapsing op); unreferenced
# vertices are swept last. Fill-hole / surgery / reconstruction stay
# unsupported by design (#762 §3).
_REPAIR_CHECKBOXES: tuple[tuple[str, str, str], ...] = (
    ('exact_duplicate_vertex_consolidation', 'A', '完全一致する頂点を統合'),
    ('tolerance_vertex_weld', 'B', '許容誤差内の頂点を溶接'),
    ('correct_consistent_winding', 'B', '面の巻き方向を統一'),
    ('remove_exact_duplicate_faces', 'A', '完全一致する重複面を削除'),
    ('remove_degenerate_faces', 'A', '面積ゼロの退化面を削除'),
    ('remove_unreferenced_vertices', 'A', '未参照頂点を削除'),
)

_READINESS_STATE_LABELS = {
    'ready': '対応可',
    'ready_with_limitations': '制限付きで対応可',
    'unresolved_items': '未解決項目あり',
    'blocked': 'ブロック',
    'not_validated': '未検証',
}

_READINESS_TARGET_LABELS = {
    'visual_mesh': '表示メッシュ',
    'semantic_room': 'セマンティック部屋面',
    'ga_direct_early': 'GA直接音/初期反射',
    'wave_closed_volume': '波動ソルバー（閉空間）',
    'general_3d_production': '一般3D制作',
}

_SEVERITY_LABELS = {
    'blocker': 'ブロッカー',
    'warning': '警告',
    'info': '情報',
}

_CATEGORY_LABELS = {
    'topology': 'トポロジー',
    'surface_quality': '面品質',
    'acoustic_model': '音響モデル',
}

_COMPILER_READINESS_LABELS = {
    'ready_for_r120_geometry_compiler_contract': 'R120ジオメトリコンパイラー契約に対応可',
    'blocked_by_geometry': 'ブロック: ジオメトリ未解決',
    'blocked_by_surface_semantics': 'ブロック: 面の意味分類が未割当',
}

#: The mesh issue codes in raw_mesh_health._ISSUE_TEXT are the authority's
#: diagnostic language; the dialog renders their localized names and the
#: recommended next action in Japanese.
_ISSUE_CODE_LABELS = {
    'open_boundary': '開放境界',
    'non_manifold_edge': '非多様体エッジ',
    'duplicate_face': '重複面',
    'overlapping_face': '重なり面',
    'inverted_normal': '法線反転',
    'sliver_face': '細長面',
    'tiny_feature': '微小フィーチャ',
    'watertightness': '水密性',
}

_ISSUE_ACTION_LABELS = {
    'open_boundary': '開放境界エッジを確認し、小さな穴を閉じるか意図的な開口として扱ってください',
    'non_manifold_edge': '非多様体エッジを確認してください — クリーンアップではなく再構築が必要な場合があります',
    'duplicate_face': '決定論的クリーンアップで完全一致する重複を削除してください',
    'overlapping_face': '重なりを確認してください — 修復はジオメトリ変更を伴うため手動で解決します',
    'inverted_normal': '向きが確定的に解決できる面のみ巻き方向を修正してください',
    'sliver_face': '細長面を確認してください — 境界付き修復には明示的な許容誤差が必要です',
    'tiny_feature': '成分を棚卸ししてください — 保持・非表示・除外を明示的に選びます（自動削除しません）',
    'watertightness': '漏れている境界を特定し、隙間ごとに壁か意図的な開口か判断してください',
}

_ACOUSTIC_VOLUME_LABELS = {
    'not_ready': '未対応',
    'geometry_checks_pass_but_semantic_conversion_required':
        '検査合格（意味変換が必要）',
}

_REPAIR_OPERATION_LABELS = {
    'exact_duplicate_vertex_consolidation': '完全一致頂点の統合',
    'tolerance_vertex_weld': '許容誤差内の頂点溶接',
    'correct_consistent_winding': '面の巻き方向の統一',
    'remove_exact_duplicate_faces': '完全一致する重複面の削除',
    'remove_degenerate_faces': '退化面の削除',
    'remove_unreferenced_vertices': '未参照頂点の削除',
    'fill_hole': '穴埋め',
    'large_gap_closure': '大きな隙間の閉鎖',
    'non_manifold_surgery': '非多様体の手術的修復',
    'self_intersection_remesh': '自己交差のリメッシュ',
    'boolean_reconstruction': 'ブール演算による再構築',
    'point_cloud_surface_reconstruction': '点群からの表面再構築',
    'overlapping_surface_resolution': '重なり面の解決',
    'acoustic_room_inference': '音響空間の推定',
}

_REPAIR_RISK_LABELS = {
    'A_deterministic': 'A・決定論的',
    'B_bounded': 'B・境界付き',
    'C_semantic': 'C・意味分類',
    'unsupported': '対象外',
}

_PREVIEW_VIEW_ITEMS: tuple[tuple[str, str], ...] = (
    ('元のメッシュ', 'original'),
    ('修復済みメッシュ', 'repaired'),
    ('修復差分', 'diff'),
)

_PREVIEW_NOTICE_TEXTS = {
    'unit_undeclared': '単位未宣言 — ソース座標をそのまま表示（m換算なし）',
    'axis_convention_unresolved': '軸・座標系の宣言が未解決 — 回転なしで表示',
    'preview_decimated': '表示用に間引き済み（取込データは変更されません）',
}

#: Diff-view fate colors (render space). Neutral kept faces, then
#: distinct hues for each lineage fate so removed/flipped/moved regions
#: read at a glance; 'unknown' stays grey, never a guessed color.
_FATE_COLORS = {
    'kept': '#8FB8C9',
    'flipped': '#E4C06B',
    'moved': '#BD9CF4',
    'removed': '#E0655A',
    'unknown': '#6E7B87',
}

_FATE_LABELS = {
    'kept': '保持',
    'flipped': '巻き方向修正',
    'moved': '頂点移動（連結修復）',
    'removed': '削除',
    'unknown': '判定不能',
}

_FOCUS_ALL = '全体'

#: Above this drawn-face count edge display is skipped — the wireframe
#: would dominate the render cost on software GL for no visual gain.
_PREVIEW_EDGE_FACE_LIMIT = 20_000


def _unresolved_finding_label(text: str) -> str:
    """Localize a repair-runner unresolved finding (``kind:detail``)."""
    head, _, detail = text.partition(':')
    if head == 'unsupported_operation':
        kind = detail
        return f'{_REPAIR_OPERATION_LABELS.get(kind, kind)}は対象外のため未解決'
    if head == 'blocked_operation':
        kind, _, detail = detail.partition(':')
        name = _REPAIR_OPERATION_LABELS.get(kind, kind)
        if detail == 'would_remove_all_faces':
            return f'{name}がブロックされました（全面が削除されるため）'
        if detail.startswith('non_manifold_components='):
            count = detail.partition('=')[2]
            return f'{name}がブロックされました（非多様体成分 {count} 件）'
        if detail.startswith('non_orientable_components='):
            count = detail.partition('=')[2]
            return f'{name}がブロックされました（非可配向成分 {count} 件）'
        return f'{name}がブロックされました（{detail or "詳細不明"}）'
    return text


def _diagnostic_count_text(findings) -> str:
    failed = [f for f in findings if f.state == 'fail']
    unknown = [f for f in findings if f.state == 'unknown']
    if not failed and not unknown:
        return 'すべてのジオメトリ検査に合格しました'
    parts = []
    if failed:
        parts.append(f'不合格 {len(failed)} 件')
    if unknown:
        parts.append(f'判定不能 {len(unknown)} 件')
    return ' / '.join(parts)


class GeometryImportDialog(QDialog):
    """One guided import: declare axes/units → QA → repair preview → commit."""

    def __init__(
        self,
        file_path: str | Path,
        *,
        entity_target: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._path = Path(file_path)
        self._entity_target = entity_target
        self.mesh: RawVisualMesh = import_raw_visual_mesh(
            read_file_bounded(
                self._path,
                MAX_ATTACHMENT_BYTES,
                label='幾何ソースファイル',
            ),
            source_name=self._path.name,
        )
        self.diagnostics = diagnose_raw_visual_mesh(self.mesh)
        self.health: MeshHealthSummary = build_mesh_health_summary(
            self.diagnostics, mesh=self.mesh, created_at_utc='1970-01-01T00:00:00+00:00'
        )
        self._repaired_mesh: RepairedRawMesh | None = None
        self._repaired_diagnostic: RepairedRawMeshDiagnosticResult | None = None
        self._evaluated_geometry: SemanticAcousticGeometry | None = None
        self._preview_plotter = None
        self._preview_render_state: str | None = None

        self.setWindowTitle('ジオメトリをインポート')
        self.resize(1120, 720)
        layout = QVBoxLayout(self)

        source = QLabel(
            f'{self._path.name} — {self.mesh.provenance.asset_format} · '
            f'頂点 {len(self.mesh.vertices)} / 面 {len(self.mesh.triangles)}'
        )
        set_typography_role(source, TypographyRole.SECTION_TITLE)
        layout.addWidget(source)

        # The four group boxes measure ~1130px stacked — far past a 768px
        # screen, so the resize(680, 720) alone hid the import button and
        # lower groups. Keep the header and the Ok/Cancel row pinned and
        # scroll the content instead (same pattern as _scroll_wrap in
        # measurement_record_surfaces).
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addWidget(self._build_declaration_group())
        content_layout.addWidget(self._build_diagnostics_group())
        content_layout.addWidget(self._build_repair_group())
        content_layout.addWidget(self._build_destination_group())
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(content)

        # #980: read-only 3D preview beside the import controls — the
        # declared unit/axis/anchor and repair lineage re-render it.
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(scroll)
        splitter.addWidget(self._build_preview_pane())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([640, 480])
        layout.addWidget(splitter, stretch=1)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText('インポート')
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        wire_label_buddies(self)

    def done(self, result: int) -> None:
        # Release the VTK render window with the dialog — the preview
        # plotter holds GL resources that outlive the widget otherwise.
        plotter = getattr(self, '_preview_plotter', None)
        if plotter is not None:
            try:
                plotter.close()
            except Exception:  # error-boundary: teardown — a plotter close failure logs and the dialog still releases its reference (noqa: BLE001)
                _LOGGER.warning('preview plotter close failed', exc_info=True)
            self._preview_plotter = None
        super().done(result)

    # --- declaration group ---------------------------------------------------

    def _build_declaration_group(self) -> QGroupBox:
        group = QGroupBox('ソースの単位・座標系（オペレーターによる宣言）')
        form = QFormLayout(group)

        self.unit_combo = QComboBox()
        for label, unit in _UNIT_ITEMS:
            self.unit_combo.addItem(label, unit)
        spec_unit = format_declared_source_unit(self.mesh.provenance.asset_format)
        if spec_unit == 'meters':
            index = self.unit_combo.findData('meters')
            self.unit_combo.setCurrentIndex(index)
            self.unit_combo.setEnabled(False)
            self.unit_combo.setToolTip(
                'このフォーマットはメートルを宣言しています（フォーマット仕様）'
            )
        else:
            self.unit_combo.insertItem(0, '（単位を選択してください）', None)
            self.unit_combo.setCurrentIndex(0)
            self.unit_combo.setToolTip(
                'OBJ/PLY/STL は単位を持ちません — 明示的な宣言が必須です'
            )
        self.unit_combo.activated.connect(lambda _i: self._sync_unit_state())
        form.addRow('ソース単位', self.unit_combo)

        self.custom_scale = QDoubleSpinBox()
        self.custom_scale.setDecimals(9)
        self.custom_scale.setRange(1e-9, 1e9)
        self.custom_scale.setValue(1.0)
        self.custom_scale.setSuffix(' m/ソース単位')
        self.custom_scale.setEnabled(False)
        apply_field_tooltip(
            self.custom_scale,
            '1ソース単位あたりのメートル数 — 単位に「カスタム」を選んだとき有効',
            form,
        )
        self.custom_scale.valueChanged.connect(lambda _v: self._refresh_preview())
        form.addRow('カスタム係数', self.custom_scale)

        self.up_axis = QComboBox()
        for label, axis in _AXIS_ITEMS:
            self.up_axis.addItem(label, axis)
        self.forward_axis = QComboBox()
        for label, axis in _AXIS_ITEMS:
            self.forward_axis.addItem(label, axis)
        self.handedness = QComboBox()
        for label, value in _HANDEDNESS_ITEMS:
            self.handedness.addItem(label, value)
        apply_field_tooltip(self.up_axis, 'ソースモデルの上方向として宣言する軸', form)
        apply_field_tooltip(self.forward_axis, 'ソースモデルの前方向として宣言する軸', form)
        apply_field_tooltip(self.handedness, 'ソースの座標系の向き（右手系・左手系）', form)
        for combo in (self.up_axis, self.forward_axis, self.handedness):
            combo.activated.connect(lambda _i: self._refresh_preview())
        form.addRow('上方向軸', self.up_axis)
        form.addRow('前方向軸', self.forward_axis)
        form.addRow('座標系の向き', self.handedness)

        self.anchor = QComboBox()
        for label, value in _ANCHOR_ITEMS:
            self.anchor.addItem(label, value)
        apply_field_tooltip(
            self.anchor, 'インポート後のローカル原点の置き方', form
        )
        self.anchor.activated.connect(lambda _i: self._refresh_preview())
        form.addRow('ローカル原点', self.anchor)
        return group

    def _sync_unit_state(self) -> None:
        is_custom = self.unit_combo.currentData() == 'custom'
        self.custom_scale.setEnabled(is_custom)
        self._refresh_preview()

    # --- read-only 3D preview pane (#980) -------------------------------------

    def _build_preview_pane(self) -> QGroupBox:
        group = QGroupBox('3Dプレビュー（読み取り専用）')
        layout = QVBoxLayout(group)

        controls = QHBoxLayout()
        self.preview_view = QComboBox()
        for label, mode in _PREVIEW_VIEW_ITEMS:
            self.preview_view.addItem(label, mode)
        # Repaired/diff views only exist after a repair preview ran; the
        # items stay visible but disabled until then (never silently
        # re-showing stale repair data).
        self._set_repair_views_enabled(False)
        self.preview_view.setToolTip(
            '元のメッシュ・修復済みメッシュ・修復差分を切り替えます'
        )
        self.preview_view.activated.connect(lambda _i: self._refresh_preview())
        controls.addWidget(self.preview_view)

        self.preview_focus = QComboBox()
        self.preview_focus.addItem(_FOCUS_ALL, None)
        seen: set[str] = set()
        for issue in self.health.issues:
            if issue.code in seen:
                continue
            seen.add(issue.code)
            self.preview_focus.addItem(
                _ISSUE_CODE_LABELS.get(issue.code, issue.code), issue.code
            )
        self.preview_focus.setToolTip(
            '欠陥カテゴリを選ぶと該当位置にフォーカスします'
        )
        self.preview_focus.activated.connect(lambda _i: self._refresh_preview())
        controls.addWidget(self.preview_focus)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.preview_info = QLabel('')
        self.preview_info.setWordWrap(True)
        set_typography_role(self.preview_info, TypographyRole.SECONDARY)
        layout.addWidget(self.preview_info)

        self._preview_plotter = self._make_preview_plotter(group)
        if self._preview_plotter is not None:
            layout.addWidget(self._preview_plotter.interactor, stretch=1)
        else:
            fallback = QLabel('3Dプレビューを初期化できませんでした')
            fallback.setWordWrap(True)
            set_semantic_state(fallback, SemanticState.ERROR)
            layout.addWidget(fallback, stretch=1)
        self._refresh_preview()
        return group

    def _make_preview_plotter(self, parent: QWidget):
        """Create the PyVista interactor, or ``None`` when VTK cannot start.

        ``None`` is a render-layer failure (driver/GL), distinct from the
        adapter's ``unsupported_geometry`` — the dialog still works.
        """
        try:
            import pyvista as pv  # noqa: F401
            from pyvistaqt import QtInteractor
        except Exception:  # error-boundary: optional import — a missing/broken render stack honestly yields no preview layer (noqa: BLE001)
            return None
        try:
            plotter = QtInteractor(parent, auto_update=False)
            plotter.set_background('#171F27')
            plotter.enable_anti_aliasing('fxaa')
            return plotter
        except Exception:  # error-boundary: render-layer construction — a GL/driver failure honestly yields no preview layer (noqa: BLE001)
            return None

    def _set_repair_views_enabled(self, enabled: bool) -> None:
        model = self.preview_view.model()
        for index, (_label, mode) in enumerate(_PREVIEW_VIEW_ITEMS):
            if mode != 'original':
                model.item(index).setEnabled(enabled)

    def _preview_declaration(self) -> PreviewDeclaration:
        unit = self.unit_combo.currentData()
        return PreviewDeclaration(
            source_unit=unit,
            custom_scale_to_meters=(
                float(self.custom_scale.value()) if unit == 'custom' else None
            ),
            up_axis=str(self.up_axis.currentData()),
            forward_axis=str(self.forward_axis.currentData()),
            handedness=str(self.handedness.currentData()),
            local_anchor=str(self.anchor.currentData()),
        )

    @staticmethod
    def _to_render(point: tuple[float, float, float]) -> tuple[float, float, float]:
        """Entity-local → VTK render space, same map as the room viewport."""
        return (point[0], -point[1], point[2])

    def _refresh_preview(self) -> None:
        if not hasattr(self, 'preview_info'):
            return  # declaration signals can fire before the pane exists
        view_mode = self.preview_view.currentData()
        repaired = self._repaired_mesh
        scene = build_import_preview_scene(
            self.mesh,
            self.diagnostics,
            self._preview_declaration(),
            view_mode=view_mode,
            repaired=repaired if view_mode != 'original' else None,
            focus_codes=tuple(
                self.preview_focus.itemData(i)
                for i in range(self.preview_focus.count())
                if self.preview_focus.itemData(i) is not None
            ),
        )
        self._render_preview_scene(scene)

    def _preview_info_text(self, scene: ImportPreviewScene) -> str:
        unit_label = 'm' if scene.coordinate_space == 'meters' else 'ソース単位'
        dims = ' × '.join(f'{value:.3g}' for value in scene.dims)
        parts = [
            f'頂点 {scene.vertex_count_full} / 面 {scene.face_count_full}'
            + (f'（表示 {len(scene.faces)} 面）' if scene.decimated else ''),
            f'寸法 {dims} {unit_label}',
        ]
        if scene.view_mode == 'diff' and scene.source_fates:
            counts: dict[str, int] = {}
            for fate in scene.source_fates:
                counts[fate] = counts.get(fate, 0) + 1
            parts.append(
                '差分: '
                + ' · '.join(
                    f'{_FATE_LABELS[fate]} {counts[fate]}'
                    for fate in ('kept', 'flipped', 'moved', 'removed', 'unknown')
                    if counts.get(fate)
                )
            )
        parts.extend(
            _PREVIEW_NOTICE_TEXTS[notice]
            for notice in scene.notices
            if notice in _PREVIEW_NOTICE_TEXTS
        )
        if self._preview_render_state == 'render_failure':
            parts.append('3D描画に失敗しました（メッシュ自体は取込可能です）')
        return '\n'.join(parts)

    def _render_preview_scene(self, scene: ImportPreviewScene) -> None:
        focus_code = self.preview_focus.currentData()
        focus = next(
            (item for item in scene.focuses if item.code == focus_code), None
        )
        if self._preview_plotter is None:
            self.preview_info.setText(self._preview_info_text(scene))
            return
        self._preview_render_state = None
        plotter = self._preview_plotter
        try:
            import numpy as np
            import pyvista as pv

            plotter.clear()
            if not scene.supported:
                self.preview_info.setText(
                    ('このジオメトリは3D表示できません'
                        if scene.unsupported_reason == 'unsupported_geometry'
                        else '修復プレビューを先に実行してください')
                    + '\n'
                    + self._preview_info_text(scene)
                )
                return
            points = np.asarray(
                [self._to_render(p) for p in scene.vertices], dtype=float
            )
            faces_flat = np.hstack(
                [[3, *face] for face in scene.faces]
            ).astype(np.int64)
            surface = pv.PolyData(points, faces_flat)
            show_edges = len(scene.faces) <= _PREVIEW_EDGE_FACE_LIMIT
            if scene.view_mode == 'diff' and scene.source_fates:
                for fate in ('kept', 'flipped', 'moved', 'removed', 'unknown'):
                    cells = [
                        index
                        for index, source_index in enumerate(scene.kept_face_indices)
                        if scene.source_fates[source_index] == fate
                    ]
                    if not cells:
                        continue
                    plotter.add_mesh(
                        surface.extract_cells(cells),
                        color=_FATE_COLORS[fate],
                        show_edges=show_edges,
                        label=f'{_FATE_LABELS[fate]} {len(cells)}',
                    )
            else:
                plotter.add_mesh(
                    surface,
                    color='#8FB8C9',
                    show_edges=show_edges,
                    label='インポートメッシュ',
                )
            # Origin marker + axes triad + coordinate grid + dimensioned bbox.
            plotter.add_mesh(
                pv.Sphere(radius=max(scene.dims) * 0.02 or 0.01),
                color='#E9CE7A',
            )
            plotter.add_axes()
            bounds_render = (
                scene.bbox_min[0], scene.bbox_max[0],
                -scene.bbox_max[1], -scene.bbox_min[1],
                scene.bbox_min[2], scene.bbox_max[2],
            )
            bbox = pv.Box(bounds=bounds_render)
            plotter.add_mesh(bbox, style='wireframe', color='#4CC5B1')
            plotter.show_grid()
            if focus is not None and focus.state == 'located' and focus.points:
                markers = np.asarray(
                    [self._to_render(p) for p in focus.points], dtype=float
                )
                radius = max(max(scene.dims) * 0.03, 1e-6)
                for point in markers:
                    plotter.add_mesh(
                        pv.Sphere(radius=radius, center=point),
                        color='#E0655A',
                    )
                centroid = markers.mean(axis=0)
                distance = max(max(scene.dims) * 1.5, radius * 8.0)
                plotter.camera.focal_point = tuple(centroid)
                plotter.camera.position = (
                    centroid[0] + distance,
                    centroid[1] - distance,
                    centroid[2] + distance * 0.6,
                )
            elif focus is not None and focus.state != 'located':
                self._preview_render_state = 'focus_unknown'
            plotter.reset_camera() if focus is None else plotter.render()
        except Exception:  # error-boundary: preview render — a draw failure records the honest 'render_failure' state, never a fake preview (noqa: BLE001)
            self._preview_render_state = 'render_failure'
        info = self._preview_info_text(scene)
        if self._preview_render_state == 'focus_unknown':
            info += '\nこの欠陥の位置は判定不能です（メッシュ全体を表示）'
        self.preview_info.setText(info)

    # --- diagnostics group ---------------------------------------------------

    def _build_diagnostics_group(self) -> QGroupBox:
        group = QGroupBox('ジオメトリ診断（QA）')
        layout = QVBoxLayout(group)
        inventory = mesh_component_inventory(self.mesh)
        components = f'{len(inventory.components)} 連結成分'
        self.diagnostic_summary = QLabel(
            _diagnostic_count_text(self.diagnostics.findings)
            + f' · {components} · '
            f'音響体積: '
            f'{_ACOUSTIC_VOLUME_LABELS.get(self.diagnostics.acoustic_volume_readiness, self.diagnostics.acoustic_volume_readiness)}'
        )
        self.diagnostic_summary.setWordWrap(True)
        layout.addWidget(self.diagnostic_summary)

        self.issues_table = QTableWidget(0, 5)
        self.issues_table.setAccessibleName('検査結果一覧')
        self.issues_table.setHorizontalHeaderLabels(
            ('深刻度', '分類', '検査', '件数', '推奨対処')
        )
        self.issues_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.issues_table.setToolTip(
            'ジオメトリ検査の結果一覧 — 深刻度・分類・推奨対処を確認できます'
        )
        self.issues_table.horizontalHeader().setStretchLastSection(True)
        self.issues_table.setMaximumHeight(150)
        self._populate_issues(self.health)
        layout.addWidget(self.issues_table)

        self.readiness_label = QLabel(self._readiness_text(self.health))
        self.readiness_label.setWordWrap(True)
        set_typography_role(self.readiness_label, TypographyRole.SECONDARY)
        layout.addWidget(self.readiness_label)
        return group

    def _populate_issues(self, health: MeshHealthSummary) -> None:
        self.issues_table.setRowCount(len(health.issues))
        for row, issue in enumerate(health.issues):
            severity = QTableWidgetItem(
                _SEVERITY_LABELS.get(issue.severity, issue.severity)
            )
            if issue.severity == 'blocker':
                severity.setForeground(Qt.GlobalColor.red)
            self.issues_table.setItem(row, 0, severity)
            self.issues_table.setItem(
                row, 1, QTableWidgetItem(_CATEGORY_LABELS.get(issue.category, issue.category))
            )
            self.issues_table.setItem(
                row, 2,
                QTableWidgetItem(_ISSUE_CODE_LABELS.get(issue.code, issue.code)),
            )
            self.issues_table.setItem(
                row, 3, QTableWidgetItem('' if issue.count is None else str(issue.count))
            )
            self.issues_table.setItem(
                row, 4,
                QTableWidgetItem(
                    _ISSUE_ACTION_LABELS.get(issue.code, issue.action)
                ),
            )
        self.issues_table.resizeColumnsToContents()

    def _readiness_text(self, health: MeshHealthSummary) -> str:
        return ' · '.join(
            f'{_READINESS_TARGET_LABELS.get(row.target, row.target)}='
            f'{_READINESS_STATE_LABELS.get(row.state, row.state)}'
            for row in health.readiness
        )

    # --- repair preview group -------------------------------------------------

    def _build_repair_group(self) -> QGroupBox:
        group = QGroupBox('境界付き修復プレビュー')
        layout = QVBoxLayout(group)
        hint = QLabel(
            '決定論的（A）・境界付き（B）の修復のみ実行できます。'
            '穴埋め・再構築などの意味分類（C）・対象外の操作は実行できません。'
        )
        hint.setWordWrap(True)
        set_typography_role(hint, TypographyRole.SECONDARY)
        layout.addWidget(hint)

        self.repair_checks: dict[str, QCheckBox] = {}
        for kind, risk, label in _REPAIR_CHECKBOXES:
            check = QCheckBox(f'[{risk}] {label}')
            risk_class = classify_repair_operation(kind)
            check.setToolTip(
                f'リスククラス: {_REPAIR_RISK_LABELS.get(risk_class, risk_class)}'
            )
            self.repair_checks[kind] = check
            layout.addWidget(check)

        weld_row = QHBoxLayout()
        weld_row.addWidget(QLabel('溶接許容誤差（ソース単位）:'))
        self.weld_tolerance = QDoubleSpinBox()
        self.weld_tolerance.setDecimals(9)
        self.weld_tolerance.setRange(1e-12, 1e6)
        self.weld_tolerance.setValue(1e-6)
        self.weld_tolerance.setToolTip(
            '近接頂点を結合する許容誤差（ソース単位）— 溶接修復を選んだとき有効'
        )
        self.weld_tolerance.lineEdit().setToolTip(self.weld_tolerance.toolTip())
        weld_row.addWidget(self.weld_tolerance)
        weld_row.addStretch(1)
        layout.addLayout(weld_row)

        preview_row = QHBoxLayout()
        self.preview_button = QPushButton('修復をプレビュー')
        self.preview_button.setToolTip(
            '選択した修復操作をプレビュー実行し、前後の頂点・面数を確認します'
        )
        set_control_size(self.preview_button, ControlSize.COMPACT)
        self.preview_button.clicked.connect(self._preview_repair)
        preview_row.addWidget(self.preview_button)
        self.use_repaired = QCheckBox('修復済みメッシュを使用')
        self.use_repaired.setToolTip(
            'プレビューした修復済みメッシュをインポートに使います'
        )
        self.use_repaired.setEnabled(False)
        self.use_repaired.toggled.connect(lambda _on: self._refresh_preview())
        preview_row.addWidget(self.use_repaired)
        preview_row.addStretch(1)
        layout.addLayout(preview_row)

        self.repair_result = QLabel('')
        self.repair_result.setWordWrap(True)
        # Three-line floor: the before→after counts must stay visible.
        self.repair_result.setMinimumHeight(
            self.repair_result.fontMetrics().lineSpacing() * 3
        )
        set_typography_role(self.repair_result, TypographyRole.SECONDARY)
        layout.addWidget(self.repair_result)
        return group

    def _selected_operations(self) -> tuple[RawMeshRepairOperation, ...]:
        operations: list[RawMeshRepairOperation] = []
        for kind, _risk, _label in _REPAIR_CHECKBOXES:
            if not self.repair_checks[kind].isChecked():
                continue
            if kind == 'exact_duplicate_vertex_consolidation':
                operations.append(ExactDuplicateVertexConsolidation())
            elif kind == 'remove_unreferenced_vertices':
                operations.append(RemoveUnreferencedVertices())
            elif kind == 'remove_exact_duplicate_faces':
                operations.append(RemoveExactDuplicateFaces())
            elif kind == 'remove_degenerate_faces':
                operations.append(
                    RemoveDegenerateFaces(area_tolerance_source_units_squared=0.0)
                )
            elif kind == 'correct_consistent_winding':
                operations.append(CorrectConsistentWinding())
            elif kind == 'tolerance_vertex_weld':
                operations.append(
                    ToleranceVertexWeld(
                        tolerance_source_units=float(self.weld_tolerance.value())
                    )
                )
        return tuple(operations)

    def _preview_repair(self) -> None:
        operations = self._selected_operations()
        if not operations:
            self.repair_result.setText('修復操作を選択してください')
            self.use_repaired.setEnabled(False)
            self.use_repaired.setChecked(False)
            return
        try:
            plan = make_raw_mesh_repair_plan(
                self.mesh,
                self.diagnostics,
                operations=operations,
                requested_by='explicit_user_selected',
                request_reason='ガイド付きインポートダイアログ修復プレビュー (#762)',
            )
            repaired = apply_raw_mesh_repair(self.mesh, self.diagnostics, plan)
            diagnostic = diagnose_repaired_raw_mesh(self.mesh, repaired)
        except (ValueError, KeyError) as exc:
            self._repaired_mesh = None
            self._repaired_diagnostic = None
            self.use_repaired.setEnabled(False)
            self.use_repaired.setChecked(False)
            self._set_repair_views_enabled(False)
            self._refresh_preview()
            self.repair_result.setText(f'修復プレビューに失敗しました: {operation_error_message(exc)}')
            set_semantic_state(self.repair_result, SemanticState.ERROR)
            return
        set_semantic_state(self.repair_result, None)
        self._repaired_mesh = repaired
        self._repaired_diagnostic = diagnostic
        self.use_repaired.setEnabled(True)
        self.use_repaired.setChecked(True)
        self._set_repair_views_enabled(True)
        self._refresh_preview()

        applied = [
            result.operation.kind
            for result in repaired.operation_results
            if result.execution_state == 'applied'
        ]
        unresolved = list(repaired.unsupported_unresolved_findings)
        applied_labels = [
            _REPAIR_OPERATION_LABELS.get(kind, kind) for kind in applied
        ]
        self.repair_result.setText(
            f'頂点 {repaired.vertex_count_before}→{repaired.vertex_count_after} · '
            f'面 {repaired.triangle_count_before}→{repaired.triangle_count_after} · '
            f'適用: {("・".join(applied_labels)) or "なし"}'
            + (
                f' · 未解決: {"・".join(_unresolved_finding_label(u) for u in unresolved)}'
                if unresolved
                else ''
            )
            + f' · 修復後: {_diagnostic_count_text(diagnostic.findings)}'
        )

    # --- destination group ----------------------------------------------------

    def _build_destination_group(self) -> QGroupBox:
        group = QGroupBox('取り込み先')
        layout = QVBoxLayout(group)
        self.entity_radio = QRadioButton()
        if self._entity_target is None:
            self.entity_radio.setText('選択中のオブジェクトのボディ（オブジェクト未選択）')
            self.entity_radio.setEnabled(False)
        else:
            self.entity_radio.setText(
                f'選択中のオブジェクト「{self._entity_target}」のボディ'
            )
            self.entity_radio.setChecked(True)
        layout.addWidget(self.entity_radio)

        self.entity_radio.setToolTip(
            'インポート結果を選択中オブジェクトのボディとして取り込みます'
        )
        self.room_radio = QRadioButton('部屋の音響ジオメトリ（R120 ソルバー契約）')
        self.room_radio.setToolTip(
            'インポート結果を部屋の音響ジオメトリとして取り込みます'
        )
        self.room_radio.setChecked(self._entity_target is None)
        self.room_radio.toggled.connect(lambda _on: self._sync_surface_combo())
        layout.addWidget(self.room_radio)

        surface_row = QHBoxLayout()
        surface_row.addWidget(QLabel('全ての面の意味分類:'))
        self.surface_class = QComboBox()
        for label, value in _SURFACE_CLASS_ITEMS:
            self.surface_class.addItem(label, value)
        self.surface_class.setToolTip(
            '取り込む全ての面に付ける意味分類（部屋ジオメトリ向け）'
        )
        surface_row.addWidget(self.surface_class, 1)
        evaluate = QPushButton('ソルバー適性を評価')
        evaluate.setToolTip(
            'インポート実行時と同じ変換でソルバー適性を事前評価します'
        )
        set_control_size(evaluate, ControlSize.COMPACT)
        evaluate.clicked.connect(self._evaluate_readiness)
        surface_row.addWidget(evaluate)
        layout.addLayout(surface_row)

        self.destination_readiness = QLabel('')
        self.destination_readiness.setWordWrap(True)
        self.destination_readiness.setMinimumHeight(
            self.destination_readiness.fontMetrics().lineSpacing() * 2
        )
        set_typography_role(self.destination_readiness, TypographyRole.SECONDARY)
        layout.addWidget(self.destination_readiness)
        self._sync_surface_combo()
        return group

    def _sync_surface_combo(self) -> None:
        self.surface_class.setEnabled(self.room_radio.isChecked())

    def _effective_meshes(self):
        """(display_mesh, repaired_mesh, repaired_diagnostic) for conversion."""

        if self.use_repaired.isChecked() and self._repaired_mesh is not None:
            return self._repaired_mesh, self._repaired_mesh, self._repaired_diagnostic
        return self.mesh, None, None

    def _surface_assignments(self) -> tuple[SurfaceSemanticAssignment, ...]:
        surface_class = self.surface_class.currentData()
        if surface_class is None:
            return ()
        display_mesh, repaired, _diag = self._effective_meshes()
        ids = (
            repaired_triangle_ids(repaired)
            if repaired is not None
            else raw_triangle_ids(display_mesh)
        )
        if not ids:
            return ()
        return (
            SurfaceSemanticAssignment(
                surface_key='imported',
                triangle_ids=ids,
                semantic_class=surface_class,
            ),
        )

    def _evaluate_readiness(self) -> SemanticAcousticGeometry | None:
        """Run the conversion exactly as commit would; report readiness."""

        try:
            _display, repaired, diagnostic = self._effective_meshes()
            request = make_semantic_geometry_conversion_request(
                self.mesh,
                source_scene_revision_id=None,
                source_to_scene_transform=explicit_identity_source_to_scene_transform(
                    reason='ガイド付きインポートプレビュー (コントローラーがシーン変換を供給)'
                ),
                surface_assignments=self._surface_assignments(),
                repaired_mesh=repaired,
                repaired_diagnostic=diagnostic,
            )
            geometry = convert_raw_visual_mesh_to_semantic_geometry(
                self.mesh, request, repaired_mesh=repaired, repaired_diagnostic=diagnostic
            )
        except ValueError as exc:
            self.destination_readiness.setText(f'評価できません: {operation_error_message(exc)}')
            set_semantic_state(self.destination_readiness, SemanticState.ERROR)
            self._evaluated_geometry = None
            return None
        self._evaluated_geometry = geometry
        readiness = _COMPILER_READINESS_LABELS.get(
            geometry.geometry_compiler_readiness, geometry.geometry_compiler_readiness
        )
        unresolved = ' / '.join(geometry.unresolved_conditions) or 'なし'
        suggestions = ' / '.join(
            suggestion.guidance for suggestion in geometry.guided_repair_suggestions
        )
        self.destination_readiness.setText(
            f'{readiness} · 未解決: {unresolved}'
            + (f' · 提案: {suggestions}' if suggestions else '')
        )
        set_semantic_state(self.destination_readiness, None)
        return geometry

    # --- accept / request -----------------------------------------------------

    def _accept(self) -> None:
        if self.unit_combo.isEnabled() and self.unit_combo.currentData() is None:
            self.destination_readiness.setText(
                'ソース単位を宣言してください（OBJ/PLY/STL は単位を持ちません）'
            )
            set_semantic_state(self.destination_readiness, SemanticState.ERROR)
            return
        self.accept()

    def import_request(self) -> GeometryImportRequest:
        unit = self.unit_combo.currentData()
        return GeometryImportRequest(
            destination=(
                'entity_body'
                if self.entity_radio.isChecked() and self.entity_radio.isEnabled()
                else 'room_geometry'
            ),
            source_unit=str(unit) if unit else 'meters',
            custom_scale_to_meters=(
                float(self.custom_scale.value())
                if unit == 'custom'
                else None
            ),
            up_axis=str(self.up_axis.currentData()),
            forward_axis=str(self.forward_axis.currentData()),
            handedness=str(self.handedness.currentData()),
            local_anchor=str(self.anchor.currentData()),
            repaired_mesh=(
                self._repaired_mesh if self.use_repaired.isChecked() else None
            ),
            repaired_diagnostic=(
                self._repaired_diagnostic if self.use_repaired.isChecked() else None
            ),
            surface_assignments=self._surface_assignments(),
        )


__all__ = [
    'GeometryImportDestination',
    'GeometryImportDialog',
    'GeometryImportRequest',
]
