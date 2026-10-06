"""Direct-view / LED-wall acoustic-boundary authority (issue #760).

A large direct-view LED/MicroLED/video wall is not only a video
device — it is a several-square-metre acoustic boundary that blocks
behind-screen loudspeaker placement, changes early reflections/SBIR,
constrains source geometry and can alter the front soundstage.
Marketed 'acoustically transparent/perforated' LED structures still
have non-trivial transmission loss that must be measured, not
assumed.
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


class DisplayWallBoundary(BaseModel):
    """Declared display-wall acoustic boundary (#760) — geometry,
    surface treatment and whether speakers sit behind it. Marketing
    'acoustically transparent' is a claim needing measurement."""

    model_config = ConfigDict(frozen=True)

    boundary_id: str
    boundary_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    wall_kind: Literal[
        'led_wall', 'microled_wall', 'lcd_video_wall',
        'solid_direct_view', 'projection_screen', 'other', 'unknown',
    ]
    area_m2: float | None = None
    behind_speaker_placement: bool | None = None
    acoustic_transparency_claim: Literal[
        'transparent', 'perforated', 'opaque', 'unclaimed',
    ] = 'unclaimed'
    transmission_evidence_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('wall_kind') not in (
                'led_wall', 'microled_wall', 'lcd_video_wall',
                'solid_direct_view', 'projection_screen', 'other',
                'unknown',
            ):
                raise ValueError('unknown wall kind')
            if data.get('wall_kind') == 'unknown':
                raise ValueError(
                    'a display wall must declare its kind'
                )
            if data.get('acoustic_transparency_claim') in (
                'transparent', 'perforated'
            ) and data.get('behind_speaker_placement') and (
                data.get('transmission_evidence_ref') is None
            ):
                raise ValueError(
                    'speakers behind a transparency-claimed wall '
                    'require measured transmission evidence — '
                    'marketing transparency is not acoustic'
                )
            if data.get('acoustic_transparency_claim') not in (
                'transparent', 'perforated', 'opaque', 'unclaimed'
            ):
                raise ValueError('unknown transparency claim')
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'boundary_id', 'boundary_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'DisplayWallBoundary':
        return _seal(
            cls, payload, 'boundary_id', 'boundary_sha256', 'dwb'
        )


class WallAcousticImpact(BaseModel):
    """Measured/declared acoustic impact of the wall (#760) — early
    reflections, SBIR shift, transmission loss band profile."""

    model_config = ConfigDict(frozen=True)

    impact_id: str
    impact_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    boundary_ref: AuthorityRef
    transmission_loss_db: float | None = None
    reflection_added: bool | None = None
    sbir_shift_hz: float | None = None
    measurement_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('boundary_ref') is None:
                raise ValueError(
                    'a wall-impact record requires the pinned '
                    'boundary'
                )
            if data.get('transmission_loss_db') is not None and (
                data.get('measurement_ref') is None
            ):
                raise ValueError(
                    'a transmission-loss figure requires '
                    'measurement evidence'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'impact_id', 'impact_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'WallAcousticImpact':
        return _seal(
            cls, payload, 'impact_id', 'impact_sha256', 'wai'
        )


WallVerdict = Literal[
    'qualified_boundary',
    'video_wall_is_also_acoustic',
    'transparency_unmeasured',
    'impact_unbounded',
]


def evaluate_wall_claim(
    boundary: DisplayWallBoundary | None,
    impact: WallAcousticImpact | None,
) -> tuple[WallVerdict, str]:
    """Judge a display-wall acoustic claim (#760)."""
    if boundary is None:
        return (
            'video_wall_is_also_acoustic',
            'a multi-m2 display wall is an acoustic boundary — '
            'register it before claiming the front soundstage',
        )
    if (
        boundary.behind_speaker_placement
        and boundary.acoustic_transparency_claim in (
            'transparent', 'perforated'
        )
        and boundary.transmission_evidence_ref is None
    ):
        return (
            'transparency_unmeasured',
            'marketing transparency without measured transmission — '
            'perforated LED still has non-trivial loss',
        )
    if (
        boundary.wall_kind != 'projection_screen'
        and impact is None
    ):
        return (
            'impact_unbounded',
            'solid display wall present with no acoustic-impact '
            'record — reflections/SBIR/source constraints unknown',
        )
    return (
        'qualified_boundary',
        'display wall registered with bounded acoustic impact',
    )


WALL_LABELS: dict[str, str] = {
    'qualified_boundary': '表示壁境界適格',
    'video_wall_is_also_acoustic': '映像壁は音響境界でもある',
    'transparency_unmeasured': '透過性未測定',
    'impact_unbounded': '音響影響上限なし',
}
