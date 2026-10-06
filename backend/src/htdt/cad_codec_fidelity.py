"""Codec / playback-fidelity authority (issues #747, #753).

Successful decode/playback does not prove the programme reached the
renderer unchanged or perceptually transparent — service-side
transcoding, bitrate adaptation, generation loss, channel/layout
reduction, SRC/word-length conversion and hidden DRC all pass
'plays fine' (#747 audio; #753 video: coding artifacts, ABR
switches, texture/detail loss, scaling/crop). The decoded-format
negotiation (#632) and display calibration are separate layers —
fidelity of the delivered programme is its own authority.
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


_CODEC_FAMILIES = (
    'lossless', 'aac', 'ac3', 'eac3', 'dts', 'av1', 'hevc', 'h264',
    'vp9', 'other', 'unknown',
)
_IMPAIRMENTS = (
    'transcoding', 'bitrate_adaptation', 'generation_loss',
    'layout_reduction', 'src_conversion', 'hidden_drc',
    'blocking_ringing', 'texture_loss', 'chroma_degradation',
    'temporal_artifact', 'other',
)


class CodecFidelityEvidence(BaseModel):
    """Delivered-programme fidelity record (#747/#753) — the
    negotiated codec plus detected/reported impairments along the
    path; 'unknown' family fails closed."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    media_kind: Literal['audio', 'video']
    codec_family: Literal[
        'lossless', 'aac', 'ac3', 'eac3', 'dts', 'av1', 'hevc',
        'h264', 'vp9', 'other', 'unknown',
    ]
    impairments: tuple[
        Literal[
            'transcoding', 'bitrate_adaptation', 'generation_loss',
            'layout_reduction', 'src_conversion', 'hidden_drc',
            'blocking_ringing', 'texture_loss', 'chroma_degradation',
            'temporal_artifact', 'other',
        ],
        ...,
    ] = ()
    source_master_ref: AuthorityRef | None = None
    transparency_evidence_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('media_kind') not in ('audio', 'video'):
                raise ValueError('media kind must be audio/video')
            if data.get('codec_family') not in _CODEC_FAMILIES:
                raise ValueError('unknown codec family')
            if data.get('codec_family') == 'unknown':
                raise ValueError(
                    'fidelity evidence must declare the codec — '
                    'unidentified delivery cannot be judged'
                )
            bad = [
                i for i in (data.get('impairments') or ())
                if i not in _IMPAIRMENTS
            ]
            if bad:
                raise ValueError(f'unknown impairment kinds: {bad}')
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evidence_id', 'evidence_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'CodecFidelityEvidence':
        return _seal(
            cls, payload, 'evidence_id', 'evidence_sha256', 'cfx'
        )


CodecVerdict = Literal[
    'qualified_transparent',
    'playback_is_not_fidelity',
    'impairment_detected',
    'transparency_unverified',
]


def evaluate_codec_claim(
    evidence: CodecFidelityEvidence | None,
    *,
    playback_verified: bool = False,
) -> tuple[CodecVerdict, str]:
    """Judge a programme-fidelity claim (#747/#753)."""
    if evidence is None:
        if playback_verified:
            return (
                'playback_is_not_fidelity',
                'successful decode/playback does not prove the '
                'programme arrived unchanged',
            )
        return (
            'transparency_unverified',
            'no fidelity evidence pinned',
        )
    if evidence.impairments:
        return (
            'impairment_detected',
            f'delivered programme shows: '
            f'{", ".join(evidence.impairments)}',
        )
    if (
        evidence.codec_family not in ('lossless',)
        and evidence.transparency_evidence_ref is None
    ):
        return (
            'transparency_unverified',
            'lossy codec with no transparency/listening evidence — '
            'transparent delivery unverified',
        )
    if (
        evidence.codec_family == 'lossless'
        and evidence.source_master_ref is None
    ):
        return (
            'transparency_unverified',
            'lossless codec but source/master identity unpinned',
        )
    return (
        'qualified_transparent',
        'fidelity evidence pinned with no impairment',
    )


CODEC_LABELS: dict[str, str] = {
    'qualified_transparent': '透過忠実度適格',
    'playback_is_not_fidelity': '再生可能は忠実度ではない',
    'impairment_detected': '劣化検出',
    'transparency_unverified': '透過性未検証',
}
