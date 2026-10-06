"""Displayed-result fidelity authority (issues #660, #688, #672).

A source/path reporting 10-bit HDR does not prove the displayed
image preserves smooth tonal gradation, near-black detail, chroma
detail or quantization/range semantics without banding/contouring
(#660). A 2D gamut triangle / coverage percentage does not describe
the colour volume across luminance (#688 — IDMS v1.3 volumetric
methods). A nominal 4K raster does not prove installed spatial
resolution — resolution from contrast modulation / MTF is a distinct
authority (#672 — IDMS v1.3 Spatial Measurements).

Basis: SID/ICDM IDMS v1.3 (Colour Gamut Volume methods, Resolution
from Contrast Modulation).
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


class DisplayedGradationObservation(BaseModel):
    """Observed tonal-gradation outcome on the actual image (#660) —
    banding/contouring verdict bound to the exact stimulus and range
    semantics; a nominal bit depth is not evidence."""

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    stimulus_ref: AuthorityRef
    range_semantics: Literal[
        'full', 'limited', 'unknown',
    ]
    banding_observed: bool | None = None
    near_black_loss: bool | None = None
    chroma_detail_loss: bool | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('stimulus_ref') is None:
                raise ValueError(
                    'a gradation observation requires the pinned '
                    'stimulus'
                )
            if data.get('range_semantics') not in (
                'full', 'limited', 'unknown'
            ):
                raise ValueError('unknown range semantics')
            if data.get('range_semantics') == 'unknown':
                raise ValueError(
                    'a gradation observation requires declared '
                    'quantization range semantics (full/limited) — '
                    'mismatched range itself produces banding'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'observation_id', 'observation_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'DisplayedGradationObservation':
        return _seal(
            cls, payload, 'observation_id', 'observation_sha256', 'dgo'
        )


class ColourVolumeMeasurement(BaseModel):
    """Volumetric gamut measurement (#688) — measured colour volume
    in a declared space, not a 2D coverage percentage. IDMS v1.3
    method identity pinned."""

    model_config = ConfigDict(frozen=True)

    volume_id: str
    volume_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    colour_space: Literal[
        'cie_lab', 'ictcp', 'ciexyz', 'other', 'unknown',
    ]
    method: Literal[
        'idms_v1_3_gamut_volume', 'gamut_rings',
        'volumetric_characterisation', 'other',
    ]
    coverage_fraction: float | None = None
    reference_volume_ref: AuthorityRef | None = None
    measurement_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('colour_space') not in (
                'cie_lab', 'ictcp', 'ciexyz', 'other', 'unknown'
            ):
                raise ValueError('unknown colour space')
            if data.get('colour_space') == 'unknown':
                raise ValueError(
                    'a colour-volume measurement must declare its '
                    'colour space (L*a*b*/ICtCp volumes differ)'
                )
            if data.get('method') not in (
                'idms_v1_3_gamut_volume', 'gamut_rings',
                'volumetric_characterisation', 'other'
            ):
                raise ValueError('unknown volume method')
            if data.get('coverage_fraction') is not None and (
                data.get('reference_volume_ref') is None
            ):
                raise ValueError(
                    'a coverage fraction requires the pinned '
                    'reference volume'
                )
            if data.get('measurement_ref') is None:
                raise ValueError(
                    'a colour-volume claim requires measurement '
                    'evidence'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'volume_id', 'volume_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'ColourVolumeMeasurement':
        return _seal(
            cls, payload, 'volume_id', 'volume_sha256', 'cvol'
        )


class SpatialResolutionEvidence(BaseModel):
    """Resolved spatial detail evidence (#672) — contrast-modulation
    or MTF measurement bound to the installed chain (projector +
    lens + screen + processing), not the nominal raster."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    method: Literal[
        'contrast_modulation', 'mtf_slanted_edge', 'line_pattern',
        'other', 'unknown',
    ]
    resolving_element: str | None = None
    installed_chain_ref: AuthorityRef | None = None
    measurement_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('method') not in (
                'contrast_modulation', 'mtf_slanted_edge',
                'line_pattern', 'other', 'unknown'
            ):
                raise ValueError('unknown resolution method')
            if data.get('method') == 'unknown':
                raise ValueError(
                    'spatial-resolution evidence must declare its '
                    'method (contrast modulation / MTF / line '
                    'pattern)'
                )
            if data.get('measurement_ref') is None:
                raise ValueError(
                    'resolved-detail evidence requires measurement'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evidence_id', 'evidence_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'SpatialResolutionEvidence':
        return _seal(
            cls, payload, 'evidence_id', 'evidence_sha256', 'sres'
        )


DisplayFidelityVerdict = Literal[
    'qualified_fidelity',
    'nominal_spec_is_not_result',
    'triangle_is_not_volume',
    'raster_is_not_resolution',
    'range_semantics_unqualified',
]


def evaluate_display_fidelity_claim(
    gradation: DisplayedGradationObservation | None,
    volume: ColourVolumeMeasurement | None,
    resolution: SpatialResolutionEvidence | None,
    *,
    nominal_10bit_declared: bool = False,
    nominal_4k_declared: bool = False,
    gamut_coverage_2d_pct: float | None = None,
) -> tuple[DisplayFidelityVerdict, str]:
    """Judge a displayed-result claim (#660/#688/#672)."""
    if gradation is not None and (
        gradation.banding_observed is True
        or gradation.near_black_loss is True
        or gradation.chroma_detail_loss is True
    ):
        return (
            'range_semantics_unqualified',
            'displayed result shows banding/near-black/chroma loss '
            'despite nominal chain specs',
        )
    if gradation is None and nominal_10bit_declared:
        return (
            'nominal_spec_is_not_result',
            '10-bit/HDR chain reporting does not prove displayed '
            'gradation — no stimulus-bound observation',
        )
    if volume is None and gamut_coverage_2d_pct is not None:
        return (
            'triangle_is_not_volume',
            'a 2D gamut coverage figure does not describe the '
            'luminance-dependent colour volume',
        )
    if resolution is None and nominal_4k_declared:
        return (
            'raster_is_not_resolution',
            'a nominal 4K raster does not prove installed resolved '
            'detail — contrast modulation/MTF unmeasured',
        )
    return (
        'qualified_fidelity',
        'displayed result bound to stimulus/method-qualified '
        'evidence',
    )


DISPLAY_FIDELITY_LABELS: dict[str, str] = {
    'qualified_fidelity': '表示忠実度適格',
    'nominal_spec_is_not_result': '公称スペックは結果ではない',
    'triangle_is_not_volume': '2D色域は体積ではない',
    'raster_is_not_resolution': '公称ラスタは解像ではない',
    'range_semantics_unqualified': 'レンジ意味論未適格',
}
