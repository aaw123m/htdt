"""Playback-chain / amplifier headroom authoring surface.

Authors the evidence a ``PlaybackChainScenario`` needs — amplifier output
capability, speaker electrical load, routing and simultaneous-channel
condition — and evaluates it, all inside the native workflow. Unknown
fields stay unknown; no marketing wattage is promoted to an exact
multi-channel capability.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Sequence
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .cad_display_labels import (
    limiter_state_label,
    objective_state_label,
    solver_reason_label,
)
from .cad_amplifier_headroom import (
    AmplifierChannelCountCondition,
    AmplifierLoadDomain,
    AmplifierOutputCapability,
    ElectricalValue,
    PlaybackChainEvaluation,
    PlaybackChainScenario,
    PlaybackRouting,
    SimultaneousChannelCondition,
    SpeakerElectricalLoadAuthority,
    build_amplifier_output_capability,
    build_playback_chain_scenario,
    build_speaker_electrical_load_authority,
    evaluate_playback_chain,
)
from .cad_amplifier_headroom_repository import CadAmplifierHeadroomRepository
from .cad_direct_level import DirectLevelFrequencyBand
from .cad_equipment import EquipmentDataProvenance, FrequencyDomain
from .cad_equipment_repository import CadEquipmentRepository
from .cad_repository import SceneRepository
from .cad_system_variant_repository import CadSystemVariantRepository
from .field_tooltips import apply_field_tooltip
from .user_facing_error import warn_user


def _provenance(
    *,
    evidence_kind: str,
    source_name: str,
    source_version: str,
    source_reference: str,
    source_bytes: bytes | None = None,
) -> EquipmentDataProvenance:
    if source_bytes is not None:
        source_sha = sha256(source_bytes).hexdigest()
    else:
        source_sha = sha256(
            f"{source_name}|{source_version}|{source_reference}".encode(
                "utf-8"
            )
        ).hexdigest()
    return EquipmentDataProvenance(
        evidence_kind=evidence_kind,
        source_name=source_name,
        source_version=source_version,
        source_reference=source_reference,
        source_sha256=source_sha,
    )


def _capability_label(capability: AmplifierOutputCapability) -> str:
    name = (
        f"{capability.manufacturer} {capability.model}"
        if capability.manufacturer and capability.model
        else capability.user_label or capability.capability_id
    )
    return f"{name} / {capability.output_id} (v{capability.version})"


def _load_label(load: SpeakerElectricalLoadAuthority) -> str:
    return (
        f"{load.equipment_definition_id} — "
        f"{load.resistance_ohm} Ω ({load.semantics}, v{load.version})"
    )


def _scalar_text(result, unit_fallback: str = "") -> str:
    if result.state == "available":
        return f"{result.value:.2f} {result.unit or unit_fallback}"
    state = objective_state_label(result.state)
    reason = solver_reason_label(result.reason) if result.reason else ""
    return f"{state}: {reason}" if reason else state


def evaluation_summary(evaluation: PlaybackChainEvaluation) -> str:
    """Speaker-limited vs amplifier-limited comparison text."""
    lines = [
        f"連続アンプ余裕: {_scalar_text(evaluation.continuous_electrical_margin)}",
        f"ピークアンプ余裕: {_scalar_text(evaluation.peak_electrical_margin)}",
        "連続SPL上限: "
        f"アンプ {_scalar_text(evaluation.continuous_amplifier_spl_ceiling)} / "
        f"スピーカー {_scalar_text(evaluation.continuous_speaker_spl_ceiling)}",
        "ピークSPL上限: "
        f"アンプ {_scalar_text(evaluation.peak_amplifier_spl_ceiling)} / "
        f"スピーカー {_scalar_text(evaluation.peak_speaker_spl_ceiling)}",
        "ターゲット余裕（アンプ制約）: "
        + _scalar_text(evaluation.amplifier_constrained_target_margin),
        f"連続リミッター: {limiter_state_label(evaluation.continuous_limiter)}",
        f"ピークリミッター: {limiter_state_label(evaluation.peak_limiter)}",
    ]
    if evaluation.support_reasons:
        lines.append("根拠:")
        lines.extend(
            f"- {solver_reason_label(reason)}"
            for reason in evaluation.support_reasons
        )
    return "\n".join(lines)


class PlaybackChainService:
    """Workflow adapter over the amplifier headroom authorities."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        document_id: str,
    ) -> None:
        self.scene_repository = scene_repository
        self.document_id = document_id
        self.variant_repository = CadSystemVariantRepository(scene_repository)
        self.equipment_repository = CadEquipmentRepository(
            scene_repository,
            self.variant_repository,
        )
        self.headroom_repository = CadAmplifierHeadroomRepository(
            scene_repository,
            self.variant_repository,
            self.equipment_repository,
        )

    def variants(self):
        return self.variant_repository.list_variants(self.document_id)

    def speaker_entities(self) -> tuple[tuple[str, str, str], ...]:
        """(entity_id, name, role) for scene speakers on the current head."""
        latest = self.scene_repository.current_head(self.document_id)
        if latest is None:
            return ()
        return tuple(
            (
                entity.entity_id,
                entity.name,
                entity.speaker_role or "",
            )
            for entity in latest.document.entities
            if entity.kind == "speaker"
        )

    def source_equipment_choices(self) -> tuple[tuple[str, str], ...]:
        result: list[tuple[str, str]] = []
        for item in self.equipment_repository.list_definitions():
            label = (
                f"{item.manufacturer} {item.model}"
                if item.manufacturer and item.model
                else item.user_label or item.definition_id
            )
            result.append((item.semantic_sha256, f"{label} (v{item.version})"))
        return tuple(result)

    def amplifier_capabilities(self) -> tuple[AmplifierOutputCapability, ...]:
        return self.headroom_repository.list_amplifier_capabilities()

    def speaker_loads(self) -> tuple[SpeakerElectricalLoadAuthority, ...]:
        return self.headroom_repository.list_speaker_loads()

    def create_amplifier_capability(
        self,
        *,
        user_label: str,
        output_id: str,
        evidence_kind: str,
        source_name: str,
        source_version: str,
        source_reference: str,
        supported_min_load_ohm: float,
        supported_max_load_ohm: float,
        simultaneous_channel_count: int,
        shared_supply_evidence: bool,
        continuous_voltage_v_rms: float | None = None,
        continuous_duration_s: float | None = None,
        peak_voltage_v_rms: float | None = None,
        peak_duration_s: float | None = None,
        gain_db: float | None = None,
        manufacturer: str | None = None,
        model: str | None = None,
        band_min_hz: float = 20.0,
        band_max_hz: float = 20000.0,
        clipping_reference: str = "連続定格正弦波クリップ",
        weighting: str = "none",
    ) -> AmplifierOutputCapability:
        """Persist a user-defined amplifier output capability.

        ``simultaneous_channel_count`` > 1 requires explicit shared-supply
        evidence; a single-channel datasheet is never promoted into a
        multi-channel rating.
        """
        missing: list[str] = []
        if continuous_voltage_v_rms is None:
            missing.append("continuous_capability")
        if peak_voltage_v_rms is None:
            missing.append("peak_capability")
        if gain_db is None:
            missing.append("gain_db")
        capability = build_amplifier_output_capability(
            capability_id=f"amp-{uuid4().hex[:16]}",
            version="1",
            identity_kind="user_defined",
            output_id=output_id,
            provenance=(
                _provenance(
                    evidence_kind=evidence_kind,
                    source_name=source_name,
                    source_version=source_version,
                    source_reference=source_reference,
                ),
            ),
            manufacturer=manufacturer,
            model=model,
            user_label=user_label,
            supported_load=AmplifierLoadDomain(
                reference_load_ohm=supported_min_load_ohm,
                minimum_load_ohm=supported_min_load_ohm,
                maximum_load_ohm=supported_max_load_ohm,
            ),
            continuous_capability=(
                None
                if continuous_voltage_v_rms is None
                else ElectricalValue(
                    quantity="voltage_v_rms",
                    value=continuous_voltage_v_rms,
                )
            ),
            peak_capability=(
                None
                if peak_voltage_v_rms is None
                else ElectricalValue(
                    quantity="voltage_v_rms",
                    value=peak_voltage_v_rms,
                )
            ),
            continuous_duration_s=continuous_duration_s,
            peak_duration_s=peak_duration_s,
            gain_db=gain_db,
            clipping_reference_definition=clipping_reference,
            valid_frequency_band=FrequencyDomain(
                minimum_hz=band_min_hz,
                maximum_hz=band_max_hz,
            ),
            weighting=weighting,
            channel_count_condition=AmplifierChannelCountCondition(
                simultaneous_channel_count=simultaneous_channel_count,
                shared_supply_evidence=shared_supply_evidence,
                condition_description=(
                    f"{simultaneous_channel_count}ch 同時"
                    + (" (共有電源証拠あり)" if shared_supply_evidence else "")
                ),
            ),
            missing_unsupported_fields=tuple(missing),
        )
        return self.headroom_repository.save_amplifier_capability(capability)

    def create_speaker_load(
        self,
        *,
        equipment_sha256: str,
        semantics: str,
        resistance_ohm: float,
        evidence_kind: str,
        source_name: str,
        source_version: str,
        source_reference: str,
        band_min_hz: float = 20.0,
        band_max_hz: float = 20000.0,
    ) -> SpeakerElectricalLoadAuthority:
        definition = self.equipment_repository.get_definition_by_hash(
            equipment_sha256
        )
        if definition is None:
            raise ValueError("equipment definition is not persisted")
        load = build_speaker_electrical_load_authority(
            load_id=f"load-{uuid4().hex[:16]}",
            version="1",
            equipment_definition=definition,
            semantics=semantics,
            resistance_ohm=resistance_ohm,
            valid_frequency_band=FrequencyDomain(
                minimum_hz=band_min_hz,
                maximum_hz=band_max_hz,
            ),
            provenance=_provenance(
                evidence_kind=evidence_kind,
                source_name=source_name,
                source_version=source_version,
                source_reference=source_reference,
            ),
        )
        return self.headroom_repository.save_speaker_load(load)

    def create_scenario(
        self,
        *,
        variant_id: str,
        source_entity_id: str,
        channel_role_id: str,
        amplifier_sha256: str,
        speaker_load_sha256: str | None,
        source_equipment_sha256: str,
        simultaneous_output_ids: Sequence[str],
        requested_input_v_rms: float,
        requested_continuous_v_rms: float,
        requested_peak_v_rms: float,
        continuous_duration_s: float,
        peak_duration_s: float,
        target_spl_db_spl: float,
        acoustic_target_distance_m: float,
        target_mode: str,
        band_min_hz: float = 20.0,
        band_max_hz: float = 20000.0,
        weighting: str = "none",
        target_reference_condition: str = "ユーザー設定ターゲット",
    ) -> PlaybackChainScenario:
        latest = self.scene_repository.current_head(self.document_id)
        if latest is None:
            raise ValueError("scene revision is required")
        revision = self.scene_repository.get(latest.revision_id)
        variant = self.variant_repository.get_variant(variant_id)
        if variant is None or variant.document_id != self.document_id:
            raise ValueError("unknown system variant")
        source_equipment = self.equipment_repository.get_definition_by_hash(
            source_equipment_sha256
        )
        if source_equipment is None:
            raise ValueError("source equipment definition is not persisted")
        amplifier = self.headroom_repository.get_amplifier_capability_by_hash(
            amplifier_sha256
        )
        if amplifier is None:
            raise ValueError("amplifier capability is not persisted")
        speaker_load = (
            None
            if speaker_load_sha256 is None
            else self.headroom_repository.get_speaker_load_by_hash(
                speaker_load_sha256
            )
        )
        if speaker_load_sha256 is not None and speaker_load is None:
            raise ValueError("speaker load is not persisted")
        condition = SimultaneousChannelCondition(
            output_ids=tuple(
                dict.fromkeys(
                    (*simultaneous_output_ids, amplifier.output_id)
                )
            )
        )
        scenario = build_playback_chain_scenario(
            revision=revision,
            variant=variant,
            source_equipment=source_equipment,
            amplifier_capability=amplifier,
            speaker_load=speaker_load,
            routing=PlaybackRouting(
                source_entity_id=source_entity_id,
                channel_role_id=channel_role_id,
                amplifier_output_id=amplifier.output_id,
            ),
            requested_input=ElectricalValue(
                quantity="voltage_v_rms",
                value=requested_input_v_rms,
            ),
            requested_output_quantity="voltage_v_rms",
            requested_continuous_output_value=requested_continuous_v_rms,
            requested_peak_output_value=requested_peak_v_rms,
            continuous_duration_s=continuous_duration_s,
            peak_duration_s=peak_duration_s,
            frequency_band=DirectLevelFrequencyBand(
                low_hz=band_min_hz,
                high_hz=band_max_hz,
            ),
            weighting=weighting,
            target_spl_db_spl=target_spl_db_spl,
            target_reference_condition=target_reference_condition,
            acoustic_target_distance_m=acoustic_target_distance_m,
            target_mode=target_mode,
            simultaneous_channel_condition=condition,
        )
        return self.headroom_repository.save_scenario(scenario)

    def evaluate(
        self,
        scenario: PlaybackChainScenario,
    ) -> PlaybackChainEvaluation:
        latest = self.scene_repository.current_head(self.document_id)
        if latest is None:
            raise ValueError("scene revision is required")
        revision = self.scene_repository.get(latest.revision_id)
        variant = self.variant_repository.get_variant(scenario.variant_id)
        if variant is None:
            raise ValueError("unknown system variant")
        source_equipment = self.equipment_repository.get_definition_by_hash(
            scenario.source_equipment.semantic_sha256
        )
        if source_equipment is None:
            raise ValueError("source equipment definition is not persisted")
        amplifier = self.headroom_repository.get_amplifier_capability_by_hash(
            scenario.amplifier_capability.semantic_sha256
        )
        if amplifier is None:
            raise ValueError("amplifier capability is not persisted")
        speaker_load = (
            None
            if scenario.speaker_load is None
            else self.headroom_repository.get_speaker_load_by_hash(
                scenario.speaker_load.semantic_sha256
            )
        )
        evaluation = evaluate_playback_chain(
            revision=revision,
            variant=variant,
            equipment_definition=source_equipment,
            amplifier_capability=amplifier,
            speaker_load=speaker_load,
            scenario=scenario,
        )
        return self.headroom_repository.save_evaluation(evaluation)


