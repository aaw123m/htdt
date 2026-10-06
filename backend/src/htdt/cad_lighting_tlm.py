"""Theater-lighting temporal-modulation (TLM/TLA) authority (#748).

Correct illuminance, colour and dimming control do not prove that room
lighting has acceptable temporal-light-modulation behaviour. At low dim
levels, LED drivers and architectural dimmers can change modulation even
when average photometry stays correct — a theater's actual movie scene
is exactly where that matters. This module pins the exact lighting state,
retains the raw optical waveform as canonical evidence, keeps the three
TLA phenomena separate, and binds every metric to its method/version/
applicability domain so a polished scalar can never be promoted beyond
its validated profile.

Basis: issue #748 scope; CIE 249:2022-Cor1 (published 2026-10-05,
supersedes CIE 249:2022 — the corrigendum clarifies the SVM calculation
needs further verification and that the phantom-array visibility
framework is not a standardization method); CIE TN 012:2021 (measurement
guidance, non-standardizing); IEC TR 61547-1:2020 (objective flickermeter,
stable-mains intrinsic vs voltage-fluctuation immunity, stability date
2028); IEC TR 63158:2018 (SVM meter, declared application domain —
typical indoor levels above 100 lx, moderate motion below 4 m/s); IEEE
1789-2015 (Inactive-Reserved since 2026-03-26 — historical citation only).
Photometry stays with #293, camera evidence with #716, display temporal
behaviour with #647, AC power-quality events with #738, standards
lifecycle with #599.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


_SHA256_PATTERN = r'^[0-9a-f]{64}$'

# IEC TR 63158's declared SVM application domain: indoor levels above
# ~100 lx — a dark theater scene sits outside it.
_SVM_DOMAIN_MIN_ILLUMINANCE_LX = 100.0


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


TLAPhenomenon = Literal[
    'flicker', 'stroboscopic_effect', 'phantom_array_effect',
    'other', 'unknown',
]

ModulationSource = Literal[
    'intrinsic', 'power_quality_induced', 'control_dimmer_induced',
    'correlated_unknown', 'undeclared',
]

MetricSource = Literal[
    'IEC TR 61547-1:2020', 'IEC TR 63158:2018', 'CIE 249:2022-Cor1',
    'CIE 249:2022', 'CIE TN 012:2021', 'IEEE 1789-2015', 'other',
]

ApplicabilityDomain = Literal[
    'general', 'svm_indoor_gt100lx', 'phantom_array_research',
    'flickermeter_61547', 'guidance_only', 'historical_ieee1789',
    'other',
]

TLA_LABELS: dict[str, str] = {
    'illuminance_is_not_tla': '照度適合は時間変調品質を意味しない',
    'insufficient_evidence': '証拠不足',
    'instrument_bandwidth_insufficient': '計測帯域不足',
    'outside_profile_domain': '適用域外（低照度等）',
    'limited_by_corrigendum': 'CIE 249 Cor1 の留保あり',
    'historical_reference_only': '履歴的参照のみ（IEEE 1789 は非現行）',
    'power_quality_induced': '電源品質起因の変調',
    'control_induced': '調光制御起因の変調',
    'tla_within_domain': '適用域内で TLA 評価成立',
    'tla_failed_within_domain': '適用域内で不合格',
}


class DimmingTemporalProfile(BaseModel):
    """The exact lighting state under which temporal behaviour was
    qualified (dtp- prefix). Changing dim level or driver mode creates a
    new qualification identity — full-output results never transfer to
    low-dim scenes."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    luminaire_ref: AuthorityRef
    driver_model: str | None = None
    dimmer_ref: AuthorityRef | None = None
    control_scene: str | None = None
    commanded_dim_level_pct: float
    observed_dim_level_pct: float | None = None
    cct_k: float | None = None
    measurement_location: str | None = None
    warm_up_state: Literal['cold', 'warmed', 'unknown'] = 'unknown'
    mains_state_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'DimmingTemporalProfile':
        _require_refs(self.luminaire_ref)
        for ref in (self.dimmer_ref, self.mains_state_ref):
            if ref is not None:
                _require_refs(ref)
        for level in (self.commanded_dim_level_pct,
                      self.observed_dim_level_pct):
            if level is not None and not 0.0 <= level <= 100.0:
                raise ValueError('dim level must be within 0..100 %')
        if self.cct_k is not None and self.cct_k <= 0.0:
            raise ValueError('cct_k must be > 0')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'DimmingTemporalProfile':
        return _seal(cls, payload, 'profile_id', 'profile_sha256', 'dtp')


