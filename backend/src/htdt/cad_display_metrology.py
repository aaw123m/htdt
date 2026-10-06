"""Low-luminance metrology and projector dynamic-light authority
(issues #756, #759).

A calibrated luminance meter does not automatically produce valid
black-level or contrast evidence when detector offset/noise,
stray light, FOV contamination or veiling glare are comparable to
the DUT black signal (#756). And modern projectors change laser/
iris/tone-mapping with current and prior image content — one
scalar contrast is not a stable physical quantity unless stimulus,
adaptation history, control mode and observation time are pinned
(#759).

Basis: IDMS v1.3 low-luminance/stray-light guidance; projector
dynamic-contrast behavior documentation.
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


class LowLuminanceCapability(BaseModel):
    """Declared low-luminance measurement capability (#756) —
    minimum measurable luminance, zero/dark offset, stray-light
    control. 'unknown' contamination handling fails closed."""

    model_config = ConfigDict(frozen=True)

    capability_id: str
    capability_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    minimum_luminance_cd_m2: float | None = None
    dark_offset_cd_m2: float | None = None
    stray_light_control: Literal[
        'elimination_tube', 'mask', 'frustum', 'instrument_corrected',
        'none', 'unknown',
    ]
    fov_contamination_checked: bool | None = None
    instrument_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('stray_light_control') not in (
                'elimination_tube', 'mask', 'frustum',
                'instrument_corrected', 'none', 'unknown'
            ):
                raise ValueError('unknown stray-light control')
            if data.get('stray_light_control') == 'unknown':
                raise ValueError(
                    'a black/contrast claim requires declared '
                    'stray-light control — an unqualified meter '
                    'reads its own veiling glare'
                )
            if data.get('stray_light_control') == 'none' and (
                data.get('minimum_luminance_cd_m2') is None
            ):
                raise ValueError(
                    'without stray-light control a minimum '
                    'measurable luminance must be declared'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'capability_id', 'capability_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'LowLuminanceCapability':
        return _seal(
            cls, payload, 'capability_id', 'capability_sha256', 'llc'
        )


class DynamicContrastMeasurement(BaseModel):
    """Contrast measurement with pinned dynamics (#759) — stimulus,
    adaptation history, control mode and observation time; a bare
    scalar without these is not a stable physical quantity."""

    model_config = ConfigDict(frozen=True)

    measurement_id: str
    measurement_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    contrast_kind: Literal[
        'native_sequential', 'ansi', 'on_off', 'checkerboard',
        'dynamic_advertised', 'other', 'unknown',
    ]
    stimulus_ref: AuthorityRef | None = None
    adaptation_history: str | None = None
    control_mode: Literal[
        'laser_fixed', 'iris_manual', 'dynamic_light_control',
        'frame_adaptive', 'other', 'unknown',
    ] | None = None
    contrast_value: float | None = None
    capability_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('contrast_kind') not in (
                'native_sequential', 'ansi', 'on_off',
                'checkerboard', 'dynamic_advertised', 'other',
                'unknown',
            ):
                raise ValueError('unknown contrast kind')
            if data.get('contrast_kind') == 'unknown':
                raise ValueError(
                    'a contrast claim must declare its measurement '
                    'kind (native/ANSI/dynamic differ)'
                )
            if data.get('contrast_kind') == 'dynamic_advertised' and (
                data.get('control_mode') is None
                or data.get('adaptation_history') is None
            ):
                raise ValueError(
                    'a dynamic-contrast claim requires the control '
                    'mode and adaptation history — the value '
                    'depends on prior content'
                )
            if data.get('control_mode') == 'unknown':
                raise ValueError(
                    'contrast requires a declared light-control mode'
                )
            if data.get('stimulus_ref') is None:
                raise ValueError(
                    'contrast requires the pinned stimulus'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'measurement_id', 'measurement_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'DynamicContrastMeasurement':
        return _seal(
            cls, payload, 'measurement_id', 'measurement_sha256', 'dcm'
        )


MetrologyVerdict = Literal[
    'qualified_metrology',
    'calibrated_meter_is_not_black_evidence',
    'contrast_context_unpinned',
    'dynamic_is_not_stable_quantity',
    'capability_unqualified',
]


def evaluate_metrology_claim(
    capability: LowLuminanceCapability | None,
    contrast: DynamicContrastMeasurement | None,
    *,
    meter_calibrated: bool = False,
) -> tuple[MetrologyVerdict, str]:
    """Judge a black/contrast evidence claim (#756/#759)."""
    if capability is None:
        if meter_calibrated:
            return (
                'calibrated_meter_is_not_black_evidence',
                'a calibrated meter still reads its own offset and '
                'stray light at the DUT black level',
            )
        return (
            'capability_unqualified',
            'no low-luminance capability declared',
        )
    if contrast is None:
        return (
            'capability_unqualified',
            'capability declared but no contrast measurement bound',
        )
    if contrast.contrast_kind == 'dynamic_advertised':
        return (
            'dynamic_is_not_stable_quantity',
            'dynamic-contrast value recorded — not a stable '
            'physical quantity, treat as marketing context only',
        )
    return (
        'qualified_metrology',
        'capability-qualified contrast bound to pinned stimulus '
        'and control state',
    )


METROLOGY_LABELS: dict[str, str] = {
    'qualified_metrology': '計測適格',
    'calibrated_meter_is_not_black_evidence': '校正済み計器は黒証拠ではない',
    'contrast_context_unpinned': 'コントラスト文脈未固定',
    'dynamic_is_not_stable_quantity': 'ダイナミック値は安定量ではない',
    'capability_unqualified': '計測能力未適格',
}