class PlaybackChainDialog(QDialog):
    """Author amplifier capability / speaker load / routing, then evaluate."""

    def __init__(
        self,
        service: PlaybackChainService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("再生チェーン / ヘッドルーム")
        self.resize(720, 560)
        layout = QVBoxLayout(self)

        self.tabs = QTabWidget()
        self.tabs.setAccessibleName("再生チェーン設定")
        layout.addWidget(self.tabs, 1)

        self._build_amplifier_tab()
        self._build_load_tab()
        self._build_scenario_tab()

        self.box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.box.rejected.connect(self.reject)
        layout.addWidget(self.box)

        # The QTabWidget would otherwise claim initial focus — a data-entry
        # dialog should open on its first field so typing works immediately.
        self.amp_label.setFocus()

        self._refresh_amplifier_combo()
        self._refresh_load_combo()
        self._refresh_scenario_inputs()

    # --- amplifier tab -------------------------------------------------
    def _build_amplifier_tab(self) -> None:
        tab = QWidget()
        form = QFormLayout(tab)
        self.amp_existing = QComboBox()
        self.amp_existing.setAccessibleName("保存済みアンプ能力")
        form.addRow("保存済みアンプ能力", self.amp_existing)
        self.amp_label = QLineEdit()
        form.addRow("ラベル（必須）", self.amp_label)
        self.amp_manufacturer = QLineEdit()
        form.addRow("メーカー", self.amp_manufacturer)
        self.amp_model = QLineEdit()
        form.addRow("モデル", self.amp_model)
        self.amp_output_id = QLineEdit()
        self.amp_output_id.setPlaceholderText("例: front-l")
        form.addRow("出力ID（必須）", self.amp_output_id)
        self.amp_min_load = QDoubleSpinBox()
        self.amp_min_load.setRange(1.0, 32.0)
        self.amp_min_load.setValue(4.0)
        form.addRow("負荷下限 Ω", self.amp_min_load)
        self.amp_max_load = QDoubleSpinBox()
        self.amp_max_load.setRange(1.0, 64.0)
        self.amp_max_load.setValue(16.0)
        form.addRow("負荷上限 Ω", self.amp_max_load)
        self.amp_continuous_v = QDoubleSpinBox()
        self.amp_continuous_v.setRange(0.0, 200.0)
        self.amp_continuous_v.setSpecialValueText("不明")
        form.addRow("連続出力電圧 V RMS", self.amp_continuous_v)
        self.amp_continuous_s = QDoubleSpinBox()
        self.amp_continuous_s.setRange(0.0, 86400.0)
        self.amp_continuous_s.setSpecialValueText("不明")
        form.addRow("連続持続時間 s", self.amp_continuous_s)
        self.amp_peak_v = QDoubleSpinBox()
        self.amp_peak_v.setRange(0.0, 300.0)
        self.amp_peak_v.setSpecialValueText("不明")
        form.addRow("ピーク出力電圧 V RMS", self.amp_peak_v)
        self.amp_gain = QDoubleSpinBox()
        self.amp_gain.setRange(-40.0, 60.0)
        self.amp_gain.setValue(0.0)
        form.addRow("ゲイン dB（0=不明扱い）", self.amp_gain)
        self.amp_channels = QDoubleSpinBox()
        self.amp_channels.setRange(1, 32)
        self.amp_channels.setDecimals(0)
        self.amp_channels.setValue(1)
        form.addRow("同時チャンネル数", self.amp_channels)
        self.amp_shared_supply = QCheckBox("共有電源証拠あり")
        form.addRow("", self.amp_shared_supply)
        self.amp_source_name = QLineEdit()
        form.addRow("出典名（必須）", self.amp_source_name)
        self.amp_source_version = QLineEdit()
        form.addRow("出典バージョン（必須）", self.amp_source_version)
        self.amp_source_reference = QLineEdit()
        form.addRow("出典参照（必須）", self.amp_source_reference)
        self.amp_save_button = QPushButton("アンプ能力を保存")
        self.amp_save_button.setToolTip("現在のアンプ能力設定を権威として保存します")
        self.amp_save_button.clicked.connect(self._save_amplifier)
        form.addRow("", self.amp_save_button)
        self.amp_status = QLabel()
        self.amp_status.setWordWrap(True)
        form.addRow(self.amp_status)
        for field, tip in (
            (self.amp_existing, "保存済みアンプ能力の一覧（表示のみ）"),
            (self.amp_label, "このアンプ能力の表示名（必須）"),
            (self.amp_manufacturer, "アンプのメーカー名（任意）"),
            (self.amp_model, "アンプのモデル名（任意）"),
            (self.amp_output_id, "この出力を識別するID（必須・例: front-l）— シナリオのルーティングで使います"),
            (self.amp_min_load, "この出力が駆動できる負荷の下限（1–32 Ω）"),
            (self.amp_max_load, "この出力が駆動できる負荷の上限（1–64 Ω）"),
            (self.amp_continuous_v, "連続定格出力電圧 V RMS · 「不明」のままなら不明として扱われます"),
            (self.amp_continuous_s, "連続出力の持続時間（秒）· 「不明」のままなら不明として扱われます"),
            (self.amp_peak_v, "ピーク出力電圧 V RMS · 「不明」のままなら不明として扱われます"),
            (self.amp_gain, "アンプのゲイン dB · 0は「不明」として扱われます"),
            (self.amp_channels, "この定格が有効な同時駆動チャンネル数 · 2ch以上には共有電源証拠が必要です"),
            (self.amp_shared_supply, "共有電源の証拠がある場合にオン · 1ch定格を多ch定格へ推定しません"),
            (self.amp_source_name, "この能力値の出典名（必須 — メーカー仕様書・実測など）"),
            (self.amp_source_version, "出典の版・日付（必須）"),
            (self.amp_source_reference, "出典内の参照位置（必須 — ページ・項目名）"),
            (self.amp_save_button, "入力したアンプ能力をライブラリに保存します"),
        ):
            apply_field_tooltip(field, tip, form)
        self.tabs.addTab(tab, "アンプ能力")
        self.tabs.setTabToolTip(
            self.tabs.count() - 1, "アンプの出力能力を登録します"
        )

    def _refresh_amplifier_combo(self) -> None:
        self.amp_existing.clear()
        for capability in self.service.amplifier_capabilities():
            self.amp_existing.addItem(
                _capability_label(capability), capability.semantic_sha256
            )

    def _save_amplifier(self) -> None:
        try:
            continuous = (
                self.amp_continuous_v.value()
                if self.amp_continuous_v.value() > 0.0
                else None
            )
            capability = self.service.create_amplifier_capability(
                user_label=self.amp_label.text().strip(),
                output_id=self.amp_output_id.text().strip(),
                evidence_kind="user_defined",
                source_name=self.amp_source_name.text().strip(),
                source_version=self.amp_source_version.text().strip(),
                source_reference=self.amp_source_reference.text().strip(),
                supported_min_load_ohm=self.amp_min_load.value(),
                supported_max_load_ohm=self.amp_max_load.value(),
                simultaneous_channel_count=int(self.amp_channels.value()),
                shared_supply_evidence=self.amp_shared_supply.isChecked(),
                continuous_voltage_v_rms=continuous,
                continuous_duration_s=(
                    self.amp_continuous_s.value()
                    if self.amp_continuous_s.value() > 0.0
                    else None
                ),
                peak_voltage_v_rms=(
                    self.amp_peak_v.value()
                    if self.amp_peak_v.value() > 0.0
                    else None
                ),
                gain_db=(
                    self.amp_gain.value()
                    if self.amp_gain.value() != 0.0
                    else None
                ),
                manufacturer=self.amp_manufacturer.text().strip() or None,
                model=self.amp_model.text().strip() or None,
            )
        except ValueError as exc:
            warn_user(self, "アンプ能力を保存できませんでした", exc)
            return
        self._refresh_amplifier_combo()
        self.amp_status.setText(
            f"保存しました: {_capability_label(capability)}"
        )
        self._refresh_scenario_inputs()

    # --- speaker load tab ----------------------------------------------
    def _build_load_tab(self) -> None:
        tab = QWidget()
        form = QFormLayout(tab)
        self.load_existing = QComboBox()
        self.load_existing.setAccessibleName("保存済みスピーカー負荷")
        form.addRow("保存済みスピーカー負荷", self.load_existing)
        self.load_equipment = QComboBox()
        form.addRow("対象機器定義", self.load_equipment)
        self.load_semantics = QComboBox()
        self.load_semantics.addItem(
            "正確な抵抗基準（実測）", "exact_resistive_reference"
        )
        self.load_semantics.addItem(
            "公称インピーダンスのみ", "nominal_impedance_only"
        )
        form.addRow("負荷セマンティクス", self.load_semantics)
        self.load_resistance = QDoubleSpinBox()
        self.load_resistance.setRange(0.1, 100.0)
        self.load_resistance.setValue(8.0)
        form.addRow("抵抗 / 公称 Ω", self.load_resistance)
        self.load_source_name = QLineEdit()
        form.addRow("出典名（必須）", self.load_source_name)
        self.load_source_version = QLineEdit()
        form.addRow("出典バージョン（必須）", self.load_source_version)
        self.load_source_reference = QLineEdit()
        form.addRow("出典参照（必須）", self.load_source_reference)
        self.load_save_button = QPushButton("スピーカー負荷を保存")
        self.load_save_button.setToolTip("現在のスピーカー負荷設定を権威として保存します")
        self.load_save_button.clicked.connect(self._save_load)
        form.addRow("", self.load_save_button)
        self.load_status = QLabel()
        self.load_status.setWordWrap(True)
        form.addRow(self.load_status)
        for field, tip in (
            (self.load_existing, "保存済みスピーカー負荷の一覧（表示のみ）"),
            (self.load_equipment, "この負荷を適用する機器定義 — 機器ライブラリの登録済み定義から選びます"),
            (self.load_semantics, "負荷値の意味 — 実測抵抗なら「正確な抵抗基準」、公称値だけなら「公称インピーダンスのみ」"),
            (self.load_resistance, "抵抗または公称インピーダンス（0.1–100 Ω）"),
            (self.load_source_name, "この負荷値の出典名（必須 — メーカー仕様書・実測など）"),
            (self.load_source_version, "出典の版・日付（必須）"),
            (self.load_source_reference, "出典内の参照位置（必須 — ページ・項目名）"),
            (self.load_save_button, "入力したスピーカー負荷をライブラリに保存します"),
        ):
            apply_field_tooltip(field, tip, form)
        self.tabs.addTab(tab, "スピーカー負荷")
        self.tabs.setTabToolTip(
            self.tabs.count() - 1, "スピーカーの電気的負荷を登録します"
        )

    def _refresh_load_combo(self) -> None:
        self.load_existing.clear()
        for load in self.service.speaker_loads():
            self.load_existing.addItem(
                _load_label(load), load.semantic_sha256
            )

    def _save_load(self) -> None:
        if self.load_equipment.currentData() is None:
            QMessageBox.warning(
                self, "スピーカー負荷", "対象機器定義を選択してください"
            )
            return
        try:
            load = self.service.create_speaker_load(
                equipment_sha256=str(self.load_equipment.currentData()),
                semantics=str(self.load_semantics.currentData()),
                resistance_ohm=self.load_resistance.value(),
                evidence_kind="user_defined",
                source_name=self.load_source_name.text().strip(),
                source_version=self.load_source_version.text().strip(),
                source_reference=self.load_source_reference.text().strip(),
            )
        except ValueError as exc:
            warn_user(self, "スピーカー負荷を保存できませんでした", exc)
            return
        self._refresh_load_combo()
        self.load_status.setText(f"保存しました: {_load_label(load)}")
        self._refresh_scenario_inputs()

    # --- scenario tab ---------------------------------------------------
    def _build_scenario_tab(self) -> None:
        tab = QWidget()
        form = QFormLayout(tab)
        self.variant_combo = QComboBox()
        form.addRow("システムバリアント提案", self.variant_combo)
        self.entity_combo = QComboBox()
        form.addRow("音源エンティティ", self.entity_combo)
        self.role_edit = QLineEdit()
        self.role_edit.setPlaceholderText("例: FL")
        form.addRow("チャンネル役割", self.role_edit)
        self.source_combo = QComboBox()
        form.addRow("音源機器定義", self.source_combo)
        self.scenario_amp = QComboBox()
        form.addRow("アンプ能力", self.scenario_amp)
        self.scenario_load = QComboBox()
        self.scenario_load.addItem("負荷なし（不明として評価）", None)
        form.addRow("スピーカー負荷", self.scenario_load)
        self.simultaneous_edit = QLineEdit()
        self.simultaneous_edit.setPlaceholderText(
            "同時出力ID（空白区切り; ルーティング先は自動追加）"
        )
        form.addRow("同時チャンネル条件", self.simultaneous_edit)
        self.target_spl = QDoubleSpinBox()
        self.target_spl.setRange(40.0, 130.0)
        self.target_spl.setValue(85.0)
        form.addRow("ターゲット SPL dB", self.target_spl)
        self.target_distance = QDoubleSpinBox()
        self.target_distance.setRange(0.1, 20.0)
        self.target_distance.setValue(2.0)
        form.addRow("ターゲット距離 m", self.target_distance)
        self.target_mode = QComboBox()
        self.target_mode.addItem("連続", "continuous")
        self.target_mode.addItem("ピーク", "peak")
        form.addRow("ターゲットモード", self.target_mode)
        self.requested_continuous = QDoubleSpinBox()
        self.requested_continuous.setRange(0.01, 200.0)
        self.requested_continuous.setValue(10.0)
        form.addRow("要求連続出力 V RMS", self.requested_continuous)
        self.requested_peak = QDoubleSpinBox()
        self.requested_peak.setRange(0.01, 300.0)
        self.requested_peak.setValue(20.0)
        form.addRow("要求ピーク出力 V RMS", self.requested_peak)
        self.continuous_duration = QDoubleSpinBox()
        self.continuous_duration.setRange(0.001, 86400.0)
        self.continuous_duration.setValue(60.0)
        form.addRow("連続持続時間 s", self.continuous_duration)
        self.peak_duration = QDoubleSpinBox()
        self.peak_duration.setRange(0.001, 600.0)
        self.peak_duration.setValue(0.05)
        form.addRow("ピーク持続時間 s", self.peak_duration)
        self.requested_input = QDoubleSpinBox()
        self.requested_input.setRange(0.001, 100.0)
        self.requested_input.setValue(1.0)
        form.addRow("入力レベル V RMS", self.requested_input)
        self.evaluate_button = QPushButton("シナリオを保存して評価")
        self.evaluate_button.setToolTip("再生チェーン構成をシナリオとして保存し評価します")
        self.evaluate_button.clicked.connect(self._evaluate)
        form.addRow("", self.evaluate_button)
        self.result_label = QLabel()
        self.result_label.setWordWrap(True)
        self.result_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        form.addRow(self.result_label)
        for field, tip in (
            (self.variant_combo, "評価対象のシステムバリアント提案"),
            (self.entity_combo, "音源となるシーン内のスピーカーエンティティ"),
            (self.role_edit, "チャンネル役割（例: FL, FR, C）— ルーティングに使います"),
            (self.source_combo, "音源機器の定義 — 機器ライブラリの登録済み定義から選びます"),
            (self.scenario_amp, "このシナリオで使うアンプ能力 — 先に「アンプ能力」タブで保存します"),
            (self.scenario_load, "このシナリオで使うスピーカー負荷 — 未選択なら負荷は「不明」として評価されます"),
            (self.simultaneous_edit, "同時に駆動する他の出力ID（空白区切り）— ルーティング先は自動で追加されます"),
            (self.target_spl, "到達したい音圧レベル（40–130 dB SPL）"),
            (self.target_distance, "ターゲットSPLを評価する距離（0.1–20 m）"),
            (self.target_mode, "ターゲットを連続定格に対して評価するか、ピーク定格に対して評価するか"),
            (self.requested_continuous, "アンプに要求する連続出力電圧（0.01–200 V RMS）"),
            (self.requested_peak, "アンプに要求するピーク出力電圧（0.01–300 V RMS）"),
            (self.continuous_duration, "連続出力の持続時間（秒）"),
            (self.peak_duration, "ピーク出力の持続時間（秒）"),
            (self.requested_input, "アンプへの入力レベル（V RMS）"),
            (self.evaluate_button, "シナリオを保存し、アンプとスピーカーのヘッドルームを評価します"),
        ):
            apply_field_tooltip(field, tip, form)
        self.tabs.addTab(tab, "シナリオ / 評価")
        self.tabs.setTabToolTip(
            self.tabs.count() - 1,
            "機器・アンプ・負荷を組み合わせてヘッドルームを評価します",
        )

    def _refresh_scenario_inputs(self) -> None:
        self.variant_combo.clear()
        for variant in self.service.variants():
            self.variant_combo.addItem(variant.name, variant.variant_id)
        self.entity_combo.clear()
        for entity_id, name, role in self.service.speaker_entities():
            label = f"{name}（{role or '未設定'}）"
            self.entity_combo.addItem(label, entity_id)
        self.source_combo.clear()
        for sha, label in self.service.source_equipment_choices():
            self.source_combo.addItem(label, sha)
        self.load_equipment.clear()
        for sha, label in self.service.source_equipment_choices():
            self.load_equipment.addItem(label, sha)
        self.scenario_amp.clear()
        for capability in self.service.amplifier_capabilities():
            self.scenario_amp.addItem(
                _capability_label(capability), capability.semantic_sha256
            )
        self.scenario_load.clear()
        self.scenario_load.addItem("負荷なし（不明として評価）", None)
        for load in self.service.speaker_loads():
            self.scenario_load.addItem(_load_label(load), load.semantic_sha256)

    def _evaluate(self) -> None:
        if self.variant_combo.currentData() is None:
            QMessageBox.warning(
                self, "再生チェーン", "システムバリアント提案を選択してください"
            )
            return
        if self.entity_combo.currentData() is None:
            QMessageBox.warning(
                self, "再生チェーン", "音源エンティティを選択してください"
            )
            return
        if self.source_combo.currentData() is None:
            QMessageBox.warning(
                self, "再生チェーン", "音源機器定義を選択してください"
            )
            return
        if self.scenario_amp.currentData() is None:
            QMessageBox.warning(
                self, "再生チェーン", "アンプ能力を先に保存してください"
            )
            return
        role = self.role_edit.text().strip()
        if not role:
            QMessageBox.warning(
                self, "再生チェーン", "チャンネル役割を入力してください"
            )
            return
        simultaneous = tuple(
            item for item in self.simultaneous_edit.text().split() if item
        )
        try:
            scenario = self.service.create_scenario(
                variant_id=str(self.variant_combo.currentData()),
                source_entity_id=str(self.entity_combo.currentData()),
                channel_role_id=role,
                amplifier_sha256=str(self.scenario_amp.currentData()),
                speaker_load_sha256=(
                    None
                    if self.scenario_load.currentData() is None
                    else str(self.scenario_load.currentData())
                ),
                source_equipment_sha256=str(self.source_combo.currentData()),
                simultaneous_output_ids=simultaneous,
                requested_input_v_rms=self.requested_input.value(),
                requested_continuous_v_rms=self.requested_continuous.value(),
                requested_peak_v_rms=self.requested_peak.value(),
                continuous_duration_s=self.continuous_duration.value(),
                peak_duration_s=self.peak_duration.value(),
                target_spl_db_spl=self.target_spl.value(),
                acoustic_target_distance_m=self.target_distance.value(),
                target_mode=str(self.target_mode.currentData()),
            )
            evaluation = self.service.evaluate(scenario)
        except ValueError as exc:
            warn_user(self, "再生チェーンを評価できませんでした", exc)
            return
        self.result_label.setText(evaluation_summary(evaluation))


__all__ = [
    "PlaybackChainDialog",
    "PlaybackChainService",
    "evaluation_summary",
]
