"""Measured acoustic-field interpolation authority (issue #755).

A smooth heatmap or continuous 3D surface interpolated from sparse
independent measurements is not itself a continuously measured acoustic
field. Interpolation can invent extrema or suppress narrow spatial
structure; solver-assisted reconstruction can look measured even though
model assumptions supplied most of the spatial detail.

Basis: sound-field reconstruction literature — spatial extrapolation
of early RIRs from sparse measurements via equivalent sources
(Tsunokuni et al., Applied Acoustics 2021); sparse/compressed-sensing
reconstruction vs basic linear interpolation; phase/IR interpolation
is materially harder than scalar SPL interpolation.
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


InterpolationMethod = Literal[
    'linear', 'nearest', 'idw', 'kriging', 'spline', 'rbf',
    'compressed_sensing', 'equivalent_source', 'image_source_model',
    'solver_assisted', 'unknown',
]
FieldQuantity = Literal[
    'spl', 'level_db', 'rt60', 'edt', 'c50', 'c80', 'ts', 'drr',
    'decay', 'pressure', 'ir', 'complex_pressure', 'unknown',
]
CellSupportKind = Literal[
    'measured_support', 'interpolated', 'extrapolated',
    'model_assisted', 'unknown',
]
FieldClaimKind = Literal[
    'interpolated_surface', 'measured_surface',
    'reconstructed_field', 'model_aided_field', 'unknown',
]
FieldVerdict = Literal[
    'admissible', 'model_dependent', 'overclaimed',
    'insufficient_evidence',
]

METHOD_LABELS: dict[str, str] = {
    'linear': '線形補間', 'nearest': '最近傍',
    'idw': '逆距離加重', 'kriging': 'クリギング',
    'spline': 'スプライン', 'rbf': 'RBF',
    'compressed_sensing': '圧縮センシング',
    'equivalent_source': '等価音源法',
    'image_source_model': '鏡像音源モデル',
    'solver_assisted': 'ソルバー支援再構成',
    'unknown': '不明',
}
QUANTITY_LABELS: dict[str, str] = {
    'spl': 'SPL', 'level_db': 'レベル(dB)', 'rt60': 'RT60',
    'edt': 'EDT', 'c50': 'C50', 'c80': 'C80', 'ts': 'Ts',
    'drr': 'DRR', 'decay': '減衰', 'pressure': '音圧',
    'ir': 'インパルス応答', 'complex_pressure': '複素音圧',
    'unknown': '不明',
}
SUPPORT_LABELS: dict[str, str] = {
    'measured_support': '測定点支持',
    'interpolated': '補間', 'extrapolated': '外挿',
    'model_assisted': 'モデル支援', 'unknown': '不明',
}
FIELD_CLAIM_LABELS: dict[str, str] = {
    'interpolated_surface': '補間面',
    'measured_surface': '実測面',
    'reconstructed_field': '再構成音場',
    'model_aided_field': 'モデル支援音場',
    'unknown': '不明',
}
FIELD_VERDICT_LABELS: dict[str, str] = {
    'admissible': '表示可能',
    'model_dependent': 'モデル依存',
    'overclaimed': '過大主張',
    'insufficient_evidence': '証拠不足',
}

_COMPLEX_QUANTITIES = {'ir', 'complex_pressure', 'pressure'}
_COMPLEX_CAPABLE_METHODS = {
    'compressed_sensing', 'equivalent_source',
    'image_source_model', 'solver_assisted',
}


class InterpolationProfile(BaseModel):
    """Declared interpolation/reconstruction configuration."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    method: InterpolationMethod
    quantity: FieldQuantity = 'unknown'
    model_assisted: bool = False
    kernel_parameters: dict[str, Any] = Field(default_factory=dict)
    spatial_scale_m: float | None = Field(default=None, gt=0)
    method_ref: AuthorityRef | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'InterpolationProfile':
        if self.method_ref is not None:
            _require_refs(self.method_ref)
        if self.method == 'unknown':
            raise ValueError(
                'interpolation method unknown cannot anchor a profile'
            )
        if (
            self.model_assisted
            and self.method not in _COMPLEX_CAPABLE_METHODS
        ):
            raise ValueError(
                'model_assisted may only be declared for '
                'reconstruction-capable methods'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'InterpolationProfile':
        return _seal(
            cls, kwargs, 'profile_id', 'profile_sha256', 'fint'
        )


class FieldCell(BaseModel):
    """One surface cell: what actually supports its value."""

    model_config = ConfigDict(frozen=True)

    cell_id: str
    support_kind: CellSupportKind = 'unknown'
    value_ref: AuthorityRef | None = None
    nearest_measure_m: float | None = Field(default=None, ge=0)


class FieldSurfaceRecord(BaseModel):
    """One rendered spatial surface bound to measured support points."""

    model_config = ConfigDict(frozen=True)

    record_id: str
    record_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    measured_point_refs: tuple[AuthorityRef, ...] = ()
    cells: tuple[FieldCell, ...] = ()
    verdict: FieldVerdict = 'insufficient_evidence'
    reasons: tuple[str, ...] = ()
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'FieldSurfaceRecord':
        _require_refs(self.profile_ref)
        for ref in self.measured_point_refs:
            _require_refs(ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'record_id', 'record_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'FieldSurfaceRecord':
        return _seal(
            cls, kwargs, 'record_id', 'record_sha256', 'fsurf'
        )


def evaluate_field_claim(
    profile: InterpolationProfile,
    record: FieldSurfaceRecord,
    claim_kind: FieldClaimKind,
) -> tuple[FieldVerdict, tuple[str, ...]]:
    """Fail-closed gate on what a rendered surface may claim.

    - ``measured_surface`` requires every cell measured-supported — any
      interpolated/extrapolated/model cell makes it ``overclaimed``.
    - Complex quantities (IR/complex pressure) via plain interpolators
      are ``overclaimed`` for anything beyond ``interpolated_surface``.
    - ``model_aided_field``/``reconstructed_field`` claims are
      admissible but flagged ``model_dependent`` so UI never renders
      them as measured.
    """
    reasons: list[str] = []
    if record.profile_ref.ref_sha256 != profile.profile_sha256:
        return 'overclaimed', ('profile_mismatch',)
    if not record.measured_point_refs:
        return 'insufficient_evidence', ('no_measured_points',)
    if not record.cells:
        return 'insufficient_evidence', ('no_cells',)

    supports = {c.support_kind for c in record.cells}
    if 'unknown' in supports:
        reasons.append('untyped_cells')

    if claim_kind == 'measured_surface':
        if supports == {'measured_support'}:
            return 'admissible', ('all_cells_measured',)
        return 'overclaimed', (
            ('non_measured_cells_present',) + tuple(reasons)
        )

    if claim_kind in ('reconstructed_field', 'model_aided_field'):
        if (
            profile.quantity in _COMPLEX_QUANTITIES
            and profile.method not in _COMPLEX_CAPABLE_METHODS
        ):
            return 'overclaimed', ('method_not_complex_capable',)
        return 'model_dependent', (
            tuple(reasons) or ('model_assumptions_in_cells',)
        )

    if claim_kind == 'interpolated_surface':
        if 'measured_support' not in supports:
            return 'insufficient_evidence', ('no_measured_support',)
        if (
            profile.quantity in _COMPLEX_QUANTITIES
            and profile.method not in _COMPLEX_CAPABLE_METHODS
        ):
            return 'model_dependent', (
                'scalar_method_on_complex_quantity',
            )
        return 'admissible', tuple(reasons)

    return 'insufficient_evidence', ('claim_kind_unknown',)
