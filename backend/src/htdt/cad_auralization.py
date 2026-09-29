"""Auralization render authority (#515, AUR10 slice).

Turns one exact predicted/measured impulse-response authority plus one exact
dry program asset into a reproducible listening artifact. The rendered audio
is a *derived* artifact — never new acoustic evidence, and a predicted IR
render never pretends to be measured evidence.

AUR10 is deliberately narrow: one mono dry program channel, one exact IR,
one receiver, one mono output. Multi-channel playback requires #505/#492
routing semantics; binaural/BRIR requires AUR40 two-ear transfer authority —
neither can be smuggled in through this contract.

Determinism contract: the semantic identity is the exact ``AuralizationRenderSpec``
hash plus the canonical float PCM hash; WAV container bytes are deterministic
as an implementation detail but are not the primary authority.
"""

from __future__ import annotations

from hashlib import sha256
import io
from math import isfinite, log10, sqrt
from typing import Any, Literal
import wave

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest, canonicalize_payload


AURALIZATION_SCHEMA_VERSION = 1
AURALIZATION_SPEC_AUTHORITY_VERSION = 'aur10-render-spec-1'
AURALIZATION_ARTIFACT_AUTHORITY_VERSION = 'aur10-render-artifact-1'
AURALIZATION_RENDERER_ID = 'htdt.aur10_offline_convolution'
AURALIZATION_RENDERER_VERSION = '1'
AURALIZATION_OUTPUT_FORMAT = 'wav_pcm_s16le'

ImpulseAuthorityKind = Literal['predicted', 'measured']
ResamplePolicy = Literal[
    'exact_rate_match_required', 'band_limited_resample'
]
GainPolicy = Literal[
    'preserve_physical_level',
    'level_matched_rms',
    'unity',
]
HeadroomPolicy = Literal['report_only', 'attenuate_to_fit']
LevelSemantics = Literal[
    'physical_level_preserved',
    'physical_level_attenuated_for_playback',
    'level_matched',
    'unity_relative',
]
# Versioned band-limited sample-rate conversion (#956): ideal periodic
# reconstruction via FFT spectrum repacking — never linear interpolation.
RESAMPLE_METHOD_BAND_LIMITED = 'htdt.fft_bandlimited_resample_v1'






