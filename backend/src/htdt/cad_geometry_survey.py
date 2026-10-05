"""As-built geometry survey authority (#613).

Home-theater design geometry (walls, openings, cavities, fixtures) is only
as trustworthy as the capture evidence behind it. A CAD document that
declares millimetre precision while its as-built state was captured by an
AR semantic scanner is overclaiming — this module is the fail-closed gate
that keeps declared precision bounded by demonstrated evidence.

The authority records the full survey provenance story:

- :class:`GeometrySurveyInstrument` — measurement-means identity: the
  instrument/device kind, make/model/serial, firmware, and for mobile
  scanning the capture application + version (for a phone LiDAR scan the
  *app* is part of the metrology chain — different apps produce
  measurably different accuracy from identical hardware). The record also
  pins the instrument capability class (traceable survey instrument /
  manufacturer diagnostic / consumer AR) and any calibration/check
  evidence references — instrument *fitness* lifecycle itself is owned by
  the #611 metrology authority; this record binds to it by reference.
- :class:`SurveyCampaign` — one capture session: instrument refs,
  operator, timestamp, coordinate frame declaration (units, scale,
  handedness), declared control targets, coverage, known limitations,
  raw artifact refs (hash-pinned), plus a structured
  :class:`MobileCaptureConditions` block for consumer/mobile captures —
  range, tracking quality, loop closure, surface reflectance, incidence
  angles, lighting, clutter, extent and relocalization events. Those
  conditions *bound* what the capture can claim; no universal tolerance
  is hard-coded.
- :class:`GeometricElementEvidence` — per-element provenance: which
  evidence classes produced this element (design drawing, manual tape,
  laser distance meter, total station, TLS, mobile LiDAR, photogrammetry,
  AR semantic capture, structured light, user-entered, derived/fitted,
  hybrid-reconciled), the derivation stage it reached (raw → registered
  → segmented → fitted → semantic → solid → solver), observation state
  (observed / partially observed / inferred / design-only / hidden), the
  campaigns it came from, per-kind uncertainty contributions (never a
  single RMS number — repeatability, bias, registration, scale, local
  fit, drift, edge localization, control residual, modelling), and a
  *separate* semantic-confidence field so a confident label never masks
  a bad vertex.
- :class:`GeometryControlMeasurement` — independent checks
  (wall-to-wall, diagonals, ceiling height, known targets, surveyed
  points, scale bars, repeat scans, manual dimensions). A control used
  for registration cannot also validate it — registration-leakage is
  flagged.
- :class:`AsBuiltReconciliation` — design-vs-as-built deltas preserved as
  evidence: per-element change record with the reconciling evidence
  refs, whether the change was approved, and which downstream claims it
  invalidates.
- :class:`GeometryTaskRequirement` — a declared downstream task
  (visualization through high-precision validation) with an optional
  tolerance requirement.
- :class:`AsBuiltGeometryQualification` — the sealed verdict:
  per-element states and per-task verdicts, evaluated fail-closed.

Contract properties:

- declared precision can never exceed evidence: an element whose only
  provenance is design documents is ``design_only``; observed evidence
  without independent controls is ``observed_unqualified``;
- a control measurement that was consumed by the registration process
  cannot validate that registration (no data leakage);
- controls failing their declared tolerance demote to
  ``control_check_failed`` — they never silently pass;
- multi-campaign geometry without a declared common registration is
  ``registration_limited``;
- evidence staleness after a declared scene change is
  ``stale_after_change`` — stale evidence never quietly serves a new
  revision;
- task fitness requires the *worst* required element to meet the task's
  evidence tier — capability never propagates from the best element.

Literature basis (issue §research): ISO 17123-9:2018 field-verification
procedures for terrestrial laser scanners (stage 90.92 "to be revised";
ISO/CD 17123-9 Ed.2 in committee draft — tracked in the #599 standards
registry) and ISO 17123-5:2018 for total stations pin control-measurement
practice; mobile-LiDAR studies (Spreafico et al. 2021, ISPRS XLIII-B4-2022
and XLVIII-2-W8-2024 evaluations of iPad Pro LiDAR apps) show local plane
fitting around 0.5 cm RMSE but trajectory/app-dependent drift to ~10 cm —
motivating per-campaign condition capture instead of universal tolerances.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical_json import canonical_sha256 as _digest, canonicalize_payload as _canon


_SHA256 = r'^[0-9a-f]{64}$'

GEOMETRY_SURVEY_AUTHORITY_VERSION = 'geometry-survey-authority-1'


# ---------------------------------------------------------------------------
# Taxonomies (issue §1–§4, §10, §17)
# ---------------------------------------------------------------------------

SurveyInstrumentKind = Literal[
    'laser_distance_meter',
    'terrestrial_laser_scanner',
    'total_station',
    'mobile_lidar_device',
    'depth_camera',
    'photogrammetry_camera',
    'semantic_capture_device',
    'manual_tools',
    'software_only',
    'unknown',
]

InstrumentCapabilityClass = Literal[
    'traceable_survey_instrument',
    'manufacturer_diagnostic',
    'consumer_ar_capability',
    'software_only',
    'unknown',
]

GeometryEvidenceClass = Literal[
    'design_bim',
    'design_drawing',
    'manual_tape_measure',
    'laser_distance_meter',
    'total_station_survey',
    'terrestrial_laser_scan',
    'mobile_lidar',
    'photogrammetry',
    'ar_semantic_capture',
    'structured_light',
    'user_entered',
    'derived_fitted',
    'hybrid_reconciled',
    'unknown',
]

GeometryDerivationStage = Literal[
    'raw_observation',
    'registered_point_cloud',
    'segmented_surface',
    'fitted_primitive',
    'semantic_element',
    'solid_room_model',
    'acoustic_solver_geometry',
    'unknown',
]

ObservationState = Literal[
    'observed_surface',
    'partially_observed',
    'inferred_continuation',
    'design_source_only',
    'hidden_unknown',
]

GeometryUncertaintyKind = Literal[
    'repeatability',
    'bias_accuracy',
    'registration_error',
    'scale_error',
    'local_fit_error',
    'global_drift',
    'edge_localization',
    'control_point_residual',
    'modelling_simplification',
    'unknown',
]

GeometryTaskClass = Literal[
    'visualization_only',
    'room_dimension_estimate',
    'speaker_seat_placement',
    'sbir_early_reflection',
    'low_frequency_wave_model',
    'screen_projector_clearance',
    'prediction_measurement_registration',
    'as_built_commissioning',
    'high_precision_validation',
]

ControlCheckKind = Literal[
    'wall_to_wall_distance',
    'diagonal',
    'ceiling_height',
    'known_target',
    'surveyed_point',
    'scale_bar',
    'repeat_scan',
    'manual_dimension',
]

GeometryElementState = Literal[
    'design_only',
    'observed_unqualified',
    'field_checked',
    'fit_for_declared_task',
    'fit_with_limitations',
    'control_check_failed',
    'registration_limited',
    'insufficient_evidence',
    'stale_after_change',
]

GeometryTaskVerdict = Literal[
    'fit_for_declared_task',
    'fit_with_limitations',
    'insufficient_evidence',
    'control_check_failed',
    'registration_limited',
    'stale_after_change',
]

GeometryQualificationReason = Literal[
    'ELEMENTS_DESIGN_ONLY',
    'ELEMENTS_OBSERVED_NO_CONTROLS',
    'ELEMENTS_FIELD_CHECKED',
    'CONTROL_CHECK_FAILED',
    'CONTROL_LEAKAGE_REGISTRATION',
    'MULTI_CAMPAIGN_UNREGISTERED',
    'REGISTRATION_UNCERTAINTY_UNDECLARED',
    'STALE_AFTER_CHANGE',
    'HIDDEN_ELEMENTS_BLOCK_CLAIM',
    'TASK_EXCEEDS_EVIDENCE_TIER',
    'TASK_TOLERANCE_UNMET',
    'RECONCILIATION_UNRESOLVED',
    'FIT_WITH_LIMITATIONS',
    'FIT_FOR_DECLARED_TASK',
    'INSUFFICIENT_EVIDENCE',
]


# ---------------------------------------------------------------------------
# Japanese product labels
# ---------------------------------------------------------------------------

EVIDENCE_CLASS_LABELS: dict[str, str] = {
    'design_bim': '設計BIM',
    'design_drawing': '設計図面',
    'manual_tape_measure': '手測り（巻尺）',
    'laser_distance_meter': 'レーザー距離計',
    'total_station_survey': 'トータルステーション測量',
    'terrestrial_laser_scan': '地上型レーザースキャン（TLS）',
    'mobile_lidar': 'モバイルLiDAR',
    'photogrammetry': '写真測量',
    'ar_semantic_capture': 'ARセマンティックキャプチャ',
    'structured_light': 'ストラクチャードライト/深度カメラ',
    'user_entered': 'ユーザー手入力',
    'derived_fitted': '導出/フィッティング済み',
    'hybrid_reconciled': 'ハイブリッド整合済み',
    'unknown': '不明',
}

INSTRUMENT_KIND_LABELS: dict[str, str] = {
    'laser_distance_meter': 'レーザー距離計',
    'terrestrial_laser_scanner': '地上型レーザースキャナ',
    'total_station': 'トータルステーション',
    'mobile_lidar_device': 'モバイルLiDARデバイス',
    'depth_camera': '深度カメラ',
    'photogrammetry_camera': '写真測量カメラ',
    'semantic_capture_device': 'セマンティックキャプチャデバイス',
    'manual_tools': '手測り工具',
    'software_only': 'ソフトウェアのみ',
    'unknown': '不明',
}

CAPABILITY_CLASS_LABELS: dict[str, str] = {
    'traceable_survey_instrument': 'トレーサブル測量機器',
    'manufacturer_diagnostic': 'メーカー診断級',
    'consumer_ar_capability': 'コンシューマAR級',
    'software_only': 'ソフトウェアのみ',
    'unknown': '不明',
}

DERIVATION_STAGE_LABELS: dict[str, str] = {
    'raw_observation': '生観測',
    'registered_point_cloud': '位置合わせ済み点群',
    'segmented_surface': '分割済み面',
    'fitted_primitive': 'フィット済みプリミティブ',
    'semantic_element': 'セマンティック要素',
    'solid_room_model': '立体部屋モデル',
    'acoustic_solver_geometry': '音響ソルバー幾何',
    'unknown': '不明',
}

OBSERVATION_STATE_LABELS: dict[str, str] = {
    'observed_surface': '観測済み面',
    'partially_observed': '部分的観測',
    'inferred_continuation': '推定延長部',
    'design_source_only': '設計由来のみ',
    'hidden_unknown': '隠蔽・不明',
}

UNCERTAINTY_KIND_LABELS: dict[str, str] = {
    'repeatability': '繰り返し性',
    'bias_accuracy': 'バイアス/器差',
    'registration_error': '位置合わせ誤差',
    'scale_error': 'スケール誤差',
    'local_fit_error': '局所フィット誤差',
    'global_drift': 'グローバルドリフト',
    'edge_localization': 'エッジ位置決定誤差',
    'control_point_residual': 'コントロールポイント残差',
    'modelling_simplification': 'モデル化単純化',
    'unknown': '不明',
}

TASK_CLASS_LABELS: dict[str, str] = {
    'visualization_only': '可視化専用',
    'room_dimension_estimate': '部屋寸法推定',
    'speaker_seat_placement': 'スピーカー/座席配置',
    'sbir_early_reflection': 'SBIR/初期反射',
    'low_frequency_wave_model': '低域波動モデル',
    'screen_projector_clearance': 'スクリーン/プロジェクター干渉',
    'prediction_measurement_registration': '予測-実測位置合わせ',
    'as_built_commissioning': '竣工コミッショニング',
    'high_precision_validation': '高精度検証',
}

ELEMENT_STATE_LABELS: dict[str, str] = {
    'design_only': '設計由来のみ',
    'observed_unqualified': '観測済み・未検証',
    'field_checked': '現地検証済み',
    'fit_for_declared_task': '宣言タスク適合',
    'fit_with_limitations': '制限付き適合',
    'control_check_failed': 'コントロール検査不合格',
    'registration_limited': '位置合わせ限定的',
    'insufficient_evidence': '証拠不足',
    'stale_after_change': '変更後の陳腐化',
}

TASK_VERDICT_LABELS: dict[str, str] = {
    'fit_for_declared_task': '宣言タスク適合',
    'fit_with_limitations': '制限付き適合',
    'insufficient_evidence': '証拠不足',
    'control_check_failed': 'コントロール検査不合格',
    'registration_limited': '位置合わせ限定的',
    'stale_after_change': '変更後の陳腐化',
}

REASON_LABELS: dict[str, str] = {
    'ELEMENTS_DESIGN_ONLY': '設計由来の要素のみ — 実測証拠なし',
    'ELEMENTS_OBSERVED_NO_CONTROLS': '観測済みだが独立コントロール未検証',
    'ELEMENTS_FIELD_CHECKED': '独立コントロールで現地検証済み',
    'CONTROL_CHECK_FAILED': 'コントロール測定が宣言公差を超過',
    'CONTROL_LEAKAGE_REGISTRATION':
        '位置合わせに使用したコントロールは検証に流用不可（データ漏洩）',
    'MULTI_CAMPAIGN_UNREGISTERED': '複数キャンペーンが共通フレーム未登録',
    'REGISTRATION_UNCERTAINTY_UNDECLARED': '位置合わせ不確かさ未宣言',
    'STALE_AFTER_CHANGE': '部屋状態の変更後に証拠が陳腐化',
    'HIDDEN_ELEMENTS_BLOCK_CLAIM': '隠蔽要素が適合主張を阻害',
    'TASK_EXCEEDS_EVIDENCE_TIER': '要求タスクが証拠の能力階層を超過',
    'TASK_TOLERANCE_UNMET': '宣言公差が達成不確かさ未満',
    'RECONCILIATION_UNRESOLVED': '設計-実測の差異が未承認',
    'FIT_WITH_LIMITATIONS': '制限付きで適合',
    'FIT_FOR_DECLARED_TASK': '宣言タスクに適合',
    'INSUFFICIENT_EVIDENCE': '証拠不足',
}


# ---------------------------------------------------------------------------
# Evidence-tier model — capability never exceeds demonstrated evidence.
# ---------------------------------------------------------------------------

# Ranked capability tiers for task fitness. An evidence class supports tasks
# at or below its tier *provided* field controls exist where required.
_DESIGN_CLASSES = frozenset({'design_bim', 'design_drawing', 'user_entered'})
_DERIVED_CLASSES = frozenset({'derived_fitted', 'hybrid_reconciled'})
_FIELD_CLASSES = frozenset({'manual_tape_measure', 'laser_distance_meter'})
_SCAN_CLASSES = frozenset(
    {'mobile_lidar', 'photogrammetry', 'ar_semantic_capture', 'structured_light'}
)
_SURVEY_CLASSES = frozenset({'total_station_survey', 'terrestrial_laser_scan'})

# Task tier required for each task class (higher = stricter).
_TASK_TIER: dict[str, int] = {
    'visualization_only': 0,
    'room_dimension_estimate': 1,
    'speaker_seat_placement': 2,
    'sbir_early_reflection': 2,
    'screen_projector_clearance': 2,
    'low_frequency_wave_model': 3,
    'prediction_measurement_registration': 3,
    'as_built_commissioning': 4,
    'high_precision_validation': 4,
}

# Evidence tier delivered by each evidence class. Derived/hybrid evidence
# inherits the tier of its *inputs* — which cannot be inspected here, so it
# delivers at most the field tier and only with explicit controls.
_EVIDENCE_TIER: dict[str, int] = {
    'design_bim': 0,
    'design_drawing': 0,
    'user_entered': 0,
    'unknown': 0,
    'derived_fitted': 1,
    'hybrid_reconciled': 1,
    'manual_tape_measure': 2,
    'laser_distance_meter': 2,
    'mobile_lidar': 2,
    'photogrammetry': 2,
    'ar_semantic_capture': 2,
    'structured_light': 2,
    'total_station_survey': 3,
    'terrestrial_laser_scan': 3,
}

#: Tiers that always require at least one independent passing control —
#: the evidence may *reach* the tier but cannot *self-certify* it.
_CONTROL_REQUIRED_TIER = 2


def _element_evidence_tier(element: 'GeometricElementEvidence') -> int:
    """Best evidence tier the element's provenance supports."""
    tiers = [
        _EVIDENCE_TIER.get(cls, 0) for cls in element.evidence_classes
    ]
    return max(tiers, default=0)