class TemporalLightWaveform(BaseModel):
    """Raw optical waveform — the canonical temporal evidence
    (tlw- prefix). Derived metrics reference this; they never replace
    it. Instrument bandwidth/sample rate gate claim strength."""

    model_config = ConfigDict(frozen=True)

    waveform_id: str
    waveform_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    artifact_ref: AuthorityRef
    instrument_ref: AuthorityRef
    sample_rate_hz: float
    bandwidth_hz: float
    duration_s: float
    illuminance_lx: float
    mean_level: float | None = None
    modulation_frequencies_hz: tuple[float, ...] = ()
    modulation_depth_pct: float | None = None
    duty_cycle: float | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'TemporalLightWaveform':
        _require_refs(
            self.profile_ref, self.artifact_ref, self.instrument_ref)
        if self.sample_rate_hz <= 0.0:
            raise ValueError('sample_rate_hz must be > 0')
        if self.bandwidth_hz <= 0.0:
            raise ValueError('bandwidth_hz must be > 0')
        if self.duration_s <= 0.0:
            raise ValueError('duration_s must be > 0')
        if self.illuminance_lx < 0.0:
            raise ValueError('illuminance_lx must be >= 0')
        for f in self.modulation_frequencies_hz:
            if f <= 0.0:
                raise ValueError('modulation frequencies must be > 0')
        if self.modulation_depth_pct is not None \
                and not 0.0 <= self.modulation_depth_pct <= 100.0:
            raise ValueError('modulation_depth_pct must be 0..100')
        if self.duty_cycle is not None \
                and not 0.0 <= self.duty_cycle <= 1.0:
            raise ValueError('duty_cycle must be within 0..1')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'waveform_id', 'waveform_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'TemporalLightWaveform':
        return _seal(
            cls, payload, 'waveform_id', 'waveform_sha256', 'tlw')


class LightingTLMObservation(BaseModel):
    """One observed temporal-light phenomenon bound to a waveform
    (tlmo- prefix). Modulation source (intrinsic vs mains-induced vs
    control-induced) stays explicit — optical flicker alone never
    diagnoses mains as the cause."""

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    waveform_ref: AuthorityRef
    phenomenon: TLAPhenomenon
    modulation_source: ModulationSource = 'undeclared'
    dominant_frequency_hz: float | None = None
    observed_modulation_depth_pct: float | None = None
    power_event_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'LightingTLMObservation':
        _require_refs(self.waveform_ref)
        if self.modulation_source == 'power_quality_induced' \
                and self.power_event_ref is None:
            raise ValueError(
                'power_quality_induced requires a #738 power-event ref')
        if self.power_event_ref is not None:
            _require_refs(self.power_event_ref)
        if self.dominant_frequency_hz is not None \
                and self.dominant_frequency_hz <= 0.0:
            raise ValueError('dominant_frequency_hz must be > 0')
        if self.observed_modulation_depth_pct is not None \
                and not 0.0 <= self.observed_modulation_depth_pct <= 100.0:
            raise ValueError(
                'observed_modulation_depth_pct must be 0..100')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'LightingTLMObservation':
        return _seal(
            cls, payload, 'observation_id',
            'observation_sha256', 'tlmo')


