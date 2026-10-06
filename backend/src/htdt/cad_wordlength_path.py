"""Digital word-length / dither / gain path authority (issue #744).

A PCM format label ("24-bit output") does not prove that word length,
digital gain, rounding and dither remain controlled through the actual
playback/measurement path. Requantization after processing reintroduces
quantization distortion unless dithered appropriately.

Basis: AES17-2020 (FS/dBFS semantics; production-current until AES17-R
publishes), Vanderkooy & Lipshitz 1989 (gain/EQ/oversampling/editing
reintroduce quantization artifacts; appropriate redithering linearizes
the quantizer), Lipshitz, Wannamaker & Vanderkooy 1992 (undithered /
subtractive / non-subtractive quantizer taxonomy).
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


WordRepresentation = Literal[
    'integer_pcm', 'floating_point', 'fixed_point', 'unknown',
]
RoundingMode = Literal[
    'truncation', 'round_to_nearest', 'convergent_rounding',
    'dithered', 'unknown',
]
DitherKind = Literal[
    'undithered', 'subtractive_tpdf', 'non_subtractive_tpdf',
    'rectangular_pdf', 'noise_shaped', 'none_declared', 'unknown',
]
TransformKind = Literal[
    'wordlength_change', 'digital_gain', 'requantization',
    'format_conversion', 'noise_shape', 'clipping_event', 'unknown',
]
LowLevelVerdict = Literal[
    'controlled', 'controlled_with_limitations', 'uncontrolled',
    'insufficient_evidence',
]


REPRESENTATION_LABELS: dict[str, str] = {
    'integer_pcm': '整数 PCM',
    'floating_point': '浮動小数点',
    'fixed_point': '固定小数点',
    'unknown': '不明',
}
DITHER_LABELS: dict[str, str] = {
    'undithered': 'ディザなし',
    'subtractive_tpdf': '減算ディザ TPDF',
    'non_subtractive_tpdf': '非減算ディザ TPDF',
    'rectangular_pdf': '矩形 PDF ディザ',
    'noise_shaped': 'ノイズシェイピング付き',
    'none_declared': 'ディザ未宣言',
    'unknown': '不明',
}
LOW_LEVEL_LABELS: dict[str, str] = {
    'controlled': '低レベル処理管理済み',
    'controlled_with_limitations': '限定付きで管理済み',
    'uncontrolled': '低レベル処理未管理',
    'insufficient_evidence': '証拠不足',
}


class DitherNoiseShapeProfile(BaseModel):
    """Declared dither / noise-shaping profile for a requantization."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    dither_kind: DitherKind
    noise_shape_order: int | None = None
    target_word_length_bits: int | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'DitherNoiseShapeProfile':
        if self.dither_kind == 'noise_shaped':
            if self.noise_shape_order is None:
                raise ValueError(
                    'noise_shaped dither needs a declared shape order'
                )
        elif self.noise_shape_order is not None:
            raise ValueError(
                'noise_shape_order only applies to noise_shaped dither'
            )
        if (
            self.target_word_length_bits is not None
            and self.target_word_length_bits <= 0
        ):
            raise ValueError('target_word_length_bits must be positive')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(cls, **payload: Any) -> 'DitherNoiseShapeProfile':
        return _seal(
            cls, payload, 'profile_id', 'profile_sha256', 'dns'
        )


class DigitalPathTransformRecord(BaseModel):
    """Sealed record of one processing stage in a digital path.

    Each requantization/rounding stage is a transform with declared
    rounding mode and optional dither profile — a format label at the
    chain end must not hide intermediate reductions.
    """

    model_config = ConfigDict(frozen=True)

    transform_id: str
    transform_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    transform_kind: TransformKind
    input_representation: WordRepresentation = 'unknown'
    input_word_length_bits: int | None = None
    output_representation: WordRepresentation = 'unknown'
    output_word_length_bits: int | None = None
    rounding_mode: RoundingMode = 'unknown'
    gain_db: float | None = None
    dither_profile_ref: AuthorityRef | None = None
    device_or_stage: str = ''
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'DigitalPathTransformRecord':
        if self.dither_profile_ref is not None:
            _require_refs(self.dither_profile_ref)
        if self.transform_kind == 'unknown':
            raise ValueError('transform_kind must be declared')
        if (
            self.transform_kind == 'requantization'
            and self.rounding_mode == 'unknown'
            and self.dither_profile_ref is None
        ):
            raise ValueError(
                'a requantization stage must declare its rounding mode '
                'or pin a dither profile'
            )
        if self.rounding_mode == 'dithered' and self.dither_profile_ref is None:
            raise ValueError(
                'dithered rounding requires a pinned dither profile'
            )
        for name, value in (
            ('input_word_length_bits', self.input_word_length_bits),
            ('output_word_length_bits', self.output_word_length_bits),
        ):
            if value is not None and value <= 0:
                raise ValueError(f'{name} must be positive')
        if (
            self.transform_kind == 'digital_gain'
            and self.gain_db is None
        ):
            raise ValueError('a digital_gain stage must declare gain_db')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'transform_id', 'transform_sha256'}
        )

    @classmethod
    def create(cls, **payload: Any) -> 'DigitalPathTransformRecord':
        return _seal(
            cls, payload, 'transform_id', 'transform_sha256', 'dpt'
        )


def evaluate_low_level_claim(
    path_end_label_bits: int | None,
    transforms: tuple[DigitalPathTransformRecord, ...],
) -> tuple[LowLevelVerdict, str]:
    """Fail-closed low-level processing gate.

    A format label alone never proves low-level linearity: an
    undithered truncation/requantization in the path makes low-level
    claims uncontrolled; every requantization stage must show declared
    dither or controlled rounding.
    """
    if not transforms:
        return 'insufficient_evidence', 'no_transform_evidence'
    saw_requantization = False
    for t in transforms:
        if t.transform_kind in ('requantization', 'wordlength_change'):
            saw_requantization = True
            if (
                t.rounding_mode in ('truncation', 'unknown')
                and t.dither_profile_ref is None
            ):
                return 'uncontrolled', 'undithered_requantization'
        if t.transform_kind == 'clipping_event':
            return 'uncontrolled', 'clipping_in_path'
    if saw_requantization:
        return 'controlled', 'all_requantizations_dithered'
    if path_end_label_bits is None:
        return 'controlled_with_limitations', 'no_label_no_reduction'
    return 'controlled_with_limitations', 'gain_only_path'
