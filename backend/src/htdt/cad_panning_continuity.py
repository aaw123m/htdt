"""Inter-channel panning continuity + subwoofer-localization
authority (issues #652, #669).

Correctly placed, routed and individually calibrated speakers can
still produce audible timbre/level/time discontinuity when an image
pans across channels (#652). And 'subwoofers are non-localizable
below 80 Hz' is not a defensible universal rule — detectability
depends on crossover, separation, count, stimulus, delay/alignment,
distortion leakage and room modal response (#669, AES listening
studies).

Basis: AES subwoofer-detection listening studies; inter-channel
matching literature.
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


class PanningContinuityEvidence(BaseModel):
    """Measured/declared inter-channel continuity (#652) — timbre,
    level and time deltas across the specific channel pair, bound
    to measurement evidence."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    channel_pair: tuple[str, str]
    timbre_delta_db: float | None = None
    level_delta_db: float | None = None
    time_delta_ms: float | None = None
    stimulus_kind: Literal[
        'pan_sweep', 'pink_noise', 'program', 'other', 'unknown',
    ]
    measurement_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            pair = data.get('channel_pair')
            if not pair or len(pair) != 2 or pair[0] == pair[1]:
                raise ValueError(
                    'continuity evidence requires a distinct '
                    'channel pair'
                )
            if data.get('stimulus_kind') not in (
                'pan_sweep', 'pink_noise', 'program', 'other',
                'unknown',
            ):
                raise ValueError('unknown stimulus kind')
            if data.get('stimulus_kind') == 'unknown':
                raise ValueError(
                    'continuity evidence must declare its '
                    'stimulus'
                )
            if data.get('measurement_ref') is None:
                raise ValueError(
                    'continuity deltas require measurement '
                    'evidence — per-channel calibration does not '
                    'prove inter-channel continuity'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evidence_id', 'evidence_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'PanningContinuityEvidence':
        return _seal(
            cls, payload, 'evidence_id', 'evidence_sha256', 'pce'
        )


class SubwooferLocalizationProfile(BaseModel):
    """Declared subwoofer-localization context (#669) — crossover,
    separation, count, distortion leakage. The bare 'below 80 Hz is
    non-localizable' rule is rejected as universal."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    crossover_hz: float
    separation_deg: float | None = None
    sub_count: int | None = None
    distortion_leakage_bounded: bool | None = None
    delay_alignment_ref: AuthorityRef | None = None
    stimulus_kind: Literal[
        'program', 'test_signal', 'both', 'unknown',
    ]

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            x = data.get('crossover_hz')
            if x is None or x <= 0:
                raise ValueError(
                    'localization evidence requires the crossover '
                    'frequency — context factors couple detectability'
                )
            if data.get('stimulus_kind') not in (
                'program', 'test_signal', 'both', 'unknown'
            ):
                raise ValueError('unknown stimulus kind')
            if data.get('stimulus_kind') == 'unknown':
                raise ValueError(
                    'localization evidence must declare its '
                    'stimulus — program material changes '
                    'detectability'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'SubwooferLocalizationProfile':
        return _seal(
            cls, payload, 'profile_id', 'profile_sha256', 'slp'
        )


PanningVerdict = Literal[
    'qualified_continuity',
    'per_channel_is_not_interchannel',
    'fixed_rule_misapplied',
    'localization_context_unqualified',
]


def evaluate_panning_claim(
    continuity: PanningContinuityEvidence | None,
    localization: SubwooferLocalizationProfile | None,
    *,
    per_channel_calibrated: bool = False,
    rule_threshold_hz: float | None = None,
) -> tuple[PanningVerdict, str]:
    """Judge a panning/localization claim (#652/#669)."""
    if localization is not None:
        if rule_threshold_hz is not None:
            return (
                'fixed_rule_misapplied',
                'a frequency-only threshold cannot decide '
                'localization — crossover/separation/count/'
                'stimulus/distortion couple detectability',
            )
        if localization.distortion_leakage_bounded is not True:
            return (
                'localization_context_unqualified',
                'harmonic/port/mechanical leakage not bounded — '
                'upper-band content localizes the sub',
            )
        if localization.delay_alignment_ref is None:
            return (
                'localization_context_unqualified',
                'main↔sub delay/alignment unpinned — interaural '
                'timing cues change detectability',
            )
    if continuity is None:
        if per_channel_calibrated:
            return (
                'per_channel_is_not_interchannel',
                'per-channel calibration does not prove '
                'inter-channel continuity',
            )
        return (
            'per_channel_is_not_interchannel',
            'no continuity evidence pinned',
        )
    return (
        'qualified_continuity',
        'inter-channel deltas measured and bounded',
    )


PANNING_LABELS: dict[str, str] = {
    'qualified_continuity': 'パンニング連続性適格',
    'per_channel_is_not_interchannel': 'ch別校正はch間連続性ではない',
    'fixed_rule_misapplied': '固定ルール誤適用',
    'localization_context_unqualified': '定位文脈未適格',
}
