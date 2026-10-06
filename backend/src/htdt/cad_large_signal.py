"""Loudspeaker/subwoofer large-signal mechanical authority (issue #754).

A clean low-level frequency response, a nominal ``Xmax`` figure,
amplifier headroom, or a short SPL burst do not by themselves prove that
a loudspeaker/subwoofer can produce the required low-frequency output
without excursion nonlinearity, suspension/motor nonlinearity, port
turbulence/compression, protection engagement or mechanical limit.

Basis: IEC 60268-22:2020 (electrical and mechanical measurements on
transducers, small- and large-signal domains), IEC 62458:2010 (lumped
large-signal parameter model — force factor Bl(x), stiffness Kms(x),
inductance Le(x,i)), Klippel, JAES 51(5) 2003 (performance-based vs
parameter-based Xmax semantics — one nominal Xmax number is
method-dependent and must not be equated across definitions), Devantier
& Rapoport AES 117 (2004) and Pene et al. AES 148 (2020) (vent
turbulence, flow separation, compression and noise at high drive
levels — mechanism evidence, not a universal port-velocity rule).
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


LargeSignalEvidenceClass = Literal[
    'manufacturer_declared_xmax',
    'standard_profiled_xmax',
    'iec_62458_parameter_measurement',
    'iec_60268_22_large_signal_measurement',
    'independent_lab_large_signal',
    'htdt_field_output_measurement',
    'model_derived_excursion',
    'protection_limiter_observed',
    'user_entered',
    'unknown',
]
XmaxDefinitionBasis = Literal[
    'performance_based', 'parameter_based', 'manufacturer_nominal',
    'unknown',
]
DisplacementConvention = Literal[
    'one_way_peak', 'peak_to_peak', 'unknown',
]
EnclosureAlignment = Literal[
    'sealed', 'bass_reflex', 'passive_radiator', 'bandpass',
    'horn_loading', 'other_loading', 'active_dsp_alignment', 'unknown',
]
NonlinearParameterKind = Literal[
    'bl_x', 'kms_x', 'le_x_i', 're_t', 'asymmetry_offset',
    'suspension_creep', 'other', 'unknown',
]
LimitingMechanism = Literal[
    'driver_excursion',
    'motor_force_factor',
    'suspension_stiffness',
    'inductance_modulation',
    'port_flow_turbulence',
    'port_compression',
    'port_noise_chuffing',
    'port_resonance_parasitic',
    'passive_radiator_excursion',
    'dsp_excursion_protection',
    'voltage_limiter',
    'current_limiter',
    'thermal_limiter',
    'amplifier_clipping',
    'unknown_combined',
    'unknown',
]
VentLimitMechanism = Literal[
    'driver_excursion_limit',
    'port_flow_turbulence_limit',
    'port_compression',
    'port_noise_chuffing',
    'port_resonance_parasitic_output',
    'passive_radiator_excursion',
    'measured_limit',
    'unknown',
]
LevelApplicability = Literal[
    'small_signal_only',
    'large_signal_validated_to_level',
    'output_compression_model_available',
    'unknown_level_applicability',
]
OutputLimitVerdict = Literal[
    'measured_bounded',
    'model_bounded',
    'protection_limited',
    'small_signal_only',
    'insufficient_evidence',
]
ExcursionEquivalence = Literal[
    'equivalent', 'definition_mismatch', 'convention_mismatch',
    'incomparable',
]

EVIDENCE_CLASS_LABELS: dict[str, str] = {
    'manufacturer_declared_xmax': 'メーカー公称 Xmax',
    'standard_profiled_xmax': '規格プロファイル Xmax',
    'iec_62458_parameter_measurement': 'IEC 62458 大信号パラメータ測定',
    'iec_60268_22_large_signal_measurement': 'IEC 60268-22 大信号測定',
    'independent_lab_large_signal': '独立ラボ大信号測定',
    'htdt_field_output_measurement': 'HTDT 現場出力測定',
    'model_derived_excursion': 'モデル導出エクスカーション',
    'protection_limiter_observed': 'プロテクション/リミッタ観測',
    'user_entered': 'ユーザー入力',
    'unknown': '不明',
}
DEFINITION_BASIS_LABELS: dict[str, str] = {
    'performance_based': '性能基準 Xmax',
    'parameter_based': 'パラメータ基準 Xmax',
    'manufacturer_nominal': 'メーカー呼称値',
    'unknown': '不明',
}
CONVENTION_LABELS: dict[str, str] = {
    'one_way_peak': '片側ピーク',
    'peak_to_peak': 'ピークツーピーク',
    'unknown': '不明',
}
ALIGNMENT_LABELS: dict[str, str] = {
    'sealed': '密閉型',
    'bass_reflex': 'バスレフ型',
    'passive_radiator': 'パッシブラジエータ型',
    'bandpass': 'バンドパス型',
    'horn_loading': 'ホーン負荷',
    'other_loading': 'その他負荷',
    'active_dsp_alignment': 'DSP アライメント',
    'unknown': '不明',
}
MECHANISM_LABELS: dict[str, str] = {
    'driver_excursion': 'ドライバーエクスカーション限界',
    'motor_force_factor': 'モーター（Bl）非線形',
    'suspension_stiffness': 'サスペンション剛性非線形',
    'inductance_modulation': 'インダクタンス変調',
    'port_flow_turbulence': 'ポート乱流限界',
    'port_compression': 'ポート圧縮',
    'port_noise_chuffing': 'ポートノイズ（チャフィング）',
    'port_resonance_parasitic': 'ポート共振/寄生出力',
    'passive_radiator_excursion': 'パッシブラジエータ変位限界',
    'dsp_excursion_protection': 'DSP エクスカーション保護',
    'voltage_limiter': '電圧リミッタ',
    'current_limiter': '電流リミッタ',
    'thermal_limiter': 'サーマルリミッタ',
    'amplifier_clipping': 'アンプクリッピング',
    'unknown_combined': '複合（原因不明）',
    'unknown': '不明',
}
APPLICABILITY_LABELS: dict[str, str] = {
    'small_signal_only': '小信号のみ有効',
    'large_signal_validated_to_level': '大信号検証済み（レベル指定）',
    'output_compression_model_available': '出力圧縮モデルあり',
    'unknown_level_applicability': 'レベル適用性不明',
}
VERDICT_LABELS: dict[str, str] = {
    'measured_bounded': '実測出力限界',
    'model_bounded': 'モデル推定限界',
    'protection_limited': 'プロテクション制限',
    'small_signal_only': '小信号証拠のみ',
    'insufficient_evidence': '証拠不足',
}

_MEASURED_CLASSES = {
    'iec_62458_parameter_measurement',
    'iec_60268_22_large_signal_measurement',
    'independent_lab_large_signal',
    'htdt_field_output_measurement',
}
_STANDARD_CLASSES = {
    'standard_profiled_xmax',
    'iec_62458_parameter_measurement',
    'iec_60268_22_large_signal_measurement',
}
_PORT_MECHANISMS = {
    'port_flow_turbulence',
    'port_compression',
    'port_noise_chuffing',
    'port_resonance_parasitic',
}
_PROTECTION_MECHANISMS = {
    'dsp_excursion_protection',
    'voltage_limiter',
    'current_limiter',
    'thermal_limiter',
    'amplifier_clipping',
}


class LargeSignalTransducerModel(BaseModel):
    """Large-signal state of one exact transducer/enclosure system.

    A small-signal T-S or impedance dataset (#662) is a prerequisite and
    reference, never a substitute: nonlinear parameters are declared by
    kind (``bl_x``/``kms_x``/``le_x_i``/…) only when real evidence
    exists — they are never fabricated from small-signal fits.
    ``large_signal_validated_to_level`` therefore requires a measured
    evidence class, not a self-declared flag.
    """

    model_config = ConfigDict(frozen=True)

    model_id: str
    model_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    transducer_ref: AuthorityRef
    evidence_class: LargeSignalEvidenceClass = 'unknown'
    nonlinear_parameter_kinds: tuple[NonlinearParameterKind, ...] = ()
    enclosure_alignment: EnclosureAlignment = 'unknown'
    level_applicability: LevelApplicability = 'unknown_level_applicability'
    enclosure_ref: AuthorityRef | None = None
    dsp_alignment_ref: AuthorityRef | None = None
    amplifier_load_ref: AuthorityRef | None = None
    thermal_state_ref: AuthorityRef | None = None
    validation_ref: AuthorityRef | None = None
    method_ref: AuthorityRef | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'LargeSignalTransducerModel':
        _require_refs(self.transducer_ref)
        for ref in (
            self.enclosure_ref,
            self.dsp_alignment_ref,
            self.amplifier_load_ref,
            self.thermal_state_ref,
            self.validation_ref,
            self.method_ref,
        ):
            if ref is not None:
                _require_refs(ref)
        if 'unknown' in self.nonlinear_parameter_kinds:
            raise ValueError(
                'nonlinear parameters must be declared by kind, not '
                "'unknown'"
            )
        if (
            self.level_applicability == 'large_signal_validated_to_level'
            and self.evidence_class not in _MEASURED_CLASSES
        ):
            raise ValueError(
                'large_signal_validated_to_level requires a measured '
                'evidence class (IEC 62458/60268-22, independent lab or '
                'HTDT field measurement)'
            )
        if (
            self.evidence_class == 'model_derived_excursion'
            and not self.nonlinear_parameter_kinds
        ):
            raise ValueError(
                'model_derived_excursion requires declared nonlinear '
                'parameter evidence'
            )
        if (
            self.level_applicability == 'large_signal_validated_to_level'
            and self.validation_ref is None
        ):
            raise ValueError(
                'a validated large-signal model must pin the independent '
                'measurement it was validated against'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'model_id', 'model_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'LargeSignalTransducerModel':
        return _seal(cls, kwargs, 'model_id', 'model_sha256', 'lstm')


class ExcursionCapability(BaseModel):
    """One excursion/displacement datum with exact semantics.

    ``Xmax`` is method-dependent (Klippel 2003): the datum preserves the
    quantity, peak convention, definition basis, distortion/modulation
    criterion, stimulus, drive level, standard/method revision, source
    and uncertainty so two ``Xmax`` numbers are never silently equated.
    """

    model_config = ConfigDict(frozen=True)

    capability_id: str
    capability_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    transducer_ref: AuthorityRef
    evidence_class: LargeSignalEvidenceClass = 'unknown'
    value_mm: float = Field(gt=0)
    convention: DisplacementConvention = 'unknown'
    definition_basis: XmaxDefinitionBasis = 'unknown'
    distortion_criterion: str = ''
    frequency_hz: float | None = Field(default=None, gt=0)
    drive_level_ref: AuthorityRef | None = None
    standard_ref: AuthorityRef | None = None
    source_ref: AuthorityRef | None = None
    uncertainty_mm: float | None = Field(default=None, ge=0)
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'ExcursionCapability':
        _require_refs(self.transducer_ref)
        for ref in (
            self.drive_level_ref, self.standard_ref, self.source_ref,
        ):
            if ref is not None:
                _require_refs(ref)
        if (
            self.evidence_class in _STANDARD_CLASSES
            and self.standard_ref is None
        ):
            raise ValueError(
                'a standard-profiled or IEC excursion datum must pin '
                'the exact standard/method revision'
            )
        if (
            self.definition_basis != 'unknown'
            and self.convention == 'unknown'
        ):
            raise ValueError(
                'a defined Xmax basis requires a declared peak '
                'convention (one-way vs peak-to-peak)'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'capability_id', 'capability_sha256'},
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'ExcursionCapability':
        return _seal(
            cls, kwargs, 'capability_id', 'capability_sha256', 'excp'
        )


class VentFlowCapability(BaseModel):
    """Bass-reflex / passive-radiator flow limit for one system.

    Port turbulence, compression, chuffing and parasitic resonance are
    separate limiting mechanisms from driver excursion — there is no
    universal ``max port velocity`` rule, so geometry and evidence class
    are preserved and ``measured_limit`` remains an honest terminal
    state when only product output measurements exist.
    """

    model_config = ConfigDict(frozen=True)

    capability_id: str
    capability_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    system_ref: AuthorityRef
    mechanism: VentLimitMechanism = 'unknown'
    enclosure_alignment: EnclosureAlignment = 'unknown'
    port_diameter_mm: float | None = Field(default=None, gt=0)
    port_area_mm2: float | None = Field(default=None, gt=0)
    port_flare: str = ''
    passive_radiator_ref: AuthorityRef | None = None
    evidence_ref: AuthorityRef | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'VentFlowCapability':
        _require_refs(self.system_ref)
        for ref in (self.passive_radiator_ref, self.evidence_ref):
            if ref is not None:
                _require_refs(ref)
        if (
            self.mechanism != 'unknown'
            and self.enclosure_alignment
            not in ('bass_reflex', 'passive_radiator', 'bandpass')
            and self.mechanism != 'measured_limit'
        ):
            raise ValueError(
                'a port/vent limiting mechanism requires a vented '
                'enclosure alignment (bass_reflex / passive_radiator / '
                'bandpass)'
            )
        if (
            self.mechanism == 'passive_radiator_excursion'
            and self.passive_radiator_ref is None
        ):
            raise ValueError(
                'a passive-radiator limit must reference the radiator '
                'as an independent moving element'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'capability_id', 'capability_sha256'},
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'VentFlowCapability':
        return _seal(
            cls, kwargs, 'capability_id', 'capability_sha256', 'vflw'
        )


class MechanicalOutputLimitAssessment(BaseModel):
    """Sealed verdict: which physical mechanism limits output, and how
    strongly the claim is evidenced.

    ``verdict`` is sealed by the producer; ``evaluate_output_limit``
    re-derives it so a tampered assessment cannot launder a
    small-signal-only model into ``model_bounded``.
    """

    model_config = ConfigDict(frozen=True)

    assessment_id: str
    assessment_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    model_ref: AuthorityRef
    excursion_refs: tuple[AuthorityRef, ...] = ()
    vent_refs: tuple[AuthorityRef, ...] = ()
    frequency_hz: float | None = Field(default=None, gt=0)
    limiting_mechanism: LimitingMechanism = 'unknown'
    headroom_db: float | None = None
    verdict: OutputLimitVerdict = 'insufficient_evidence'
    reasons: tuple[str, ...] = ()
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'MechanicalOutputLimitAssessment':
        _require_refs(self.model_ref)
        for ref in (*self.excursion_refs, *self.vent_refs):
            _require_refs(ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'assessment_id', 'assessment_sha256'},
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'MechanicalOutputLimitAssessment':
        return _seal(
            cls, kwargs, 'assessment_id', 'assessment_sha256', 'molm'
        )


def evaluate_output_limit(
    model: LargeSignalTransducerModel,
    limiting_mechanism: LimitingMechanism = 'unknown',
) -> tuple[OutputLimitVerdict, tuple[str, ...]]:
    """Fail-closed verdict for one large-signal claim path.

    Short-term SPL claims and RP22-style output capability require
    large-signal evidence: a small-signal-only source model stays
    ``small_signal_only`` (linear extrapolation is never promoted),
    measured large-signal evidence earns ``measured_bounded``, a
    validated nonlinear-parameter model earns ``model_bounded``, and
    observed protection engagement bounds the deployed capability at
    ``protection_limited`` — the protected state is the capability,
    not the raw transducer behind it.
    """
    reasons: list[str] = []
    if model.level_applicability == 'unknown_level_applicability':
        return 'insufficient_evidence', ('level_applicability_unknown',)
    if model.level_applicability == 'small_signal_only':
        return 'small_signal_only', ('small_signal_source_model',)
    if limiting_mechanism in _PROTECTION_MECHANISMS:
        return 'protection_limited', ('protection_engaged_first',)
    if model.evidence_class in _MEASURED_CLASSES:
        return 'measured_bounded', ()
    if limiting_mechanism in _PORT_MECHANISMS:
        reasons.append('port_mechanism_limits_first')
    if model.nonlinear_parameter_kinds:
        return 'model_bounded', tuple(reasons)
    if model.evidence_class == 'protection_limiter_observed':
        return 'protection_limited', ('protection_state_is_capability',)
    return 'insufficient_evidence', ('no_large_signal_parameters',)


def compare_excursion_datums(
    a: ExcursionCapability,
    b: ExcursionCapability,
) -> tuple[ExcursionEquivalence, tuple[str, ...]]:
    """Comparability of two Xmax/excursion datums (LST30).

    Two ``Xmax`` numbers are exact equivalents only when convention,
    definition basis and distortion criterion agree; differing
    definitions make them incomparable rather than silently averaged or
    ranked.
    """
    if a.convention == 'unknown' or b.convention == 'unknown':
        return 'incomparable', ('convention_unknown',)
    if a.definition_basis == 'unknown' or b.definition_basis == 'unknown':
        return 'incomparable', ('definition_unknown',)
    if a.convention != b.convention:
        return 'convention_mismatch', ('peak_convention_differs',)
    if a.definition_basis != b.definition_basis:
        return 'definition_mismatch', ('xmax_definition_differs',)
    if a.distortion_criterion != b.distortion_criterion:
        return 'incomparable', ('distortion_criterion_differs',)
    return 'equivalent', ()
