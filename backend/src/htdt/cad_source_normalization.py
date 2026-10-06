"""Loudspeaker source-normalization / absolute-output authority (#734).

A directivity balloon, normalized impulse-response set or frequency-
response curve does not by itself define the loudspeaker's absolute
acoustic output for an arbitrary electrical/digital drive state. Relative
angular shape and absolute output capability are separate evidences.

Basis: ANSI/CTA-2034-B (July 2024 — frequency response, directivity and
maximum output capability are separate measurement authorities),
IEC 60268-21:2018 (transfer behaviour from an arbitrary analogue or
digital input to acoustic output; the exact input/operating condition
travels with every absolute claim), ANSI/CTA-2054 (July 2024 — amplifier
drive-level reporting compatible with CTA-2034-B capability data; #593
remains canonical for the amplifier/load side).

Core traps this module closes:
- ``2.83 V sensitivity`` and ``1 W sensitivity`` are NOT equivalent —
  electrical power depends on the frequency-dependent, possibly
  reactive load; conversion requires pinned impedance evidence.
- ``normalized to 0 dB on-axis`` never becomes an absolute source.
- Angular curves renormalized per frequency destroy absolute off-axis
  level relationships even when each curve is individually valid.
- Installed output cannot reuse a free-field anchor without a #614
  boundary transformation.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{sha_field: digest, id_field: _semantic_id(prefix, digest)},
    )


def _require_refs(*refs: AuthorityRef) -> None:
    for ref in refs:
        if ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')


SourceLevelCapability = Literal[
    'relative_shape_only',
    'absolute_spl_at_reference_drive',
    'absolute_transfer_pa_per_input',
    'absolute_sound_power_reference',
    'large_signal_output_curve',
    'maximum_output_capability',
    'installed_measured_absolute_transfer',
    'unknown',
]
DriveQuantityKind = Literal[
    'analogue_voltage_rms',
    'analogue_current_rms',
    'analogue_power',
    'digital_dbfs',
    'digital_reference_level',
    'unknown',
]
NormalizationMethod = Literal[
    'absolute_spl_per_direction',
    'complex_transfer_per_direction',
    'normalized_to_on_axis',
    'normalized_per_frequency',
    'manufacturer_polar_max_normalized',
    'arbitrary_relative_units',
    'unknown',
]
LoudspeakerDriveTopology = Literal[
    'passive', 'active_dsp', 'active_fixed_gain', 'unknown',
]
LimiterState = Literal[
    'disengaged', 'engaged', 'protection_active', 'bypassed_lab_method',
    'unknown',
]
LinearScalingEligibility = Literal[
    'linear_scaling_eligible',
    'linear_scaling_with_limits',
    'compression_observed',
    'limiter_active',
    'large_signal_model_required',
    'unknown',
]
AbsolutePredictionVerdict = Literal[
    'absolute_spl_prediction_eligible',
    'absolute_with_limitations',
    'relative_response_only',
    'large_signal_unsupported',
    'insufficient_source_level_evidence',
]
DriveConversionVerdict = Literal[
    'conversion_permitted', 'equivalence_blocked', 'incomparable',
]

CAPABILITY_LABELS: dict[str, str] = {
    'relative_shape_only': '相対形状のみ',
    'absolute_spl_at_reference_drive': '基準ドライブ条件の絶対 SPL',
    'absolute_transfer_pa_per_input': '絶対伝達量（Pa/入力）',
    'absolute_sound_power_reference': '音響パワー基準',
    'large_signal_output_curve': '大信号出力カーブ',
    'maximum_output_capability': '最大出力能力',
    'installed_measured_absolute_transfer': '設置状態実測の絶対伝達',
    'unknown': '不明',
}
DRIVE_QUANTITY_LABELS: dict[str, str] = {
    'analogue_voltage_rms': 'アナログ電圧（RMS）',
    'analogue_current_rms': 'アナログ電流（RMS）',
    'analogue_power': 'アナログ電力',
    'digital_dbfs': 'デジタル入力（dBFS）',
    'digital_reference_level': 'デジタル基準レベル',
    'unknown': '不明',
}
NORMALIZATION_LABELS: dict[str, str] = {
    'absolute_spl_per_direction': '方向別絶対 SPL',
    'complex_transfer_per_direction': '方向別複素伝達',
    'normalized_to_on_axis': 'オンアクシス正規化',
    'normalized_per_frequency': '周波数ごと独立正規化',
    'manufacturer_polar_max_normalized': 'メーカー極性最大値正規化',
    'arbitrary_relative_units': '任意相対単位',
    'unknown': '不明',
}
TOPOLOGY_LABELS: dict[str, str] = {
    'passive': 'パッシブ',
    'active_dsp': 'アクティブ/DSP',
    'active_fixed_gain': 'アクティブ（固定ゲイン）',
    'unknown': '不明',
}
LIMITER_LABELS: dict[str, str] = {
    'disengaged': 'リミッタ未作動',
    'engaged': 'リミッタ作動',
    'protection_active': 'プロテクション作動',
    'bypassed_lab_method': 'ラボ手法でバイパス',
    'unknown': '不明',
}
SCALING_LABELS: dict[str, str] = {
    'linear_scaling_eligible': '線形スケーリング可能',
    'linear_scaling_with_limits': '制限付き線形スケーリング',
    'compression_observed': '圧縮観測あり',
    'limiter_active': 'リミッタ作動域',
    'large_signal_model_required': '大信号モデル必須',
    'unknown': '不明',
}
PREDICTION_LABELS: dict[str, str] = {
    'absolute_spl_prediction_eligible': '絶対 SPL 予測可能',
    'absolute_with_limitations': '制限付き絶対予測',
    'relative_response_only': '相対応答のみ',
    'large_signal_unsupported': '大信号未対応',
    'insufficient_source_level_evidence': 'ソースレベル証拠不足',
}
CONVERSION_LABELS: dict[str, str] = {
    'conversion_permitted': '変換可能',
    'equivalence_blocked': '等価性ブロック',
    'incomparable': '比較不能',
}

_ABSOLUTE_CAPABILITIES = {
    'absolute_spl_at_reference_drive',
    'absolute_transfer_pa_per_input',
    'absolute_sound_power_reference',
    'large_signal_output_curve',
    'maximum_output_capability',
    'installed_measured_absolute_transfer',
}
_ELECTRICAL_QUANTITIES = {
    'analogue_voltage_rms',
    'analogue_current_rms',
    'analogue_power',
}


class LoudspeakerSourceNormalization(BaseModel):
    """Level capability + normalization method of one source dataset.

    ``capability`` is declared per record — a dataset may earn more
    than one capability through separate records, but there is no
    automatic upgrade from relative to absolute. Per-frequency
    renormalization destroys absolute angular level relationships and
    is recorded as its own method, never laundered into
    ``absolute_spl_per_direction``.
    """

    model_config = ConfigDict(frozen=True)

    normalization_id: str
    normalization_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    dataset_ref: AuthorityRef
    capability: SourceLevelCapability = 'unknown'
    normalization_method: NormalizationMethod = 'unknown'
    drive_topology: LoudspeakerDriveTopology = 'unknown'
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'LoudspeakerSourceNormalization':
        _require_refs(self.dataset_ref)
        if (
            self.capability in _ABSOLUTE_CAPABILITIES
            and self.normalization_method
            in (
                'normalized_per_frequency',
                'manufacturer_polar_max_normalized',
            )
        ):
            raise ValueError(
                'per-frequency or per-polar renormalization destroys '
                'absolute angular level relationships — it cannot anchor '
                'an absolute capability'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'normalization_id', 'normalization_sha256'},
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'LoudspeakerSourceNormalization':
        return _seal(
            cls, kwargs, 'normalization_id', 'normalization_sha256',
            'snrm',
        )


class ReferenceDriveCondition(BaseModel):
    """The exact input/operating condition behind an absolute anchor.

    IEC 60268-21 binds acoustic output to an arbitrary analogue or
    digital input — ``input = 2.83`` with no quantity, reference or
    state is not a reference drive condition.
    """

    model_config = ConfigDict(frozen=True)

    condition_id: str
    condition_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    quantity_kind: DriveQuantityKind = 'unknown'
    value: float | None = None
    impedance_condition_ref: AuthorityRef | None = None
    stimulus_ref: AuthorityRef | None = None
    duration_s: float | None = Field(default=None, gt=0)
    crest_factor_db: float | None = Field(default=None, ge=0)
    duty_cycle: float | None = Field(default=None, gt=0, le=1)
    channel_count: int | None = Field(default=None, ge=1)
    device_gain_state: str = ''
    dsp_preset: str = ''
    limiter_state: LimiterState = 'unknown'
    power_supply_state: str = ''
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'ReferenceDriveCondition':
        for ref in (self.impedance_condition_ref, self.stimulus_ref):
            if ref is not None:
                _require_refs(ref)
        if self.quantity_kind != 'unknown' and self.value is None:
            raise ValueError(
                'a typed drive quantity requires its value'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'condition_id', 'condition_sha256'},
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'ReferenceDriveCondition':
        return _seal(
            cls, kwargs, 'condition_id', 'condition_sha256', 'rdrv'
        )


class AbsoluteAcousticOutputAnchor(BaseModel):
    """The absolute-output anchor: source normalization + exact drive +
    acoustic reference origin + axis + boundary state.

    ``verdict`` is sealed by the producer; ``evaluate_absolute_prediction``
    re-derives it so a normalized-only dataset cannot launder into an
    absolute SPL source.
    """

    model_config = ConfigDict(frozen=True)

    anchor_id: str
    anchor_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    normalization_ref: AuthorityRef
    drive_ref: AuthorityRef | None = None
    reference_distance_m: float | None = Field(default=None, gt=0)
    acoustic_origin_ref: AuthorityRef | None = None
    reference_axis_deg: float | None = None
    boundary_state_ref: AuthorityRef | None = None
    linear_scaling: LinearScalingEligibility = 'unknown'
    verdict: AbsolutePredictionVerdict = 'insufficient_source_level_evidence'
    reasons: tuple[str, ...] = ()
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'AbsoluteAcousticOutputAnchor':
        _require_refs(self.normalization_ref)
        for ref in (
            self.drive_ref,
            self.acoustic_origin_ref,
            self.boundary_state_ref,
        ):
            if ref is not None:
                _require_refs(ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'anchor_id', 'anchor_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'AbsoluteAcousticOutputAnchor':
        return _seal(cls, kwargs, 'anchor_id', 'anchor_sha256', 'aout')


def evaluate_absolute_prediction(
    normalization: LoudspeakerSourceNormalization,
    anchor: AbsoluteAcousticOutputAnchor | None,
) -> tuple[AbsolutePredictionVerdict, tuple[str, ...]]:
    """Fail-closed absolute-output eligibility.

    Relative-shape data earns ``relative_response_only``; an absolute
    capability additionally requires a pinned drive condition and an
    acoustic reference origin/distance — and even then only within the
    evidenced linear-scaling domain (#192 compression / #731 thermal
    bounds apply downstream).
    """
    if normalization.capability == 'unknown':
        return 'insufficient_source_level_evidence', (
            'capability_unknown',
        )
    if normalization.capability == 'relative_shape_only':
        return 'relative_response_only', ('dataset_is_relative',)
    if normalization.capability not in _ABSOLUTE_CAPABILITIES:
        return 'insufficient_source_level_evidence', (
            'capability_not_absolute',
        )
    if anchor is None:
        return 'insufficient_source_level_evidence', ('no_anchor',)
    reasons: list[str] = []
    if anchor.drive_ref is None:
        reasons.append('drive_condition_missing')
    if anchor.reference_distance_m is None or (
        anchor.acoustic_origin_ref is None
    ):
        reasons.append('acoustic_origin_or_distance_missing')
    if reasons:
        return 'insufficient_source_level_evidence', tuple(reasons)
    if anchor.linear_scaling == 'linear_scaling_eligible':
        return 'absolute_spl_prediction_eligible', ()
    if anchor.linear_scaling == 'linear_scaling_with_limits':
        return 'absolute_with_limitations', ('scaling_bounded',)
    if anchor.linear_scaling in ('compression_observed', 'limiter_active'):
        return 'absolute_with_limitations', ('nonlinear_boundary',)
    if anchor.linear_scaling == 'large_signal_model_required':
        return 'large_signal_unsupported', (
            'linear_scaling_exceeded',
        )
    return 'insufficient_source_level_evidence', ('scaling_unknown',)


def convert_reference_drive(
    source: ReferenceDriveCondition,
    target_kind: DriveQuantityKind,
    impedance_ref: AuthorityRef | None,
) -> tuple[DriveConversionVerdict, tuple[str, ...]]:
    """2.83 V vs 1 W semantics (SRCN30).

    Converting between electrical drive quantities (voltage ↔ power)
    requires pinned impedance evidence — loudspeaker impedance is
    frequency-dependent and possibly reactive. Without it the
    conversion is ``equivalence_blocked``, never silently approximated.
    """
    if (
        source.quantity_kind == 'unknown'
        or target_kind == 'unknown'
    ):
        return 'incomparable', ('quantity_unknown',)
    if source.quantity_kind == target_kind:
        return 'conversion_permitted', ()
    electrical = (
        source.quantity_kind in _ELECTRICAL_QUANTITIES
        and target_kind in _ELECTRICAL_QUANTITIES
    )
    if not electrical:
        return 'incomparable', ('cross_domain_conversion',)
    if impedance_ref is None:
        return 'equivalence_blocked', ('impedance_evidence_missing',)
    if impedance_ref.ref_sha256 is None:
        return 'equivalence_blocked', ('impedance_ref_unpinned',)
    return 'conversion_permitted', ('impedance_evidenced',)
