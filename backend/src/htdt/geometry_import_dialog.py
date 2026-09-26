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
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt

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
    ('割り当てない（unknown のまま）', None),
)

# Bounded repair operations offered to the operator, in application order.
# Fill-hole / surgery / reconstruction stay unsupported by design (#762 §3):
# the dialog only exposes the deterministic (A) and bounded (B) classes.
_REPAIR_CHECKBOXES: tuple[tuple[str, str, str], ...] = (
    ('exact_duplicate_vertex_consolidation', 'A', '完全一致する頂点を統合'),
    ('remove_unreferenced_vertices', 'A', '未参照頂点を削除'),
    ('remove_exact_duplicate_faces', 'A', '完全一致する重複面を削除'),
    ('remove_degenerate_faces', 'A', '面積ゼロの退化面を削除'),
    ('correct_consistent_winding', 'B', '面の巻き方向を統一'),
    ('tolerance_vertex_weld', 'B', '許容誤差内の頂点を溶接'),
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
    'topology': 'トポロジ',
    'surface_quality': '面品質',
    'acoustic_model': '音響モデル',
}

_COMPILER_READINESS_LABELS = {
    'ready_for_r120_geometry_compiler_contract': 'R120ジオメトリコンパイラ契約に対応可',
    'blocked_by_geometry': 'ブロック: ジオメトリ未解決',
    'blocked_by_surface_semantics': 'ブロック: 面の意味分類が未割当',
}


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
            self._path.read_bytes(), source_name=self._path.name
        )
        self.diagnostics = diagnose_raw_visual_mesh(self.mesh)
        self.health: MeshHealthSummary = build_mesh_health_summary(
            self.diagnostics, mesh=self.mesh, created_at_utc='1970-01-01T00:00:00+00:00'
        )
        self._repaired_mesh: RepairedRawMesh | None = None
        self._repaired_diagnostic: RepairedRawMeshDiagnosticResult | None = None
        self._evaluated_geometry: SemanticAcousticGeometry | None = None

        self.setWindowTitle('ジオメトリをインポート')
        self.resize(680, 640)
        layout = QVBoxLayout(self)

        source = QLabel(
            f'{self._path.name} — {self.mesh.provenance.asset_format} · '
            f'頂点 {len(self.mesh.vertices)} / 面 {len(self.mesh.triangles)}'
        )
        set_typography_role(source, TypographyRole.SECTION_TITLE)
        layout.addWidget(source)

        layout.addWidget(self._build_declaration_group())
        layout.addWidget(self._build_diagnostics_group())
        layout.addWidget(self._build_repair_group())
        layout.addWidget(self._build_destination_group())

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText('インポート')
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    # --- declaration group ---------------------------------------------------

    def _build_declaration_group(self) -> QGroupBox:
        group = QGroupBox('ソースの単位・座標系（演算子の宣言）')
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
                'このフォーマットはメートルを宣言しています（format_specification）'
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
        form.addRow('上方向軸', self.up_axis)
        form.addRow('前方向軸', self.forward_axis)
        form.addRow('座標系の向き', self.handedness)

        self.anchor = QComboBox()
        for label, value in _ANCHOR_ITEMS:
            self.anchor.addItem(label, value)
        form.addRow('ローカル原点', self.anchor)
        return group

    def _sync_unit_state(self) -> None:
        is_custom = self.unit_combo.currentData() == 'custom'
        self.custom_scale.setEnabled(is_custom)

    # --- diagnostics group ---------------------------------------------------

    def _build_diagnostics_group(self) -> QGroupBox:
        group = QGroupBox('ジオメトリ診断（QA）')
        layout = QVBoxLayout(group)
        inventory = mesh_component_inventory(self.mesh)
        components = f'{len(inventory.components)} 連結成分'
        self.diagnostic_summary = QLabel(
            _diagnostic_count_text(self.diagnostics.findings)
            + f' · {components} · '
            f'音響体積: {self.diagnostics.acoustic_volume_readiness}'
        )
        self.diagnostic_summary.setWordWrap(True)
        layout.addWidget(self.diagnostic_summary)

        self.issues_table = QTableWidget(0, 5)
        self.issues_table.setHorizontalHeaderLabels(
            ('深刻度', '分類', '検査', '件数', '推奨対処')
        )
        self.issues_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
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
            self.issues_table.setItem(row, 2, QTableWidgetItem(issue.code))
            self.issues_table.setItem(
                row, 3, QTableWidgetItem('' if issue.count is None else str(issue.count))
            )
            self.issues_table.setItem(row, 4, QTableWidgetItem(issue.action))
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
            '決定論的（A）/境界付き（B）の修復のみ実行できます。'
            '穴埋め・再構築などのC/unsupported操作は失敗クローズです。'
        )
        hint.setWordWrap(True)
        set_typography_role(hint, TypographyRole.SECONDARY)
        layout.addWidget(hint)

        self.repair_checks: dict[str, QCheckBox] = {}
        for kind, risk, label in _REPAIR_CHECKBOXES:
            check = QCheckBox(f'[{risk}] {label}')
            check.setToolTip(f'操作: {kind}（リスククラス {classify_repair_operation(kind)}）')
            self.repair_checks[kind] = check
            layout.addWidget(check)

        weld_row = QHBoxLayout()
        weld_row.addWidget(QLabel('溶接許容誤差（ソース単位）:'))
        self.weld_tolerance = QDoubleSpinBox()
        self.weld_tolerance.setDecimals(9)
        self.weld_tolerance.setRange(1e-12, 1e6)
        self.weld_tolerance.setValue(1e-6)
        weld_row.addWidget(self.weld_tolerance)
        weld_row.addStretch(1)
        layout.addLayout(weld_row)

        preview_row = QHBoxLayout()
        self.preview_button = QPushButton('修復をプレビュー')
        set_control_size(self.preview_button, ControlSize.COMPACT)
        self.preview_button.clicked.connect(self._preview_repair)
        preview_row.addWidget(self.preview_button)
        self.use_repaired = QCheckBox('修復済みメッシュを使用')
        self.use_repaired.setEnabled(False)
        preview_row.addWidget(self.use_repaired)
        preview_row.addStretch(1)
        layout.addLayout(preview_row)

        self.repair_result = QLabel('')
        self.repair_result.setWordWrap(True)
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
        plan = make_raw_mesh_repair_plan(
            self.mesh,
            self.diagnostics,
            operations=operations,
            requested_by='explicit_user_selected',
            request_reason='guided import dialog repair preview (#762)',
        )
        repaired = apply_raw_mesh_repair(self.mesh, self.diagnostics, plan)
        diagnostic = diagnose_repaired_raw_mesh(self.mesh, repaired)
        self._repaired_mesh = repaired
        self._repaired_diagnostic = diagnostic
        self.use_repaired.setEnabled(True)
        self.use_repaired.setChecked(True)

        applied = [
            result.operation.kind
            for result in repaired.operation_results
            if result.execution_state == 'applied'
        ]
        unresolved = list(repaired.unsupported_unresolved_findings)
        self.repair_result.setText(
            f'頂点 {repaired.vertex_count_before}→{repaired.vertex_count_after} · '
            f'面 {repaired.triangle_count_before}→{repaired.triangle_count_after} · '
            f'適用: {("・".join(applied)) or "なし"}'
            + (f' · 未解決: {"・".join(unresolved)}' if unresolved else '')
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

        self.room_radio = QRadioButton('部屋の音響ジオメトリ（R120 ソルバー契約）')
        self.room_radio.setChecked(self._entity_target is None)
        self.room_radio.toggled.connect(lambda _on: self._sync_surface_combo())
        layout.addWidget(self.room_radio)

        surface_row = QHBoxLayout()
        surface_row.addWidget(QLabel('全ての面の意味分類:'))
        self.surface_class = QComboBox()
        for label, value in _SURFACE_CLASS_ITEMS:
            self.surface_class.addItem(label, value)
        surface_row.addWidget(self.surface_class, 1)
        evaluate = QPushButton('ソルバー適性を評価')
        set_control_size(evaluate, ControlSize.COMPACT)
        evaluate.clicked.connect(self._evaluate_readiness)
        surface_row.addWidget(evaluate)
        layout.addLayout(surface_row)

        self.destination_readiness = QLabel('')
        self.destination_readiness.setWordWrap(True)
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
                    reason='guided import preview (controller supplies scene transform)'
                ),
                surface_assignments=self._surface_assignments(),
                repaired_mesh=repaired,
                repaired_diagnostic=diagnostic,
            )
            geometry = convert_raw_visual_mesh_to_semantic_geometry(
                self.mesh, request, repaired_mesh=repaired, repaired_diagnostic=diagnostic
            )
        except ValueError as exc:
            self.destination_readiness.setText(f'評価できません: {exc}')
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
