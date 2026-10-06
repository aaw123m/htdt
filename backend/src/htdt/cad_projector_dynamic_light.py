"""Projector dynamic-light / temporal-contrast authority (issue #759).

Modern projectors modulate laser/light output, iris state and tone
mapping as a function of current AND prior image content. A single
`contrast ratio` scalar is not a stable physical quantity unless the
stimulus, adaptation history, control mode and observation time are
pinned: sequential full-white/full-black with dynamic light control
can differ by orders of magnitude from native, simultaneous, ADL-
conditioned or installed contrast. These quantities stay separate.

Basis: AVIXA V201.01:2021 (contrast is an installed-system measurand),
ANSI/SID IDMS contrast measurement families (sequential /
intra-frame / ADL variants), manufacturer dynamic-iris/laser-control
documentation distinctions.
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


DynamicLightMode = Literal[
    'native_fixed', 'laser_dynamic', 'mechanical_iris',
    'digital_iris', 'frame_adaptive_tonemap', 'light_source_shutoff',
    'unknown',
]
ContrastMeasurand = Literal[
    'native_static', 'sequential_dynamic', 'simultaneous_intraframe',
    'adl_conditioned', 'installed_system', 'unknown',
]
StimulusKind = Literal[
    'full_white_black_fields', 'checkerboard', 'ansi_grid',
    'adl_ramp', 'content_clip', 'unknown',
]
ContrastClaimVerdict = Literal[
    'comparable', 'comparable_with_caveats', 'incomparable',
    'insufficient_evidence',
]


LIGHT_MODE_LABELS: dict[str, str] = {
    'native_fixed': 'ネイティブ固定',
    'laser_dynamic': 'レーザー動的制御',
    'mechanical_iris': 'メカニカルアイリス',
    'digital_iris': 'デジタルアイリス',
    'frame_adaptive_tonemap': 'フレーム適応トーンマップ',
    'light_source_shutoff': '光源シャットオフ',
    'unknown': '不明',
}
MEASURAND_LABELS: dict[str, str] = {
    'native_static': 'ネイティブ/静的コントラスト',
    'sequential_dynamic': 'シーケンシャル動的コントラスト',
    'simultaneous_intraframe': '同時/フレーム内コントラスト',
    'adl_conditioned': 'ADL 条件付きコントラスト',
    'installed_system': '設置系コントラスト',
    'unknown': '不明',
}
VERDICT_LABELS: dict[str, str] = {
    'comparable': '比較可能',
    'comparable_with_caveats': '注意付きで比較可能',
    'incomparable': '比較不能',
    'insufficient_evidence': '証拠不足',
}


class ProjectorDynamicLightProfile(BaseModel):
    """Declared dynamic-light control state for one projector."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    device_ref: AuthorityRef | None = None
    light_modes: tuple[DynamicLightMode, ...] = ()
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'ProjectorDynamicLightProfile':
        if self.device_ref is not None:
            _require_refs(self.device_ref)
        if 'unknown' in self.light_modes:
            raise ValueError(
                'light_modes cannot include unknown — pin the control '
                'modes the device actually engages'
            )
        if not self.light_modes:
            raise ValueError(
                'declare at least one light mode; an empty set cannot '
                'anchor a contrast measurement'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(cls, **payload: Any) -> 'ProjectorDynamicLightProfile':
        return _seal(
            cls, payload, 'profile_id', 'profile_sha256', 'pdl'
        )


class TemporalContrastMeasurement(BaseModel):
    """One contrast measurement with pinned measurand semantics.

    The measurand kind, stimulus, adaptation history and control mode
    at observation time are mandatory — a bare number is not portable
    across these axes.
    """

    model_config = ConfigDict(frozen=True)

    measurement_id: str
    measurement_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    measurand: ContrastMeasurand
    stimulus: StimulusKind
    light_mode: DynamicLightMode = 'unknown'
    profile_ref: AuthorityRef | None = None
    value: float | None = None
    luminance_data_ref: AuthorityRef | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'TemporalContrastMeasurement':
        if self.profile_ref is not None:
            _require_refs(self.profile_ref)
        if self.luminance_data_ref is not None:
            _require_refs(self.luminance_data_ref)
        if self.measurand == 'unknown':
            raise ValueError(
                'contrast measurand must be declared — sequential, '
                'simultaneous, ADL and installed contrast are different '
                'quantities'
            )
        if self.stimulus == 'unknown':
            raise ValueError('stimulus must be declared')
        if self.measurand == 'sequential_dynamic' and self.light_mode in (
            'native_fixed', 'unknown'
        ):
            raise ValueError(
                'a sequential-dynamic measurand requires a declared '
                'dynamic light mode'
            )
        if self.value is not None and self.value <= 0:
            raise ValueError('contrast value must be positive')
        if self.value is None and self.luminance_data_ref is None:
            raise ValueError(
                'a measurement needs a value or pinned luminance data'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'measurement_id', 'measurement_sha256'},
        )

    @classmethod
    def create(cls, **payload: Any) -> 'TemporalContrastMeasurement':
        return _seal(
            cls, payload, 'measurement_id', 'measurement_sha256', 'tcnt'
        )


class DynamicContrastQualification(BaseModel):
    """Sealed verdict binding a contrast claim to its measurand."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    claimed_measurand: ContrastMeasurand
    measured_measurand: ContrastMeasurand
    measurement_ref: AuthorityRef
    verdict: ContrastClaimVerdict = 'insufficient_evidence'
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'DynamicContrastQualification':
        _require_refs(self.measurement_ref)
        if (
            self.verdict == 'comparable'
            and self.claimed_measurand != self.measured_measurand
        ):
            raise ValueError(
                'comparable requires the claimed and measured measurand '
                'to be the same quantity'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'qualification_id', 'qualification_sha256'},
        )

    @classmethod
    def create(cls, **payload: Any) -> 'DynamicContrastQualification':
        return _seal(
            cls,
            payload,
            'qualification_id',
            'qualification_sha256',
            'dcq',
        )


def compare_contrast_claims(
    a: TemporalContrastMeasurement,
    b: TemporalContrastMeasurement,
) -> tuple[ContrastClaimVerdict, str]:
    """Fail-closed contrast comparison.

    Measurements with different measurands or light modes cannot be
    compared or averaged — a sequential-dynamic number must never
    substitute for native or installed contrast.
    """
    if a.measurand != b.measurand:
        return 'incomparable', 'measurand_mismatch'
    if a.light_mode != b.light_mode:
        return 'incomparable', 'light_mode_mismatch'
    if a.stimulus != b.stimulus:
        return 'comparable_with_caveats', 'stimulus_differs'
    return 'comparable', 'same_measurand'
