"""Codec / transcode fidelity authority (issues #753 video, #747 audio).

Successful playback, a negotiated signal format and a calibrated
display do NOT prove delivered fidelity: lossy coding, provider
transcoding, adaptive-bitrate switches, repeated encode/decode
generations, hidden SRC/word-length/gain and layout reductions can all
change the programme while playback remains operationally correct.

Basis: ITU-R BS.1387-2:2023 (PEAQ perceived audio quality), ITU-T
P.910 (subjective video quality), ITU-T P.1204 family (streaming
video quality models), ITU-T J.247 (full-reference objective video
quality). `bit_transparent` requires a lossless end-to-end path —
lossy codecs can claim `perceptually_transparent` only with method
evidence.
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


MediaKind = Literal['video', 'audio', 'unknown']
CodecClass = Literal[
    'lossless', 'lossy', 'lossy_transcode', 'passthrough', 'unknown',
]
QualityMethodKind = Literal[
    'bs1387_peaq', 'p910_subjective', 'p1204_bitstream',
    'p1204_pixel', 'j247_full_reference', 'custom_metric',
    'signal_comparison', 'unknown',
]
FidelityVerdict = Literal[
    'bit_transparent', 'perceptually_transparent', 'impaired',
    'not_assessed', 'insufficient_evidence',
]


CODEC_LABELS: dict[str, str] = {
    'lossless': 'ロスレス',
    'lossy': 'ロッシー',
    'lossy_transcode': 'ロッシー再変換',
    'passthrough': 'パススルー',
    'unknown': '不明',
}
METHOD_LABELS: dict[str, str] = {
    'bs1387_peaq': 'ITU-R BS.1387-2 (PEAQ)',
    'p910_subjective': 'ITU-T P.910 主観評価',
    'p1204_bitstream': 'ITU-T P.1204 ビットストリーム',
    'p1204_pixel': 'ITU-T P.1204 ピクセル',
    'j247_full_reference': 'ITU-T J.247 フルリファレンス',
    'custom_metric': '独自指標',
    'signal_comparison': '信号比較',
    'unknown': '不明',
}
FIDELITY_LABELS: dict[str, str] = {
    'bit_transparent': 'ビット透過',
    'perceptually_transparent': '知覚的に透過',
    'impaired': '劣化あり',
    'not_assessed': '未評価',
    'insufficient_evidence': '証拠不足',
}


class CodecChainProfile(BaseModel):
    """Declared source→renderer codec/transcode chain.

    Every lossy step and hidden conversion/normalization step must be
    declared — the negotiated output mode does not describe what
    happened upstream.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    media_kind: MediaKind
    codec_steps: tuple[CodecClass, ...] = ()
    service_transcode_declared: bool | None = None
    hidden_processing_detected: bool = False
    layout_reduced: bool | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'CodecChainProfile':
        if self.media_kind == 'unknown':
            raise ValueError('media_kind must be video or audio')
        if 'unknown' in self.codec_steps:
            raise ValueError(
                'codec_steps cannot include unknown — pin what the path '
                'actually does'
            )
        if (
            self.codec_steps
            and all(s in ('lossless', 'passthrough') for s in self.codec_steps)
            and self.hidden_processing_detected
        ):
            raise ValueError(
                'a declared lossless/passthrough chain cannot also '
                'declare hidden processing — resolve the contradiction'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(cls, **payload: Any) -> 'CodecChainProfile':
        return _seal(
            cls, payload, 'profile_id', 'profile_sha256', 'cfp'
        )


class QualityMethodProfile(BaseModel):
    """Declared quality-assessment method binding evidence to a
    standard family."""

    model_config = ConfigDict(frozen=True)

    method_id: str
    method_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    method_kind: QualityMethodKind
    media_kind: MediaKind
    reference_required: bool = False
    standard_ref: AuthorityRef | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'QualityMethodProfile':
        if self.standard_ref is not None:
            _require_refs(self.standard_ref)
        if self.method_kind == 'unknown':
            raise ValueError('quality method must be declared')
        if self.media_kind == 'unknown':
            raise ValueError('media_kind must be declared')
        if self.method_kind == 'j247_full_reference':
            if not self.reference_required:
                raise ValueError(
                    'full-reference methods must require the reference'
                )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'method_id', 'method_sha256'}
        )

    @classmethod
    def create(cls, **payload: Any) -> 'QualityMethodProfile':
        return _seal(
            cls, payload, 'method_id', 'method_sha256', 'qmp'
        )


class CodecFidelityObservation(BaseModel):
    """One fidelity observation bound to a chain and method."""

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    chain_ref: AuthorityRef
    method_ref: AuthorityRef
    score: float | None = None
    score_scale: str = ''
    impairment_detected: bool | None = None
    observation_data_ref: AuthorityRef | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'CodecFidelityObservation':
        _require_refs(self.chain_ref, self.method_ref)
        if self.observation_data_ref is not None:
            _require_refs(self.observation_data_ref)
        if (
            self.score is None
            and self.impairment_detected is None
            and self.observation_data_ref is None
        ):
            raise ValueError(
                'an observation needs a score, an impairment flag or '
                'pinned observation data'
            )
        if self.score is not None and not self.score_scale:
            raise ValueError('a scored observation names its scale')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'},
        )

    @classmethod
    def create(cls, **payload: Any) -> 'CodecFidelityObservation':
        return _seal(
            cls, payload, 'observation_id', 'observation_sha256', 'cfo'
        )


def evaluate_fidelity_claim(
    claim: FidelityVerdict,
    chain: CodecChainProfile | None,
    observations: tuple[CodecFidelityObservation, ...],
) -> tuple[FidelityVerdict, str]:
    """Fail-closed codec-fidelity gate.

    `bit_transparent` requires a lossless/passthrough chain end to end.
    `perceptually_transparent` requires at least one observation; an
    impairment flag downgrades to `impaired`. Successful playback is
    never evidence of fidelity.
    """
    if chain is None:
        return 'insufficient_evidence', 'no_chain_profile'
    lossy_steps = {
        s for s in chain.codec_steps
        if s in ('lossy', 'lossy_transcode')
    }
    if claim == 'bit_transparent':
        if lossy_steps or chain.hidden_processing_detected:
            return 'not_assessed', 'lossy_or_hidden_steps_present'
        if chain.service_transcode_declared:
            return 'not_assessed', 'service_transcode_declared'
        return 'bit_transparent', 'lossless_path_declared'
    if claim == 'perceptually_transparent':
        if not observations:
            return 'insufficient_evidence', 'no_quality_evidence'
        if any(o.impairment_detected for o in observations):
            return 'impaired', 'impairment_detected'
        return 'perceptually_transparent', 'method_evidence_present'
    return 'insufficient_evidence', 'unsupported_claim'


# --- SIGNAL lane (evidence/claim) ----------------------------------
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
