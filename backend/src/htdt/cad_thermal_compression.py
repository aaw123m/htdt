"""Sustained-output / thermal-compression authority (issue #731).

A loudspeaker/subwoofer/amplifier can pass a short sweep or burst and
still lose output, change response, engage protection or increase
distortion after sustained program exposure — thermal state and limiter
history are time dependent. One undifferentiated ``max_spl`` number is
not a capability claim.

Basis: AES75-2023 (standardized maximum-linear-output method using
Music-Noise — level increases until a stop condition on transfer-
function magnitude/coherence change; the exact revision/profile pins
the measurement), Button, JAES 40(1/2) 1992 (heat dissipation and
power compression in moving-coil loudspeakers), IEC 60268-21:2018
(large-signal measurements retain test duration/operating state where
thermal/history effects are material).
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


OutputCapabilityClass = Literal[
    'small_signal_reference',
    'short_burst_peak',
    'standard_max_linear_output',
    'sustained_output',
    'thermally_stabilized_output',
    'repeated_burst_capability',
    'recovery_capability',
    'protection_limited_output',
    'unknown',
]
InitialThermalState = Literal[
    'cold_start',
    'normal_warmed_operation',
    'preconditioned',
    'heat_soaked',
    'recovering',
    'unknown',
]
CompressionCause = Literal[
    'voice_coil_thermal',
    'amplifier_thermal_limiting',
    'dsp_limiter',
    'excursion_protection',
    'power_supply_sag',
    'powered_sub_protection',
    'unknown_combined',
    'unknown',
]
RecoveryState = Literal[
    'fully_recovered',
    'partially_recovered',
    'recovery_not_observed',
    'permanent_change_suspected',
    'unknown',
]
SustainedVerdict = Literal[
    'sustained_verified',
    'burst_only_evidence',
    'protection_limited',
    'measurement_chain_limited',
    'insufficient_evidence',
]

CAPABILITY_CLASS_LABELS: dict[str, str] = {
    'small_signal_reference': '小信号基準',
    'short_burst_peak': '短時間バーストピーク',
    'standard_max_linear_output': '規格最大線形出力（AES75 等）',
    'sustained_output': '持続出力',
    'thermally_stabilized_output': '熱平衡後出力',
    'repeated_burst_capability': '反復バースト能力',
    'recovery_capability': '回復能力',
    'protection_limited_output': 'プロテクション制限出力',
    'unknown': '不明',
}
THERMAL_STATE_LABELS: dict[str, str] = {
    'cold_start': 'コールドスタート',
    'normal_warmed_operation': '通常暖機状態',
    'preconditioned': 'プレコンディション済み',
    'heat_soaked': 'ヒートソーク済み',
    'recovering': '回復中',
    'unknown': '不明',
}
CAUSE_LABELS: dict[str, str] = {
    'voice_coil_thermal': 'ボイスコイル熱圧縮',
    'amplifier_thermal_limiting': 'アンプ熱制限',
    'dsp_limiter': 'DSP リミッタ',
    'excursion_protection': 'エクスカーション保護',
    'power_supply_sag': '電源サグ/電流制限',
    'powered_sub_protection': 'パワードサブ保護',
    'unknown_combined': '複合（原因不明）',
    'unknown': '不明',
}
RECOVERY_LABELS: dict[str, str] = {
    'fully_recovered': '完全回復',
    'partially_recovered': '一部回復',
    'recovery_not_observed': '回復未観測',
    'permanent_change_suspected': '永続変化の疑い',
    'unknown': '不明',
}
VERDICT_LABELS: dict[str, str] = {
    'sustained_verified': '持続出力検証済み',
    'burst_only_evidence': 'バースト証拠のみ',
    'protection_limited': 'プロテクション制限',
    'measurement_chain_limited': '測定チェーン制限',
    'insufficient_evidence': '証拠不足',
}

_SUSTAINED_CLASSES = {
    'sustained_output',
    'thermally_stabilized_output',
    'repeated_burst_capability',
}
_PROTECTION_CAUSES = {
    'amplifier_thermal_limiting',
    'dsp_limiter',
    'excursion_protection',
    'power_supply_sag',
    'powered_sub_protection',
}


class CompressionSample(BaseModel):
    """One output-loss observation at an elapsed time (optionally per
    band — compression is frequency dependent, not one scalar)."""

    model_config = ConfigDict(frozen=True)

    elapsed_s: float = Field(ge=0)
    loss_db: float
    band_low_hz: float | None = Field(default=None, gt=0)
    band_high_hz: float | None = Field(default=None, gt=0)
    distortion_change_db: float | None = None
    coherence_change: float | None = Field(default=None, ge=0, le=1)


class SustainedOutputTest(BaseModel):
    """One sustained/stress test with exact stimulus and duty history.

    The same nominal SPL reached with different crest factor, duty
    cycle or prior stress history is not the same thermal test — all of
    it is identity. ``standard_max_linear_output`` requires the exact
    standard/profile reference (e.g. AES75-2023 via #599).
    """

    model_config = ConfigDict(frozen=True)

    test_id: str
    test_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    device_ref: AuthorityRef
    capability_class: OutputCapabilityClass = 'unknown'
    stimulus_ref: AuthorityRef | None = None
    duration_s: float | None = Field(default=None, gt=0)
    duty_cycle: float | None = Field(default=None, gt=0, le=1)
    crest_factor_db: float | None = Field(default=None, ge=0)
    initial_thermal_state: InitialThermalState = 'unknown'
    ambient_c: float | None = None
    active_channel_count: int | None = Field(default=None, ge=1)
    load_ref: AuthorityRef | None = None
    standard_ref: AuthorityRef | None = None
    prior_stress_ref: AuthorityRef | None = None
    installation_state_ref: AuthorityRef | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'SustainedOutputTest':
        _require_refs(self.device_ref)
        for ref in (
            self.stimulus_ref,
            self.load_ref,
            self.standard_ref,
            self.prior_stress_ref,
            self.installation_state_ref,
        ):
            if ref is not None:
                _require_refs(ref)
        if (
            self.capability_class == 'standard_max_linear_output'
            and self.standard_ref is None
        ):
            raise ValueError(
                'a standardized maximum-linear-output claim must pin '
                'the exact method/profile revision (e.g. AES75-2023)'
            )
        if (
            self.capability_class in _SUSTAINED_CLASSES
            and self.duration_s is None
        ):
            raise ValueError(
                'a sustained/repeated capability claim requires its '
                'test duration'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'test_id', 'test_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'SustainedOutputTest':
        return _seal(cls, kwargs, 'test_id', 'test_sha256', 'sout')


class ThermalCompressionObservation(BaseModel):
    """Time/frequency-resolved output loss under stress.

    The actual series is kept before any reduction to one
    ``compression dB`` scalar. ``measurement_chain_overload`` composes
    with #695: a clipping microphone/ADC must never be misread as DUT
    compression — it blocks attribution instead.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    test_ref: AuthorityRef
    samples: tuple[CompressionSample, ...] = ()
    plateau_reached: bool | None = None
    suspected_cause: CompressionCause = 'unknown'
    telemetry_refs: tuple[AuthorityRef, ...] = ()
    measurement_chain_overload: bool = False
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'ThermalCompressionObservation':
        _require_refs(self.test_ref)
        for ref in self.telemetry_refs:
            _require_refs(ref)
        for sample in self.samples:
            if (
                (sample.band_low_hz is None)
                != (sample.band_high_hz is None)
            ):
                raise ValueError(
                    'a band-limited sample needs both band edges'
                )
            if (
                sample.band_low_hz is not None
                and sample.band_high_hz is not None
                and sample.band_high_hz <= sample.band_low_hz
            ):
                raise ValueError('band_high_hz must exceed band_low_hz')
        if (
            self.measurement_chain_overload
            and self.suspected_cause not in ('unknown', 'unknown_combined')
        ):
            raise ValueError(
                'DUT compression cannot be attributed while the '
                'measurement chain itself overloaded'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'},
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'ThermalCompressionObservation':
        return _seal(
            cls, kwargs, 'observation_id', 'observation_sha256', 'tcmp'
        )


class RecoveryProfile(BaseModel):
    """Post-stress recovery evidence.

    ``recovery_not_observed`` means the rest interval was too short to
    observe recovery — it never by itself asserts
    ``permanent_change_suspected`` (device damage is a separate claim
    requiring positive evidence).
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    test_ref: AuthorityRef
    recovery_state: RecoveryState = 'unknown'
    rest_intervals_s: tuple[float, ...] = ()
    post_stress_delta_db: float | None = None
    baseline_within_db: float | None = Field(default=None, ge=0)
    health_baseline_ref: AuthorityRef | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'RecoveryProfile':
        _require_refs(self.test_ref)
        if self.health_baseline_ref is not None:
            _require_refs(self.health_baseline_ref)
        if (
            self.recovery_state == 'fully_recovered'
            and self.baseline_within_db is None
        ):
            raise ValueError(
                'fully_recovered requires the measured post-stress '
                'delta to baseline'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'RecoveryProfile':
        return _seal(cls, kwargs, 'profile_id', 'profile_sha256', 'rcvp')


def claim_output_capability(
    test: SustainedOutputTest | None,
    observation: ThermalCompressionObservation | None,
    claimed: OutputCapabilityClass,
) -> tuple[SustainedVerdict, tuple[str, ...]]:
    """Fail-closed gate between evidence and a claimed capability class.

    A ``short_burst_peak`` or ``small_signal_reference`` test can never
    substantiate ``sustained_output`` / ``thermally_stabilized_output``
    — the claim stays ``burst_only_evidence``. Where protection was the
    observed limiting cause the verdict is ``protection_limited``; a
    measurement-chain overload blocks the DUT verdict entirely.
    """
    if test is None:
        return 'insufficient_evidence', ('no_test',)
    if observation is not None and observation.measurement_chain_overload:
        return 'measurement_chain_limited', (
            'measurement_chain_overload',
        )
    if observation is not None and (
        observation.suspected_cause in _PROTECTION_CAUSES
    ):
        return 'protection_limited', ('protection_limited_output',)
    if claimed in _SUSTAINED_CLASSES:
        if test.capability_class not in _SUSTAINED_CLASSES:
            return 'burst_only_evidence', (
                'claimed_sustained_from_burst',
            )
        if observation is None or not observation.samples:
            return 'insufficient_evidence', (
                'no_compression_evidence',
            )
        return 'sustained_verified', ()
    if test.capability_class == 'unknown':
        return 'insufficient_evidence', ('capability_unknown',)
    return 'sustained_verified', ()
