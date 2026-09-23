from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_repository import SceneRepository
from .cad_scene import SceneDocument
from .cad_seat_priority import (
    CadSeatPriorityProfileRepository,
    SeatPriorityMember,
    SeatPriorityProfile,
    build_seat_priority_profile,
)


_ROLE_LABELS: tuple[tuple[str, str], ...] = (
    ('primary', 'MLP（主座席）'),
    ('secondary', '補助座席'),
    ('diagnostic', '診断のみ'),
)


class SeatPriorityPanel(QWidget):
    """Listening-population editor (#513).

    Marks each seat of the committed document head as primary / secondary /
    diagnostic with a required flag and a relative weight, then materializes
    an immutable :class:`SeatPriorityProfile` authority bound to the exact
    scene revision. Weights are relative importance only — never
    probabilities.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        document_id: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.scene_repository = scene_repository
        self.document_id = document_id
        self.profile_repository = CadSeatPriorityProfileRepository(
            scene_repository
        )
        self._rows: dict[str, tuple[QComboBox, QCheckBox, QDoubleSpinBox]] = {}

        layout = QVBoxLayout(self)
        self.status_label = QLabel('座席がありません')
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.member_tree = QTreeWidget()
        self.member_tree.setColumnCount(4)
        self.member_tree.setHeaderLabels(['座席', '役割', '必須', '重み'])
        self.member_tree.setRootIsDecorated(False)
        layout.addWidget(self.member_tree)

        preset = QPushButton('MLP優先プリセット（全座席必須）')
        preset.setToolTip(
            '先頭座席を MLP 重み3、その他を補助座席 重み1 に設定します。'
            'すべての座席は必須のままです'
        )
        preset.clicked.connect(self._apply_mlp_preset)
        layout.addWidget(preset)

        save = QPushButton('リスニング集団を保存')
        save.setToolTip(
            '現在のシーンリビジョンに正確にバインドした不変プロファイルを保存します'
        )
        save.clicked.connect(self._save_profile)
        layout.addWidget(save)

        self.saved_tree = QTreeWidget()
        self.saved_tree.setColumnCount(1)
        self.saved_tree.setHeaderLabels(['保存済みプロファイル'])
        self.saved_tree.setRootIsDecorated(False)
        self.saved_tree.setMaximumHeight(140)
        layout.addWidget(self.saved_tree)
        layout.addStretch(1)

    def refresh(self, document: SceneDocument | None) -> None:
        """Rebuild member rows from the committed document head."""
        self._rows.clear()
        self.member_tree.clear()
        seats = sorted(
            (
                entity
                for entity in (document.entities if document else ())
                if entity.kind == 'seat'
            ),
            key=lambda entity: entity.entity_id,
        )
        self.status_label.setText(
            f'{len(seats)} 座席 · 優先度を設定して保存してください'
            if seats
            else '座席がありません（先に座席を追加してください）'
        )
        for seat in seats:
            item = QTreeWidgetItem([seat.name or seat.entity_id])
            item.setData(0, Qt.ItemDataRole.UserRole, seat.entity_id)
            self.member_tree.addTopLevelItem(item)

            role_combo = QComboBox()
            for role, label in _ROLE_LABELS:
                role_combo.addItem(label, role)
            required_check = QCheckBox()
            required_check.setChecked(True)
            weight_spin = QDoubleSpinBox()
            weight_spin.setRange(0.01, 100.0)
            weight_spin.setDecimals(2)
            weight_spin.setSingleStep(0.1)
            weight_spin.setValue(1.0)
            weight_spin.setToolTip(
                '相対的な重要度（確率ではありません）· 評価時に合計1へ正規化'
            )

            self.member_tree.setItemWidget(item, 1, role_combo)
            self.member_tree.setItemWidget(item, 2, required_check)
            self.member_tree.setItemWidget(item, 3, weight_spin)
            self._rows[seat.entity_id] = (role_combo, required_check, weight_spin)
        for column in range(4):
            self.member_tree.resizeColumnToContents(column)
        self._refresh_saved()

    def _refresh_saved(self) -> None:
        self.saved_tree.clear()
        for profile in self.profile_repository.list_for_document(
            self.document_id
        ):
            roles = {'primary': 0, 'secondary': 0, 'diagnostic': 0}
            for member in profile.members:
                roles[member.seat_role] += 1
            item = QTreeWidgetItem(
                [
                    f"{profile.profile_id} · MLP{roles['primary']} "
                    f"補助{roles['secondary']} 診断{roles['diagnostic']} "
                    f"· 必須{len(profile.required_seat_entity_ids)}席"
                ]
            )
            item.setToolTip(
                0,
                'バインド revision '
                f"{profile.scene_revision_id[:12]} · "
                f"正規化 {profile.weight_normalization} "
                f"({profile.normalization_version})",
            )
            self.saved_tree.addTopLevelItem(item)

    def _apply_mlp_preset(self) -> None:
        if not self._rows:
            return
        first = sorted(self._rows)[0]
        for seat_id, (role_combo, required_check, weight_spin) in (
            self._rows.items()
        ):
            index = role_combo.findData(
                'primary' if seat_id == first else 'secondary'
            )
            role_combo.setCurrentIndex(index)
            required_check.setChecked(True)
            weight_spin.setValue(3.0 if seat_id == first else 1.0)
        self.status_label.setText(
            'MLP優先プリセット適用 · すべての座席は必須、重みは相対重要度'
        )

    def _members(self) -> tuple[SeatPriorityMember, ...]:
        members = []
        for seat_id in sorted(self._rows):
            role_combo, required_check, weight_spin = self._rows[seat_id]
            members.append(
                SeatPriorityMember(
                    seat_entity_id=seat_id,
                    seat_role=role_combo.currentData(),
                    required=required_check.isChecked(),
                    weight=float(weight_spin.value()),
                )
            )
        return tuple(members)

    def _save_profile(self) -> None:
        if not self._rows:
            QMessageBox.information(
                self, 'リスニング集団', '保存する座席がありません'
            )
            return
        try:
            profile = build_seat_priority_profile(
                scene_repository=self.scene_repository,
                document_id=self.document_id,
                members=self._members(),
            )
            saved = self.profile_repository.save(profile)
        except (ValueError, KeyError) as exc:
            QMessageBox.warning(self, 'リスニング集団', str(exc))
            return
        self._refresh_saved()
        self.status_label.setText(
            f'保存しました {saved.profile_id} · '
            f'必須{len(saved.required_seat_entity_ids)}席 · '
            'リビジョン変更後は再保存が必要です'
        )
