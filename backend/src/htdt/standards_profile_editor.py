"""StandardsProfile library editor surface.

Authors user-defined StandardsProfile authorities natively: create, clone a
built-in into a user draft, add/edit/remove criteria with source metadata,
publish a new immutable version of a user profile, and import/export the
exact profile JSON. Built-in (published) profiles stay read-only and a
user-defined profile can never overwrite another id/version.
"""

from __future__ import annotations

from typing import Sequence
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import file_dialog_memory
from .accessible_labels import wire_label_buddies
from .export_io import write_text_atomic
from .user_facing_error import warn_user
from .cad_standards import (
    CriterionDefinition,
    CriterionRule,
    CriterionSource,
    StandardsProfile,
    build_user_standards_profile,
)

_OPERATORS: tuple[tuple[str, str], ...] = (
    ("最小値 ≥", "min"),
    ("最大値 ≤", "max"),
    ("範囲", "range"),
    ("等しい", "equals"),
)

_DOMAINS: tuple[str, ...] = (
    "room",
    "seat",
    "speaker_layout",
    "wide_speaker",
    "auro_lower_speaker",
    "auro_height_speaker",
    "auro_top_speaker",
)


def _criterion_row_text(criterion: CriterionDefinition) -> str:
    rule = criterion.rule
    if rule.operator == "min":
        rule_text = f"≥ {rule.minimum}"
    elif rule.operator == "max":
        rule_text = f"≤ {rule.maximum}"
    elif rule.operator == "range":
        rule_text = f"{rule.minimum} .. {rule.maximum}"
    else:
        rule_text = f"= {rule.expected}"
    return (
        f"{criterion.quantity} [{criterion.unit}] {rule_text} / "
        f"{criterion.source.publisher}: {criterion.source.document_title} "
        f"{criterion.source.document_version} "
        f"({criterion.source.reference})"
    )


class StandardsProfileLibraryService:
    """Create/version/import/export user-defined StandardsProfiles."""

    def __init__(self, repository) -> None:
        self.repository = repository

    def profiles(self) -> tuple[StandardsProfile, ...]:
        return self.repository.list_profiles()

    def profile_versions(self, profile_id: str) -> tuple[str, ...]:
        return tuple(
            profile.version
            for profile in self.profiles()
            if profile.profile_id == profile_id
        )

    def next_version_label(self, base: StandardsProfile) -> str:
        versions = self.profile_versions(base.profile_id)
        return f"{base.version}-u{len(versions) + 1}"

    def create_user_profile(
        self,
        *,
        name: str,
        criteria: Sequence[CriterionDefinition],
    ) -> StandardsProfile:
        profile = build_user_standards_profile(
            profile_id=f"user-{uuid4().hex[:16]}",
            version="1",
            name=name,
            criteria=criteria,
        )
        return self.repository.save_profile(profile)

    def clone_profile(
        self,
        base: StandardsProfile,
        *,
        name: str,
        criteria: Sequence[CriterionDefinition],
    ) -> StandardsProfile:
        """Clone any profile (built-in included) into a new user identity.

        The source authority is never mutated — the clone carries a fresh
        ``profile_id`` with kind ``user_defined``.
        """
        profile = build_user_standards_profile(
            profile_id=f"user-{uuid4().hex[:16]}",
            version="1",
            name=name,
            criteria=criteria,
        )
        return self.repository.save_profile(profile)

    def save_new_version(
        self,
        base: StandardsProfile,
        *,
        name: str,
        criteria: Sequence[CriterionDefinition],
    ) -> StandardsProfile:
        """Publish a new immutable version of an existing user profile."""
        if base.profile_kind != "user_defined":
            raise ValueError(
                "組み込みプロファイルは変更できません。"
                "複製してユーザー定義プロファイルとして保存してください。"
            )
        profile = build_user_standards_profile(
            profile_id=base.profile_id,
            version=self.next_version_label(base),
            name=name,
            criteria=criteria,
        )
        return self.repository.save_profile(profile)

    def export_profile_json(self, profile: StandardsProfile) -> str:
        return profile.model_dump_json(indent=2)

    def import_profile_json(self, text: str) -> StandardsProfile:
        """Import exact profile JSON; malformed/unknown fields are rejected."""
        profile = StandardsProfile.model_validate_json(text)
        return self.repository.save_profile(profile)


