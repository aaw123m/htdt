"""Projection room-reflection contrast authority (#1043 / PROC10+PROC20).

Image-induced interreflection — light from the projected image bouncing
off walls/ceiling/floor back onto the screen — is a separate term from
external ambient light. This authority models room optical surfaces,
typed contrast stimuli, measured in-situ contrast and a bounded diffuse
room-return estimator.

Authorities:

- :class:`RoomOpticalSurfaceProfile` — diffuse reflectance of a room
  surface with evidence tiers. Separate from acoustic authority: a wall
  can be acoustically absorptive and optically light (or vice versa);
  acoustic absorption coefficients are never reused as optical
  reflectance, and a paint/visual color is not reflectance truth.
- :class:`RoomContrastStimulus` — typed stimulus identity
  (``on_off_sequential`` / ``ansi_checkerboard`` / ``fixed_apl`` /
  ``adl_declared`` / ``custom``) — these are different contrast
  quantities, never compared as interchangeable.
- :class:`InSituContrastMeasurement` — measured white/black/contrast
  under an exact stimulus (PROC10).
- :class:`ProjectedContrastDecomposition` — explicit per-term
  decomposition; unidentifiable components stay ``None`` and are never
  fabricated.

The estimator ``estimate_diffuse_room_return`` is explicitly labeled
``diffuse-room-return-bound-v1`` and fails closed when no surface
carries a numeric reflectance from a real evidence tier.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .canonical_json import canonical_sha256 as _hash




ROOM_RETURN_MODEL_VERSION = 'diffuse-room-return-bound-v1'

ReflectanceEvidenceTier = Literal[
    'measured', 'manufacturer', 'user_declared', 'unknown'
]
"""#1043 §7: ``user_declared``/``unknown`` can record a guess but can never
authorize a prediction — only ``measured``/``manufacturer`` feed the
estimator."""
_AUTHORITATIVE_TIERS = frozenset(('measured', 'manufacturer'))

SurfaceFinishClass = Literal[
    'matte', 'satin', 'gloss', 'mirror', 'unknown'
]
ContrastStimulusKind = Literal[
    'on_off_sequential',
    'ansi_checkerboard',
    'fixed_apl',
    'adl_declared',
    'custom',
]


class RoomOpticalSurfaceProfile(BaseModel):
    """Optical (not acoustic) reflectance of one room surface."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['room-optical-surface-1'] = (
        'room-optical-surface-1'
    )
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    surface_entity_id: str | None = None
    diffuse_reflectance_fraction: float | None = Field(
        default=None, ge=0.0, le=1.0
    )
    specular_fraction: float | None = Field(
        default=None, ge=0.0, le=1.0
    )
    finish_class: SurfaceFinishClass = 'unknown'
    directional_model_capability: bool = False
    wavelength_domain: str | None = None
    evidence_tier: ReflectanceEvidenceTier = 'unknown'
    measurement_method: str | None = None
    room_operating_state_id: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    profile_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'profile_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'RoomOpticalSurfaceProfile':
        if (
            self.diffuse_reflectance_fraction is not None
            and self.evidence_tier == 'unknown'
        ):
            raise ValueError(
                'diffuse reflectance requires an evidence tier — visual '
                'color is not reflectance truth'
            )
        expected = _hash(self.semantic_payload())
        if expected != self.profile_sha256:
            raise ValueError('room optical surface profile hash mismatch')
        return self


class RoomContrastStimulus(BaseModel):
    """Typed optical stimulus identity — contrast numbers under
    different stimuli are different quantities (#1043 §3)."""

    model_config = ConfigDict(frozen=True)

    stimulus_id: str = Field(min_length=1)
    stimulus_kind: ContrastStimulusKind
    stimulus_version: str | None = None
    apl_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    pattern_descriptor: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'RoomContrastStimulus':
        if self.stimulus_kind == 'fixed_apl' and self.apl_fraction is None:
            raise ValueError('fixed_apl stimulus requires apl_fraction')
        if self.stimulus_kind == 'custom' and (
            self.pattern_descriptor is None
        ):
            raise ValueError('custom stimulus requires pattern_descriptor')
        return self


