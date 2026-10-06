"""Low-luminance / stray-light metrology authority (issue #756).

A calibrated luminance meter does NOT automatically produce valid
black-level or contrast evidence when detector offset/noise, stray
light, field-of-view contamination or veiling glare are comparable to
the DUT black signal. `unmeasurably low` must stay distinct from
`0 cd/m²`, and contrast must fail closed when the black denominator
approaches the measurement floor — infinite contrast is never a
measurement outcome.

Basis: SID/ICDM IDMS v1.3 (A2 stray light & veiling glare, A3
low-luminance measurements — never report zero black when luminance is
visible but below instrument capability), AVIXA V201.01:2021 (contrast
is an installed-system measurement).
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


StrayLightControl = Literal[
    'uncontrolled', 'aperture_matched', 'stray_light_elimination_tube',
    'mask_or_frustum', 'dark_environment', 'unknown',
]
BlackLevelVerdict = Literal[
    'measured_black', 'below_capability', 'contaminated',
    'infinite_contrast_rejected', 'insufficient_evidence',
]


STRAY_LABELS: dict[str, str] = {
    'uncontrolled': '迷光管理なし',
    'aperture_matched': '開口/FOV 整合',
    'stray_light_elimination_tube': '迷光除去筒',
    'mask_or_frustum': 'マスク/錐台',
    'dark_environment': '暗環境',
    'unknown': '不明',
}
BLACK_LABELS: dict[str, str] = {
    'measured_black': '黒レベル測定済み',
    'below_capability': '測定能力以下',
    'contaminated': '迷光/オフセット汚染あり',
    'infinite_contrast_rejected': '無限大コントラストは計測結果にしない',
    'insufficient_evidence': '証拠不足',
}


class DisplayLightMeasurementCapability(BaseModel):
    """Declared low-end capability of a light-measurement device.

    The floor at which offset/noise/stray light dominates the signal
    must be pinned before interpreting any black reading.
    """

    model_config = ConfigDict(frozen=True)

    capability_id: str
    capability_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    instrument_ref: AuthorityRef | None = None
    min_measurable_cd_m2: float | None = None
    dark_offset_cd_m2: float | None = None
    stray_light_control: StrayLightControl = 'unknown'
    calibration_ref: AuthorityRef | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'DisplayLightMeasurementCapability':
        if self.instrument_ref is not None:
            _require_refs(self.instrument_ref)
        if self.calibration_ref is not None:
            _require_refs(self.calibration_ref)
        if self.min_measurable_cd_m2 is not None:
            if self.min_measurable_cd_m2 <= 0:
                raise ValueError(
                    'min_measurable_cd_m2 must be positive — a zero '
                    'floor claims impossible capability'
                )
        if (
            self.dark_offset_cd_m2 is not None
            and self.dark_offset_cd_m2 < 0
        ):
            raise ValueError('dark_offset_cd_m2 cannot be negative')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'capability_id', 'capability_sha256'},
        )

    @classmethod
    def create(
        cls, **payload: Any
    ) -> 'DisplayLightMeasurementCapability':
        return _seal(
            cls, payload, 'capability_id', 'capability_sha256', 'lmc'
        )


class LowLuminanceObservation(BaseModel):
    """One black-level / low-luminance reading with contamination
    context pinned."""

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    capability_ref: AuthorityRef
    reading_cd_m2: float | None = None
    aperture_mm: float | None = None
    field_of_view_matches_pattern: bool | None = None
    visible_luminance_present: bool | None = None
    reading_data_ref: AuthorityRef | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'LowLuminanceObservation':
        _require_refs(self.capability_ref)
        if self.reading_data_ref is not None:
            _require_refs(self.reading_data_ref)
        if self.reading_cd_m2 is not None and self.reading_cd_m2 < 0:
            raise ValueError('reading_cd_m2 cannot be negative')
        if self.reading_cd_m2 is None and self.reading_data_ref is None:
            raise ValueError(
                'an observation needs a reading or pinned reading data'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'},
        )

    @classmethod
    def create(cls, **payload: Any) -> 'LowLuminanceObservation':
        return _seal(
            cls, payload, 'observation_id', 'observation_sha256', 'llo'
        )


def evaluate_black_claim(
    observation: LowLuminanceObservation | None,
    capability: DisplayLightMeasurementCapability | None,
) -> tuple[BlackLevelVerdict, str]:
    """Fail-closed black-level gate.

    Below the capability floor a reading is `below_capability`, never
    literal zero — and never grounds an infinite-contrast claim.
    Uncontrolled stray light contaminates the reading.
    """
    if observation is None or capability is None:
        return 'insufficient_evidence', 'no_observation_or_capability'
    if capability.stray_light_control in ('uncontrolled', 'unknown'):
        return 'contaminated', 'stray_light_uncontrolled'
    floor = capability.min_measurable_cd_m2
    if observation.reading_cd_m2 is None:
        return 'insufficient_evidence', 'reading_data_only'
    if floor is not None and observation.reading_cd_m2 < floor:
        if observation.visible_luminance_present:
            return 'below_capability', 'visible_below_floor_not_zero'
        return 'below_capability', 'below_capability_floor'
    if observation.field_of_view_matches_pattern is False:
        return 'contaminated', 'fov_mismatch'
    return 'measured_black', 'within_capability'


def evaluate_contrast_claim(
    white_cd_m2: float,
    black_verdict: BlackLevelVerdict,
) -> tuple[str, str]:
    """Contrast gate — infinite contrast is never a valid outcome."""
    if black_verdict == 'below_capability':
        return 'lower_bound_only', 'black_below_floor'
    if black_verdict in ('contaminated', 'insufficient_evidence'):
        return 'insufficient_evidence', 'black_not_resolved'
    if black_verdict == 'infinite_contrast_rejected':
        return 'infinite_contrast_rejected', 'never_a_measurand'
    return 'finite_contrast', 'measured_denominator'