class StandardsProfileEditorDialog(QDialog):
    """Library/editor surface for user-defined StandardsProfiles."""

    def __init__(
        self,
        service: StandardsProfileLibraryService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("基準プロファイル ライブラリ / エディタ")
        self.resize(820, 560)
        self._criteria: list[CriterionDefinition] = []
        self._base_profile: StandardsProfile | None = None

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("プロファイル"))
        self.profile_combo = QComboBox()
        self.profile_combo.currentIndexChanged.connect(self._load_profile)
        top.addWidget(self.profile_combo, 1)
        self.import_button = QPushButton("JSONインポート…")
        self.import_button.clicked.connect(self._import_profile)
        top.addWidget(self.import_button)
        self.export_button = QPushButton("JSONエクスポート…")
        self.export_button.clicked.connect(self._export_profile)
        top.addWidget(self.export_button)
        layout.addLayout(top)

        name_row = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例: マイルーム基準")
        name_row.addRow("プロファイル名（必須）", self.name_edit)
        layout.addLayout(name_row)

        self.criteria_table = QTableWidget(0, 2)
        self.criteria_table.setAccessibleName("基準一覧")
        self.criteria_table.setHorizontalHeaderLabels(["基準ID", "ルール"])
        self.criteria_table.horizontalHeader().setStretchLastSection(True)
        self.criteria_table.itemSelectionChanged.connect(
            self._criterion_selection_changed
        )
        layout.addWidget(self.criteria_table, 1)

        criterion_form = QFormLayout()
        self.criterion_id_edit = QLineEdit()
        criterion_form.addRow("基準ID（必須）", self.criterion_id_edit)
        self.criterion_name_edit = QLineEdit()
        criterion_form.addRow("基準名（必須）", self.criterion_name_edit)
        self.criterion_quantity_edit = QLineEdit()
        self.criterion_quantity_edit.setPlaceholderText("例: viewing_angle")
        criterion_form.addRow("量（必須）", self.criterion_quantity_edit)
        self.criterion_unit_edit = QLineEdit()
        self.criterion_unit_edit.setPlaceholderText("例: deg / m / dB SPL")
        criterion_form.addRow("単位（必須）", self.criterion_unit_edit)
        self.operator_combo = QComboBox()
        for label, value in _OPERATORS:
            self.operator_combo.addItem(label, value)
        criterion_form.addRow("演算子", self.operator_combo)
        bounds = QHBoxLayout()
        self.minimum_spin = QDoubleSpinBox()
        self.minimum_spin.setRange(-1e6, 1e6)
        self.minimum_spin.setSpecialValueText("なし")
        self.minimum_spin.setValue(self.minimum_spin.minimum())
        self.maximum_spin = QDoubleSpinBox()
        self.maximum_spin.setRange(-1e6, 1e6)
        self.maximum_spin.setSpecialValueText("なし")
        self.maximum_spin.setValue(self.maximum_spin.minimum())
        self.expected_edit = QLineEdit()
        self.expected_edit.setPlaceholderText("equals の期待値")
        bounds.addWidget(QLabel("下限"))
        bounds.addWidget(self.minimum_spin)
        bounds.addWidget(QLabel("上限"))
        bounds.addWidget(self.maximum_spin)
        bounds.addWidget(QLabel("期待値"))
        bounds.addWidget(self.expected_edit)
        criterion_form.addRow("境界", bounds)
        self.domains_edit = QLineEdit()
        self.domains_edit.setPlaceholderText(
            "適用ドメイン（空白区切り）: " + " ".join(_DOMAINS)
        )
        criterion_form.addRow("適用ドメイン（必須）", self.domains_edit)
        self.required_inputs_edit = QLineEdit()
        self.required_inputs_edit.setPlaceholderText("必須入力（空白区切り）")
        criterion_form.addRow("必須入力", self.required_inputs_edit)
        self.publisher_edit = QLineEdit()
        criterion_form.addRow("出典publisher（必須）", self.publisher_edit)
        self.doc_title_edit = QLineEdit()
        criterion_form.addRow("文書タイトル（必須）", self.doc_title_edit)
        self.doc_version_edit = QLineEdit()
        criterion_form.addRow("文書バージョン（必須）", self.doc_version_edit)
        self.reference_edit = QLineEdit()
        criterion_form.addRow("参照（必須）", self.reference_edit)
        layout.addLayout(criterion_form)

        criterion_buttons = QHBoxLayout()
        self.add_criterion_button = QPushButton("基準を追加 / 更新")
        self.add_criterion_button.clicked.connect(self._apply_criterion)
        self.remove_criterion_button = QPushButton("基準を削除")
        self.remove_criterion_button.setEnabled(False)
        self.remove_criterion_button.clicked.connect(self._remove_criterion)
        criterion_buttons.addWidget(self.add_criterion_button)
        criterion_buttons.addWidget(self.remove_criterion_button)
        criterion_buttons.addStretch(1)
        layout.addLayout(criterion_buttons)

        save_row = QHBoxLayout()
        self.save_new_button = QPushButton("新規ユーザープロファイルとして保存")
        self.save_new_button.clicked.connect(self._save_new)
        self.clone_button = QPushButton("選択プロファイルを複製して保存")
        self.clone_button.clicked.connect(self._save_clone)
        self.save_version_button = QPushButton("新バージョンとして保存")
        self.save_version_button.clicked.connect(self._save_version)
        save_row.addWidget(self.save_new_button)
        save_row.addWidget(self.clone_button)
        save_row.addWidget(self.save_version_button)
        save_row.addStretch(1)
        layout.addLayout(save_row)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.box.rejected.connect(self.reject)
        layout.addWidget(self.box)

        self.refresh_profiles()
        wire_label_buddies(self)

    def refresh_profiles(self) -> None:
        current = self.profile_combo.currentData()
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItem("（新規）", None)
        for profile in self.service.profiles():
            kind = (
                "ユーザー定義"
                if profile.profile_kind == "user_defined"
                else "組み込み"
            )
            self.profile_combo.addItem(
                f"{profile.name} · v{profile.version} · {kind}",
                profile,
            )
        if current is not None:
            index = self.profile_combo.findData(current)
            if index >= 0:
                self.profile_combo.setCurrentIndex(index)
        self.profile_combo.blockSignals(False)
        self._load_profile(self.profile_combo.currentIndex())

    def _load_profile(self, _index: int) -> None:
        profile = self.profile_combo.currentData()
        self._base_profile = profile
        self._criteria = list(profile.criteria) if profile is not None else []
        self.name_edit.setText(
            f"{profile.name}（カスタム）" if profile is not None else ""
        )
        self._refresh_criteria_table()
        self.save_version_button.setEnabled(
            profile is not None and profile.profile_kind == "user_defined"
        )

    def _criterion_selection_changed(self) -> None:
        row = self.criteria_table.currentRow()
        self.remove_criterion_button.setEnabled(0 <= row < len(self._criteria))
        self._load_criterion(row)

    def _refresh_criteria_table(self) -> None:
        self.criteria_table.setRowCount(0)
        for criterion in self._criteria:
            row = self.criteria_table.rowCount()
            self.criteria_table.insertRow(row)
            self.criteria_table.setItem(
                row, 0, QTableWidgetItem(criterion.criterion_id)
            )
            self.criteria_table.setItem(
                row, 1, QTableWidgetItem(_criterion_row_text(criterion))
            )
        self.remove_criterion_button.setEnabled(False)

    def _load_criterion(self, row: int) -> None:
        if row < 0 or row >= len(self._criteria):
            return
        criterion = self._criteria[row]
        self.criterion_id_edit.setText(criterion.criterion_id)
        self.criterion_name_edit.setText(criterion.name)
        self.criterion_quantity_edit.setText(criterion.quantity)
        self.criterion_unit_edit.setText(criterion.unit)
        index = self.operator_combo.findData(criterion.rule.operator)
        if index >= 0:
            self.operator_combo.setCurrentIndex(index)
        self.minimum_spin.setValue(
            criterion.rule.minimum
            if criterion.rule.minimum is not None
            else self.minimum_spin.minimum()
        )
        self.maximum_spin.setValue(
            criterion.rule.maximum
            if criterion.rule.maximum is not None
            else self.maximum_spin.minimum()
        )
        self.expected_edit.setText(
            "" if criterion.rule.expected is None else str(criterion.rule.expected)
        )
        self.domains_edit.setText(" ".join(criterion.applicable_domains))
        self.required_inputs_edit.setText(" ".join(criterion.required_inputs))
        self.publisher_edit.setText(criterion.source.publisher)
        self.doc_title_edit.setText(criterion.source.document_title)
        self.doc_version_edit.setText(criterion.source.document_version)
        self.reference_edit.setText(criterion.source.reference)

    def _criterion_from_form(self) -> CriterionDefinition:
        operator = str(self.operator_combo.currentData())
        minimum = (
            self.minimum_spin.value()
            if operator in ("min", "range")
            and self.minimum_spin.value() != self.minimum_spin.minimum()
            else None
        )
        maximum = (
            self.maximum_spin.value()
            if operator in ("max", "range")
            and self.maximum_spin.value() != self.maximum_spin.minimum()
            else None
        )
        expected_text = self.expected_edit.text().strip()
        expected = None
        if expected_text:
            try:
                expected = float(expected_text)
            except ValueError:
                expected = expected_text
        rule = CriterionRule(
            operator=operator,
            minimum=minimum,
            maximum=maximum,
            expected=expected,
        )
        domains = tuple(
            item for item in self.domains_edit.text().split() if item
        )
        required_inputs = tuple(
            item for item in self.required_inputs_edit.text().split() if item
        )
        return CriterionDefinition(
            criterion_id=self.criterion_id_edit.text().strip(),
            name=self.criterion_name_edit.text().strip(),
            source=CriterionSource(
                publisher=self.publisher_edit.text().strip(),
                document_title=self.doc_title_edit.text().strip(),
                document_version=self.doc_version_edit.text().strip(),
                reference=self.reference_edit.text().strip(),
            ),
            quantity=self.criterion_quantity_edit.text().strip(),
            unit=self.criterion_unit_edit.text().strip(),
            applicable_domains=domains,
            required_inputs=required_inputs,
            rule=rule,
        )

    def _apply_criterion(self) -> None:
        try:
            criterion = self._criterion_from_form()
        except ValueError as exc:
            warn_user(self, "基準を適用できませんでした", exc)
            return
        existing = [
            i
            for i, item in enumerate(self._criteria)
            if item.criterion_id == criterion.criterion_id
        ]
        if existing:
            self._criteria[existing[0]] = criterion
        else:
            self._criteria.append(criterion)
        self._refresh_criteria_table()

    def _remove_criterion(self) -> None:
        row = self.criteria_table.currentRow()
        if 0 <= row < len(self._criteria):
            del self._criteria[row]
            self._refresh_criteria_table()

    def _save(self, fn) -> None:
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(
                self, "プロファイル", "プロファイル名を入力してください"
            )
            return
        if not self._criteria:
            QMessageBox.warning(
                self, "プロファイル", "基準を1件以上追加してください"
            )
            return
        try:
            profile = fn(name)
        except ValueError as exc:
            warn_user(self, "プロファイルを保存できませんでした", exc)
            return
        self.refresh_profiles()
        self.status_label.setText(
            f"保存しました: {profile.name} · v{profile.version} "
            f"({profile.profile_id})"
        )

    def _save_new(self) -> None:
        criteria = list(self._criteria)
        self._save(
            lambda name: self.service.create_user_profile(
                name=name, criteria=criteria
            )
        )

    def _save_clone(self) -> None:
        base = self._base_profile
        criteria = list(self._criteria)
        if base is None:
            self._save_new()
            return
        self._save(
            lambda name: self.service.clone_profile(
                base, name=name, criteria=criteria
            )
        )

    def _save_version(self) -> None:
        base = self._base_profile
        if base is None:
            return
        criteria = list(self._criteria)
        self._save(
            lambda name: self.service.save_new_version(
                base, name=name, criteria=criteria
            )
        )

    def _import_profile(self) -> None:
        selected, _filter = file_dialog_memory.get_open_file_name(
            self, "プロファイルJSONを選択", 'standards.profile_import', "JSON (*.json)"
        )
        if not selected:
            return
        try:
            with open(selected, encoding="utf-8") as handle:
                text = handle.read()
            profile = self.service.import_profile_json(text)
        except (ValueError, OSError) as exc:
            warn_user(self, "プロファイルをインポートできませんでした", exc)
            return
        self.refresh_profiles()
        self.status_label.setText(
            f"インポートしました: {profile.name} · v{profile.version}"
        )

    def _export_profile(self) -> None:
        profile = self.profile_combo.currentData()
        if profile is None:
            QMessageBox.warning(
                self, "エクスポート", "プロファイルを選択してください"
            )
            return
        selected, _filter = file_dialog_memory.get_save_file_name(
            self,
            "プロファイルJSONを保存",
            'standards.profile_export',
            "JSON (*.json)",
            suggested_name=f"{profile.profile_id}-{profile.version}.json",
        )
        if not selected:
            return
        try:
            write_text_atomic(
                selected, self.service.export_profile_json(profile)
            )
        except OSError as exc:
            warn_user(self, "エクスポートできませんでした", exc)
            return
        self.status_label.setText(f"エクスポートしました: {selected}")


__all__ = [
    "StandardsProfileEditorDialog",
    "StandardsProfileLibraryService",
]