class InSituContrastMeasurement(BaseModel):
    """PROC10: measured in-situ contrast evidence — authoritative as a
    measurement even when no model decomposition exists."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['in-situ-contrast-1'] = (
        'in-situ-contrast-1'
    )
    measurement_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    stimulus_id: str = Field(min_length=1)
    stimulus_kind: ContrastStimulusKind
    surface_entity_id: str | None = None
    white_luminance_cd_m2: float | None = Field(default=None, ge=0.0)
    black_luminance_cd_m2: float | None = Field(default=None, ge=0.0)
    contrast_ratio: float | None = Field(default=None, ge=0.0)
    room_operating_state_id: str | None = None
    measured_at_utc: str | None = None
    instrument: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    measurement_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'measurement_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'InSituContrastMeasurement':
        if (
            self.white_luminance_cd_m2 is None
            and self.black_luminance_cd_m2 is None
            and self.contrast_ratio is None
        ):
            raise ValueError(
                'in-situ contrast measurement requires at least one '
                'measured quantity'
            )
        expected = _hash(self.semantic_payload())
        if expected != self.measurement_sha256:
            raise ValueError('in-situ contrast measurement hash mismatch')
        return self


class ProjectedContrastDecomposition(BaseModel):
    """Explicit per-term contrast decomposition (#1043 §4).

    Component fields are ``None`` when not identifiable from evidence —
    the exact measurable total is retained and components are never
    fabricated.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['contrast-decomposition-1'] = (
        'contrast-decomposition-1'
    )
    decomposition_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    stimulus_id: str = Field(min_length=1)
    stimulus_kind: ContrastStimulusKind
    white_luminance_cd_m2: float | None = None
    black_luminance_cd_m2: float | None = None
    contrast_ratio: float | None = None
    projector_intrinsic_contrast: float | None = None
    external_ambient_black_lift_cd_m2: float | None = None
    room_return_lift_cd_m2: float | None = None
    screen_scatter_lift_cd_m2: float | None = None
    room_return_model_version: str | None = None
    components_identifiable: bool = False
    limitations: tuple[str, ...] = ()
    decomposition_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'decomposition_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'ProjectedContrastDecomposition':
        if not self.components_identifiable:
            component_fields = (
                self.projector_intrinsic_contrast,
                self.room_return_lift_cd_m2,
                self.screen_scatter_lift_cd_m2,
            )
            if any(v is not None for v in component_fields):
                raise ValueError(
                    'component values require '
                    'components_identifiable=True — unidentifiable '
                    'components stay None, never fabricated'
                )
        expected = _hash(self.semantic_payload())
        if expected != self.decomposition_sha256:
            raise ValueError('contrast decomposition hash mismatch')
        return self


def estimate_diffuse_room_return(
    *,
    image_mean_luminance_cd_m2: float,
    aperture_area_m2: float,
    surfaces: tuple[tuple[RoomOpticalSurfaceProfile, float], ...],
) -> float | None:
    """``diffuse-room-return-bound-v1``: bounded Lambertian-ish estimate
    of image-induced return luminance at the screen.

    ``surfaces`` pairs each optical profile with the solid-angle fraction
    of the room it subtends from the screen (sum ≤ 1). The estimate is
    ``rho_eff * image_mean_luminance / π`` where ``rho_eff`` is the
    area-weighted mean diffuse reflectance — an explicitly bounded upper
    model, never precise truth.

    Fails closed: ``None`` unless at least one bound surface carries a
    numeric reflectance from ``measured`` or ``manufacturer`` evidence
    (#1043 §6 — visual color and unprofiled surfaces are never modeled).
    """
    if image_mean_luminance_cd_m2 <= 0.0 or aperture_area_m2 <= 0.0:
        return None
    rho_sum = 0.0
    area_sum = 0.0
    usable = False
    for profile, solid_angle_fraction in surfaces:
        if (
            profile.diffuse_reflectance_fraction is None
            or profile.evidence_tier not in _AUTHORITATIVE_TIERS
        ):
            continue
        usable = True
        rho_sum += (
            profile.diffuse_reflectance_fraction * solid_angle_fraction
        )
        area_sum += solid_angle_fraction
    if not usable or area_sum <= 0.0:
        return None
    rho_eff = rho_sum / area_sum
    return rho_eff * image_mean_luminance_cd_m2 / math.pi


def build_room_optical_surface_profile(**kwargs) -> RoomOpticalSurfaceProfile:
    probe = RoomOpticalSurfaceProfile.model_construct(
        profile_sha256='x' * 64, **kwargs
    )
    digest = _hash(probe.semantic_payload())
    return RoomOpticalSurfaceProfile(
        **probe.model_dump(
            mode='python', exclude={'profile_sha256', 'schema_version'}
        ),
        profile_sha256=digest,
    )


def build_in_situ_contrast_measurement(
    **kwargs,
) -> InSituContrastMeasurement:
    probe = InSituContrastMeasurement.model_construct(
        measurement_sha256='x' * 64, **kwargs
    )
    digest = _hash(probe.semantic_payload())
    return InSituContrastMeasurement(
        **probe.model_dump(
            mode='python', exclude={'measurement_sha256', 'schema_version'}
        ),
        measurement_sha256=digest,
    )


def build_contrast_decomposition(**kwargs) -> ProjectedContrastDecomposition:
    probe = ProjectedContrastDecomposition.model_construct(
        decomposition_sha256='x' * 64, **kwargs
    )
    digest = _hash(probe.semantic_payload())
    return ProjectedContrastDecomposition(
        **probe.model_dump(
            mode='python', exclude={'decomposition_sha256', 'schema_version'}
        ),
        decomposition_sha256=digest,
    )