# ---------------------------------------------------------------------------
# Sub-records
# ---------------------------------------------------------------------------


class GeometryUncertaintyContribution(BaseModel):
    """One declared uncertainty component — never collapsed into RMS."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: GeometryUncertaintyKind
    value_mm: float | None = Field(default=None, ge=0.0)
    note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def honest(self) -> 'GeometryUncertaintyContribution':
        if self.value_mm is not None and not isfinite(float(self.value_mm)):
            raise ValueError('uncertainty values must be finite')
        if self.value_mm is None and not self.note:
            raise ValueError(
                'an uncertainty contribution needs a value or an explicit note'
            )
        return self


class GeometryFrameDeclaration(BaseModel):
    """Coordinate-frame identity of a capture: units, scale, handedness."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    frame_name: str = Field(min_length=1)
    length_unit: Literal['mm', 'cm', 'm'] = 'mm'
    scale_declared: float = Field(default=1.0, gt=0.0)
    right_handed: bool = True

    @model_validator(mode='after')
    def finite(self) -> 'GeometryFrameDeclaration':
        if not isfinite(float(self.scale_declared)):
            raise ValueError('frame scale must be finite')
        return self


class RegistrationTransform(BaseModel):
    """Declared registration of a campaign into the document frame.

    ``uncertainty_mm`` is honest: ``None`` means the registration error
    was never characterized — downstream gates treat that as a
    limitation, not as zero.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    method: str = Field(min_length=1)
    uncertainty_mm: float | None = Field(default=None, ge=0.0)
    control_measurement_ids: tuple[str, ...] = ()

    @model_validator(mode='after')
    def finite(self) -> 'RegistrationTransform':
        if self.uncertainty_mm is not None and not isfinite(
            float(self.uncertainty_mm)
        ):
            raise ValueError('registration uncertainty must be finite')
        return self


class MobileCaptureConditions(BaseModel):
    """Condition block for consumer/mobile capture (#613 §6).

    Every field stays honest — ``None``/``'unknown'`` means the condition
    was not characterized, which gates the achievable claim downstream.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    capture_range_m: float | None = Field(default=None, gt=0.0)
    tracking_quality: Literal['good', 'degraded', 'lost', 'unknown'] = 'unknown'
    loop_closure_achieved: bool | None = None
    surface_reflectance: Literal['matte', 'mixed', 'specular', 'unknown'] = (
        'unknown'
    )
    incidence_quality: Literal['near_normal', 'oblique', 'mixed', 'unknown'] = (
        'unknown'
    )
    lighting: Literal['bright', 'normal', 'dim', 'dark', 'unknown'] = 'unknown'
    clutter: Literal['empty', 'furnished', 'dense', 'unknown'] = 'unknown'
    extent_m2: float | None = Field(default=None, gt=0.0)
    relocalization_events: int | None = Field(default=None, ge=0)
    app_processing_mode: str | None = Field(default=None, min_length=1)
    export_format: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def finite(self) -> 'MobileCaptureConditions':
        for value in (self.capture_range_m, self.extent_m2):
            if value is not None and not isfinite(float(value)):
                raise ValueError('capture conditions must be finite')
        return self


class GeometryTaskVerdictEntry(BaseModel):
    """Per-task verdict inside an :class:`AsBuiltGeometryQualification`."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    task_id: str = Field(min_length=1)
    task_class: GeometryTaskClass
    verdict: GeometryTaskVerdict
    blocking_element_ids: tuple[str, ...] = ()
    reasons: tuple[GeometryQualificationReason, ...] = ()

    @model_validator(mode='after')
    def consistent(self) -> 'GeometryTaskVerdictEntry':
        if len(self.blocking_element_ids) != len(set(self.blocking_element_ids)):
            raise ValueError('blocking element ids must be unique')
        if len(self.reasons) != len(set(self.reasons)):
            raise ValueError('verdict reasons must be unique')
        if not self.reasons:
            raise ValueError('a task verdict requires at least one reason')
        return self


class GeometricElementStateEntry(BaseModel):
    """Per-element evaluated state."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    element_id: str = Field(min_length=1)
    element_sha256: str = Field(pattern=_SHA256)
    state: GeometryElementState
    evidence_tier: int = Field(ge=0, le=4)
    achieved_uncertainty_mm: float | None = Field(default=None, ge=0.0)
    reasons: tuple[GeometryQualificationReason, ...] = ()

    @model_validator(mode='after')
    def consistent(self) -> 'GeometricElementStateEntry':
        if self.achieved_uncertainty_mm is not None and not isfinite(
            float(self.achieved_uncertainty_mm)
        ):
            raise ValueError('achieved uncertainty must be finite')
        if len(self.reasons) != len(set(self.reasons)):
            raise ValueError('state reasons must be unique')
        return self


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


class GeometrySurveyInstrument(BaseModel):
    """Measurement-means identity (#613 §3).

    For mobile captures the capture application + version is part of the
    metrology identity — two apps on identical hardware deliver measurably
    different accuracy. ``calibration_evidence_refs`` binds to the #611
    instrument-fitness authority (loose references by record id).
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['geometry-survey-authority-1'] = (
        GEOMETRY_SURVEY_AUTHORITY_VERSION
    )
    instrument_id: str = Field(min_length=1)
    kind: SurveyInstrumentKind
    capability_class: InstrumentCapabilityClass = 'unknown'
    manufacturer: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    serial: str | None = Field(default=None, min_length=1)
    firmware_version: str | None = Field(default=None, min_length=1)
    capture_app: str | None = Field(default=None, min_length=1)
    capture_app_version: str | None = Field(default=None, min_length=1)
    nominal_accuracy_mm: float | None = Field(default=None, gt=0.0)
    calibration_evidence_refs: tuple[str, ...] = ()
    notes: str | None = Field(default=None, min_length=1)
    instrument_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_instrument(self) -> 'GeometrySurveyInstrument':
        if self.nominal_accuracy_mm is not None and not isfinite(
            float(self.nominal_accuracy_mm)
        ):
            raise ValueError('nominal accuracy must be finite')
        mobile_kinds = (
            'mobile_lidar_device',
            'depth_camera',
            'semantic_capture_device',
            'photogrammetry_camera',
        )
        if self.kind in mobile_kinds and (
            (self.capture_app is None) != (self.capture_app_version is None)
        ):
            raise ValueError(
                'capture app name and version must be declared together'
            )
        if len(self.calibration_evidence_refs) != len(
            set(self.calibration_evidence_refs)
        ):
            raise ValueError('calibration refs must be unique')
        if self.instrument_sha256 != _digest(self.identity_payload()):
            raise ValueError('survey instrument hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('instrument_id', None)
        payload.pop('instrument_sha256', None)
        return payload


class SurveyCampaign(BaseModel):
    """One capture session (#613 §4).

    Raw artifact references are hash-pinned so the sealed campaign always
    resolves to the same bytes; mobile captures carry a structured
    condition block.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['geometry-survey-authority-1'] = (
        GEOMETRY_SURVEY_AUTHORITY_VERSION
    )
    campaign_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    captured_at_utc: str = Field(min_length=1)
    operator: str | None = Field(default=None, min_length=1)
    instrument_ids: tuple[str, ...] = ()
    method_profile: str | None = Field(default=None, min_length=1)
    frame: GeometryFrameDeclaration | None = None
    registration: RegistrationTransform | None = None
    control_target_ids: tuple[str, ...] = ()
    coverage_note: str | None = Field(default=None, min_length=1)
    known_limitations: tuple[str, ...] = ()
    raw_artifact_sha256: tuple[str, ...] = ()
    mobile_conditions: MobileCaptureConditions | None = None
    campaign_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_campaign(self) -> 'SurveyCampaign':
        if len(self.instrument_ids) != len(set(self.instrument_ids)):
            raise ValueError('instrument ids must be unique')
        if len(self.control_target_ids) != len(set(self.control_target_ids)):
            raise ValueError('control target ids must be unique')
        if len(self.raw_artifact_sha256) != len(set(self.raw_artifact_sha256)):
            raise ValueError('raw artifact hashes must be unique')
        for sha in self.raw_artifact_sha256:
            if len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
                raise ValueError('raw artifact refs must be sha256 hex')
        if self.campaign_sha256 != _digest(self.identity_payload()):
            raise ValueError('survey campaign hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('campaign_id', None)
        payload.pop('campaign_sha256', None)
        return payload


class GeometricElementEvidence(BaseModel):
    """Per-element as-built provenance (#613 §5).

    The element key binds to the design document's geometry (wall id,
    opening id, …); evidence classes record *what actually produced* this
    element's as-built position. Semantic confidence is declared
    separately from geometric uncertainty so a confident label never
    masks a weak vertex.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['geometry-survey-authority-1'] = (
        GEOMETRY_SURVEY_AUTHORITY_VERSION
    )
    element_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    element_key: str = Field(min_length=1)
    label: str | None = Field(default=None, min_length=1)
    evidence_classes: tuple[GeometryEvidenceClass, ...] = ()
    derivation_stage: GeometryDerivationStage = 'unknown'
    observation_state: ObservationState = 'design_source_only'
    campaign_ids: tuple[str, ...] = ()
    uncertainty: tuple[GeometryUncertaintyContribution, ...] = ()
    semantic_confidence: Literal['high', 'medium', 'low', 'unknown'] = 'unknown'
    provenance_refs: tuple[str, ...] = ()
    stale_after_change: bool = False
    notes: str | None = Field(default=None, min_length=1)
    element_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_element(self) -> 'GeometricElementEvidence':
        if not self.evidence_classes:
            raise ValueError('element evidence requires at least one class')
        if len(set(self.evidence_classes)) != len(self.evidence_classes):
            raise ValueError('evidence classes must be unique')
        if len(set(self.campaign_ids)) != len(self.campaign_ids):
            raise ValueError('campaign ids must be unique')
        if len(set(self.provenance_refs)) != len(self.provenance_refs):
            raise ValueError('provenance refs must be unique')
        kinds = [item.kind for item in self.uncertainty]
        if len(kinds) != len(set(kinds)):
            raise ValueError('uncertainty kinds must be unique')
        if self.observation_state == 'design_source_only' and any(
            cls not in _DESIGN_CLASSES for cls in self.evidence_classes
        ):
            raise ValueError(
                'design_source_only elements cannot claim observed evidence'
            )
        if self.element_sha256 != _digest(self.identity_payload()):
            raise ValueError('element evidence hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('element_id', None)
        payload.pop('element_sha256', None)
        return payload


class GeometryControlMeasurement(BaseModel):
    """Independent verification check (#613 §8).

    ``used_for_registration`` marks controls consumed by the registration
    fit — they can never serve as that registration's validation
    (data-leakage guard).
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['geometry-survey-authority-1'] = (
        GEOMETRY_SURVEY_AUTHORITY_VERSION
    )
    control_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    kind: ControlCheckKind
    campaign_id: str | None = Field(default=None, min_length=1)
    element_keys: tuple[str, ...] = ()
    measured_value_mm: float = Field(gt=0.0)
    declared_value_mm: float | None = Field(default=None, gt=0.0)
    tolerance_mm: float | None = Field(default=None, gt=0.0)
    instrument_id: str | None = Field(default=None, min_length=1)
    measured_at_utc: str | None = Field(default=None, min_length=1)
    used_for_registration: bool = False
    notes: str | None = Field(default=None, min_length=1)
    control_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_control(self) -> 'GeometryControlMeasurement':
        for value in (
            self.measured_value_mm,
            self.declared_value_mm,
            self.tolerance_mm,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('control values must be finite')
        if len(set(self.element_keys)) != len(self.element_keys):
            raise ValueError('element keys must be unique')
        if self.control_sha256 != _digest(self.identity_payload()):
            raise ValueError('control measurement hash mismatch')
        return self

    @property
    def residual_mm(self) -> float | None:
        if self.declared_value_mm is None:
            return None
        return abs(self.measured_value_mm - self.declared_value_mm)

    @property
    def passed(self) -> bool | None:
        """None = no declared tolerance — the check is evidence only."""
        residual = self.residual_mm
        if residual is None or self.tolerance_mm is None:
            return None
        return residual <= self.tolerance_mm

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('control_id', None)
        payload.pop('control_sha256', None)
        return payload


class AsBuiltReconciliation(BaseModel):
    """Design-vs-as-built delta record (#613 §11).

    Preserves the delta as evidence rather than overwriting either side;
    ``downstream_invalidated_refs`` names claims the change invalidates.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['geometry-survey-authority-1'] = (
        GEOMETRY_SURVEY_AUTHORITY_VERSION
    )
    reconciliation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    element_key: str = Field(min_length=1)
    design_value_mm: float | None = Field(default=None)
    as_built_value_mm: float | None = Field(default=None)
    delta_mm: float | None = Field(default=None)
    evidence_refs: tuple[str, ...] = ()
    approved_change: bool = False
    downstream_invalidated_refs: tuple[str, ...] = ()
    reconciled_at_utc: str = Field(min_length=1)
    notes: str | None = Field(default=None, min_length=1)
    reconciliation_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_reconciliation(self) -> 'AsBuiltReconciliation':
        for value in (
            self.design_value_mm,
            self.as_built_value_mm,
            self.delta_mm,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('reconciliation values must be finite')
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError('evidence refs must be unique')
        if self.reconciliation_sha256 != _digest(self.identity_payload()):
            raise ValueError('reconciliation hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('reconciliation_id', None)
        payload.pop('reconciliation_sha256', None)
        return payload


class GeometryTaskRequirement(BaseModel):
    """A declared downstream use with its fitness bar (#613 §9)."""

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['geometry-survey-authority-1'] = (
        GEOMETRY_SURVEY_AUTHORITY_VERSION
    )
    task_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    task_class: GeometryTaskClass
    required_element_keys: tuple[str, ...] = ()
    tolerance_mm: float | None = Field(default=None, gt=0.0)
    notes: str | None = Field(default=None, min_length=1)
    task_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_task(self) -> 'GeometryTaskRequirement':
        if self.tolerance_mm is not None and not isfinite(
            float(self.tolerance_mm)
        ):
            raise ValueError('task tolerance must be finite')
        if len(set(self.required_element_keys)) != len(
            self.required_element_keys
        ):
            raise ValueError('required element keys must be unique')
        if self.task_sha256 != _digest(self.identity_payload()):
            raise ValueError('task requirement hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('task_id', None)
        payload.pop('task_sha256', None)
        return payload


class AsBuiltGeometryQualification(BaseModel):
    """Sealed fail-closed verdict over a document's as-built geometry."""

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['geometry-survey-authority-1'] = (
        GEOMETRY_SURVEY_AUTHORITY_VERSION
    )
    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    element_states: tuple[GeometricElementStateEntry, ...] = ()
    task_verdicts: tuple[GeometryTaskVerdictEntry, ...] = ()
    campaign_ids: tuple[str, ...] = ()
    control_ids: tuple[str, ...] = ()
    reconciliation_ids: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_qualification(self) -> 'AsBuiltGeometryQualification':
        for ids, name in (
            (self.campaign_ids, 'campaign'),
            (self.control_ids, 'control'),
            (self.reconciliation_ids, 'reconciliation'),
        ):
            if len(set(ids)) != len(ids):
                raise ValueError(f'{name} ids must be unique')
        if len(self.task_verdicts) != len(
            {v.task_id for v in self.task_verdicts}
        ):
            raise ValueError('task verdicts must be unique per task')
        if len(self.element_states) != len(
            {s.element_id for s in self.element_states}
        ):
            raise ValueError('element states must be unique per element')
        if self.qualification_sha256 != _digest(self.identity_payload()):
            raise ValueError('qualification hash mismatch')
        return self

    def element_state(self, element_id: str) -> GeometryElementState | None:
        for entry in self.element_states:
            if entry.element_id == element_id:
                return entry.state
        return None

    def task_verdict(self, task_id: str) -> GeometryTaskVerdict | None:
        for entry in self.task_verdicts:
            if entry.task_id == task_id:
                return entry.verdict
        return None

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


def build_survey_instrument(
    *,
    kind: SurveyInstrumentKind,
    capability_class: InstrumentCapabilityClass = 'unknown',
    **fields: Any,
) -> GeometrySurveyInstrument:
    return _seal(
        GeometrySurveyInstrument,
        dict(
            kind=kind,
            capability_class=capability_class,
            **fields,
        ),
        'instrument_id',
        'instrument_sha256',
        'gsi:',
    )  # type: ignore[return-value]


def build_survey_campaign(
    *,
    document_id: str,
    label: str,
    captured_at_utc: str,
    instrument_ids: Sequence[str] = (),
    mobile_conditions: MobileCaptureConditions | None = None,
    **fields: Any,
) -> SurveyCampaign:
    return _seal(
        SurveyCampaign,
        dict(
            document_id=document_id,
            label=label,
            captured_at_utc=captured_at_utc,
            instrument_ids=tuple(instrument_ids),
            mobile_conditions=mobile_conditions,
            **fields,
        ),
        'campaign_id',
        'campaign_sha256',
        'gsc:',
    )  # type: ignore[return-value]


def build_element_evidence(
    *,
    document_id: str,
    element_key: str,
    evidence_classes: Sequence[GeometryEvidenceClass] = ('unknown',),
    observation_state: ObservationState = 'design_source_only',
    **fields: Any,
) -> GeometricElementEvidence:
    return _seal(
        GeometricElementEvidence,
        dict(
            document_id=document_id,
            element_key=element_key,
            evidence_classes=tuple(evidence_classes),
            observation_state=observation_state,
            **fields,
        ),
        'element_id',
        'element_sha256',
        'gee:',
    )  # type: ignore[return-value]


def build_control_measurement(
    *,
    document_id: str,
    kind: ControlCheckKind,
    measured_value_mm: float,
    **fields: Any,
) -> GeometryControlMeasurement:
    return _seal(
        GeometryControlMeasurement,
        dict(
            document_id=document_id,
            kind=kind,
            measured_value_mm=measured_value_mm,
            **fields,
        ),
        'control_id',
        'control_sha256',
        'gcm:',
    )  # type: ignore[return-value]


def build_reconciliation(
    *,
    document_id: str,
    element_key: str,
    reconciled_at_utc: str,
    **fields: Any,
) -> AsBuiltReconciliation:
    return _seal(
        AsBuiltReconciliation,
        dict(
            document_id=document_id,
            element_key=element_key,
            reconciled_at_utc=reconciled_at_utc,
            **fields,
        ),
        'reconciliation_id',
        'reconciliation_sha256',
        'gar:',
    )  # type: ignore[return-value]


def build_task_requirement(
    *,
    document_id: str,
    task_class: GeometryTaskClass,
    required_element_keys: Sequence[str] = (),
    **fields: Any,
) -> GeometryTaskRequirement:
    return _seal(
        GeometryTaskRequirement,
        dict(
            document_id=document_id,
            task_class=task_class,
            required_element_keys=tuple(required_element_keys),
            **fields,
        ),
        'task_id',
        'task_sha256',
        'gtr:',
    )  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Fail-closed qualification evaluator
# ---------------------------------------------------------------------------


def _element_controls(
    element: GeometricElementEvidence,
    controls: Sequence[GeometryControlMeasurement],
) -> tuple[GeometryControlMeasurement, ...]:
    """Controls bound to this element via key or campaign."""
    out: list[GeometryControlMeasurement] = []
    for control in controls:
        if element.element_key in control.element_keys:
            out.append(control)
        elif (
            control.campaign_id is not None
            and control.campaign_id in element.campaign_ids
        ):
            out.append(control)
    return tuple(out)


def evaluate_geometry_qualification(
    *,
    document_id: str,
    elements: Sequence[GeometricElementEvidence],
    campaigns: Sequence[SurveyCampaign] = (),
    controls: Sequence[GeometryControlMeasurement] = (),
    task_requirements: Sequence[GeometryTaskRequirement] = (),
    reconciliations: Sequence[AsBuiltReconciliation] = (),
    evaluated_at_utc: str,
) -> AsBuiltGeometryQualification:
    """Fail-closed per-element + per-task qualification (#613 §16).

    Element states:

    - ``stale_after_change`` — the element's evidence predates a declared
      scene change;
    - ``design_only`` — no observed evidence at all;
    - ``observed_unqualified`` — observed but never checked against an
      independent control;
    - ``field_checked`` — at least one *independent* control passed;
    - ``control_check_failed`` — an independent control exceeded its
      declared tolerance;
    - ``registration_limited`` — the element depends on multiple
      campaigns without a declared common registration, or registration
      uncertainty was never characterized;
    - ``insufficient_evidence`` — observed but nothing about it can be
      honestly claimed (e.g. hidden elements).
    """
    campaign_map = {c.campaign_id: c for c in campaigns}
    element_states: list[GeometricElementStateEntry] = []
    limitations: list[str] = []

    unapproved = {
        r.element_key for r in reconciliations if not r.approved_change
    }
    for reconciliation in reconciliations:
        limitations.extend(reconciliation.downstream_invalidated_refs)

    for element in elements:
        reasons: list[GeometryQualificationReason] = []
        element_controls = _element_controls(element, controls)
        independent = tuple(
            c for c in element_controls if not c.used_for_registration
        )
        leaked = len(element_controls) - len(independent)
        if leaked:
            reasons.append('CONTROL_LEAKAGE_REGISTRATION')

        tier = _element_evidence_tier(element)
        failed = [c for c in independent if c.passed is False]
        passed = [c for c in independent if c.passed is True]
        achieved: float | None = None
        if passed:
            residuals = [
                c.residual_mm for c in passed if c.residual_mm is not None
            ]
            if residuals:
                achieved = max(residuals)

        missing_campaigns = [
            cid for cid in element.campaign_ids if cid not in campaign_map
        ]
        multi_campaign_unregistered = (
            len(element.campaign_ids) > 1
            and any(
                campaign_map[cid].registration is None
                for cid in element.campaign_ids
                if cid in campaign_map
            )
        ) or bool(missing_campaigns and len(element.campaign_ids) > 1)
        registration_uncharacterized = any(
            campaign_map[cid].registration is not None
            and campaign_map[cid].registration.uncertainty_mm is None  # type: ignore[union-attr]
            for cid in element.campaign_ids
            if cid in campaign_map
        ) or bool(missing_campaigns)

        state: GeometryElementState
        if element.stale_after_change:
            state = 'stale_after_change'
            reasons.append('STALE_AFTER_CHANGE')
        elif element.observation_state == 'hidden_unknown':
            state = 'insufficient_evidence'
            reasons.append('INSUFFICIENT_EVIDENCE')
        elif element.observation_state == 'design_source_only' or all(
            cls in _DESIGN_CLASSES for cls in element.evidence_classes
        ):
            state = 'design_only'
            reasons.append('ELEMENTS_DESIGN_ONLY')
        elif failed:
            state = 'control_check_failed'
            reasons.append('CONTROL_CHECK_FAILED')
        elif multi_campaign_unregistered:
            state = 'registration_limited'
            reasons.append('MULTI_CAMPAIGN_UNREGISTERED')
        elif registration_uncharacterized:
            state = 'registration_limited'
            reasons.append('REGISTRATION_UNCERTAINTY_UNDECLARED')
        elif passed:
            state = 'field_checked'
            reasons.append('ELEMENTS_FIELD_CHECKED')
        else:
            state = 'observed_unqualified'
            reasons.append('ELEMENTS_OBSERVED_NO_CONTROLS')

        element_states.append(
            GeometricElementStateEntry(
                element_id=element.element_id,
                element_sha256=element.element_sha256,
                state=state,
                evidence_tier=tier,
                achieved_uncertainty_mm=achieved,
                reasons=tuple(reasons),
            )
        )

    by_key = {e.element_key: e for e in elements}
    state_by_id = {s.element_id: s for s in element_states}

    task_verdicts: list[GeometryTaskVerdictEntry] = []
    for task in task_requirements:
        required = [
            by_key[key] for key in task.required_element_keys if key in by_key
        ]
        missing_keys = set(task.required_element_keys) - set(by_key)
        blocking: list[str] = []
        task_reasons: list[GeometryQualificationReason] = []
        required_tier = _TASK_TIER[task.task_class]

        verdict: GeometryTaskVerdict
        if missing_keys:
            verdict = 'insufficient_evidence'
            task_reasons.append('INSUFFICIENT_EVIDENCE')
        else:
            states = [state_by_id[e.element_id] for e in required]
            if any(s.state == 'stale_after_change' for s in states):
                verdict = 'stale_after_change'
                task_reasons.append('STALE_AFTER_CHANGE')
                blocking.extend(
                    s.element_id
                    for s in states
                    if s.state == 'stale_after_change'
                )
            elif any(s.state == 'control_check_failed' for s in states):
                verdict = 'control_check_failed'
                task_reasons.append('CONTROL_CHECK_FAILED')
                blocking.extend(
                    s.element_id
                    for s in states
                    if s.state == 'control_check_failed'
                )
            elif any(
                s.state in ('design_only', 'insufficient_evidence')
                for s in states
            ):
                verdict = 'insufficient_evidence'
                task_reasons.append('ELEMENTS_DESIGN_ONLY')
                blocking.extend(
                    s.element_id
                    for s in states
                    if s.state in ('design_only', 'insufficient_evidence')
                )
            elif any(s.state == 'registration_limited' for s in states):
                verdict = 'registration_limited'
                task_reasons.append('REGISTRATION_UNCERTAINTY_UNDECLARED')
                blocking.extend(
                    s.element_id
                    for s in states
                    if s.state == 'registration_limited'
                )
            else:
                worst_tier = min(
                    (_element_evidence_tier(e) for e in required), default=0
                )
                if worst_tier < required_tier:
                    verdict = 'fit_with_limitations'
                    task_reasons.append('TASK_EXCEEDS_EVIDENCE_TIER')
                    blocking.extend(
                        e.element_id
                        for e in required
                        if _element_evidence_tier(e) < required_tier
                    )
                elif (
                    required_tier >= _CONTROL_REQUIRED_TIER
                    and any(s.state != 'field_checked' for s in states)
                ):
                    verdict = 'fit_with_limitations'
                    task_reasons.append('ELEMENTS_OBSERVED_NO_CONTROLS')
                    blocking.extend(
                        s.element_id
                        for s in states
                        if s.state != 'field_checked'
                    )
                else:
                    verdict = 'fit_for_declared_task'
                    task_reasons.append('FIT_FOR_DECLARED_TASK')

            if task.tolerance_mm is not None and verdict in (
                'fit_for_declared_task',
                'fit_with_limitations',
            ):
                achieved = [
                    s.achieved_uncertainty_mm
                    for s in states
                    if s.achieved_uncertainty_mm is not None
                ]
                if not achieved or max(achieved) > task.tolerance_mm:
                    verdict = 'fit_with_limitations'
                    task_reasons.append('TASK_TOLERANCE_UNMET')

        if any(e in unapproved for e in (k for k in task.required_element_keys)):
            if verdict == 'fit_for_declared_task':
                verdict = 'fit_with_limitations'
            task_reasons.append('RECONCILIATION_UNRESOLVED')

        task_verdicts.append(
            GeometryTaskVerdictEntry(
                task_id=task.task_id,
                task_class=task.task_class,
                verdict=verdict,
                blocking_element_ids=tuple(blocking),
                reasons=tuple(dict.fromkeys(task_reasons)),
            )
        )

    probe = AsBuiltGeometryQualification.model_construct(
        **_canon(
            AsBuiltGeometryQualification,
            dict(
                qualification_id='',
                document_id=document_id,
                element_states=tuple(element_states),
                task_verdicts=tuple(task_verdicts),
                campaign_ids=tuple(c.campaign_id for c in campaigns),
                control_ids=tuple(c.control_id for c in controls),
                reconciliation_ids=tuple(
                    r.reconciliation_id for r in reconciliations
                ),
                limitations=tuple(dict.fromkeys(limitations)),
                evaluated_at_utc=evaluated_at_utc,
                qualification_sha256='',
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return AsBuiltGeometryQualification(
        **probe.model_dump(
            exclude={'qualification_id', 'qualification_sha256'}
        ),
        qualification_id=f'gaq:{sha}',
        qualification_sha256=sha,
    )


__all__ = [
    'AsBuiltGeometryQualification',
    'AsBuiltReconciliation',
    'CAPABILITY_CLASS_LABELS',
    'ControlCheckKind',
    'DERIVATION_STAGE_LABELS',
    'ELEMENT_STATE_LABELS',
    'EVIDENCE_CLASS_LABELS',
    'GEOMETRY_SURVEY_AUTHORITY_VERSION',
    'GeometricElementEvidence',
    'GeometricElementStateEntry',
    'GeometryControlMeasurement',
    'GeometryDerivationStage',
    'GeometryElementState',
    'GeometryEvidenceClass',
    'GeometryFrameDeclaration',
    'GeometryQualificationReason',
    'GeometrySurveyInstrument',
    'GeometryTaskClass',
    'GeometryTaskRequirement',
    'GeometryTaskVerdict',
    'GeometryTaskVerdictEntry',
    'GeometryUncertaintyContribution',
    'GeometryUncertaintyKind',
    'INSTRUMENT_KIND_LABELS',
    'InstrumentCapabilityClass',
    'MobileCaptureConditions',
    'OBSERVATION_STATE_LABELS',
    'ObservationState',
    'REASON_LABELS',
    'RegistrationTransform',
    'SurveyCampaign',
    'SurveyInstrumentKind',
    'TASK_CLASS_LABELS',
    'TASK_VERDICT_LABELS',
    'UNCERTAINTY_KIND_LABELS',
    'build_control_measurement',
    'build_element_evidence',
    'build_reconciliation',
    'build_survey_campaign',
    'build_survey_instrument',
    'build_task_requirement',
    'evaluate_geometry_qualification',
]
