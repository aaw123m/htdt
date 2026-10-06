"""Direct-to-reverberant ratio (DRR) authority (issue #678).

DRR relates direct sound to the reflected/reverberant field and is
a documented cue for auditory distance perception. But DRR is
method dependent, especially in small rooms: direct-sound
integration window, omni vs binaural receiver, source angle,
source-near-boundary geometry, frequency band and non-diffuse
behavior all change the result — one unqualified scalar is not
honest.

Basis: DRR method-dependence literature (direct-window, receiver
kind, small-room non-diffuseness).
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


_RECEIVER_KINDS = ('omni', 'binaural', 'directional', 'other', 'unknown')

DrrVerdict = Literal[
    'qualified_drr',
    'method_unqualified',
    'cross_method_comparison',
    'unbounded_scalar',
]


class DRRMethodProfile(BaseModel):
    """Declared DRR computation method (#678) — window, receiver and
    geometry that make a DRR value comparable. 'unknown' fields fail
    closed."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    direct_window_ms: float
    receiver_kind: Literal[
        'omni', 'binaural', 'directional', 'other', 'unknown',
    ]
    source_angle_deg: float | None = None
    frequency_band_hz: tuple[float, float] | None = None
    source_near_boundary: bool | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('receiver_kind') not in _RECEIVER_KINDS:
                raise ValueError('unknown receiver kind')
            if data.get('receiver_kind') == 'unknown':
                raise ValueError(
                    'a DRR method must declare its receiver kind — '
                    'omni and binaural values differ'
                )
            w = data.get('direct_window_ms')
            if w is None or w <= 0:
                raise ValueError(
                    'a DRR method requires a positive direct-sound '
                    'integration window'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'DRRMethodProfile':
        return _seal(
            cls, payload, 'profile_id', 'profile_sha256', 'drrm'
        )


class DRRMeasurement(BaseModel):
    """One measured DRR value bound to its method (#678)."""

    model_config = ConfigDict(frozen=True)

    measurement_id: str
    measurement_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    method_ref: AuthorityRef
    drr_db: float
    position_ref: AuthorityRef | None = None
    measurement_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('method_ref') is None:
                raise ValueError(
                    'a DRR measurement requires a pinned method — a '
                    'bare scalar is method-dependent'
                )
            if data.get('measurement_ref') is None:
                raise ValueError(
                    'a measured DRR requires underlying measurement '
                    'evidence'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'measurement_id', 'measurement_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'DRRMeasurement':
        return _seal(
            cls, payload, 'measurement_id', 'measurement_sha256', 'drrv'
        )


def evaluate_drr_claim(
    method: DRRMethodProfile | None,
    measurement: DRRMeasurement | None,
    *,
    compared_method: DRRMethodProfile | None = None,
) -> tuple[DrrVerdict, str]:
    """Judge a DRR value/comparison (#678)."""
    if measurement is None:
        return (
            'unbounded_scalar',
            'no measurement record — an unqualified scalar DRR is '
            'not evidence',
        )
    if method is None:
        return (
            'method_unqualified',
            'no method profile — window and receiver kind unknown',
        )
    if compared_method is not None:
        same = (
            compared_method.direct_window_ms == method.direct_window_ms
            and compared_method.receiver_kind == method.receiver_kind
            and compared_method.frequency_band_hz == (
                method.frequency_band_hz
            )
        )
        if not same:
            return (
                'cross_method_comparison',
                'compared DRRs use different windows, receivers or '
                'bands — values are not comparable',
            )
    return (
        'qualified_drr',
        'method-qualified DRR bound to measurement evidence',
    )


DRR_LABELS: dict[str, str] = {
    'qualified_drr': '適格DRR',
    'method_unqualified': '手法未適格',
    'cross_method_comparison': '手法跨ぎ比較',
    'unbounded_scalar': '方法不明スカラー',
}