def _finite(value: float, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


class DryProgramAssetRef(BaseModel):
    """Exact dry program input; the asset bytes are referenced by hash only."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    # Identity of the decoded float PCM the renderer must receive — distinct
    # from the source asset's byte hash (#1031).
    decoded_pcm_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    sample_rate_hz: int = Field(gt=0)
    channel_count: Literal[1] = 1
    sample_count: int = Field(gt=0)
    program_level_authority: Literal[
        'absolute_calibrated', 'unknown'
    ] = 'unknown'


class ImpulseAuthorityRef(BaseModel):
    """Exact predicted/measured impulse-response authority being rendered."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: ImpulseAuthorityKind
    artifact_id: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    # Identity of the decoded float IR the renderer must receive (#1031).
    decoded_pcm_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    sample_rate_hz: int = Field(gt=0)
    sample_count: int = Field(gt=0)
    channel_count: Literal[1] = 1
    absolute_amplitude_authority: bool
    transfer_matrix_path: Literal[
        'one_program_channel_x_one_source_x_one_receiver'
    ] = 'one_program_channel_x_one_source_x_one_receiver'


class AuralizationReceiverRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    receiver_id: str = Field(min_length=1)
    receiver_entity_id: str = Field(min_length=1)


class AuralizationRenderSpec(BaseModel):
    """Immutable auralization render request authority (AUR10 MVP)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = AURALIZATION_SCHEMA_VERSION
    authority_version: Literal[
        'aur10-render-spec-1'
    ] = AURALIZATION_SPEC_AUTHORITY_VERSION
    spec_id: str = Field(pattern=r'^auralization-render-spec:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    system_variant_id: str | None = Field(default=None, min_length=1)
    system_variant_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    source_scenario_id: str = Field(min_length=1)
    source_scenario_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    receiver: AuralizationReceiverRef
    impulse_authority: ImpulseAuthorityRef
    dry_source: DryProgramAssetRef

    output_sample_rate_hz: int = Field(gt=0)
    output_channel_layout: Literal['mono'] = 'mono'
    render_algorithm: Literal['fft_convolution'] = 'fft_convolution'
    render_algorithm_version: Literal[
        'aur10-offline-fft-v1'
    ] = 'aur10-offline-fft-v1'
    resample_policy: ResamplePolicy = 'exact_rate_match_required'
    gain_policy: GainPolicy
    rms_target_dbfs: float | None = None
    headroom_policy: HeadroomPolicy = 'report_only'
    render_mode: Literal['offline_deterministic'] = 'offline_deterministic'

    @field_validator('rms_target_dbfs')
    @classmethod
    def finite_rms(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='rms target')

    @model_validator(mode='after')
    def validate_spec(self) -> 'AuralizationRenderSpec':
        if (self.system_variant_id is None) != (self.system_variant_sha256 is None):
            raise ValueError('system variant id/hash must be supplied together')
        if self.impulse_authority.channel_count != 1:
            raise ValueError(
                'AUR10 renders exactly one impulse channel; a second output '
                'channel requires a second exact receiver transfer (AUR20)'
            )
        if self.dry_source.channel_count != 1:
            raise ValueError(
                'AUR10 renders one mono program channel; multi-channel program '
                'material requires #505/#492 routing semantics'
            )
        if self.gain_policy == 'level_matched_rms' and self.rms_target_dbfs is None:
            raise ValueError('level_matched_rms requires an explicit rms_target_dbfs')
        if self.gain_policy != 'level_matched_rms' and self.rms_target_dbfs is not None:
            raise ValueError('rms target only applies to level_matched_rms policy')
        if self.gain_policy == 'preserve_physical_level':
            if not self.impulse_authority.absolute_amplitude_authority:
                raise ValueError(
                    'preserve_physical_level requires an IR chain with absolute '
                    'amplitude authority; a normalized IR can only render a '
                    'relative/level-matched presentation'
                )
            if self.dry_source.program_level_authority != 'absolute_calibrated':
                raise ValueError(
                    'preserve_physical_level requires a calibrated program level; '
                    'an unknown mastering level cannot establish room SPL'
                )
            if self.headroom_policy not in (
                'report_only', 'attenuate_to_fit'
            ):
                raise ValueError(
                    'preserve_physical_level renders may only report headroom '
                    'or explicitly attenuate to fit'
                )
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('auralization render spec semantic hash mismatch')
        if self.spec_id != f'auralization-render-spec:{expected}':
            raise ValueError('auralization render spec id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'spec_id', 'semantic_sha256'},
        )


def build_auralization_render_spec(**kwargs: Any) -> AuralizationRenderSpec:
    """Build a spec letting the model validator pin identity from semantics."""
    probe = AuralizationRenderSpec.model_construct(**canonicalize_payload(AuralizationRenderSpec, dict(
        spec_id='auralization-render-spec:' + '0' * 64,
        semantic_sha256='0' * 64,
        **kwargs,
    )))
    digest = _digest(probe.semantic_payload())
    return AuralizationRenderSpec(
        spec_id=f'auralization-render-spec:{digest}',
        semantic_sha256=digest,
        **kwargs,
    )


class RenderedPcm(BaseModel):
    """Canonical float PCM output of one deterministic render."""

    model_config = ConfigDict(frozen=True, extra='forbid', arbitrary_types_allowed=True)

    sample_rate_hz: int = Field(gt=0)
    channel_count: Literal[1] = 1
    samples: tuple[float, ...] = Field(min_length=1)
    pcm_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    applied_gain_db: float
    # Headroom attenuation is persisted separately from the gain policy's
    # own gain so a physical-level claim is never silently broken (#1028).
    headroom_gain_db: float = 0.0
    peak_linear: float = Field(ge=0.0)
    clipped_sample_count: int = Field(ge=0)
    pre_headroom_peak_linear: float = Field(ge=0.0)
    pre_headroom_clipped_sample_count: int = Field(ge=0)
    headroom_dbfs: float | None = None
    level_semantics: LevelSemantics
    # SRC actually applied to any input; None means no conversion occurred.
    resample_method: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def validate_pcm(self) -> 'RenderedPcm':
        if any(not isfinite(float(value)) for value in self.samples):
            raise ValueError('rendered PCM samples must be finite')
        expected = sha256(
            np.asarray(self.samples, dtype='<f8').tobytes()
        ).hexdigest()
        if self.pcm_semantic_sha256 != expected:
            raise ValueError('rendered PCM semantic hash mismatch')
        if self.peak_linear != max(abs(float(v)) for v in self.samples):
            raise ValueError('rendered PCM peak does not match samples')
        if (
            self.headroom_gain_db < 0.0
            and self.level_semantics == 'physical_level_preserved'
        ):
            raise ValueError(
                'headroom attenuation can never report preserved physical '
                'level'
            )
        return self


def _resample_band_limited(
    samples: np.ndarray,
    source_rate: int,
    target_rate: int,
) -> np.ndarray:
    """Ideal band-limited sample-rate conversion (htdt.fft_bandlimited_resample_v1).

    Convention: the output preserves the input duration
    (``n_out = round(n_in * target_rate / source_rate)``), keeps the time
    origin aligned (input sample 0 maps to output time 0 — the direct
    arrival of an IR is never shifted), applies brick-wall anti-aliasing
    on downsample and sinc reconstruction on upsample, and treats the
    record as periodic at the edges (no tapering).
    """
    if source_rate == target_rate:
        return np.asarray(samples, dtype=np.float64).copy()
    samples = np.asarray(samples, dtype=np.float64)
    n_in = len(samples)
    n_out = max(1, int(round(n_in * target_rate / source_rate)))
    if n_out == n_in:
        return samples.copy()
    spectrum = np.fft.rfft(samples)
    bins_out = n_out // 2 + 1
    out = np.zeros(bins_out, dtype=np.complex128)
    if n_out > n_in:
        bins_in = n_in // 2 + 1
        out[:bins_in] = spectrum
        if n_in % 2 == 0:
            # Input Nyquist bin is single-sided real; as an interior output
            # bin its negative-frequency half is implied by the Hermitian
            # convention, so only half the coefficient is kept.
            out[bins_in - 1] = spectrum[bins_in - 1] / 2.0
    else:
        keep = n_out // 2
        out[:keep] = spectrum[:keep]
        if n_out % 2 == 0:
            # The output Nyquist bin is single-sided real; fold the
            # positive- and negative-frequency contributions at that
            # frequency into it.
            out[keep] = spectrum[keep] + np.conj(spectrum[keep])
        else:
            out[keep] = spectrum[keep]
    resampled = np.fft.irfft(out, n=n_out)
    return resampled * (n_out / n_in)


def render_auralization(
    spec: AuralizationRenderSpec,
    *,
    dry_samples: tuple[float, ...] | np.ndarray,
    impulse_samples: tuple[float, ...] | np.ndarray,
    dry_asset_sha256: str,
    impulse_artifact_sha256: str,
) -> RenderedPcm:
    """Deterministic offline mono convolution: dry * IR -> mono PCM out.

    The renderer never fabricates stereo/binaural output: AUR10's transfer
    matrix is exactly one program channel x one source x one receiver.
    """
    dry = np.asarray(dry_samples, dtype=np.float64)
    ir = np.asarray(impulse_samples, dtype=np.float64)
    if dry.ndim != 1 or ir.ndim != 1 or len(dry) == 0 or len(ir) == 0:
        raise ValueError('AUR10 render requires non-empty mono dry and IR samples')
    if not np.all(np.isfinite(dry)) or not np.all(np.isfinite(ir)):
        raise ValueError('dry/IR samples must be finite')
    # The caller-declared asset/artifact hashes must be the ones the spec
    # pinned — supplying samples for a different asset fails closed (#1031).
    if dry_asset_sha256 != spec.dry_source.asset_sha256:
        raise ValueError(
            'supplied dry asset hash is not the asset pinned by the spec'
        )
    if impulse_artifact_sha256 != spec.impulse_authority.artifact_sha256:
        raise ValueError(
            'supplied impulse artifact hash is not the artifact pinned by the spec'
        )
    if len(dry) != spec.dry_source.sample_count:
        raise ValueError('dry sample count does not match the pinned asset')
    if len(ir) != spec.impulse_authority.sample_count:
        raise ValueError('IR sample count does not match the pinned artifact')
    if (
        sha256(np.asarray(dry_samples, dtype='<f8').tobytes()).hexdigest()
        != spec.dry_source.decoded_pcm_sha256
    ):
        raise ValueError('dry samples do not match the pinned decoded PCM hash')
    if (
        sha256(np.asarray(impulse_samples, dtype='<f8').tobytes()).hexdigest()
        != spec.impulse_authority.decoded_pcm_sha256
    ):
        raise ValueError('IR samples do not match the pinned decoded PCM hash')

    resample_method: str | None = None
    if spec.resample_policy == 'exact_rate_match_required':
        if (
            spec.dry_source.sample_rate_hz != spec.output_sample_rate_hz
            or spec.impulse_authority.sample_rate_hz != spec.output_sample_rate_hz
        ):
            raise ValueError(
                'exact_rate_match_required: dry/IR/output sample rates must match'
            )
        dry_resampled = dry
        ir_resampled = ir
    else:
        if (
            spec.dry_source.sample_rate_hz != spec.output_sample_rate_hz
            or spec.impulse_authority.sample_rate_hz != spec.output_sample_rate_hz
        ):
            resample_method = RESAMPLE_METHOD_BAND_LIMITED
        dry_resampled = _resample_band_limited(
            dry, spec.dry_source.sample_rate_hz, spec.output_sample_rate_hz
        )
        ir_resampled = _resample_band_limited(
            ir, spec.impulse_authority.sample_rate_hz, spec.output_sample_rate_hz
        )

    count = len(dry_resampled) + len(ir_resampled) - 1
    nfft = 1 << (count - 1).bit_length()
    rendered = np.fft.irfft(
        np.fft.rfft(dry_resampled, nfft) * np.fft.rfft(ir_resampled, nfft),
        n=nfft,
    )[:count]

    applied_gain_db = 0.0
    if spec.gain_policy == 'level_matched_rms':
        rms = float(sqrt(float(np.mean(rendered ** 2)))) if count else 0.0
        if rms <= 0.0:
            raise ValueError('level_matched_rms cannot normalize a silent render')
        try:
            target_linear = 10.0 ** (float(spec.rms_target_dbfs) / 20.0)
        except OverflowError as exc:
            raise ValueError(
                'rms target dBFS out of representable range'
            ) from exc
        gain = target_linear / rms
        rendered = rendered * gain
        applied_gain_db = 20.0 * log10(gain)
    elif spec.gain_policy == 'unity':
        applied_gain_db = 0.0

    pre_headroom_peak = float(np.max(np.abs(rendered)))
    pre_headroom_clipped = int(np.count_nonzero(np.abs(rendered) > 1.0))
    clipped = pre_headroom_clipped
    headroom_gain_db = 0.0
    attenuated = False
    if spec.headroom_policy == 'attenuate_to_fit' and clipped:
        scale = 0.999 / pre_headroom_peak
        rendered = rendered * scale
        headroom_gain_db = 20.0 * log10(scale)
        applied_gain_db += headroom_gain_db
        clipped = int(np.count_nonzero(np.abs(rendered) > 1.0))
        attenuated = True
    if spec.gain_policy == 'preserve_physical_level':
        level_semantics: LevelSemantics = (
            'physical_level_attenuated_for_playback'
            if attenuated
            else 'physical_level_preserved'
        )
    elif spec.gain_policy == 'level_matched_rms':
        level_semantics = 'level_matched'
    else:
        level_semantics = 'unity_relative'

    peak = float(np.max(np.abs(rendered)))
    headroom_dbfs = -20.0 * log10(peak) if peak > 0.0 else None
    samples = tuple(float(v) for v in rendered)
    pcm_hash = sha256(np.asarray(samples, dtype='<f8').tobytes()).hexdigest()
    return RenderedPcm(
        sample_rate_hz=spec.output_sample_rate_hz,
        channel_count=1,
        samples=samples,
        pcm_semantic_sha256=pcm_hash,
        applied_gain_db=applied_gain_db,
        headroom_gain_db=headroom_gain_db,
        peak_linear=peak,
        clipped_sample_count=clipped,
        pre_headroom_peak_linear=pre_headroom_peak,
        pre_headroom_clipped_sample_count=pre_headroom_clipped,
        headroom_dbfs=headroom_dbfs,
        level_semantics=level_semantics,
        resample_method=resample_method,
    )


def encode_wav_pcm_s16le(pcm: RenderedPcm) -> bytes:
    """Deterministic minimal WAV container (PCM s16le, no optional chunks)."""
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(pcm.sample_rate_hz)
        quantized = np.clip(
            np.round(np.asarray(pcm.samples) * 32767.0), -32768, 32767
        ).astype('<i2')
        handle.writeframes(quantized.tobytes())
    return buffer.getvalue()


class AuralizationArtifact(BaseModel):
    """Persisted derived render artifact; derived/cache data, not evidence."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = AURALIZATION_SCHEMA_VERSION
    authority_version: Literal[
        'aur10-render-artifact-1'
    ] = AURALIZATION_ARTIFACT_AUTHORITY_VERSION
    artifact_id: str = Field(pattern=r'^auralization-artifact:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    spec_id: str = Field(pattern=r'^auralization-render-spec:[0-9a-f]{64}$')
    spec_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    ir_kind: ImpulseAuthorityKind
    output_format: Literal['wav_pcm_s16le'] = AURALIZATION_OUTPUT_FORMAT
    output_asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    pcm_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    duration_s: float = Field(gt=0.0)
    sample_rate_hz: int = Field(gt=0)
    channel_count: Literal[1] = 1
    peak_linear: float = Field(ge=0.0)
    clipped_sample_count: int = Field(ge=0)
    pre_headroom_peak_linear: float = Field(ge=0.0)
    pre_headroom_clipped_sample_count: int = Field(ge=0)
    headroom_dbfs: float | None = None
    applied_gain_db: float
    headroom_gain_db: float = 0.0
    level_semantics: LevelSemantics
    resample_method: str | None = Field(default=None, min_length=1)
    dry_asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    dry_decoded_pcm_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    impulse_artifact_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    impulse_decoded_pcm_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    renderer_id: Literal[
        'htdt.aur10_offline_convolution'
    ] = AURALIZATION_RENDERER_ID
    renderer_version: Literal['1'] = AURALIZATION_RENDERER_VERSION

    @model_validator(mode='after')
    def validate_artifact(self) -> 'AuralizationArtifact':
        _finite(self.duration_s, field_name='duration')
        _finite(self.applied_gain_db, field_name='applied gain')
        _finite(self.headroom_gain_db, field_name='headroom gain')
        if self.headroom_dbfs is not None:
            _finite(self.headroom_dbfs, field_name='headroom')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('auralization artifact semantic hash mismatch')
        if self.artifact_id != f'auralization-artifact:{expected}':
            raise ValueError('auralization artifact id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'artifact_id', 'semantic_sha256'},
        )


def build_auralization_artifact(
    spec: AuralizationRenderSpec,
    pcm: RenderedPcm,
) -> tuple[AuralizationArtifact, bytes]:
    """Materialize the immutable artifact + deterministic WAV bytes."""
    wav = encode_wav_pcm_s16le(pcm)
    payload = {
        'schema_version': AURALIZATION_SCHEMA_VERSION,
        'authority_version': AURALIZATION_ARTIFACT_AUTHORITY_VERSION,
        'spec_id': spec.spec_id,
        'spec_semantic_sha256': spec.semantic_sha256,
        'ir_kind': spec.impulse_authority.kind,
        'output_format': AURALIZATION_OUTPUT_FORMAT,
        'output_asset_sha256': sha256(wav).hexdigest(),
        'pcm_semantic_sha256': pcm.pcm_semantic_sha256,
        'duration_s': len(pcm.samples) / float(pcm.sample_rate_hz),
        'sample_rate_hz': pcm.sample_rate_hz,
        'channel_count': 1,
        'peak_linear': pcm.peak_linear,
        'clipped_sample_count': pcm.clipped_sample_count,
        'pre_headroom_peak_linear': pcm.pre_headroom_peak_linear,
        'pre_headroom_clipped_sample_count': (
            pcm.pre_headroom_clipped_sample_count
        ),
        'headroom_dbfs': pcm.headroom_dbfs,
        'applied_gain_db': pcm.applied_gain_db,
        'headroom_gain_db': pcm.headroom_gain_db,
        'level_semantics': pcm.level_semantics,
        'resample_method': pcm.resample_method,
        'dry_asset_sha256': spec.dry_source.asset_sha256,
        'dry_decoded_pcm_sha256': spec.dry_source.decoded_pcm_sha256,
        'impulse_artifact_sha256': spec.impulse_authority.artifact_sha256,
        'impulse_decoded_pcm_sha256': (
            spec.impulse_authority.decoded_pcm_sha256
        ),
        'renderer_id': AURALIZATION_RENDERER_ID,
        'renderer_version': AURALIZATION_RENDERER_VERSION,
    }
    digest = _digest(payload)
    artifact = AuralizationArtifact(
        spec_id=spec.spec_id,
        spec_semantic_sha256=spec.semantic_sha256,
        ir_kind=spec.impulse_authority.kind,
        output_asset_sha256=sha256(wav).hexdigest(),
        pcm_semantic_sha256=pcm.pcm_semantic_sha256,
        duration_s=len(pcm.samples) / float(pcm.sample_rate_hz),
        sample_rate_hz=pcm.sample_rate_hz,
        peak_linear=pcm.peak_linear,
        clipped_sample_count=pcm.clipped_sample_count,
        pre_headroom_peak_linear=pcm.pre_headroom_peak_linear,
        pre_headroom_clipped_sample_count=(
            pcm.pre_headroom_clipped_sample_count
        ),
        headroom_dbfs=pcm.headroom_dbfs,
        applied_gain_db=pcm.applied_gain_db,
        headroom_gain_db=pcm.headroom_gain_db,
        level_semantics=pcm.level_semantics,
        resample_method=pcm.resample_method,
        dry_asset_sha256=spec.dry_source.asset_sha256,
        dry_decoded_pcm_sha256=spec.dry_source.decoded_pcm_sha256,
        impulse_artifact_sha256=spec.impulse_authority.artifact_sha256,
        impulse_decoded_pcm_sha256=(
            spec.impulse_authority.decoded_pcm_sha256
        ),
        artifact_id=f'auralization-artifact:{digest}',
        semantic_sha256=digest,
    )
    return artifact, wav


ArtifactCurrency = Literal['CURRENT', 'STALE']


class AuralizationArtifactCurrency(BaseModel):
    """Stale view of one artifact against its current upstream authorities."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    artifact_id: str = Field(min_length=1)
    state: ArtifactCurrency
    stale_reasons: tuple[str, ...]

    @model_validator(mode='after')
    def validate_state(self) -> 'AuralizationArtifactCurrency':
        if self.state == 'CURRENT' and self.stale_reasons:
            raise ValueError('CURRENT artifact cannot carry stale reasons')
        if self.state == 'STALE' and not self.stale_reasons:
            raise ValueError('STALE artifact requires reasons')
        return self


def assess_auralization_currency(
    artifact: AuralizationArtifact,
    spec: AuralizationRenderSpec,
    *,
    current_scene_content_hash: str,
    current_system_variant_sha256: str | None,
    current_ir_artifact_sha256: str,
    current_dry_asset_sha256: str,
    current_source_scenario_sha256: str,
) -> AuralizationArtifactCurrency:
    """A render is stale whenever any pinned upstream authority changed."""
    if artifact.spec_semantic_sha256 != spec.semantic_sha256:
        raise ValueError('artifact does not belong to the supplied spec')
    reasons: list[str] = []
    if spec.scene_content_hash != current_scene_content_hash:
        reasons.append('SceneRevision changed')
    if spec.system_variant_sha256 != current_system_variant_sha256:
        reasons.append('SystemVariant changed')
    if spec.impulse_authority.artifact_sha256 != current_ir_artifact_sha256:
        reasons.append('impulse-response authority changed')
    if spec.dry_source.asset_sha256 != current_dry_asset_sha256:
        reasons.append('dry program asset changed')
    if spec.source_scenario_sha256 != current_source_scenario_sha256:
        reasons.append('source/channel scenario changed')
    return AuralizationArtifactCurrency(
        artifact_id=artifact.artifact_id,
        state='STALE' if reasons else 'CURRENT',
        stale_reasons=tuple(dict.fromkeys(reasons)),
    )
