"""製作プレビュー (FabricationPreview) surface for issue #1008.

A non-modal dialog that issues a sealed ``TreatmentFabricationPackage``
(explicit ``FabricationSpec`` — every value the package depends on is
typed here, never defaulted silently) and drives the read-only Room
viewport overlay: cross-section and exploded sliders, part/cut-group
selection that highlights both the 3D box and the exact cut-list row,
and a report export whose numbering/dimensions/counts are shared with
the preview by construction.

The dialog never fabricates geometry itself — the viewport overlay and
``fabrication_preview`` resolver own that — and it never feeds factory
dimensions back into the acoustic boundary model. Fabrication output is
設計値由来: absorption/scattering performance is a different authority.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_treatment_fabrication import (
    FabricationSpec,
    FabricationToleranceProfile,
    generate_panel_fabrication,
    generate_qrd_fabrication,
)
from .clock import utc_now_iso
from .user_facing_error import operation_error_message
from .fabrication_preview import (
    PART_KIND_LABELS,
    UNSPECIFIED,
    build_fabrication_report,
)
from .ui_theme import TypographyRole, set_typography_role


_FAB10_TYPES = frozenset(
    {'porous_absorber', 'absorber_with_air_gap', 'bass_trap'}
)
_FAB20_TYPES = frozenset({'diffuser_scattering_element'})
_QRD_PRIMES = (3, 5, 7, 11, 13, 17, 19, 23)

_TOLERANCE_FIELDS = (
    ('overall_dimension_mm', '全体寸法'),
    ('well_depth_mm', '井戸深さ'),
    ('fin_thickness_mm', 'フィン厚'),
    ('spacing_mm', 'スペース間隔'),
    ('air_gap_mm', '気隙'),
)


class FabricationPreviewDialog(QDialog):
    """Issue + inspect a sealed fabrication package (read-only, #1008).

    ``host`` is the workspace facade: ``show_fabrication_preview``,
    ``update_fabrication_preview_view``, ``select_fabrication_part`` and
    ``clear_fabrication_preview``. Signals stay the public surface so the
    dialog is testable with any stub host."""

    previewRequested = Signal(object, object)
    previewViewChanged = Signal(object, object)
    previewSelectionChanged = Signal(object)
    previewCleared = Signal()

    def __init__(
        self,
        controller,
        host=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self._host = host
        if host is not None:
            self.previewRequested.connect(
                lambda package, placement_id: host.show_fabrication_preview(
                    package, placement_instance_id=placement_id
                )
            )
            self.previewViewChanged.connect(
                lambda section, exploded: host.update_fabrication_preview_view(
                    section_fraction=section,
                    exploded_fraction=exploded,
                )
            )
            self.previewSelectionChanged.connect(
                host.select_fabrication_part
            )
            self.previewCleared.connect(host.clear_fabrication_preview)
        self._package = None
        self._definitions = ()
        self._placements = ()
        self.setWindowTitle('製作プレビュー (FabricationPreview)')
        self.setModal(False)

        root = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        scroll.setWidget(body)
        root.addWidget(scroll)

        header = QLabel('製作パッケージ発行 + 3Dプレビュー')
        set_typography_role(header, TypographyRole.SECTION_TITLE)
        layout.addWidget(header)
        honesty = QLabel(
            '発行済み (封緘) パッケージのみ描画します。寸法は設計値由来 — '
            '吸音率/散乱性能・施工強度は別の権威で評価します。'
        )
        honesty.setWordWrap(True)
        set_typography_role(honesty, TypographyRole.SECONDARY)
        layout.addWidget(honesty)

        source_form = QFormLayout()
        self.definition_combo = QComboBox()
        self.definition_combo.setToolTip('プレビューする吸音処理の定義')
        self.definition_combo.currentIndexChanged.connect(
            self._definition_changed
        )
        source_form.addRow('定義', self.definition_combo)
        self.placement_combo = QComboBox()
        self.placement_combo.setToolTip(
            '取付向き・クリアランスの参考ジオメトリに使う配置 '
            '(canonicalな配置のみ参考表示)'
        )
        source_form.addRow('参考配置', self.placement_combo)
        layout.addLayout(source_form)

        spec_box = QGroupBox('製作仕様 (FabricationSpec)')
        spec_form = QFormLayout(spec_box)
        self.spec_version = QLineEdit('1')
        self.spec_version.setToolTip('仕様バージョン — パッケージに固定されます')
        spec_form.addRow('仕様バージョン', self.spec_version)
        self.panel_count = QSpinBox()
        self.panel_count.setRange(1, 99)
        self.panel_count.setValue(1)
        self.panel_count.setToolTip('同じパネルの製作枚数')
        spec_form.addRow('パネル数', self.panel_count)
        self.kerf_mm = QDoubleSpinBox()
        self.kerf_mm.setRange(0.0, 20.0)
        self.kerf_mm.setDecimals(2)
        self.kerf_mm.setToolTip('刃こぼし・切断代 (mm)')
        spec_form.addRow('kerf (mm)', self.kerf_mm)
        self.material_ref = QLineEdit()
        self.material_ref.setPlaceholderText('空欄 = unspecified')
        self.material_ref.setToolTip(
            '材料参照 — 空欄のままだと unspecified と表示します'
        )
        spec_form.addRow('材料参照', self.material_ref)
        layout.addWidget(spec_box)

        tol_box = QGroupBox('公差 (mm) — チェックなしは unspecified')
        tol_grid = QGridLayout(tol_box)
        self._tolerance_fields = {}
        for row, (field, label) in enumerate(_TOLERANCE_FIELDS):
            check = QCheckBox(label)
            check.setToolTip('指定する場合だけチェック')
            spin = QDoubleSpinBox()
            spin.setRange(0.0, 50.0)
            spin.setDecimals(2)
            spin.setValue(0.5)
            spin.setEnabled(False)
            check.toggled.connect(spin.setEnabled)
            tol_grid.addWidget(check, row, 0)
            tol_grid.addWidget(spin, row, 1)
            self._tolerance_fields[field] = (check, spin)
        layout.addWidget(tol_box)

        self.qrd_box = QGroupBox('1D QRD パラメータ')
        qrd_form = QFormLayout(self.qrd_box)
        self.qrd_prime = QComboBox()
        for prime in _QRD_PRIMES:
            self.qrd_prime.addItem(str(prime), prime)
        self.qrd_prime.setCurrentIndex(3)
        qrd_form.addRow('素数 N', self.qrd_prime)
        self.qrd_frequency = QDoubleSpinBox()
        self.qrd_frequency.setRange(100.0, 10000.0)
        self.qrd_frequency.setValue(1000.0)
        self.qrd_frequency.setSuffix(' Hz')
        qrd_form.addRow('設計周波数', self.qrd_frequency)
        self.qrd_periods = QSpinBox()
        self.qrd_periods.setRange(1, 4)
        self.qrd_periods.setValue(1)
        qrd_form.addRow('周期数', self.qrd_periods)
        self.qrd_well_width = QDoubleSpinBox()
        self.qrd_well_width.setRange(0.005, 0.5)
        self.qrd_well_width.setDecimals(3)
        self.qrd_well_width.setValue(0.05)
        self.qrd_well_width.setSuffix(' m')
        qrd_form.addRow('井戸幅', self.qrd_well_width)
        self.qrd_fin_thickness = QDoubleSpinBox()
        self.qrd_fin_thickness.setRange(0.001, 0.1)
        self.qrd_fin_thickness.setDecimals(3)
        self.qrd_fin_thickness.setValue(0.005)
        self.qrd_fin_thickness.setSuffix(' m')
        qrd_form.addRow('フィン厚', self.qrd_fin_thickness)
        self.qrd_back_thickness = QDoubleSpinBox()
        self.qrd_back_thickness.setRange(0.005, 0.2)
        self.qrd_back_thickness.setDecimals(3)
        self.qrd_back_thickness.setValue(0.012)
        self.qrd_back_thickness.setSuffix(' m')
        qrd_form.addRow('背板厚', self.qrd_back_thickness)
        layout.addWidget(self.qrd_box)

        issue_row = QHBoxLayout()
        self.issue_button = QPushButton('発行してプレビュー')
        self.issue_button.setToolTip(
            'この仕様で封緘パッケージを発行し、3Dビューに描画します'
        )
        self.issue_button.clicked.connect(self.issue)
        issue_row.addWidget(self.issue_button)
        self.export_button = QPushButton('レポートをエクスポート…')
        self.export_button.setToolTip(
            '図面一覧とパーツ一覧をテキスト化 — '
            'プレビューと同じ番号・寸法・数量になります'
        )
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export_report)
        issue_row.addWidget(self.export_button)
        layout.addLayout(issue_row)

        view_box = QGroupBox('ビュー')
        view_form = QFormLayout(view_box)
        self.section_enabled = QCheckBox('断面を表示')
        self.section_slider = QSlider(Qt.Orientation.Horizontal)
        self.section_slider.setRange(0, 100)
        self.section_slider.setValue(50)
        self.section_slider.setEnabled(False)
        self.section_enabled.toggled.connect(
            self.section_slider.setEnabled
        )
        self.section_enabled.toggled.connect(self._view_changed)
        self.section_slider.valueChanged.connect(self._view_changed)
        section_row = QHBoxLayout()
        section_row.addWidget(self.section_enabled)
        section_row.addWidget(self.section_slider)
        view_form.addRow('断面位置', section_row)
        self.exploded_slider = QSlider(Qt.Orientation.Horizontal)
        self.exploded_slider.setRange(0, 100)
        self.exploded_slider.setValue(0)
        self.exploded_slider.valueChanged.connect(self._view_changed)
        view_form.addRow('分解', self.exploded_slider)
        layout.addWidget(view_box)

        self.package_header = QLabel('パッケージ未発行')
        self.package_header.setWordWrap(True)
        set_typography_role(
            self.package_header, TypographyRole.SECONDARY
        )
        layout.addWidget(self.package_header)

        self.parts_table = QTableWidget(0, 8)
        self.parts_table.setAccessibleName('製作パーツ一覧')
        self.parts_table.setHorizontalHeaderLabels(
            ('#', 'part_id', '種別', 'W×H×D (m)', '数量', '材料', 'cut_group', 'メモ')
        )
        self.parts_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.parts_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.parts_table.itemSelectionChanged.connect(
            self._part_selection_changed
        )
        layout.addWidget(self.parts_table)

        self.cut_table = QTableWidget(0, 6)
        self.cut_table.setAccessibleName('カットリスト')
        self.cut_table.setHorizontalHeaderLabels(
            ('cut_group', '説明', 'W×H×D (m)', '数量', '材料', '公差 (mm)')
        )
        self.cut_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.cut_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.cut_table.itemSelectionChanged.connect(
            self._cut_selection_changed
        )
        layout.addWidget(self.cut_table)

        self.well_table = QTableWidget(0, 4)
        self.well_table.setAccessibleName('井戸テーブル')
        self.well_table.setHorizontalHeaderLabels(
            ('well', 'period', 'residue', '深さ')
        )
        self.well_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        layout.addWidget(self.well_table)

        self.detail = QLabel('')
        self.detail.setWordWrap(True)
        set_typography_role(self.detail, TypographyRole.SECONDARY)
        layout.addWidget(self.detail)
        self.status = QLabel('')
        self.status.setWordWrap(True)
        set_typography_role(self.status, TypographyRole.SECONDARY)
        layout.addWidget(self.status)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_button = QPushButton('閉じる')
        close_button.clicked.connect(self.close)
        close_row.addWidget(close_button)
        layout.addLayout(close_row)
        self.refresh_sources()

    # ------------------------------------------------------------------
    # Sources

    def refresh_sources(self) -> None:
        """Re-list definitions + placements from the live repositories."""

        repository = self.controller.treatment_repository
        self._definitions = repository.list_definitions()
        self.definition_combo.blockSignals(True)
        self.definition_combo.clear()
        for definition in self._definitions:
            self.definition_combo.addItem(
                f'{definition.name} v{definition.version}',
                (definition.definition_id, definition.version),
            )
        self.definition_combo.blockSignals(False)
        self._definition_changed(self.definition_combo.currentIndex())

    def _definition(self):
        data = self.definition_combo.currentData()
        if data is None:
            return None
        return self.controller.treatment_repository.get_definition(*data)

    def _definition_changed(self, _index: int) -> None:
        definition = self._definition()
        treatment_type = (
            '' if definition is None else definition.treatment_type
        )
        is_qrd = treatment_type in _FAB20_TYPES
        supported = treatment_type in _FAB10_TYPES or is_qrd
        self.qrd_box.setVisible(is_qrd)
        self.issue_button.setEnabled(supported)
        if definition is None:
            self.status.setText('定義がありません — 先に定義を作成してください。')
        elif not supported:
            self.status.setText(
                f'種別 {treatment_type} は製作パッケージ未対応です。'
            )
        else:
            self.status.setText('')
        self._refresh_placements(definition)

    def _refresh_placements(self, definition) -> None:
        repository = self.controller.treatment_repository
        revision = self.controller.repository.current_head(
            self.controller.document_id
        )
        self.placement_combo.blockSignals(True)
        self.placement_combo.clear()
        self.placement_combo.addItem('なし (中立アンカー)', None)
        self._placements = ()
        if definition is not None and revision is not None:
            placements = tuple(
                p
                for p in repository.list_placements_for_scene(
                    revision.revision_id
                )
                if p.definition_id == definition.definition_id
                and p.definition_version == definition.version
            )
            self._placements = placements
            for placement in placements:
                try:
                    evaluation = (
                        repository.evaluate_placement_surface_binding(
                            placement
                        )
                    )
                    state = evaluation.binding_state
                except ValueError:
                    state = 'unreadable'
                self.placement_combo.addItem(
                    f'{placement.instance_id} ({placement.lifecycle} · '
                    f'{state})',
                    placement.instance_id,
                )
        self.placement_combo.blockSignals(False)

    def _tolerance_profile(self) -> FabricationToleranceProfile:
        values = {
            field: (spin.value() if check.isChecked() else None)
            for field, (check, spin) in self._tolerance_fields.items()
        }
        return FabricationToleranceProfile(**values)

    def _spec(self) -> FabricationSpec:
        return FabricationSpec(
            spec_version=self.spec_version.text().strip() or '1',
            panel_count=int(self.panel_count.value()),
            kerf_mm=float(self.kerf_mm.value()),
            tolerances=self._tolerance_profile(),
            material_ref=self.material_ref.text().strip() or None,
        )

    # ------------------------------------------------------------------
    # Issue / preview

    def issue(self) -> None:
        """Generate the sealed package and arm the preview."""

        definition = self._definition()
        if definition is None:
            self.status.setText('定義がありません。')
            return
        spec = self._spec()
        try:
            if definition.treatment_type in _FAB20_TYPES:
                package = generate_qrd_fabrication(
                    definition,
                    spec,
                    qrd_prime=int(self.qrd_prime.currentData()),
                    design_frequency_hz=float(self.qrd_frequency.value()),
                    well_width_m=float(self.qrd_well_width.value()),
                    fin_thickness_m=float(self.qrd_fin_thickness.value()),
                    periods=int(self.qrd_periods.value()),
                    back_thickness_m=float(self.qrd_back_thickness.value()),
                    created_at_utc=utc_now_iso(),
                )
            else:
                package = generate_panel_fabrication(
                    definition,
                    spec,
                    created_at_utc=utc_now_iso(),
                )
        except ValueError as exc:
            self.status.setText(
                f'発行失敗: {operation_error_message(exc)}'
            )
            return
        self._package = package
        self._populate(package)
        placement_id = self.placement_combo.currentData()
        self.previewRequested.emit(package, placement_id)

    def _populate(self, package) -> None:
        tol = package.tolerances
        supersedes = package.supersedes_package_sha256
        self.package_header.setText(
            f'pkg {package.package_sha256[:16]}… · '
            f'{package.definition_id} v{package.definition_version} '
            f'(sha {package.definition_sha256[:12]}…) · '
            f'仕様 v{package.spec_version} · 発行 {package.created_at_utc}'
            f' · supersedes {supersedes[:12] + "…" if supersedes else "なし"}'
        )
        self.parts_table.setRowCount(len(package.parts))
        for row, (index, part) in enumerate(
            enumerate(package.parts, start=1)
        ):
            kind = PART_KIND_LABELS.get(part.part_kind, part.part_kind)
            cells = (
                f'P{index}',
                part.part_id,
                kind,
                f'{part.finished_width_m:.3f}×'
                f'{part.finished_height_m:.3f}×'
                f'{part.finished_depth_m:.3f}',
                str(part.quantity),
                part.material_ref or UNSPECIFIED,
                part.cut_group,
                part.notes,
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, part.part_id)
                self.parts_table.setItem(row, column, item)
        self.parts_table.resizeColumnsToContents()

        self.cut_table.setRowCount(len(package.cut_list))
        for row, entry in enumerate(package.cut_list):
            w, h, d = entry.dimensions_m
            cells = (
                entry.cut_group,
                entry.description,
                f'{w:.3f}×{h:.3f}×{d:.3f}',
                str(entry.quantity),
                entry.material_ref or UNSPECIFIED,
                (
                    UNSPECIFIED
                    if entry.tolerance_mm is None
                    else f'{entry.tolerance_mm:g}'
                ),
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, entry.cut_group)
                self.cut_table.setItem(row, column, item)
        self.cut_table.resizeColumnsToContents()

        self.well_table.setRowCount(len(package.well_table))
        self.well_table.setVisible(bool(package.well_table))
        for row, well in enumerate(package.well_table):
            cells = (
                str(well.well_index),
                str(well.period_index),
                str(well.residue),
                f'{well.depth_m:.3f} m ({well.depth_m * 1000.0:.0f} mm)',
            )
            for column, text in enumerate(cells):
                self.well_table.setItem(row, column, QTableWidgetItem(text))
        self.well_table.resizeColumnsToContents()

        if package.warnings:
            self.status.setText('警告: ' + ' / '.join(package.warnings))
        else:
            self.status.setText('発行済み — プレビュー描画中')
        self.export_button.setEnabled(True)
        self.detail.setText(
            '公差: '
            f'全体 {_fmt_or_unspecified(tol.overall_dimension_mm)} / '
            f'井戸 {_fmt_or_unspecified(tol.well_depth_mm)} / '
            f'フィン {_fmt_or_unspecified(tol.fin_thickness_mm)} / '
            f'間隔 {_fmt_or_unspecified(tol.spacing_mm)} / '
            f'気隙 {_fmt_or_unspecified(tol.air_gap_mm)} mm · '
            f'kerf {package.kerf_mm:g} mm'
        )

    # ------------------------------------------------------------------
    # View + selection + export

    def _view_changed(self, *_args) -> None:
        if self._package is None:
            return
        section = (
            self.section_slider.value() / 100.0
            if self.section_enabled.isChecked()
            else None
        )
        exploded = self.exploded_slider.value() / 100.0
        self.previewViewChanged.emit(section, exploded)

    def _part_selection_changed(self) -> None:
        items = self.parts_table.selectedItems()
        if not items:
            return
        key = items[0].data(Qt.ItemDataRole.UserRole)
        self.previewSelectionChanged.emit(key)
        self._show_selection_detail(key)

    def _cut_selection_changed(self) -> None:
        items = self.cut_table.selectedItems()
        if not items:
            return
        key = items[0].data(Qt.ItemDataRole.UserRole)
        self.previewSelectionChanged.emit(key)
        self._show_selection_detail(key)

    def _show_selection_detail(self, key: str) -> None:
        package = self._package
        if package is None:
            return
        matched = [
            p
            for p in package.parts
            if p.part_id == key or p.cut_group == key
        ]
        lines = []
        for part in matched:
            entry = next(
                (
                    e
                    for e in package.cut_list
                    if e.cut_group == part.cut_group
                ),
                None,
            )
            lines.append(
                f'{part.part_id} ({PART_KIND_LABELS.get(part.part_kind, part.part_kind)})'
                f' — {part.finished_width_m:.3f}×'
                f'{part.finished_height_m:.3f}×'
                f'{part.finished_depth_m:.3f} m ×{part.quantity} · '
                f'材料 {part.material_ref or UNSPECIFIED}'
            )
            if entry is not None:
                lines.append(
                    f'  cut list {entry.cut_group}: ×{entry.quantity} · '
                    f'材料 {entry.material_ref or UNSPECIFIED} · 公差 '
                    + (
                        UNSPECIFIED
                        if entry.tolerance_mm is None
                        else f'{entry.tolerance_mm:g}'
                    )
                    + ' mm'
                )
        self.detail.setText('\n'.join(lines) or f'{key}: 該当なし')

    def report_text(self) -> str:
        """Export text — the preview's numbering/dimensions verbatim."""

        if self._package is None:
            return ''
        return build_fabrication_report(self._package)

    def export_report(self) -> None:
        if self._package is None:
            return
        path, _selected = QFileDialog.getSaveFileName(
            self,
            '製作レポートをエクスポート',
            f'fabrication-{self._package.package_sha256[:8]}.md',
            'Markdown (*.md);;Text (*.txt)',
        )
        if not path:
            return
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(self.report_text())
        self.status.setText(f'エクスポートしました: {path}')

    def closeEvent(self, event) -> None:
        # Closing the preview disarms the overlay — actor cleanup rides
        # the same clear path as a package revision swap.
        self.previewCleared.emit()
        super().closeEvent(event)


def _fmt_or_unspecified(value) -> str:
    return UNSPECIFIED if value is None else f'{value:g}'


__all__ = ['FabricationPreviewDialog']
