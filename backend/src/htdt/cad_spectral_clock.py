"""Spectral-estimator and digital-clock-domain authority (issues
#749, #670).

A dense FFT plot does not prove resolving power or accurate
amplitude — record duration, FFT size, zero padding, window,
coherent gain, ENBW and leakage all determine what the estimate
actually resolves (#749). And correct routing + sample-rate labels
do not prove the playback system's digital clock domains are
synchronized — unlocked sources, asynchronous domains, unplanned
ASRC, relock and slips live underneath (#670; #609 owns the
*measurement* timebase, this owns the playback chain).
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


_WINDOW_KINDS = (
    'hann', 'blackman_harris', 'flat_top', 'rectangular', 'kaiser',
    'other', 'unknown',
)


class SpectralEstimatorProfile(BaseModel):
    """Declared FFT estimator semantics (#749) — aperture, window,
    ENBW and amplitude correction make a spectrum comparable; a
    dense bin grid alone is not resolving power."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    record_duration_s: float
    fft_size: int
    zero_padded: bool = False
    window_kind: Literal[
        'hann', 'blackman_harris', 'flat_top', 'rectangular',
        'kaiser', 'other', 'unknown',
    ]
    enbw_bins: float | None = None
    coherent_gain_corrected: bool | None = None
    leakage_bounded: bool | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data.get('record_duration_s') or (
                data['record_duration_s'] <= 0
            ):
                raise ValueError(
                    'a spectral estimate requires the record '
                    'duration — time aperture sets resolution'
                )
            if not data.get('fft_size') or data['fft_size'] <= 0:
                raise ValueError('a positive FFT size is required')
            if data.get('window_kind') not in _WINDOW_KINDS:
                raise ValueError('unknown window kind')
            if data.get('window_kind') == 'unknown':
                raise ValueError(
                    'the window function determines ENBW and '
                    'leakage — declare it'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'SpectralEstimatorProfile':
        return _seal(
            cls, payload, 'profile_id', 'profile_sha256', 'sep'
        )


class ClockDomainObservation(BaseModel):
    """Observed digital clock state of the playback chain (#670) —
    domain kind, lock status, ASRC presence, slips/dropouts; a
    routing diagram does not prove the clock tree."""

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    domain_kind: Literal[
        'internal', 'word_clock', 'aes3', 'spdif', 'madi',
        'network_ptp', 'asrc', 'other', 'unknown',
    ]
    lock_state: Literal[
        'locked', 'unlocked', 'relock_event', 'unknown',
    ]
    asrc_present: bool | None = None
    slips_observed: int | None = None
    transition_evidence_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('domain_kind') not in (
                'internal', 'word_clock', 'aes3', 'spdif', 'madi',
                'network_ptp', 'asrc', 'other', 'unknown',
            ):
                raise ValueError('unknown domain kind')
            if data.get('domain_kind') == 'unknown':
                raise ValueError(
                    'a clock observation must declare the domain '
                    'kind'
                )
            if data.get('lock_state') not in (
                'locked', 'unlocked', 'relock_event', 'unknown',
            ):
                raise ValueError('unknown lock state')
            if data.get('lock_state') == 'unknown':
                raise ValueError(
                    'declare the lock state — unlocked sources '
                    'silently corrupt'
                )
            if data.get('lock_state') == 'relock_event' and (
                data.get('transition_evidence_ref') is None
            ):
                raise ValueError(
                    'a relock/transition event requires capture '
                    'evidence'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'observation_id', 'observation_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'ClockDomainObservation':
        return _seal(
            cls, payload, 'observation_id', 'observation_sha256', 'cdo'
        )


SpectralVerdict = Literal[
    'qualified_spectral',
    'density_is_not_resolution',
    'amplitude_uncorrected',
    'lock_unverified',
    'asrc_undeclared',
]


def evaluate_spectral_claim(
    profile: SpectralEstimatorProfile | None,
    clocks: tuple[ClockDomainObservation, ...] | None,
    *,
    plotted_bins_dense: bool = False,
) -> tuple[SpectralVerdict, str]:
    """Judge spectral-amplitude and clock claims (#749/#670)."""
    if profile is None:
        if plotted_bins_dense:
            return (
                'density_is_not_resolution',
                'a dense FFT grid does not prove resolving power — '
                'aperture/window/ENBW undeclared',
            )
        return ('amplitude_uncorrected', 'no estimator profile')
    if profile.window_kind == 'rectangular' and (
        profile.leakage_bounded is not True
    ):
        return (
            'amplitude_uncorrected',
            'rectangular window with unbounded leakage — amplitude '
            'estimates unreliable',
        )
    if profile.coherent_gain_corrected is not True:
        return (
            'amplitude_uncorrected',
            'window coherent gain not corrected — amplitude scale '
            'is window-dependent',
        )
    if clocks:
        for c in clocks:
            if c.lock_state != 'locked':
                return (
                    'lock_unverified',
                    f'clock domain {c.domain_kind} not locked',
                )
            if c.asrc_present is True:
                return (
                    'asrc_undeclared',
                    'asynchronous SRC present — delivered stream '
                    'is converted, not bit-transparent',
                )
    return (
        'qualified_spectral',
        'estimator declared and clock tree locked',
    )


SPECTRAL_LABELS: dict[str, str] = {
    'qualified_spectral': 'スペクトル適格',
    'density_is_not_resolution': '密なビンは分解能ではない',
    'amplitude_uncorrected': '振幅未補正',
    'lock_unverified': 'クロック未ロック',
    'asrc_undeclared': 'ASR未宣言',
}