class LightingTLAAssessment(BaseModel):
    """A metric result bound to its method/version/applicability domain
    (tlaa- prefix). CIE 249:2022-Cor1 supersedes the 2022 original and
    its SVM/PAE caveats are carried, not dropped; IEEE 1789-2015 is
    Inactive-Reserved — historical citation only, never a current
    production requirement."""

    model_config = ConfigDict(frozen=True)

    assessment_id: str
    assessment_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    observation_refs: tuple[AuthorityRef, ...]
    metric_id: Literal[
        'PstLM', 'SVM', 'Mp', 'flicker_percent', 'flicker_index',
        'other',
    ]
    metric_source: MetricSource
    metric_version: str | None = None
    applicability_domain: ApplicabilityDomain
    value: float | None = None
    verdict: Literal[
        'within_domain_pass', 'within_domain_fail',
        'outside_profile_domain', 'limited_by_corrigendum',
        'insufficient_evidence',
    ]

    @model_validator(mode='after')
    def _validate(self) -> 'LightingTLAAssessment':
        if not self.observation_refs:
            raise ValueError('observation_refs must not be empty')
        for ref in self.observation_refs:
            _require_refs(ref)
        if self.metric_source == 'CIE 249:2022':
            raise ValueError(
                'CIE 249:2022 is superseded — cite CIE 249:2022-Cor1')
        if self.metric_source == 'IEEE 1789-2015' \
                and self.applicability_domain != 'historical_ieee1789':
            raise ValueError(
                'IEEE 1789-2015 is Inactive-Reserved — '
                'historical_ieee1789 domain only')
        if self.metric_source == 'IEC TR 63158:2018' \
                and self.applicability_domain != 'svm_indoor_gt100lx':
            raise ValueError(
                'IEC TR 63158 SVM carries the svm_indoor_gt100lx '
                'domain')
        if self.metric_id == 'Mp' \
                and self.applicability_domain != 'phantom_array_research':
            raise ValueError(
                'phantom-array Mp stays research-scoped per '
                'CIE 249:2022-Cor1')
        if self.verdict == 'limited_by_corrigendum' \
                and self.metric_source != 'CIE 249:2022-Cor1':
            raise ValueError(
                'limited_by_corrigendum verdict belongs to '
                'CIE 249:2022-Cor1 metrics')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'assessment_id', 'assessment_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'LightingTLAAssessment':
        return _seal(
            cls, payload, 'assessment_id',
            'assessment_sha256', 'tlaa')


def evaluate_tla_claim(
    profile: DimmingTemporalProfile | None,
    waveform: TemporalLightWaveform | None,
    observation: LightingTLMObservation | None,
    assessment: LightingTLAAssessment | None,
    photometry_ok: bool = False,
) -> tuple[str, str]:
    """Fail-closed temporal-light claim gate (#748).

    Average illuminance/colour compliance never implies temporal-light
    quality. A usable TLA claim needs the exact lighting state, a raw
    waveform, an instrument fast enough for the phenomenon, and a metric
    inside its applicability domain — a <100 lx theater scene places SVM
    outside IEC TR 63158's domain, and CIE 249:2022-Cor1 keeps SVM/Mp
    caveated.
    """
    if profile is None:
        if photometry_ok:
            return ('illuminance_is_not_tla',
                    'photometry_pass_without_temporal_profile')
        return ('insufficient_evidence', 'no_dimming_temporal_profile')
    if waveform is None:
        return ('insufficient_evidence', 'no_raw_waveform')
    if observation is not None \
            and observation.dominant_frequency_hz is not None \
            and observation.dominant_frequency_hz \
            > waveform.bandwidth_hz:
        return ('instrument_bandwidth_insufficient',
                'phenomenon_above_instrument_bandwidth')
    if assessment is None:
        return ('insufficient_evidence', 'no_metric_assessment')
    if assessment.metric_source == 'IEEE 1789-2015':
        return ('historical_reference_only',
                'ieee1789_inactive_reserved_2026_03_26')
    if assessment.applicability_domain == 'svm_indoor_gt100lx' \
            and waveform.illuminance_lx < _SVM_DOMAIN_MIN_ILLUMINANCE_LX:
        return ('outside_profile_domain',
                f'illuminance_below_100lx_svm_domain:'
                f'{waveform.illuminance_lx}')
    if assessment.metric_id == 'SVM' \
            and assessment.metric_source == 'CIE 249:2022-Cor1':
        return ('limited_by_corrigendum',
                'svm_method_needs_further_verification')
    if assessment.metric_id == 'Mp':
        return ('limited_by_corrigendum',
                'phantom_array_not_a_standardization_method')
    if assessment.verdict == 'outside_profile_domain':
        return ('outside_profile_domain',
                'assessment_verdict:outside_profile_domain')
    if assessment.verdict == 'limited_by_corrigendum':
        return ('limited_by_corrigendum',
                'assessment_verdict:limited_by_corrigendum')
    if assessment.verdict == 'insufficient_evidence':
        return ('insufficient_evidence',
                'assessment_verdict:insufficient_evidence')
    if observation is not None \
            and observation.modulation_source == 'power_quality_induced':
        return ('power_quality_induced',
                'modulation_bound_to_power_event_not_intrinsic')
    if observation is not None \
            and observation.modulation_source == 'control_dimmer_induced':
        return ('control_induced',
                'modulation_bound_to_control_state')
    if assessment.verdict == 'within_domain_fail':
        return ('tla_failed_within_domain', 'metric_failed_in_domain')
    return ('tla_within_domain',
            'metric_within_declared_domain:' + assessment.metric_id)
