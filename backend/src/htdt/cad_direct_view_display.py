"""Direct-view display dynamic/spatial qualification authority (#625).

A TV/OLED/LCD/MiniLED/direct-view LED display does **not** have one
content-independent luminance, black level, EOTF or color-performance
state.  Real output depends on test-window size / APL, local-dimming
zone interaction, emissive automatic brightness limiting (ABL),
static-content protection (temporal dimming), panel thermal state,
tone mapping, viewing angle, screen position, picture mode, eco /
ambient-light controls and frame-rate processing.

This module is the fail-closed authority over those dependencies:

- :class:`DirectViewDisplayState` — the exact device state identity:
  panel technology, firmware, picture mode, SDR/HDR mode, backlight /
  OLED-light control, local-dimming setting, dynamic contrast, tone
  mapping, ambient-light adaptation, eco/power-saving, motion
  processing, refresh rate.  Unknown settings stay ``unknown`` — they
  never default to ``off``.
- :class:`DisplayStimulusContext` — the stimulus identity composed with
  the #608 stimulus registry: window size, patch position, surround /
  background level, APL, sequence order, hold duration, inter-pattern
  interval, full-field vs windowed, static vs dynamic content, HDR
  metadata/EOTF/container.  A ``2 % window`` and a ``100 % full field``
  are different stimuli and never compare as one measurand.
- :class:`DisplayPhotometricMeasurement` — one photometric observation
  bound by hash to both a display state and a stimulus context, with a
  quantity kind that keeps TRANSIENT_PEAK / STABILIZED_WINDOW /
  SUSTAINED_FULL_FIELD / TIME_SERIES luminance distinct, plus
  time-from-onset, integration interval, warm-up and prior-stimulus
  history.
- :class:`DisplayTemporalObservation` — observed/provider-documented
  temporal (static-content) dimming state, with stimulus sequence,
  elapsed time, affected region and recovery behavior.
- :class:`DisplaySpatialMeasurement` — raw screen-coordinate panel
  uniformity points (luminance / white chromaticity / near-black /
  tint / black level); interpolation is always derived, never stored.
- :class:`DisplayAngleMeasurement` — off-axis observations bound to
  exact observer/meter geometry (h/v angle, distance, screen point,
  seat).
- :func:`evaluate_direct_view_qualification` — seals a
  :class:`DirectViewQualification` verdict against declared claims.

Contract properties:

- ``peak_nits = X`` or ``black_level = Y`` is never stored without the
  stimulus/state semantics that produced it;
- a short transient peak never establishes sustained full-screen
  brightness;
- ABL / power limiting is represented as state-dependent measured
  behavior, never dismissed as "calibration drift";
- temporal dimming is only ever *observed or provider-documented* —
  community labels (ASBL/TPC/GSR) do not transfer across manufacturers;
- this authority never recommends disabling panel-protection functions;
- full-field black under active local dimming never equals real-content
  black performance;
- an emissive black below the instrument floor reports "bounded/below
  measurable floor" — never ``infinite contrast``;
- a center-axis calibration never proves side-seat performance;
- a static grayscale sweep never proves dynamic tone-mapping behavior.

Literature basis (issue §research): SID/ICDM IDMS v1.3 (2025-05-31,
production-current measurement procedures — v1.4 is research-only until
2028); Report ITU-R BT.2408-9 (03/2026 HDR operational guidance);
ANSI/CTA-2037-D + ANSI/CTA-6002 / IEC 62087-2:2023 (exact dynamic
media/stimulus semantics).
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical_json import canonical_sha256 as _digest, canonicalize_payload as _canon


_SHA256 = r'^[0-9a-f]{64}$'

DIRECT_VIEW_AUTHORITY_VERSION = 'direct-view-display-1'


# ---------------------------------------------------------------------------
# Taxonomies (issue §1–§11)
# ---------------------------------------------------------------------------

DirectViewPanelTechnology = Literal[
    'woled',
    'qd_oled',
    'lcd_va',
    'lcd_ips',
    'lcd_ads',
    'miniled_lcd',
    'microled',
    'direct_view_led',
    'other_emissive',
    'other_backlit',
    'unknown',
]

DisplayContentMode = Literal[
    'sdr',
    'hdr10',
    'hlg',
    'dolby_vision',
    'hdr10_plus',
    'mixed',
    'unknown',
]

TriState = Literal['off', 'on', 'auto', 'unknown']

LocalDimmingSetting = Literal[
    'off', 'low', 'medium', 'high', 'auto', 'custom', 'unknown'
]

LuminanceQuantityKind = Literal[
    'transient_peak_luminance',
    'stabilized_window_luminance',
    'sustained_full_field_luminance',
    'time_series_luminance',
    'black_luminance',
    'chromaticity',
    'eotf_tracking_point',
]

TemporalDimmingState = Literal[
    'no_observed_dimming',
    'temporal_dimming_observed',
    'provider_documented_protection',
    'state_unknown',
]

DimmingRegion = Literal['whole_screen', 'partial_region', 'unknown']

RecoveryState = Literal['recovered', 'partial_recovery', 'not_observed', 'unknown']

SpatialObservable = Literal[
    'luminance',
    'white_chromaticity',
    'near_black_uniformity',
    'color_tint',
    'black_level',
]

ContrastKind = Literal[
    'full_field_contrast',
    'ansi_checkerboard_contrast',
    'local_contrast',
    'content_conditioned_contrast',
    'off_axis_contrast',
]

FieldKind = Literal['windowed', 'full_field']

ContentKind = Literal['static', 'dynamic', 'mixed', 'unknown']

HdrMetadataState = Literal['none', 'static', 'dynamic', 'unknown']

DisplayClaimKind = Literal[
    'peak_luminance',
    'sustained_full_field_luminance',
    'black_level',
    'contrast',
    'eotf_tracking',
    'color_performance',
    'uniformity',
    'off_axis_performance',
    'multi_seat_consistency',
    'content_independent_performance',
]

ClaimScope = Literal['reference_mlp', 'declared_seats', 'all_seats', 'unknown']

ClaimVerdictState = Literal[
    'supported',
    'supported_with_limitations',
    'insufficient_evidence',
    'unsupported',
    'conflicting_evidence',
]

QualificationState = Literal[
    'qualified',
    'qualified_with_limitations',
    'insufficient_evidence',
    'unsupported',
    'conflicting_evidence',
]

DirectViewReason = Literal[
    'QUALIFIED_AS_MEASURED',
    'QUALIFIED_WITH_LIMITATIONS',
    'INSUFFICIENT_EVIDENCE',
    'DISPLAY_STATE_INCOMPLETE',
    'STIMULUS_IDENTITY_MISSING',
    'WINDOW_SIZE_UNDECLARED',
    'APL_COVERAGE_MISSING',
    'PEAK_PROMOTED_TO_SUSTAINED',
    'TRANSIENT_ONLY_EVIDENCE',
    'ABL_BEHAVIOR_UNCHARACTERIZED',
    'TEMPORAL_DIMMING_OBSERVED',
    'TEMPORAL_PROTECTION_UNRESOLVED',
    'LOCAL_DIMMING_ACTIVE_UNMEASURED',
    'LOCAL_DIMMING_STATE_UNKNOWN',
    'INSTRUMENT_FLOOR_LIMITED',
    'INFINITE_CONTRAST_REJECTED',
    'CONTRAST_KIND_CONFLATED',
    'UNIFORMITY_UNMEASURED',
    'UNIFORMITY_PARTIAL',
    'VIEWING_ANGLE_UNVERIFIED',
    'SEAT_COVERAGE_INCOMPLETE',
    'WARMUP_STATE_UNKNOWN',
    'THERMAL_HISTORY_MISMATCH',
    'MEASUREMENT_ORDER_UNRECORDED',
    'ADAPTIVE_BEHAVIOR_UNDECLARED',
    'CONTENT_STATE_MISMATCH',
    'PROTECTION_BYPASS_NEVER_RECOMMENDED',
    'MEASUREMENT_METHOD_NOT_COMPLIANCE',
]


# ---------------------------------------------------------------------------
# Japanese product labels
# ---------------------------------------------------------------------------

PANEL_TECHNOLOGY_LABELS: dict[str, str] = {
    'woled': 'WOLED',
    'qd_oled': 'QD-OLED',
    'lcd_va': 'LCD (VA)',
    'lcd_ips': 'LCD (IPS)',
    'lcd_ads': 'LCD (ADS)',
    'miniled_lcd': 'MiniLED LCD',
    'microled': 'MicroLED',
    'direct_view_led': '直下型 LED ディスプレイ',
    'other_emissive': 'その他自発光',
    'other_backlit': 'その他バックライト型',
    'unknown': '不明',
}

CONTENT_MODE_LABELS: dict[str, str] = {
    'sdr': 'SDR',
    'hdr10': 'HDR10',
    'hlg': 'HLG',
    'dolby_vision': 'Dolby Vision',
    'hdr10_plus': 'HDR10+',
    'mixed': '混合',
    'unknown': '不明',
}

TRI_STATE_LABELS: dict[str, str] = {
    'off': 'オフ',
    'on': 'オン',
    'auto': '自動',
    'unknown': '不明',
}

LOCAL_DIMMING_LABELS: dict[str, str] = {
    'off': 'オフ',
    'low': '低',
    'medium': '中',
    'high': '高',
    'auto': '自動',
    'custom': 'カスタム',
    'unknown': '不明',
}

QUANTITY_KIND_LABELS: dict[str, str] = {
    'transient_peak_luminance': '過渡ピーク輝度',
    'stabilized_window_luminance': '安定化ウィンドウ輝度',
    'sustained_full_field_luminance': '持続全画面輝度',
    'time_series_luminance': '時系列輝度',
    'black_luminance': '黒輝度',
    'chromaticity': '色度',
    'eotf_tracking_point': 'EOTF 追従点',
}

TEMPORAL_DIMMING_LABELS: dict[str, str] = {
    'no_observed_dimming': '調光なし（観測）',
    'temporal_dimming_observed': '時間調光を観測',
    'provider_documented_protection': 'プロバイダ文書化保護',
    'state_unknown': '状態不明',
}

SPATIAL_OBSERVABLE_LABELS: dict[str, str] = {
    'luminance': '輝度',
    'white_chromaticity': '白色度',
    'near_black_uniformity': 'ニアブラック均一性',
    'color_tint': '色ティント',
    'black_level': '黒レベル',
}

CONTRAST_KIND_LABELS: dict[str, str] = {
    'full_field_contrast': '全画面コントラスト',
    'ansi_checkerboard_contrast': 'ANSI/チェッカーボード コントラスト',
    'local_contrast': 'ローカルコントラスト',
    'content_conditioned_contrast': 'コンテンツ条件コントラスト',
    'off_axis_contrast': 'オフアクシス コントラスト',
}

CLAIM_KIND_LABELS: dict[str, str] = {
    'peak_luminance': 'ピーク輝度',
    'sustained_full_field_luminance': '持続全画面輝度',
    'black_level': '黒レベル',
    'contrast': 'コントラスト',
    'eotf_tracking': 'EOTF 追従',
    'color_performance': '色性能',
    'uniformity': '均一性',
    'off_axis_performance': 'オフアクシス性能',
    'multi_seat_consistency': 'マルチシート一貫性',
    'content_independent_performance': 'コンテンツ非依存性能',
}

CLAIM_VERDICT_LABELS: dict[str, str] = {
    'supported': '支持',
    'supported_with_limitations': '制限付き支持',
    'insufficient_evidence': '証拠不足',
    'unsupported': '未支持',
    'conflicting_evidence': '証拠が矛盾',
}

QUALIFICATION_STATE_LABELS: dict[str, str] = {
    'qualified': '適格',
    'qualified_with_limitations': '制限付き適格',
    'insufficient_evidence': '証拠不足',
    'unsupported': '未支持',
    'conflicting_evidence': '証拠が矛盾',
}

REASON_LABELS: dict[str, str] = {
    'QUALIFIED_AS_MEASURED': '実測どおり適格',
    'QUALIFIED_WITH_LIMITATIONS': '制限付き適格',
    'INSUFFICIENT_EVIDENCE': '証拠不足',
    'DISPLAY_STATE_INCOMPLETE': 'ディスプレイ状態が不完全',
    'STIMULUS_IDENTITY_MISSING': '刺激同一性が欠落',
    'WINDOW_SIZE_UNDECLARED': 'ウィンドウサイズ未宣言',
    'APL_COVERAGE_MISSING': 'APL カバレッジ不足',
    'PEAK_PROMOTED_TO_SUSTAINED': '過渡ピークを持続輝度へ昇格',
    'TRANSIENT_ONLY_EVIDENCE': '過渡証拠のみ',
    'ABL_BEHAVIOR_UNCHARACTERIZED': 'ABL/電力制限挙動が未特性化',
    'TEMPORAL_DIMMING_OBSERVED': '時間調光を観測',
    'TEMPORAL_PROTECTION_UNRESOLVED': '保護調光状態が未解決',
    'LOCAL_DIMMING_ACTIVE_UNMEASURED': 'ローカルディミング有効だが相互作用未測定',
    'LOCAL_DIMMING_STATE_UNKNOWN': 'ローカルディミング状態不明',
    'INSTRUMENT_FLOOR_LIMITED': '計器フロア以下（境界値として報告）',
    'INFINITE_CONTRAST_REJECTED': '無限コントラスト主張を拒否',
    'CONTRAST_KIND_CONFLATED': 'コントラスト種別の混同',
    'UNIFORMITY_UNMEASURED': '均一性が未測定',
    'UNIFORMITY_PARTIAL': '均一性測定が部分的',
    'VIEWING_ANGLE_UNVERIFIED': '視角が未検証',
    'SEAT_COVERAGE_INCOMPLETE': 'シート網羅が不完全',
    'WARMUP_STATE_UNKNOWN': 'ウォームアップ状態不明',
    'THERMAL_HISTORY_MISMATCH': '熱履歴が不一致',
    'MEASUREMENT_ORDER_UNRECORDED': '測定順序が未記録',
    'ADAPTIVE_BEHAVIOR_UNDECLARED': '適応挙動（環境光/省電力）が未宣言',
    'CONTENT_STATE_MISMATCH': '静的掃引を動的挙動の証明に昇格不可',
    'PROTECTION_BYPASS_NEVER_RECOMMENDED': 'パネル保護の無効化は推奨しない',
    'MEASUREMENT_METHOD_NOT_COMPLIANCE': '測定手順は適合限界を意味しない',
}


# ---------------------------------------------------------------------------
# Sub-records
# ---------------------------------------------------------------------------


class SpatialPoint(BaseModel):
    """One raw screen-coordinate measurement point.

    ``x_frac``/``y_frac`` are fractional screen coordinates in [0, 1].
    Raw measured points are canonical — any interpolation or heatmap is
    derived and never stored here.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    x_frac: float = Field(ge=0.0, le=1.0)
    y_frac: float = Field(ge=0.0, le=1.0)
    value: float | None = None
    chromaticity_x: float | None = Field(default=None, ge=0.0, le=1.0)
    chromaticity_y: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode='after')
    def finite(self) -> 'SpatialPoint':
        for value in (
            self.x_frac,
            self.y_frac,
            self.value,
            self.chromaticity_x,
            self.chromaticity_y,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('spatial point values must be finite')
        return self


class DisplayClaim(BaseModel):
    """One declared claim evaluated against bound evidence.

    ``kind`` selects the measurand family; ``contrast_kind`` is required
    whenever ``kind == 'contrast'`` so full-field/ANSI/local/content-
    conditioned/off-axis contrast are never conflated; ``seat_refs``
    name the important viewer positions for multi-seat claims — a single
    average never hides an unacceptable seat.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: DisplayClaimKind
    label: str | None = Field(default=None, min_length=1)
    contrast_kind: ContrastKind | None = None
    scope: ClaimScope = 'reference_mlp'
    seat_refs: tuple[str, ...] = ()
    target_value_cd_m2: float | None = Field(default=None, gt=0.0)
    requires_reference_environment: bool = False

    @model_validator(mode='after')
    def valid_claim(self) -> 'DisplayClaim':
        if self.kind == 'contrast' and self.contrast_kind is None:
            raise ValueError(
                'contrast claims must declare their contrast kind — '
                'full-field/ANSI/local/content/off-axis are distinct'
            )
        if self.kind != 'contrast' and self.contrast_kind is not None:
            raise ValueError(
                'contrast_kind is only meaningful on contrast claims'
            )
        if self.kind in ('off_axis_performance', 'multi_seat_consistency'):
            if not self.seat_refs:
                raise ValueError(
                    'seat-dependent claims must name their seats — an '
                    'average score must never hide one seat'
                )
        if len(set(self.seat_refs)) != len(self.seat_refs):
            raise ValueError('claim seat refs must be unique')
        if self.target_value_cd_m2 is not None and not isfinite(
            float(self.target_value_cd_m2)
        ):
            raise ValueError('claim target must be finite')
        return self


class ClaimVerdict(BaseModel):
    """Per-claim verdict inside a qualification."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: DisplayClaimKind
    label: str | None = None
    scope: ClaimScope = 'reference_mlp'
    verdict: ClaimVerdictState
    reasons: tuple[DirectViewReason, ...] = ()
    evidence_measurement_ids: tuple[str, ...] = ()
    unverified_seat_refs: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_verdict(self) -> 'ClaimVerdict':
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError('verdict reasons must be unique')
        if len(set(self.evidence_measurement_ids)) != len(
            self.evidence_measurement_ids
        ):
            raise ValueError('evidence refs must be unique')
        if len(set(self.unverified_seat_refs)) != len(
            self.unverified_seat_refs
        ):
            raise ValueError('unverified seat refs must be unique')
        return self


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


class DirectViewDisplayState(BaseModel):
    """Exact direct-view device state identity (#625 §1).

    Every materially relevant control is part of identity: changing one
    creates a distinct qualification identity.  Unknown settings remain
    ``unknown`` — they are never defaulted to ``off``.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['direct-view-display-1'] = (
        DIRECT_VIEW_AUTHORITY_VERSION
    )
    display_state_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    label: str | None = Field(default=None, min_length=1)
    manufacturer: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    hardware_revision: str | None = Field(default=None, min_length=1)
    panel_technology: DirectViewPanelTechnology = 'unknown'
    firmware: str | None = Field(default=None, min_length=1)
    picture_mode: str | None = Field(default=None, min_length=1)
    content_mode: DisplayContentMode = 'unknown'
    backlight_setting: float | None = Field(default=None, ge=0.0)
    oled_light_setting: float | None = Field(default=None, ge=0.0)
    local_dimming: LocalDimmingSetting = 'unknown'
    contrast_enhancer: TriState = 'unknown'
    tone_mapping_mode: str | None = Field(default=None, min_length=1)
    ambient_light_adaptation: TriState = 'unknown'
    eco_power_state: TriState = 'unknown'
    motion_processing_state: str | None = Field(
        default=None, min_length=1
    )
    refresh_rate_hz: float | None = Field(default=None, gt=0.0)
    source_profile_ref: str | None = Field(default=None, min_length=1)
    color_profile_ref: str | None = Field(default=None, min_length=1)
    captured_at_utc: str = Field(min_length=1)
    notes: str | None = Field(default=None, min_length=1)
    display_state_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_state(self) -> 'DirectViewDisplayState':
        for value in (
            self.backlight_setting,
            self.oled_light_setting,
            self.refresh_rate_hz,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('display state values must be finite')
        if self.display_state_sha256 != _digest(self.identity_payload()):
            raise ValueError('display state hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('display_state_id', None)
        payload.pop('display_state_sha256', None)
        return payload

    @property
    def unknown_fields(self) -> tuple[str, ...]:
        """Materially relevant controls still UNKNOWN — honesty, not off."""
        unknown: list[str] = []
        if self.panel_technology == 'unknown':
            unknown.append('panel_technology')
        if self.content_mode == 'unknown':
            unknown.append('content_mode')
        if self.local_dimming == 'unknown':
            unknown.append('local_dimming')
        if self.contrast_enhancer == 'unknown':
            unknown.append('contrast_enhancer')
        if self.ambient_light_adaptation == 'unknown':
            unknown.append('ambient_light_adaptation')
        if self.eco_power_state == 'unknown':
            unknown.append('eco_power_state')
        if self.picture_mode is None:
            unknown.append('picture_mode')
        if self.firmware is None:
            unknown.append('firmware')
        return tuple(unknown)

    @property
    def adaptive_behavior_risk(self) -> bool:
        """Ambient adaptation or eco state is engaged or unknown."""
        return self.ambient_light_adaptation in ('on', 'auto', 'unknown') or (
            self.eco_power_state in ('on', 'auto')
        )


class DisplayStimulusContext(BaseModel):
    """Stimulus identity for one photometric condition (#625 §2, §14).

    Composes with the #608 stimulus registry via ``stimulus_ref`` — the
    window size, position, surround, APL, ordering and timing are part
    of what was measured, so two different contexts never compare as
    one measurand.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['direct-view-display-1'] = (
        DIRECT_VIEW_AUTHORITY_VERSION
    )
    stimulus_context_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    stimulus_ref: str | None = Field(default=None, min_length=1)
    stimulus_sha256: str | None = Field(default=None, pattern=_SHA256)
    label: str | None = Field(default=None, min_length=1)
    window_size_percent: float | None = Field(
        default=None, gt=0.0, le=100.0
    )
    field_kind: FieldKind = 'windowed'
    patch_position: str | None = Field(default=None, min_length=1)
    surround_background_level: float | None = Field(
        default=None, ge=0.0
    )
    apl_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    sequence_id: str | None = Field(default=None, min_length=1)
    sequence_order: int | None = Field(default=None, ge=0)
    hold_duration_s: float | None = Field(default=None, ge=0.0)
    inter_pattern_interval_s: float | None = Field(default=None, ge=0.0)
    content_kind: ContentKind = 'unknown'
    eotf: str | None = Field(default=None, min_length=1)
    hdr_metadata_state: HdrMetadataState = 'unknown'
    container: str | None = Field(default=None, min_length=1)
    preconditioning_ref: str | None = Field(default=None, min_length=1)
    notes: str | None = Field(default=None, min_length=1)
    stimulus_context_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_context(self) -> 'DisplayStimulusContext':
        for value in (
            self.window_size_percent,
            self.surround_background_level,
            self.apl_percent,
            self.hold_duration_s,
            self.inter_pattern_interval_s,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('stimulus context values must be finite')
        if self.field_kind == 'full_field':
            if (
                self.window_size_percent is not None
                and self.window_size_percent < 100.0
            ):
                raise ValueError(
                    'a full-field stimulus cannot declare a sub-100% '
                    'window size'
                )
        elif self.window_size_percent == 100.0:
            raise ValueError(
                'a 100% window is a full-field stimulus, not a windowed one'
            )
        if self.stimulus_context_sha256 != _digest(
            self.identity_payload()
        ):
            raise ValueError('stimulus context hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('stimulus_context_id', None)
        payload.pop('stimulus_context_sha256', None)
        return payload

    @property
    def is_full_field(self) -> bool:
        return self.field_kind == 'full_field'

    @property
    def sequence_recorded(self) -> bool:
        return self.sequence_id is not None and self.sequence_order is not None


class DisplayPhotometricMeasurement(BaseModel):
    """One state-bound photometric observation (#625 §3, §4, §11, §15).

    The quantity kind is part of the measurand: a TRANSIENT_PEAK can
    never be promoted to SUSTAINED_FULL_FIELD.  ``instrument_floor_cd_m2``
    keeps black/contrast claims honest — a reading at or below the floor
    is bounded evidence, never ``infinite``.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['direct-view-display-1'] = (
        DIRECT_VIEW_AUTHORITY_VERSION
    )
    measurement_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    display_state_id: str = Field(min_length=1)
    display_state_sha256: str = Field(pattern=_SHA256)
    stimulus_context_id: str = Field(min_length=1)
    stimulus_context_sha256: str = Field(pattern=_SHA256)
    quantity: LuminanceQuantityKind
    contrast_kind: ContrastKind | None = None
    value_cd_m2: float | None = Field(default=None, ge=0.0)
    chromaticity_x: float | None = Field(default=None, ge=0.0, le=1.0)
    chromaticity_y: float | None = Field(default=None, ge=0.0, le=1.0)
    contrast_value: float | None = Field(default=None, ge=0.0)
    screen_point: str | None = Field(default=None, min_length=1)
    time_from_pattern_onset_s: float | None = Field(default=None, ge=0.0)
    acquisition_interval_s: float | None = Field(default=None, ge=0.0)
    warmup_minutes: float | None = Field(default=None, ge=0.0)
    prior_stimulus_ref: str | None = Field(default=None, min_length=1)
    thermal_telemetry_ref: str | None = Field(default=None, min_length=1)
    instrument_ref: str | None = Field(default=None, min_length=1)
    instrument_sha256: str | None = Field(default=None, pattern=_SHA256)
    instrument_floor_cd_m2: float | None = Field(default=None, ge=0.0)
    below_instrument_floor: bool = False
    measured_at_utc: str = Field(min_length=1)
    notes: str | None = Field(default=None, min_length=1)
    measurement_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_measurement(self) -> 'DisplayPhotometricMeasurement':
        for value in (
            self.value_cd_m2,
            self.chromaticity_x,
            self.chromaticity_y,
            self.contrast_value,
            self.time_from_pattern_onset_s,
            self.acquisition_interval_s,
            self.warmup_minutes,
            self.instrument_floor_cd_m2,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('measurement values must be finite')
        if self.quantity == 'chromaticity' and (
            self.chromaticity_x is None or self.chromaticity_y is None
        ):
            raise ValueError(
                'a chromaticity measurement must carry chromaticity'
            )
        if self.below_instrument_floor and self.instrument_floor_cd_m2 is None:
            raise ValueError(
                'a below-floor claim must carry the instrument floor'
            )
        if self.measurement_sha256 != _digest(self.identity_payload()):
            raise ValueError('photometric measurement hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('measurement_id', None)
        payload.pop('measurement_sha256', None)
        return payload

    @property
    def sustained(self) -> bool:
        return self.quantity == 'sustained_full_field_luminance'

    @property
    def transient(self) -> bool:
        return self.quantity == 'transient_peak_luminance'


class DisplayTemporalObservation(BaseModel):
    """Observed/provider-documented temporal dimming evidence (#625 §5).

    Represents only observable or provider-evidenced behavior — community
    terminology (ASBL/TPC/GSR) never transfers across manufacturers or
    firmware versions.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['direct-view-display-1'] = (
        DIRECT_VIEW_AUTHORITY_VERSION
    )
    observation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    display_state_id: str = Field(min_length=1)
    display_state_sha256: str = Field(pattern=_SHA256)
    stimulus_context_id: str | None = Field(default=None, min_length=1)
    stimulus_context_sha256: str | None = Field(
        default=None, pattern=_SHA256
    )
    state: TemporalDimmingState = 'state_unknown'
    stimulus_sequence_ref: str | None = Field(default=None, min_length=1)
    elapsed_s: float | None = Field(default=None, ge=0.0)
    luminance_drop_percent: float | None = Field(default=None, ge=0.0)
    region: DimmingRegion = 'unknown'
    recovery: RecoveryState = 'unknown'
    firmware_observed: str | None = Field(default=None, min_length=1)
    picture_mode_observed: str | None = Field(default=None, min_length=1)
    temperature_state: str | None = Field(default=None, min_length=1)
    provider_document_ref: str | None = Field(default=None, min_length=1)
    observed_at_utc: str = Field(min_length=1)
    notes: str | None = Field(default=None, min_length=1)
    observation_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_observation(self) -> 'DisplayTemporalObservation':
        for value in (self.elapsed_s, self.luminance_drop_percent):
            if value is not None and not isfinite(float(value)):
                raise ValueError('temporal observation values must be finite')
        if self.state == 'provider_documented_protection' and (
            self.provider_document_ref is None
        ):
            raise ValueError(
                'provider-documented protection requires the document ref '
                '— community labels are not evidence'
            )
        if self.observation_sha256 != _digest(self.identity_payload()):
            raise ValueError('temporal observation hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('observation_id', None)
        payload.pop('observation_sha256', None)
        return payload


class DisplaySpatialMeasurement(BaseModel):
    """Screen-coordinate uniformity measurement (#625 §7).

    Raw measured points are canonical; any interpolation/heatmap is
    derived downstream and never stored.  Distinct from the #619
    projection+screen-system uniformity authority.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['direct-view-display-1'] = (
        DIRECT_VIEW_AUTHORITY_VERSION
    )
    spatial_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    display_state_id: str = Field(min_length=1)
    display_state_sha256: str = Field(pattern=_SHA256)
    stimulus_context_id: str = Field(min_length=1)
    stimulus_context_sha256: str = Field(pattern=_SHA256)
    observable: SpatialObservable
    points: tuple[SpatialPoint, ...]
    instrument_ref: str | None = Field(default=None, min_length=1)
    measured_at_utc: str = Field(min_length=1)
    notes: str | None = Field(default=None, min_length=1)
    spatial_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_spatial(self) -> 'DisplaySpatialMeasurement':
        if not self.points:
            raise ValueError('a spatial measurement needs raw points')
        seen = {(p.x_frac, p.y_frac) for p in self.points}
        if len(seen) != len(self.points):
            raise ValueError('spatial points must have unique coordinates')
        if self.spatial_sha256 != _digest(self.identity_payload()):
            raise ValueError('spatial measurement hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('spatial_id', None)
        payload.pop('spatial_sha256', None)
        return payload

    @property
    def coverage(self) -> str:
        """Honest coverage class of the sampled grid."""
        n = len(self.points)
        if n < 2:
            return 'single_point'
        if n < 5:
            return 'sparse'
        xs = {p.x_frac for p in self.points}
        ys = {p.y_frac for p in self.points}
        if len(xs) >= 3 and len(ys) >= 3:
            return 'grid'
        return 'sparse'


class DisplayAngleMeasurement(BaseModel):
    """Off-axis observation bound to exact observer/meter geometry
    (#625 §8–§9).  Composes with #259 viewing envelope via ``seat_ref``.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['direct-view-display-1'] = (
        DIRECT_VIEW_AUTHORITY_VERSION
    )
    angle_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    display_state_id: str = Field(min_length=1)
    display_state_sha256: str = Field(pattern=_SHA256)
    stimulus_context_id: str = Field(min_length=1)
    stimulus_context_sha256: str = Field(pattern=_SHA256)
    horizontal_angle_deg: float = Field(ge=-90.0, le=90.0)
    vertical_angle_deg: float = Field(ge=-90.0, le=90.0)
    distance_m: float | None = Field(default=None, gt=0.0)
    screen_point: str | None = Field(default=None, min_length=1)
    seat_ref: str | None = Field(default=None, min_length=1)
    luminance_cd_m2: float | None = Field(default=None, ge=0.0)
    chromaticity_x: float | None = Field(default=None, ge=0.0, le=1.0)
    chromaticity_y: float | None = Field(default=None, ge=0.0, le=1.0)
    black_level_cd_m2: float | None = Field(default=None, ge=0.0)
    local_dimming_appearance: str | None = Field(
        default=None, min_length=1
    )
    measured_at_utc: str = Field(min_length=1)
    notes: str | None = Field(default=None, min_length=1)
    angle_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_angle(self) -> 'DisplayAngleMeasurement':
        for value in (
            self.horizontal_angle_deg,
            self.vertical_angle_deg,
            self.distance_m,
            self.luminance_cd_m2,
            self.chromaticity_x,
            self.chromaticity_y,
            self.black_level_cd_m2,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('angle measurement values must be finite')
        if self.angle_sha256 != _digest(self.identity_payload()):
            raise ValueError('angle measurement hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('angle_id', None)
        payload.pop('angle_sha256', None)
        return payload

    @property
    def on_axis(self) -> bool:
        return self.horizontal_angle_deg == 0.0 and (
            self.vertical_angle_deg == 0.0
        )


class DirectViewQualification(BaseModel):
    """Sealed fail-closed verdict for one direct-view display state."""

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['direct-view-display-1'] = (
        DIRECT_VIEW_AUTHORITY_VERSION
    )
    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    display_state_id: str = Field(min_length=1)
    display_state_sha256: str = Field(pattern=_SHA256)
    state: QualificationState
    claim_verdicts: tuple[ClaimVerdict, ...] = ()
    measurement_ids: tuple[str, ...] = ()
    temporal_observation_ids: tuple[str, ...] = ()
    spatial_ids: tuple[str, ...] = ()
    angle_ids: tuple[str, ...] = ()
    reasons: tuple[DirectViewReason, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_qualification(self) -> 'DirectViewQualification':
        for refs in (
            self.measurement_ids,
            self.temporal_observation_ids,
            self.spatial_ids,
            self.angle_ids,
            self.reasons,
        ):
            if len(set(refs)) != len(refs):
                raise ValueError('qualification refs must be unique')
        if self.state == 'qualified' and any(
            v.verdict in ('insufficient_evidence', 'unsupported')
            for v in self.claim_verdicts
        ):
            raise ValueError(
                'a qualified state cannot carry unsupported claims'
            )
        if self.qualification_sha256 != _digest(self.identity_payload()):
            raise ValueError('qualification hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('qualification_id', None)
        payload.pop('qualification_sha256', None)
        return payload


# ---------------------------------------------------------------------------
# Builders (content-sealed construction)
# ---------------------------------------------------------------------------


def _seal(model: type[BaseModel], payload: dict[str, Any], id_field: str, sha_field: str, prefix: str) -> BaseModel:
    probe = model.model_construct(
        **_canon(model, dict(**payload, **{id_field: '', sha_field: ''}))
    )
    sha = _digest(probe.identity_payload())  # type: ignore[attr-defined]
    return model(
        **probe.model_dump(exclude={id_field, sha_field}),
        **{id_field: f'{prefix}{sha}', sha_field: sha},
    )


def build_display_state(
    *,
    document_id: str,
    captured_at_utc: str,
    **fields: Any,
) -> DirectViewDisplayState:
    return _seal(
        DirectViewDisplayState,
        dict(
            document_id=document_id,
            captured_at_utc=captured_at_utc,
            **fields,
        ),
        'display_state_id',
        'display_state_sha256',
        'dvs:',
    )  # type: ignore[return-value]


def build_stimulus_context(
    *,
    document_id: str,
    **fields: Any,
) -> DisplayStimulusContext:
    return _seal(
        DisplayStimulusContext,
        dict(document_id=document_id, **fields),
        'stimulus_context_id',
        'stimulus_context_sha256',
        'dst:',
    )  # type: ignore[return-value]


def build_photometric_measurement(
    *,
    document_id: str,
    display_state: DirectViewDisplayState,
    stimulus_context: DisplayStimulusContext,
    quantity: LuminanceQuantityKind,
    measured_at_utc: str,
    **fields: Any,
) -> DisplayPhotometricMeasurement:
    return _seal(
        DisplayPhotometricMeasurement,
        dict(
            document_id=document_id,
            display_state_id=display_state.display_state_id,
            display_state_sha256=display_state.display_state_sha256,
            stimulus_context_id=stimulus_context.stimulus_context_id,
            stimulus_context_sha256=stimulus_context.stimulus_context_sha256,
            quantity=quantity,
            measured_at_utc=measured_at_utc,
            **fields,
        ),
        'measurement_id',
        'measurement_sha256',
        'dpm:',
    )  # type: ignore[return-value]


def build_temporal_observation(
    *,
    document_id: str,
    display_state: DirectViewDisplayState,
    observed_at_utc: str,
    stimulus_context: DisplayStimulusContext | None = None,
    **fields: Any,
) -> DisplayTemporalObservation:
    return _seal(
        DisplayTemporalObservation,
        dict(
            document_id=document_id,
            display_state_id=display_state.display_state_id,
            display_state_sha256=display_state.display_state_sha256,
            stimulus_context_id=(
                None
                if stimulus_context is None
                else stimulus_context.stimulus_context_id
            ),
            stimulus_context_sha256=(
                None
                if stimulus_context is None
                else stimulus_context.stimulus_context_sha256
            ),
            observed_at_utc=observed_at_utc,
            **fields,
        ),
        'observation_id',
        'observation_sha256',
        'dto:',
    )  # type: ignore[return-value]


def build_spatial_measurement(
    *,
    document_id: str,
    display_state: DirectViewDisplayState,
    stimulus_context: DisplayStimulusContext,
    observable: SpatialObservable,
    points: Sequence[SpatialPoint],
    measured_at_utc: str,
    **fields: Any,
) -> DisplaySpatialMeasurement:
    return _seal(
        DisplaySpatialMeasurement,
        dict(
            document_id=document_id,
            display_state_id=display_state.display_state_id,
            display_state_sha256=display_state.display_state_sha256,
            stimulus_context_id=stimulus_context.stimulus_context_id,
            stimulus_context_sha256=stimulus_context.stimulus_context_sha256,
            observable=observable,
            points=tuple(points),
            measured_at_utc=measured_at_utc,
            **fields,
        ),
        'spatial_id',
        'spatial_sha256',
        'dsm:',
    )  # type: ignore[return-value]


def build_angle_measurement(
    *,
    document_id: str,
    display_state: DirectViewDisplayState,
    stimulus_context: DisplayStimulusContext,
    horizontal_angle_deg: float,
    vertical_angle_deg: float,
    measured_at_utc: str,
    **fields: Any,
) -> DisplayAngleMeasurement:
    return _seal(
        DisplayAngleMeasurement,
        dict(
            document_id=document_id,
            display_state_id=display_state.display_state_id,
            display_state_sha256=display_state.display_state_sha256,
            stimulus_context_id=stimulus_context.stimulus_context_id,
            stimulus_context_sha256=stimulus_context.stimulus_context_sha256,
            horizontal_angle_deg=horizontal_angle_deg,
            vertical_angle_deg=vertical_angle_deg,
            measured_at_utc=measured_at_utc,
            **fields,
        ),
        'angle_id',
        'angle_sha256',
        'dam:',
    )  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _claim_verdict(
    claim: DisplayClaim,
    verdict: ClaimVerdictState,
    reasons: Sequence[DirectViewReason],
    evidence_ids: Sequence[str] = (),
    unverified_seats: Sequence[str] = (),
) -> ClaimVerdict:
    return ClaimVerdict(
        kind=claim.kind,
        label=claim.label,
        scope=claim.scope,
        verdict=verdict,
        reasons=tuple(dict.fromkeys(reasons)),
        evidence_measurement_ids=tuple(dict.fromkeys(evidence_ids)),
        unverified_seat_refs=tuple(dict.fromkeys(unverified_seats)),
    )


def evaluate_direct_view_qualification(
    *,
    document_id: str,
    display_state: DirectViewDisplayState,
    claims: Sequence[DisplayClaim],
    stimulus_contexts: Sequence[DisplayStimulusContext] = (),
    measurements: Sequence[DisplayPhotometricMeasurement] = (),
    temporal_observations: Sequence[DisplayTemporalObservation] = (),
    spatial_measurements: Sequence[DisplaySpatialMeasurement] = (),
    angle_measurements: Sequence[DisplayAngleMeasurement] = (),
    evaluated_at_utc: str,
) -> DirectViewQualification:
    """Fail-closed direct-view qualification (#625 §20, acceptance).

    Rules that can never be bypassed:

    - a transient peak never supports a sustained-full-field claim;
    - a contrast claim evaluated against the wrong contrast kind is
      conflation — insufficient evidence;
    - a value at/below the instrument floor is a bounded claim — never
      infinite contrast;
    - active/unknown local dimming makes one full-screen black
      measurement insufficient for real-content black claims;
    - seat-dependent claims need per-seat off-axis measurements;
    - a static-sweep EOTF claim never proves dynamic tone mapping;
    - observed temporal dimming attaches to every sustained claim;
    - unknown adaptive/eco state limits reference-scope claims;
    - a single-APL evidence set never supports an across-content claim.
    """
    state = display_state
    measurements = tuple(
        m for m in measurements
        if m.display_state_sha256 == state.display_state_sha256
    )
    temporal = tuple(
        t for t in temporal_observations
        if t.display_state_sha256 == state.display_state_sha256
    )
    spatial = tuple(
        s for s in spatial_measurements
        if s.display_state_sha256 == state.display_state_sha256
    )
    angles = tuple(
        a for a in angle_measurements
        if a.display_state_sha256 == state.display_state_sha256
    )
    contexts = {c.stimulus_context_id: c for c in stimulus_contexts}

    def _context(measurement: DisplayPhotometricMeasurement):
        return contexts.get(measurement.stimulus_context_id)

    # Global limitations that attach to every claim family.
    global_reasons: list[DirectViewReason] = []
    if state.unknown_fields:
        global_reasons.append('DISPLAY_STATE_INCOMPLETE')
    dimming_observed = any(
        t.state == 'temporal_dimming_observed' for t in temporal
    )
    protection_unresolved = any(
        t.state in ('provider_documented_protection', 'state_unknown')
        for t in temporal
    )
    local_dimming_active = state.local_dimming in (
        'low', 'medium', 'high', 'auto', 'custom'
    )
    local_dimming_unknown = state.local_dimming == 'unknown'

    verdicts: list[ClaimVerdict] = []
    all_reasons: list[DirectViewReason] = list(global_reasons)

    for claim in claims:
        reasons: list[DirectViewReason] = []
        evidence_ids: list[str] = []
        unverified: list[str] = []
        verdict: ClaimVerdictState

        bound = list(measurements)

        if claim.kind == 'peak_luminance':
            peak = [
                m for m in bound
                if m.quantity in (
                    'transient_peak_luminance',
                    'stabilized_window_luminance',
                )
            ]
            if not peak:
                verdict = 'insufficient_evidence'
                reasons.append('INSUFFICIENT_EVIDENCE')
            else:
                evidence_ids.extend(m.measurement_id for m in peak)
                missing_window = any(
                    (c := _context(m)) is None
                    or (
                        not c.is_full_field
                        and c.window_size_percent is None
                    )
                    for m in peak
                )
                if missing_window:
                    reasons.append('WINDOW_SIZE_UNDECLARED')
                apl_set = {
                    _context(m).apl_percent
                    for m in peak
                    if _context(m) is not None
                    and _context(m).apl_percent is not None
                }
                if len(apl_set) < 2:
                    # One APL point cannot support an across-content
                    # peak claim — only the measured state is supported.
                    reasons.append('APL_COVERAGE_MISSING')
                if state.unknown_fields:
                    reasons.append('DISPLAY_STATE_INCOMPLETE')
                verdict = (
                    'supported_with_limitations'
                    if reasons
                    else 'supported'
                )
                if not reasons:
                    reasons.append('QUALIFIED_AS_MEASURED')

        elif claim.kind == 'sustained_full_field_luminance':
            sustained = [
                m for m in bound
                if m.quantity == 'sustained_full_field_luminance'
                and (
                    (c := _context(m)) is None or c.is_full_field
                )
            ]
            transient_only = bool(bound) and all(
                m.transient for m in bound
            )
            if not sustained:
                verdict = (
                    'unsupported'
                    if transient_only
                    else 'insufficient_evidence'
                )
                if transient_only:
                    reasons.append('PEAK_PROMOTED_TO_SUSTAINED')
                    reasons.append('TRANSIENT_ONLY_EVIDENCE')
                else:
                    reasons.append('INSUFFICIENT_EVIDENCE')
            else:
                evidence_ids.extend(m.measurement_id for m in sustained)
                if dimming_observed:
                    reasons.append('TEMPORAL_DIMMING_OBSERVED')
                if protection_unresolved:
                    reasons.append('TEMPORAL_PROTECTION_UNRESOLVED')
                if any(m.warmup_minutes is None for m in sustained):
                    reasons.append('WARMUP_STATE_UNKNOWN')
                if any(
                    (c := _context(m)) is not None
                    and not c.sequence_recorded
                    for m in sustained
                ):
                    reasons.append('MEASUREMENT_ORDER_UNRECORDED')
                # ABL / power limiting is state-dependent measured
                # behavior: when the display is emissive or zoned and the
                # bound evidence spans a single APL, the limiting
                # behavior is uncharacterized — never dismissed as drift.
                apl_span = {
                    _context(m).apl_percent
                    for m in bound
                    if _context(m) is not None
                    and _context(m).apl_percent is not None
                }
                emissive = state.panel_technology in (
                    'woled', 'qd_oled', 'microled',
                    'direct_view_led', 'other_emissive',
                )
                if (
                    (emissive or local_dimming_active)
                    and len(apl_span) < 2
                ):
                    reasons.append('ABL_BEHAVIOR_UNCHARACTERIZED')
                verdict = (
                    'supported_with_limitations'
                    if reasons
                    else 'supported'
                )
                if not reasons:
                    reasons.append('QUALIFIED_AS_MEASURED')

        elif claim.kind == 'black_level':
            blacks = [
                m for m in bound
                if m.quantity == 'black_luminance'
            ]
            if not blacks:
                verdict = 'insufficient_evidence'
                reasons.append('INSUFFICIENT_EVIDENCE')
            else:
                evidence_ids.extend(m.measurement_id for m in blacks)
                floor_limited = any(
                    m.below_instrument_floor
                    or (
                        m.instrument_floor_cd_m2 is not None
                        and m.value_cd_m2 is not None
                        and m.value_cd_m2 <= m.instrument_floor_cd_m2
                    )
                    for m in blacks
                )
                if floor_limited:
                    reasons.append('INSTRUMENT_FLOOR_LIMITED')
                if local_dimming_active:
                    reasons.append('LOCAL_DIMMING_ACTIVE_UNMEASURED')
                if local_dimming_unknown:
                    reasons.append('LOCAL_DIMMING_STATE_UNKNOWN')
                verdict = (
                    'supported_with_limitations'
                    if reasons
                    else 'supported'
                )
                if not reasons:
                    reasons.append('QUALIFIED_AS_MEASURED')

        elif claim.kind == 'contrast':
            same_kind = [
                m for m in bound
                if m.contrast_kind == claim.contrast_kind
            ]
            other_kind = [
                m for m in bound
                if m.contrast_kind is not None
                and m.contrast_kind != claim.contrast_kind
            ]
            if other_kind and not same_kind:
                verdict = 'insufficient_evidence'
                reasons.append('CONTRAST_KIND_CONFLATED')
            elif not same_kind:
                verdict = 'insufficient_evidence'
                reasons.append('INSUFFICIENT_EVIDENCE')
            else:
                evidence_ids.extend(m.measurement_id for m in same_kind)
                floor_limited = any(
                    m.below_instrument_floor
                    or (
                        m.instrument_floor_cd_m2 is not None
                        and m.value_cd_m2 is not None
                        and m.value_cd_m2 <= m.instrument_floor_cd_m2
                    )
                    for m in same_kind
                )
                if floor_limited:
                    reasons.append('INSTRUMENT_FLOOR_LIMITED')
                if claim.contrast_kind in (
                    'full_field_contrast',
                    'content_conditioned_contrast',
                ) and local_dimming_active:
                    reasons.append('LOCAL_DIMMING_ACTIVE_UNMEASURED')
                verdict = (
                    'supported_with_limitations'
                    if reasons
                    else 'supported'
                )
                if not reasons:
                    reasons.append('QUALIFIED_AS_MEASURED')

        elif claim.kind == 'eotf_tracking':
            tracking = [
                m for m in bound
                if m.quantity == 'eotf_tracking_point'
            ]
            if not tracking:
                verdict = 'insufficient_evidence'
                reasons.append('INSUFFICIENT_EVIDENCE')
            else:
                evidence_ids.extend(m.measurement_id for m in tracking)
                # A static sweep can never prove dynamic tone mapping.
                static_only = all(
                    (c := _context(m)) is not None
                    and c.content_kind == 'static'
                    for m in tracking
                )
                dynamic_seen = any(
                    (c := _context(m)) is not None
                    and c.content_kind in ('dynamic', 'mixed')
                    for m in tracking
                )
                if static_only and not dynamic_seen:
                    reasons.append('CONTENT_STATE_MISMATCH')
                verdict = (
                    'supported_with_limitations'
                    if reasons
                    else 'supported'
                )
                if not reasons:
                    reasons.append('QUALIFIED_AS_MEASURED')

        elif claim.kind == 'uniformity':
            matching = [
                s for s in spatial
                if s.observable in ('luminance', 'white_chromaticity')
            ]
            if not matching:
                verdict = 'insufficient_evidence'
                reasons.append('UNIFORMITY_UNMEASURED')
            else:
                evidence_ids.extend(s.spatial_id for s in matching)
                coverages = {s.coverage for s in matching}
                if coverages == {'single_point'}:
                    verdict = 'insufficient_evidence'
                    reasons.append('UNIFORMITY_PARTIAL')
                elif 'grid' not in coverages:
                    verdict = 'supported_with_limitations'
                    reasons.append('UNIFORMITY_PARTIAL')
                else:
                    verdict = 'supported'
                    reasons.append('QUALIFIED_AS_MEASURED')

        elif claim.kind in (
            'off_axis_performance', 'multi_seat_consistency'
        ):
            measured_seats = {
                a.seat_ref for a in angles if a.seat_ref is not None
            }
            unverified = [
                s for s in claim.seat_refs if s not in measured_seats
            ]
            evidence_ids.extend(
                a.angle_id
                for a in angles
                if a.seat_ref in set(claim.seat_refs)
            )
            if unverified or not evidence_ids:
                verdict = 'insufficient_evidence'
                reasons.append('SEAT_COVERAGE_INCOMPLETE')
                reasons.append('VIEWING_ANGLE_UNVERIFIED')
            else:
                verdict = 'supported'
                reasons.append('QUALIFIED_AS_MEASURED')

        elif claim.kind == 'color_performance':
            chroma = [
                m for m in bound
                if m.quantity == 'chromaticity'
            ]
            if not chroma:
                verdict = 'insufficient_evidence'
                reasons.append('INSUFFICIENT_EVIDENCE')
            else:
                evidence_ids.extend(m.measurement_id for m in chroma)
                if state.unknown_fields:
                    reasons.append('DISPLAY_STATE_INCOMPLETE')
                verdict = (
                    'supported_with_limitations'
                    if reasons
                    else 'supported'
                )
                if not reasons:
                    reasons.append('QUALIFIED_AS_MEASURED')

        else:  # content_independent_performance
            # A direct-view display never has one content-independent
            # performance state — the claim itself is unsupported.
            verdict = 'unsupported'
            reasons.append('APL_COVERAGE_MISSING')
            reasons.append('CONTENT_STATE_MISMATCH')

        # Cross-cutting limitations.
        if claim.requires_reference_environment and (
            state.adaptive_behavior_risk
        ):
            if 'ADAPTIVE_BEHAVIOR_UNDECLARED' not in reasons:
                reasons.append('ADAPTIVE_BEHAVIOR_UNDECLARED')
            if verdict == 'supported':
                verdict = 'supported_with_limitations'

        all_reasons.extend(reasons)
        verdicts.append(
            _claim_verdict(
                claim, verdict, reasons, evidence_ids, unverified
            )
        )

    # Roll the per-claim verdicts up into one honest state.
    if not claims:
        state_out: QualificationState = 'insufficient_evidence'
        all_reasons.append('INSUFFICIENT_EVIDENCE')
    elif any(v.verdict == 'conflicting_evidence' for v in verdicts):
        state_out = 'conflicting_evidence'
    elif any(v.verdict == 'unsupported' for v in verdicts):
        state_out = 'unsupported'
    elif any(v.verdict == 'insufficient_evidence' for v in verdicts):
        state_out = 'insufficient_evidence'
    elif any(v.verdict == 'supported_with_limitations' for v in verdicts):
        state_out = 'qualified_with_limitations'
        if 'QUALIFIED_WITH_LIMITATIONS' not in all_reasons:
            all_reasons.append('QUALIFIED_WITH_LIMITATIONS')
    else:
        state_out = 'qualified'
        if 'QUALIFIED_AS_MEASURED' not in all_reasons:
            all_reasons.append('QUALIFIED_AS_MEASURED')

    if dimming_observed and 'TEMPORAL_DIMMING_OBSERVED' not in all_reasons:
        all_reasons.append('TEMPORAL_DIMMING_OBSERVED')

    return _seal(
        DirectViewQualification,
        dict(
            document_id=document_id,
            display_state_id=state.display_state_id,
            display_state_sha256=state.display_state_sha256,
            state=state_out,
            claim_verdicts=tuple(verdicts),
            measurement_ids=tuple(m.measurement_id for m in measurements),
            temporal_observation_ids=tuple(
                t.observation_id for t in temporal
            ),
            spatial_ids=tuple(s.spatial_id for s in spatial),
            angle_ids=tuple(a.angle_id for a in angles),
            reasons=tuple(dict.fromkeys(all_reasons)),
            evaluated_at_utc=evaluated_at_utc,
        ),
        'qualification_id',
        'qualification_sha256',
        'dvq:',
    )  # type: ignore[return-value]


__all__ = [
    'DIRECT_VIEW_AUTHORITY_VERSION',
    'CLAIM_KIND_LABELS',
    'CLAIM_VERDICT_LABELS',
    'CONTRAST_KIND_LABELS',
    'CONTENT_MODE_LABELS',
    'ClaimVerdict',
    'DisplayAngleMeasurement',
    'DisplayClaim',
    'DisplayPhotometricMeasurement',
    'DisplaySpatialMeasurement',
    'DisplayStimulusContext',
    'DisplayTemporalObservation',
    'DirectViewDisplayState',
    'DirectViewQualification',
    'LOCAL_DIMMING_LABELS',
    'PANEL_TECHNOLOGY_LABELS',
    'QUALIFICATION_STATE_LABELS',
    'QUANTITY_KIND_LABELS',
    'REASON_LABELS',
    'SPATIAL_OBSERVABLE_LABELS',
    'SpatialPoint',
    'TEMPORAL_DIMMING_LABELS',
    'TRI_STATE_LABELS',
    'build_angle_measurement',
    'build_display_state',
    'build_photometric_measurement',
    'build_spatial_measurement',
    'build_stimulus_context',
    'build_temporal_observation',
    'evaluate_direct_view_qualification',
]
