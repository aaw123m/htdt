"""Spectral-estimator / FFT-analysis authority (issue #749).

A dense FFT plot or small frequency-bin spacing does NOT by itself
prove high resolving power or accurate amplitude estimation. True
resolution is set by the analysis window and record length; zero
padding interpolates the display without adding information. The
estimator authority separates declared parameters (window, record
length, overlap, averaging) from derived cosmetic properties (bin
spacing after padding) so resolution claims stay fail-closed.

Basis: issue #749 scope; Harris 1978 (windows); #575 transformation
authority; #663 live transfer/coherence; #697 deconvolution; #706
decay waterfall.
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


WindowKind = Literal[
    'rectangular', 'hann', 'hamming', 'blackman', 'blackman_harris',
    'flattop', 'kaiser', 'exponential', 'other', 'unknown',
]

RESOLUTION_LABELS: dict[str, str] = {
    'resolution_declared': '分解能は宣言済み',
    'padded_display_not_resolution': 'ゼロ詰め表示は分解能ではない',
    'unknown_window_unresolvable': '窓不明では分解能を語れない',
    'insufficient_evidence': '証拠不足',
}


class SpectralEstimatorProfile(BaseModel):
    """Declared FFT/estimator parameters (sep- prefix)."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    estimator_kind: str
    window_kind: WindowKind
    record_length_s: float | None = None
    fft_size: int | None = None
    zero_padding_factor: float = 1.0
    overlap_fraction: float | None = None
    averaging: str | None = None
    sample_rate_hz: float | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'SpectralEstimatorProfile':
        if not self.estimator_kind:
            raise ValueError('estimator_kind must be declared')
        if self.fft_size is not None and self.fft_size <= 0:
            raise ValueError('fft_size must be positive')
        if self.zero_padding_factor < 1.0:
            raise ValueError('zero_padding_factor must be >= 1')
        if self.record_length_s is not None \
                and self.record_length_s <= 0.0:
            raise ValueError('record_length_s must be positive')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'SpectralEstimatorProfile':
        return _seal(cls, payload, 'profile_id', 'profile_sha256', 'sep')

    def displayed_bin_spacing_hz(self) -> float | None:
        """Cosmetic bin spacing — includes zero padding (display only)."""
        if self.sample_rate_hz is None or self.fft_size is None:
            return None
        return self.sample_rate_hz / (
            self.fft_size * self.zero_padding_factor)

    def true_resolution_hz(self) -> float | None:
        """Record-length bound on resolution (window-dependent floor)."""
        if self.record_length_s is not None:
            return 1.0 / self.record_length_s
        if self.sample_rate_hz is not None and self.fft_size is not None:
            return self.sample_rate_hz / self.fft_size
        return None


class SpectralResolutionClaim(BaseModel):
    """A resolution claim bound to a pinned estimator profile
    (src2- prefix)."""

    model_config = ConfigDict(frozen=True)

    claim_id: str
    claim_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    estimator_ref: AuthorityRef
    claimed_resolution_hz: float
    verdict: str

    @model_validator(mode='after')
    def _validate(self) -> 'SpectralResolutionClaim':
        _require_refs(self.estimator_ref)
        if self.claimed_resolution_hz <= 0.0:
            raise ValueError('claimed_resolution_hz must be positive')
        if self.verdict not in RESOLUTION_LABELS:
            raise ValueError(f'unknown resolution verdict {self.verdict!r}')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'claim_id', 'claim_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'SpectralResolutionClaim':
        return _seal(cls, payload, 'claim_id', 'claim_sha256', 'src2')


def evaluate_resolution_claim(
    profile: SpectralEstimatorProfile | None,
    claimed_resolution_hz: float,
) -> tuple[str, str]:
    """Dense bins or padding never substitute for record-length
    resolution."""
    if profile is None:
        return ('insufficient_evidence', 'no_estimator_profile')
    if profile.window_kind == 'unknown':
        return ('unknown_window_unresolvable',
                'window_must_be_declared')
    true_res = profile.true_resolution_hz()
    if true_res is None:
        return ('insufficient_evidence',
                'record_length_or_fft_undeclared')
    if claimed_resolution_hz < true_res * 0.999:
        return ('padded_display_not_resolution',
                'claim_finer_than_record_length')
    return ('resolution_declared', 'claim_within_record_limit')
