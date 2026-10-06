"""Direct-view / LED-wall acoustic-boundary authority (issue #760).

A large direct-view LED / MicroLED / video wall is not only a video
device: it is a multi-square-metre acoustic boundary that can block
behind-screen loudspeaker placement, alter early reflections/SBIR and
constrain the front stage. `Acoustically transparent` marketing must
carry transmission evidence; an opaque wall must not silently admit a
behind-screen LCR layout.
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


BoundaryTransmission = Literal[
    'opaque', 'acoustically_transparent', 'micro_perforated',
    'partial_coverage', 'unknown',
]
FrontStageStrategy = Literal[
    'behind_screen_lcr', 'beside_screen', 'above_screen',
    'below_screen', 'reflected_path', 'in_wall_adjacent', 'unknown',
]
FrontStageVerdict = Literal[
    'placement_supported', 'placement_blocked',
    'transmission_evidence_required', 'insufficient_evidence',
]


TRANSMISSION_LABELS: dict[str, str] = {
    'opaque': '不透過',
    'acoustically_transparent': '音響透過',
    'micro_perforated': 'マイクロパーフォレーテッド',
    'partial_coverage': '部分被覆',
    'unknown': '不明',
}
STRATEGY_LABELS: dict[str, str] = {
    'behind_screen_lcr': 'スクリーン背面 L/C/R',
    'beside_screen': 'スクリーン横',
    'above_screen': 'スクリーン上',
    'below_screen': 'スクリーン下',
    'reflected_path': '反射経路',
    'in_wall_adjacent': '隣接壁埋込',
    'unknown': '不明',
}
VERDICT_LABELS: dict[str, str] = {
    'placement_supported': '配置可能',
    'placement_blocked': '配置不可',
    'transmission_evidence_required': '透過証拠が必要',
    'insufficient_evidence': '証拠不足',
}


class DisplayAcousticBoundaryProfile(BaseModel):
    """Declared acoustic behavior of a display surface region.

    A `transparent`/`micro_perforated` declaration requires pinned
    transmission evidence — marketing copy is not evidence.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    surface_area_m2: float | None = None
    transmission: BoundaryTransmission = 'unknown'
    transmission_evidence_ref: AuthorityRef | None = None
    reflection_coefficient_ref: AuthorityRef | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'DisplayAcousticBoundaryProfile':
        if self.transmission_evidence_ref is not None:
            _require_refs(self.transmission_evidence_ref)
        if self.reflection_coefficient_ref is not None:
            _require_refs(self.reflection_coefficient_ref)
        if (
            self.transmission
            in ('acoustically_transparent', 'micro_perforated')
            and self.transmission_evidence_ref is None
        ):
            raise ValueError(
                'an acoustically transparent declaration requires '
                'pinned transmission evidence'
            )
        if self.surface_area_m2 is not None and self.surface_area_m2 <= 0:
            raise ValueError('surface_area_m2 must be positive')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(cls, **payload: Any) -> 'DisplayAcousticBoundaryProfile':
        return _seal(
            cls, payload, 'profile_id', 'profile_sha256', 'dab'
        )


class FrontStageVariantRecord(BaseModel):
    """Sealed front-stage loudspeaker placement variant vs a display
    boundary."""

    model_config = ConfigDict(frozen=True)

    variant_id: str
    variant_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    boundary_ref: AuthorityRef
    strategy: FrontStageStrategy
    speaker_refs: tuple[AuthorityRef, ...] = ()
    verdict: FrontStageVerdict = 'insufficient_evidence'
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'FrontStageVariantRecord':
        _require_refs(self.boundary_ref)
        for ref in self.speaker_refs:
            _require_refs(ref)
        if self.strategy == 'unknown':
            raise ValueError('front-stage strategy must be declared')
        if (
            self.verdict == 'placement_supported'
            and not self.speaker_refs
        ):
            raise ValueError(
                'a supported placement names its loudspeakers'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'variant_id', 'variant_sha256'}
        )

    @classmethod
    def create(cls, **payload: Any) -> 'FrontStageVariantRecord':
        return _seal(
            cls, payload, 'variant_id', 'variant_sha256', 'fsv'
        )


def evaluate_frontstage_claim(
    boundary: DisplayAcousticBoundaryProfile | None,
    strategy: FrontStageStrategy,
) -> tuple[FrontStageVerdict, str]:
    """Fail-closed front-stage placement gate.

    Behind-screen LCR on an opaque display wall is blocked; on a
    boundary of unknown transmission it requires evidence first.
    """
    if boundary is None:
        return 'insufficient_evidence', 'no_boundary_profile'
    if strategy == 'behind_screen_lcr':
        if boundary.transmission == 'opaque':
            return 'placement_blocked', 'opaque_wall_blocks_behind'
        if boundary.transmission == 'partial_coverage':
            return 'transmission_evidence_required', 'partial_coverage'
        if boundary.transmission in (
            'acoustically_transparent', 'micro_perforated'
        ):
            return 'placement_supported', 'transmission_evidence_pinned'
        return 'transmission_evidence_required', 'transmission_unknown'
    return 'placement_supported', 'non_behind_strategy'
