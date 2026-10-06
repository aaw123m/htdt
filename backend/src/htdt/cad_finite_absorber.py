"""Finite acoustic-treatment edge / size-effect authority (issue #694).

A measured or modelled infinite/extended planar absorber does not
automatically represent a finite wall panel, cloud, bass trap or
discrete treatment object of the same material — the size/edge
effect changes apparent diffuse-incidence absorption, and ISO-354
sample size itself changes the measured result for the same nominal
material.

Basis: finite-absorber size/edge-effect literature (diffuse/random-
incidence absorption of finite objects vs infinite-plane values);
ISO 354 sample-size sensitivity.
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


_MOUNTING_KINDS = (
    'wall_patch', 'free_hanging_cloud', 'corner_trap',
    'baffled_edge', 'spaced_panels', 'other', 'unknown',
)
_EDGE_STATES = ('exposed', 'sealed', 'baffled', 'mixed', 'unknown')
_REACTION_KINDS = ('locally_reacting', 'non_locally_reacting', 'unknown')

FiniteVerdict = Literal[
    'finite_model_declared',
    'infinite_plane_misapplied',
    'sample_size_unaccounted',
    'edge_effect_unqualified',
    'insufficient_geometry',
]


class FiniteAbsorberGeometry(BaseModel):
    """Declared finite treatment object geometry (#694) — dimensions,
    perimeter/edge state, mounting and neighboring baffle that make
    the object finite rather than an extended plane."""

    model_config = ConfigDict(frozen=True)

    geometry_id: str
    geometry_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    width_m: float
    height_m: float
    depth_m: float | None = None
    edge_state: Literal[
        'exposed', 'sealed', 'baffled', 'mixed', 'unknown'
    ] = 'unknown'
    mounting_kind: Literal[
        'wall_patch', 'free_hanging_cloud', 'corner_trap',
        'baffled_edge', 'spaced_panels', 'other', 'unknown',
    ] = 'unknown'
    neighboring_baffle: bool | None = None
    panel_spacing_m: float | None = None
    material_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for dim in ('width_m', 'height_m'):
                v = data.get(dim)
                if v is None or v <= 0:
                    raise ValueError(f'{dim} must be a positive extent')
            if data.get('edge_state') not in _EDGE_STATES:
                raise ValueError('unknown edge state')
            if data.get('mounting_kind') not in _MOUNTING_KINDS:
                raise ValueError('unknown mounting kind')
        return data

    @property
    def perimeter_to_area_ratio(self) -> float:
        return 2.0 * (self.width_m + self.height_m) / (
            self.width_m * self.height_m
        )

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'geometry_id', 'geometry_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'FiniteAbsorberGeometry':
        return _seal(
            cls, payload, 'geometry_id', 'geometry_sha256', 'fag'
        )


class FiniteTreatmentBoundaryModel(BaseModel):
    """Binds a finite geometry to the boundary model actually used by
    the solver (#694) — extended-plane equivalence claims require
    pinned edge-effect evidence."""

    model_config = ConfigDict(frozen=True)

    model_id: str
    model_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    geometry_ref: AuthorityRef
    material_evidence_ref: AuthorityRef | None = None
    reaction_kind: Literal[
        'locally_reacting', 'non_locally_reacting', 'unknown'
    ] = 'unknown'
    edge_effect_model_ref: AuthorityRef | None = None
    claims_infinite_plane_equivalence: bool = False

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('geometry_ref') is None:
                raise ValueError(
                    'a finite boundary model requires a pinned '
                    'geometry_ref'
                )
            if data.get('reaction_kind') not in _REACTION_KINDS:
                raise ValueError('unknown reaction kind')
            if (
                data.get('claims_infinite_plane_equivalence')
                and data.get('edge_effect_model_ref') is None
            ):
                raise ValueError(
                    'infinite-plane equivalence requires a pinned '
                    'edge-effect model/evidence'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'model_id', 'model_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'FiniteTreatmentBoundaryModel':
        return _seal(
            cls, payload, 'model_id', 'model_sha256', 'ftbm'
        )


def evaluate_finite_absorber_claim(
    geometry: FiniteAbsorberGeometry | None,
    model: FiniteTreatmentBoundaryModel | None,
    *,
    measurement_sample_is_finite: bool | None = None,
) -> tuple[FiniteVerdict, str]:
    """Judge whether an infinite-plane absorption value may stand for
    the finite installed object (#694).
    """
    if geometry is None:
        return (
            'insufficient_geometry',
            'no finite geometry declared — cannot reason about the '
            'size/edge effect',
        )
    if model is None:
        return (
            'infinite_plane_misapplied',
            'finite object with no boundary model — an infinite-plane '
            'value cannot represent it',
        )
    if (
        model.claims_infinite_plane_equivalence
        and model.edge_effect_model_ref is None
    ):
        return (
            'edge_effect_unqualified',
            'infinite-plane equivalence claimed without edge-effect '
            'evidence',
        )
    if measurement_sample_is_finite is False:
        return (
            'sample_size_unaccounted',
            'the lab sample was not the installed object — ISO-354 '
            'sample size changes the measured result',
        )
    if geometry.edge_state == 'unknown' or geometry.mounting_kind in (
        'unknown', 'other'
    ):
        return (
            'edge_effect_unqualified',
            'edge state / mounting undeclared — the edge effect is '
            'unqualified',
        )
    return (
        'finite_model_declared',
        'finite object bound to a boundary model with declared edge '
        'conditions',
    )


FINITE_LABELS: dict[str, str] = {
    'finite_model_declared': '有限吸音体モデル宣言済み',
    'infinite_plane_misapplied': '無限平面値の誤適用',
    'sample_size_unaccounted': '試料サイズ影響未考慮',
    'edge_effect_unqualified': '縁効果未適格',
    'insufficient_geometry': '幾何情報不足',
}
