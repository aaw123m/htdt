"""Adaptive acoustic identification authority (issue #661).

Arbitrary-programme / noise system identification (e.g. FSAF-style
adaptive filtering over music or noise) is not an ordinary sweep
measurement with a nicer stimulus: the estimated linear transfer and
the residual depend on the estimator identity, excitation spectrum,
clock relationship, convergence and model error. These records pin
method, stimulus realization, convergence and residual decomposition
so an adaptive result cannot be silently equated to calibrated
sweep evidence.

Basis: REW 5.40 FSAF documentation (clock sensitivity, input-file
defects raising residual/TD+N, non-interchangeability with swept-sine
THD); IEC 60268-21:2018 (arbitrary analogue/digital input to acoustic
output, small- and large-signal domains); AES75-2023 (crest-factor /
spectrum / level semantics for programme-like noise); public FSAF
reference material (Tsiroulnikov/Zrull).
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


AdaptiveMethod = Literal[
    'fsaf_external_or_compatible_profile',
    'subband_adaptive_identifier',
    'frequency_domain_adaptive_identifier',
    'wiener_least_squares_identifier',
    'dual_channel_transfer_estimate',
    'custom_validated_method',
    'external_imported_result',
    'unknown',
]

LicenseReviewStatus = Literal[
    'reviewed_permitted',
    'review_pending',
    'restricted',
    'external_import_only',
    'unknown',
]

StimulusKind = Literal[
    'file_segment',
    'generated_noise',
    'programme_stream',
    'external_imported',
    'unknown',
]

StimulusDefectState = Literal[
    'stimulus_validated',
    'stimulus_clipped',
    'stimulus_band_insufficient',
    'stimulus_lossy',
    'stimulus_unknown',
]

ClockQualification = Literal[
    'common_clock',
    'digitally_locked',
    'async_corrected',
    'async_uncorrected',
    'unknown',
]

ConvergenceState = Literal[
    'converged',
    'partially_converged',
    'not_converged',
    'system_changed_during_adaptation',
    'insufficient_duration',
    'unknown',
]

ExcitationBandState = Literal[
    'identified',
    'insufficient_excitation',
    'low_confidence',
]

ResidualComponentKind = Literal[
    'adaptive_model_residual',
    'noise_contribution',
    'nonlinear_contribution',
    'unmodelled_linear_time_variation',
    'estimator_model_error',
]

DeclaredQuantity = Literal[
    'adaptive_residual_tdn_like',
    'noise_only',
    'nonlinear_only',
    'mixed_unresolved',
]


class ExcitationSupportBand(BaseModel):
    """Per-band excitation/observability record (#661 §5)."""

    model_config = ConfigDict(frozen=True)

    band_hz: tuple[float, float]
    state: ExcitationBandState
    integrated_energy: float | None = None
    usable_snr_db: float | None = None
    excitation_duration_s: float | None = None


class ResidualComponent(BaseModel):
    """One decomposed residual contribution (#661 §8)."""

    model_config = ConfigDict(frozen=True)

    kind: ResidualComponentKind
    magnitude_db: float | None = None
    band_hz: tuple[float, float] | None = None


class AdaptiveIdentificationProfile(BaseModel):
    """Declared adaptive system-identification method (#661 §1).

    'unknown' method or license status fails closed: the estimator
    identity and its rights status are part of the result identity.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    method: AdaptiveMethod
    method_reference: str
    implementation_version: str | None = None
    estimator_parameters: str | None = None
    regularization: str | None = None
    model_length_samples: int | None = None
    subband_config: str | None = None
    convergence_rule: str | None = None
    latency_alignment_strategy: str | None = None
    tool_identity: str | None = None
    license_review_status: LicenseReviewStatus

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('method') == 'unknown':
                raise ValueError(
                    'an adaptive identification profile must declare '
                    'its exact method — adaptive = true is not a method'
                )
            if data.get('license_review_status') in (
                None, 'unknown', 'review_pending'
            ):
                raise ValueError(
                    'algorithm/library license/IP review status must '
                    'be resolved before the method is registered '
                    '(external_imported_result may use '
                    'external_import_only)'
                )
            if (
                data.get('method') != 'external_imported_result'
                and data.get('license_review_status')
                == 'external_import_only'
            ):
                raise ValueError(
                    'a native method cannot claim external_import_only '
                    'license status'
                )
            if not data.get('method_reference'):
                raise ValueError(
                    'the method requires an exact algorithm/reference '
                    'identity'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'AdaptiveIdentificationProfile':
        return _seal(cls, payload, 'profile_id', 'profile_sha256', 'aam')


class ArbitraryStimulusMeasurement(BaseModel):
    """Exact arbitrary-stimulus identity (#661 §2/§3).

    A track title is not a stimulus identity: file/programme stimuli
    pin the content hash and the selected segment; generated noise pins
    the realization/seed. Source defects (clipping, lossy codec,
    insufficient band energy) become measurement-input defects rather
    than DUT distortion.
    """

    model_config = ConfigDict(frozen=True)

    stimulus_id: str
    stimulus_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    stimulus_kind: StimulusKind
    defect_state: StimulusDefectState
    asset_ref: AuthorityRef | None = None
    asset_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    realization_seed: str | None = None
    sample_rate_hz: float | None = None
    bit_depth: int | None = None
    segment_start_s: float | None = None
    segment_end_s: float | None = None
    channel_mapping: str | None = None
    peak_level_dbfs: float | None = None
    rms_level_dbfs: float | None = None
    crest_factor_db: float | None = None
    spectral_support_hz: tuple[tuple[float, float], ...] | None = None
    normalization: str | None = None
    preprocessing: str | None = None
    playback_gain_db: float | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('stimulus_kind') == 'unknown':
                raise ValueError(
                    'the stimulus kind must be declared — an unnamed '
                    'track is not a stimulus identity'
                )
            if (
                data.get('stimulus_kind') == 'file_segment'
                and not data.get('asset_sha256')
            ):
                raise ValueError(
                    'a file-segment stimulus requires the asset content '
                    'hash plus the selected segment'
                )
            if (
                data.get('stimulus_kind') == 'file_segment'
                and (
                    data.get('segment_start_s') is None
                    or data.get('segment_end_s') is None
                )
            ):
                raise ValueError(
                    'a file-segment stimulus requires segment start/end'
                )
            if (
                data.get('stimulus_kind') == 'generated_noise'
                and not data.get('realization_seed')
            ):
                raise ValueError(
                    'a generated-noise stimulus requires the exact '
                    'realization/seed'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'stimulus_id', 'stimulus_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'ArbitraryStimulusMeasurement':
        return _seal(
            cls, payload, 'stimulus_id', 'stimulus_sha256', 'asm'
        )


class AdaptiveTransferEstimate(BaseModel):
    """One adaptive transfer estimate with its evidence gates (#661).

    Clock qualification (#609) and convergence state are part of the
    estimate identity: a pretty curve before convergence is not
    production evidence, and phase/timing capability is denied under
    uncorrected asynchronous clocks.
    """

    model_config = ConfigDict(frozen=True)

    estimate_id: str
    estimate_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    stimulus_ref: AuthorityRef
    clock_qualification: ClockQualification
    convergence_state: ConvergenceState
    excitation_support: tuple[ExcitationSupportBand, ...]
    rate_error_ppm: float | None = None
    correction_method: str | None = None
    residual_timebase_uncertainty: str | None = None
    adaptation_interval_s: float | None = None
    model_length_samples: int | None = None
    room_decay_window_s: float | None = None
    stationarity_ref: AuthorityRef | None = None
    phase_timing_capability: Literal[
        'supported', 'limited', 'unsupported'
    ] = 'unsupported'

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('profile_ref') is None:
                raise ValueError(
                    'an adaptive estimate requires its identification '
                    'profile'
                )
            if data.get('stimulus_ref') is None:
                raise ValueError(
                    'an adaptive estimate requires its exact stimulus '
                    'record'
                )
            if not data.get('excitation_support'):
                raise ValueError(
                    'excitation support must be recorded per band — no '
                    'silent 20 Hz–20 kHz extrapolation'
                )
            if (
                data.get('convergence_state')
                == 'system_changed_during_adaptation'
                and data.get('stationarity_ref') is None
            ):
                raise ValueError(
                    'a system change during adaptation requires the '
                    'state evidence showing it'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'estimate_id', 'estimate_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'AdaptiveTransferEstimate':
        return _seal(
            cls, payload, 'estimate_id', 'estimate_sha256', 'ate'
        )


class ResidualEvidence(BaseModel):
    """Decomposed adaptive residual (#661 §8/§9).

    A residual is not automatically loudspeaker distortion: noise,
    nonlinearity, unmodelled linear time-variation and estimator error
    stay distinct, and an adaptive TD+N-like quantity is never equated
    to swept-sine harmonic THD (#192 taxonomy).
    """

    model_config = ConfigDict(frozen=True)

    residual_id: str
    residual_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    estimate_ref: AuthorityRef
    declared_quantity: DeclaredQuantity
    components: tuple[ResidualComponent, ...]
    comparable_to_sweep_thd: bool = False

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('estimate_ref') is None:
                raise ValueError(
                    'residual evidence requires its parent estimate'
                )
            if not data.get('components'):
                raise ValueError(
                    'the residual must be decomposed — one opaque '
                    'distortion number is not evidence'
                )
            if (
                data.get('declared_quantity')
                == 'adaptive_residual_tdn_like'
                and data.get('comparable_to_sweep_thd')
            ):
                raise ValueError(
                    'an adaptive TD+N-like residual is not '
                    'interchangeable with swept-sine harmonic THD'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'residual_id', 'residual_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'ResidualEvidence':
        return _seal(
            cls, payload, 'residual_id', 'residual_sha256', 'are'
        )


AdaptiveClaimVerdict = Literal[
    'qualified_adaptive_estimate',
    'unqualified_method',
    'stimulus_defect_limited',
    'clock_limited',
    'stationarity_violated',
    'not_converged',
    'band_limited_estimate',
    'no_estimate',
]


def evaluate_adaptive_claim(
    profile: AdaptiveIdentificationProfile | None,
    stimulus: ArbitraryStimulusMeasurement | None,
    estimate: AdaptiveTransferEstimate | None,
) -> tuple[AdaptiveClaimVerdict, str]:
    """Judge whether an adaptive estimate is usable evidence (#661)."""
    if estimate is None:
        return (
            'no_estimate',
            'no adaptive estimate record — nothing to qualify',
        )
    if profile is None:
        return (
            'unqualified_method',
            'no identification profile — estimator identity unknown',
        )
    if estimate.convergence_state == 'system_changed_during_adaptation':
        return (
            'stationarity_violated',
            'the physical/system state changed during adaptation — a '
            'fixed transfer claim is invalid',
        )
    if estimate.convergence_state in (
        'not_converged', 'insufficient_duration', 'unknown',
    ):
        return (
            'not_converged',
            'the adaptive model did not demonstrably converge',
        )
    if estimate.clock_qualification in (
        'async_uncorrected', 'unknown',
    ):
        return (
            'clock_limited',
            'replay/capture clock evidence is insufficient — '
            'phase/timing-capable claims are denied',
        )
    if stimulus is None or stimulus.defect_state in (
        'stimulus_clipped', 'stimulus_lossy', 'stimulus_unknown',
    ):
        return (
            'stimulus_defect_limited',
            'the stimulus carries clipping/lossy/unknown defects — '
            'residual defects must not be attributed to the DUT',
        )
    if any(
        band.state != 'identified' for band in estimate.excitation_support
    ):
        return (
            'band_limited_estimate',
            'at least one band lacks excitation or confidence — the '
            'estimate is partial, not a full-range trace',
        )
    return (
        'qualified_adaptive_estimate',
        'method-, clock- and convergence-qualified adaptive estimate '
        'bound to its exact stimulus',
    )


ADAPTIVE_CLAIM_LABELS: dict[str, str] = {
    'qualified_adaptive_estimate': '適格適応推定',
    'unqualified_method': '推定手法未適格',
    'stimulus_defect_limited': '刺激欠損限定',
    'clock_limited': 'クロック限定',
    'stationarity_violated': '定常性違反',
    'not_converged': '未収束',
    'band_limited_estimate': '帯域限定推定',
    'no_estimate': '推定なし',
}
