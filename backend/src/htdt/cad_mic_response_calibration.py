"""Microphone response-calibration applicability authority (#991).

``CadMicrophoneCapture.calibration_profile`` records ``0deg``/``90deg``
and a comment says the profile must match the direction the mic was used
in — but nothing proves it. This module makes the correction profile its
own immutable authority and adds the applicability evaluation that binds
profile incidence semantics to actual/planned acquisition orientation.

Key rules honored:

- the response-correction profile is a distinct authority from absolute
  SPL calibration (#643) — a calibration file being present proves
  neither applicability nor that it was applied;
- incidence semantics live on the profile/instrument authority, never as
  a global "90deg == world +Z" assumption — different microphones define
  0°/90° differently;
- where the profile declares an explicit angular tolerance it is applied;
  where it does not, the exact angular mismatch is reported and no
  universal ±N° acceptance is invented;
- planned direction without observed pose supports ``assumed_compatible``
  at best — only observed pose can verify.
"""

from __future__ import annotations

from math import acos, degrees, isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_scene import Direction3, Position3
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash, canonicalize_payload






_SHA256_PATTERN = r'^[0-9a-f]{64}$'


CalibrationIncidenceKind = Literal[
    'on_axis_0deg',
    'perpendicular_90deg',
    'diffuse_field',
    'free_field',
    'custom_angle',
    'manufacturer_defined',
    'unknown',
]
"""How the correction profile's reference incidence is defined. The
physical meaning (e.g. '90°' == capsule axis toward ceiling) is declared
on the instrument/profile — never assumed globally."""

CalibrationApplicabilityState = Literal[
    'verified_compatible',
    'assumed_compatible',
    'incompatible',
    'unknown_orientation',
    'unknown_profile_semantics',
]

OrientationEvidenceSource = Literal['observed', 'planned', 'unknown']


