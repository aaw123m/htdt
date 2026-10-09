"""指向性バルーン・オーバーレイのQtパネル (Issue #1000).

セレクタと状態表示だけを持つ薄いパネル — 解決ロジックはすべて
``room_directivity_overlay`` (Qt非依存) にあり、パネルは
``DirectivityOverlayRequest`` を組み立てて ``changed`` を発するだけ。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from .room_directivity_overlay import (
    DIRECTIVITY_DISCLAIMER_JA,
    DirectivityOverlayRequest,
    DirectivityOverlayScene,
    DirectivitySpeakerOption,
)

_ALL_SPEAKERS = '__all__'
_FLOOR_CHOICES = (-15.0, -20.0, -25.0, -30.0)


class RoomDirectivityPanel(QWidget):
    """指向性バルーン表示のトグル + スピーカー/周波数/フロア選択。"""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._options: tuple[DirectivitySpeakerOption, ...] = ()
        self._frequencies: tuple[float, ...] = ()
        self._block = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        group = QGroupBox('指向性バルーン（実測データ）')
        form = QFormLayout(group)

        self.enable_check = QCheckBox('3Dに指向性バルーンを表示')
        self.enable_check.setToolTip(
            '封緘された実測指向性データセットを、スピーカーの設置エイムに'
            '整合させて3D表示します。データのないスピーカーは'
            'UNKNOWN（グレー枠）として正直に表示し、推定モデルは描きません。'
        )
        form.addRow(self.enable_check)

        self.speaker_combo = QComboBox()
        self.speaker_combo.setToolTip('表示するスピーカーを選択')
        form.addRow(QLabel('スピーカー'), self.speaker_combo)

        self.frequency_combo = QComboBox()
        self.frequency_combo.setToolTip(
            '測定グリッド上の周波数のみ選択できます（グリッド外補間はしません）'
        )
        form.addRow(QLabel('周波数'), self.frequency_combo)

        self.floor_combo = QComboBox()
        for floor in _FLOOR_CHOICES:
            self.floor_combo.addItem(f'{floor:g} dB', floor)
        self.floor_combo.setCurrentIndex(2)
        self.floor_combo.setToolTip(
            '表示レンジの下限（相対dB）。これより低い測定点は最小半径に丸めます'
        )
        form.addRow(QLabel('下限レベル'), self.floor_combo)

        self.status_label = QLabel('—')
        self.status_label.setWordWrap(True)
        form.addRow(self.status_label)

        self.detail_label = QLabel('')
        self.detail_label.setWordWrap(True)
        self.detail_label.setStyleSheet('color:#9aa0a6;')
        form.addRow(self.detail_label)

        disclaimer = QLabel(DIRECTIVITY_DISCLAIMER_JA)
        disclaimer.setWordWrap(True)
        disclaimer.setStyleSheet('color:#7c848e; font-size:11px;')
        form.addRow(disclaimer)

        root.addWidget(group)

        self.enable_check.toggled.connect(self._emit)
        self.speaker_combo.currentIndexChanged.connect(self._emit)
        self.frequency_combo.currentIndexChanged.connect(self._emit)
        self.floor_combo.currentIndexChanged.connect(self._emit)
        self._refresh_speaker_combo()
        self._refresh_frequency_combo()

    @property
    def directivity_enabled(self) -> bool:
        return self.enable_check.isChecked()

    def _emit(self, *_args) -> None:
        if not self._block:
            self.changed.emit()

    def request(self) -> DirectivityOverlayRequest:
        speaker = self.speaker_combo.currentData()
        if speaker in (None, _ALL_SPEAKERS):
            speaker = None
        frequency = self.frequency_combo.currentData()
        floor = self.floor_combo.currentData()
        return DirectivityOverlayRequest(
            speaker_entity_id=speaker,
            frequency_hz=(
                float(frequency) if frequency is not None else None
            ),
            floor_db=float(floor) if floor is not None else -25.0,
        )

    def set_options(
        self, options: tuple[DirectivitySpeakerOption, ...]
    ) -> None:
        """Refresh the speaker list without losing the user's selection."""
        self._options = tuple(options)
        self._refresh_speaker_combo()

    def _refresh_speaker_combo(self) -> None:
        current = self.speaker_combo.currentData()
        self._block = True
        try:
            self.speaker_combo.clear()
            self.speaker_combo.addItem('すべてのスピーカー', _ALL_SPEAKERS)
            for option in self._options:
                self.speaker_combo.addItem(
                    option.label, option.speaker_entity_id
                )
            index = self.speaker_combo.findData(current)
            if index >= 0:
                self.speaker_combo.setCurrentIndex(index)
        finally:
            self._block = False

    def _refresh_frequency_combo(self) -> None:
        current = self.frequency_combo.currentData()
        self._block = True
        try:
            self.frequency_combo.clear()
            for freq in self._frequencies:
                self.frequency_combo.addItem(f'{freq:g} Hz', float(freq))
            index = self.frequency_combo.findData(current)
            self.frequency_combo.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self._block = False

    def show_scene(self, scene: DirectivityOverlayScene | None) -> None:
        """Post-resolve status/detail + frequency list refresh."""
        if scene is None:
            self.status_label.setText('—')
            self.detail_label.setText('')
            return
        speaker = self.speaker_combo.currentData()
        grids: tuple[float, ...] = ()
        for balloon in scene.balloons:
            if speaker in (None, _ALL_SPEAKERS) or (
                balloon.speaker_entity_id == speaker
            ):
                if balloon.frequency_grid_hz:
                    grids = balloon.frequency_grid_hz
                    break
        if grids != self._frequencies:
            self._frequencies = grids
            self._refresh_frequency_combo()
        self.status_label.setText(scene.summary_ja)
        detail_lines: list[str] = []
        for balloon in scene.balloons:
            tag = {
                'mesh': 'バルーン',
                'points': '点群',
                'unknown': 'UNKNOWN',
                'blocked': 'ブロック',
            }.get(balloon.state, balloon.state)
            line = f'{balloon.speaker_name}: {tag}'
            if balloon.dataset_id is not None:
                line += (
                    f' — {balloon.dataset_id}'
                    f' v{balloon.dataset_version}'
                )
            detail_lines.append(line)
            for gap in balloon.gaps:
                detail_lines.append(f'  ・{gap.label_ja}（{gap.count}）')
            for reason in balloon.reasons:
                detail_lines.append(f'  ・{reason}')
        self.detail_label.setText('\n'.join(detail_lines[:24]))
