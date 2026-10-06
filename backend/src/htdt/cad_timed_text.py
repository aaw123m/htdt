"""Subtitle / caption presentation qualification (issue #733).

Selecting or decoding a subtitle/caption track does not prove the
text is correctly timed, rendered, legible, positioned within the
actual masked/cropped image, or faithful to the intended language/
style/profile. Track support is not presentation evidence.

Basis: W3C IMSC Text Profile 1.3 (Recommendation 2026-05-21 — text-
only TTML2 profile incl. Japanese authoring guidance); ANSI/CTA-708-E
S-2023 (DTV closed-captioning coding).
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


_PROFILE_KINDS = (
    'imsc_text_1_3', 'imsc_text_1_2', 'imsc_image', 'ttml2',
    'cta_708e', 'cea608', 'webvtt', 'srt', 'other', 'unknown',
)

SubtitleVerdict = Literal[
    'qualified',
    'track_support_is_not_presentation',
    'timing_not_verified',
    'outside_masked_area',
    'illegible',
    'profile_mismatch',
]


class TimedTextPresentationProfile(BaseModel):
    """Declares which timed-text profile a presentation intends to
    satisfy on a pinned render surface (#733)."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_kind: Literal[
        'imsc_text_1_3', 'imsc_text_1_2', 'imsc_image', 'ttml2',
        'cta_708e', 'cea608', 'webvtt', 'srt', 'other', 'unknown',
    ]
    render_surface_ref: AuthorityRef | None = None
    language: str | None = None
    font_descriptor: str | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('profile_kind') not in _PROFILE_KINDS:
                raise ValueError('unknown timed-text profile kind')
            if data.get('profile_kind') == 'unknown':
                raise ValueError(
                    'a presentation profile must declare its kind — '
                    'unknown is not a presentation claim'
                )
            if data.get('render_surface_ref') is None:
                raise ValueError(
                    'presentation requires a pinned render surface '
                    '(the actual masked screen)'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'TimedTextPresentationProfile':
        return _seal(
            cls, payload, 'profile_id', 'profile_sha256', 'ttp'
        )


class CaptionRenderObservation(BaseModel):
    """Measured caption-presentation evidence on the actual masked
    screen (#733). Every quality dimension binds its own measured
    ref — declared support carries none.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    timing_verified_ref: AuthorityRef | None = None
    positioning_inside_mask: bool | None = None
    legibility_ref: AuthorityRef | None = None
    language_match: bool | None = None
    style_profile_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('profile_ref') is None:
                raise ValueError(
                    'a render observation requires a pinned profile'
                )
            has_any = any(
                data.get(k) is not None
                for k in (
                    'timing_verified_ref', 'positioning_inside_mask',
                    'legibility_ref', 'language_match',
                )
            )
            if not has_any:
                raise ValueError(
                    'a render observation without any measured '
                    'dimension is not evidence'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'},
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'CaptionRenderObservation':
        return _seal(
            cls, payload, 'observation_id', 'observation_sha256', 'cro'
        )


def evaluate_subtitle_claim(
    profile: TimedTextPresentationProfile | None,
    observation: CaptionRenderObservation | None,
    *,
    track_decoded: bool = False,
    track_profile_kind: str | None = None,
) -> tuple[SubtitleVerdict, str]:
    """Judge whether subtitle/caption presentation is proven (#733).

    - track decoded but no presentation evidence →
      'track_support_is_not_presentation'
    - declared profile kind ≠ track kind → 'profile_mismatch'
    - timing unverified → 'timing_not_verified'
    - text outside the masked image → 'outside_masked_area'
    - legibility unverified/failed → 'illegible'
    """
    if profile is None:
        if track_decoded:
            return (
                'track_support_is_not_presentation',
                'decoding the track is not rendering it correctly on '
                'the masked screen',
            )
        return (
            'track_support_is_not_presentation',
            'no presentation profile pinned',
        )
    if (
        track_profile_kind is not None
        and track_profile_kind != profile.profile_kind
    ):
        return (
            'profile_mismatch',
            f'track is {track_profile_kind} but profile is '
            f'{profile.profile_kind}',
        )
    if observation is None:
        return (
            'track_support_is_not_presentation',
            'profile declared but no render observation pinned',
        )
    if observation.timing_verified_ref is None:
        return (
            'timing_not_verified',
            'caption timing never verified against media time',
        )
    if observation.positioning_inside_mask is False:
        return (
            'outside_masked_area',
            'captions rendered outside the masked/cropped image',
        )
    if observation.positioning_inside_mask is None:
        return (
            'timing_not_verified',
            'caption positioning inside the mask never verified',
        )
    if observation.legibility_ref is None:
        return (
            'illegible',
            'no legibility evidence (size/contrast/duration)',
        )
    return (
        'qualified',
        'presentation verified: timing, masking, legibility pinned',
    )


SUBTITLE_LABELS: dict[str, str] = {
    'qualified': '字幕表示は検証済み',
    'track_support_is_not_presentation': 'トラック対応は表示証拠ではない',
    'timing_not_verified': 'タイミング/位置未検証',
    'outside_masked_area': 'マスク領域外に表示',
    'illegible': '可読性証拠なし',
    'profile_mismatch': 'プロファイル不一致',
}