class MicrophoneResponseCalibrationProfile(BaseModel):
    """Immutable microphone FR-correction authority (distinct from #643
    absolute SPL calibration)."""

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    instrument_manufacturer: str | None = None
    instrument_model: str | None = None
    instrument_serial: str | None = None
    correction_asset_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    correction_filename: str | None = None
    producer: str | None = None
    producer_version: str | None = None
    source_ref: str | None = None
    incidence_kind: CalibrationIncidenceKind = 'unknown'
    reference_axis: Direction3 | None = None
    reference_axis_semantics: str | None = None
    custom_angle_deg: float | None = None
    valid_frequency_hz: tuple[float, float] | None = None
    sample_rate_hz: tuple[float, ...] = ()
    interpolation_rule: str | None = None
    angle_tolerance_deg: float | None = Field(default=None, ge=0.0)
    uncertainty_json: str = '{}'
    notes: str | None = None
    provenance_json: str = '{}'
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_profile(self) -> 'MicrophoneResponseCalibrationProfile':
        if self.incidence_kind == 'custom_angle' and self.custom_angle_deg is None:
            raise ValueError('custom_angle incidence requires custom_angle_deg')
        if self.custom_angle_deg is not None and not (
            0.0 <= self.custom_angle_deg <= 180.0
        ):
            raise ValueError('custom_angle_deg must lie in [0, 180]')
        if self.valid_frequency_hz is not None:
            low, high = self.valid_frequency_hz
            if not (isfinite(low) and isfinite(high)) or not (0 < low < high):
                raise ValueError('valid_frequency_hz must satisfy 0 < low < high')
        if self.angle_tolerance_deg is not None and not isfinite(
            float(self.angle_tolerance_deg)
        ):
            raise ValueError('angle_tolerance_deg must be finite')
        if self.incidence_kind in (
            'on_axis_0deg',
            'perpendicular_90deg',
            'custom_angle',
        ) and self.reference_axis_semantics is None:
            raise ValueError(
                'directional incidence kinds require declared '
                'reference_axis_semantics (what 0°/90° means for this '
                'instrument)'
            )
        if self.profile_sha256 != _hash(self.identity_payload()):
            raise ValueError('response calibration profile hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'profile_sha256'})


class MicOrientationEvidence(BaseModel):
    """Planned (#471) and/or observed (#732) microphone axis evidence."""

    model_config = ConfigDict(frozen=True)

    planned_direction: Direction3 | None = None
    observed_direction: Direction3 | None = None
    observed_pose_provenance: str | None = None
    target_source_position: Position3 | None = None
    microphone_position: Position3 | None = None

    @model_validator(mode='after')
    def valid_evidence(self) -> 'MicOrientationEvidence':
        if (
            self.target_source_position is not None
            and self.microphone_position is None
        ):
            raise ValueError(
                'source-position evaluation requires microphone_position'
            )
        return self

    @property
    def evidence_source(self) -> OrientationEvidenceSource:
        if self.observed_direction is not None:
            return 'observed'
        if self.planned_direction is not None:
            return 'planned'
        return 'unknown'


def _angle_between_deg(a: Direction3, b: Direction3) -> float:
    dot = a.x * b.x + a.y * b.y + a.z * b.z
    return degrees(acos(max(-1.0, min(1.0, dot))))


class CalibrationApplicabilityVerdict(BaseModel):
    """One evaluation of profile-vs-orientation compatibility."""

    model_config = ConfigDict(frozen=True)

    verdict_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    measurement_id: str | None = None
    state: CalibrationApplicabilityState
    orientation_evidence: OrientationEvidenceSource = 'unknown'
    angular_mismatch_deg: float | None = None
    evaluated_frequency_hz: tuple[float, float] | None = None
    frequency_domain_applicable: bool | None = None
    reasons: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_verdict(self) -> 'CalibrationApplicabilityVerdict':
        if self.angular_mismatch_deg is not None:
            if not isfinite(float(self.angular_mismatch_deg)):
                raise ValueError('angular_mismatch_deg must be finite')
        if self.state == 'verified_compatible' and self.orientation_evidence != 'observed':
            raise ValueError(
                'verified compatibility requires observed orientation — a '
                'planned direction is an assumption'
            )
        if self.state == 'incompatible' and self.angular_mismatch_deg is None:
            raise ValueError(
                'incompatible verdicts must report the exact angular mismatch'
            )
        return self


def build_response_calibration_profile(
    **kwargs: Any,
) -> MicrophoneResponseCalibrationProfile:
    """Assemble and seal a :class:`MicrophoneResponseCalibrationProfile`."""
    payload = {'profile_sha256': '0' * 64, **kwargs}
    provisional = MicrophoneResponseCalibrationProfile.model_construct(**canonicalize_payload(MicrophoneResponseCalibrationProfile, dict(**payload)))
    payload['profile_sha256'] = _hash(provisional.identity_payload())
    return MicrophoneResponseCalibrationProfile(**payload)


def evaluate_calibration_angle_applicability(
    profile: MicrophoneResponseCalibrationProfile,
    evidence: MicOrientationEvidence,
    *,
    verdict_id: str,
    measurement_id: str | None = None,
) -> CalibrationApplicabilityVerdict:
    """Evaluate profile incidence semantics against orientation evidence.

    Fail-closed ladder:

    1. profile incidence unknown / manufacturer-defined without reference
       semantics → ``unknown_profile_semantics``;
    2. no orientation evidence at all → ``unknown_orientation``;
    3. directional incidences: angular mismatch is computed against the
       profile's declared reference direction (or against the exact
       source direction when a source position is bound);
    4. mismatch vs explicit profile tolerance → ``verified_compatible`` /
       ``incompatible`` under observed pose, ``assumed_compatible`` under
       planned-only evidence; no tolerance → the exact mismatch is
       reported with ``assumed_compatible`` and never a fabricated pass.
    """
    reasons: list[str] = []
    source = evidence.evidence_source

    if profile.incidence_kind == 'unknown' or (
        profile.incidence_kind == 'manufacturer_defined'
        and profile.reference_axis_semantics is None
    ):
        return CalibrationApplicabilityVerdict(
            verdict_id=verdict_id,
            profile_id=profile.profile_id,
            profile_sha256=profile.profile_sha256,
            measurement_id=measurement_id,
            state='unknown_profile_semantics',
            orientation_evidence=source,
            reasons=('profile does not declare reference-axis semantics',),
        )

    if source == 'unknown':
        return CalibrationApplicabilityVerdict(
            verdict_id=verdict_id,
            profile_id=profile.profile_id,
            profile_sha256=profile.profile_sha256,
            measurement_id=measurement_id,
            state='unknown_orientation',
            orientation_evidence='unknown',
            reasons=('no planned or observed microphone direction',),
        )

    direction = (
        evidence.observed_direction
        if evidence.observed_direction is not None
        else evidence.planned_direction
    )
    assert direction is not None  # narrowed by evidence_source
    observed = evidence.observed_direction is not None

    if profile.incidence_kind in ('free_field', 'diffuse_field'):
        # Orientation-independent semantics over the declared band.
        return CalibrationApplicabilityVerdict(
            verdict_id=verdict_id,
            profile_id=profile.profile_id,
            profile_sha256=profile.profile_sha256,
            measurement_id=measurement_id,
            state='verified_compatible' if observed else 'assumed_compatible',
            orientation_evidence=source,
            angular_mismatch_deg=None,
            reasons=(
                'profile incidence semantics are orientation-independent '
                'over its declared valid band',
            ),
        )

    # Directional semantics: 0°/90°/custom relative to a declared
    # reference; the reference is resolved to an exact direction when a
    # source position is bound, otherwise the declared reference axis.
    reference = profile.reference_axis
    if reference is None and evidence.target_source_position is not None:
        mic = evidence.microphone_position
        assert mic is not None
        delta = Position3(
            x_m=evidence.target_source_position.x_m - mic.x_m,
            y_m=evidence.target_source_position.y_m - mic.y_m,
            z_m=evidence.target_source_position.z_m - mic.z_m,
        )
        norm = (
            delta.x_m**2 + delta.y_m**2 + delta.z_m**2
        ) ** 0.5
        if norm <= 1e-9:
            return CalibrationApplicabilityVerdict(
                verdict_id=verdict_id,
                profile_id=profile.profile_id,
                profile_sha256=profile.profile_sha256,
                measurement_id=measurement_id,
                state='unknown_orientation',
                orientation_evidence=source,
                reasons=('source and microphone positions coincide',),
            )
        reference = Direction3(
            x=delta.x_m / norm, y=delta.y_m / norm, z=delta.z_m / norm
        )
    if reference is None:
        return CalibrationApplicabilityVerdict(
            verdict_id=verdict_id,
            profile_id=profile.profile_id,
            profile_sha256=profile.profile_sha256,
            measurement_id=measurement_id,
            state='unknown_profile_semantics',
            orientation_evidence=source,
            reasons=(
                'profile declares directional incidence but no reference '
                'axis and no source position is bound',
            ),
        )

    axis_angle = _angle_between_deg(direction, reference)
    required: float | None = None
    if profile.incidence_kind == 'on_axis_0deg':
        required = 0.0
    elif profile.incidence_kind == 'perpendicular_90deg':
        required = 90.0
    elif profile.incidence_kind == 'custom_angle':
        assert profile.custom_angle_deg is not None
        required = float(profile.custom_angle_deg)
    if required is None:
        return CalibrationApplicabilityVerdict(
            verdict_id=verdict_id,
            profile_id=profile.profile_id,
            profile_sha256=profile.profile_sha256,
            measurement_id=measurement_id,
            state='unknown_profile_semantics',
            orientation_evidence=source,
        )
    mismatch = abs(axis_angle - float(required))

    if profile.angle_tolerance_deg is None:
        reasons.append(
            'no explicit angular tolerance declared; exact mismatch '
            'reported without an invented acceptance band'
        )
        state: CalibrationApplicabilityState = 'assumed_compatible'
    elif mismatch <= profile.angle_tolerance_deg:
        reasons.append(
            f'mismatch {mismatch:.2f}deg within profile tolerance '
            f'{profile.angle_tolerance_deg:g}deg'
        )
        state = 'verified_compatible' if observed else 'assumed_compatible'
    else:
        reasons.append(
            f'mismatch {mismatch:.2f}deg exceeds profile tolerance '
            f'{profile.angle_tolerance_deg:g}deg'
        )
        state = 'incompatible'

    return CalibrationApplicabilityVerdict(
        verdict_id=verdict_id,
        profile_id=profile.profile_id,
        profile_sha256=profile.profile_sha256,
        measurement_id=measurement_id,
        state=state,
        orientation_evidence=source,
        angular_mismatch_deg=mismatch,
        evaluated_frequency_hz=profile.valid_frequency_hz,
        reasons=tuple(reasons),
    )
