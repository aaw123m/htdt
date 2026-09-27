"""Projection screen moiré compatibility authority (#1017 / MOIRE10).

Moiré is a beat-pattern interference between the projector's fixed
pixel grid and a woven/perforated screen microstructure — a
projector + screen + geometry + viewing problem, never a scalar
``screen.moire_safe`` flag on a material.

Authorities:

- :class:`ScreenMicrostructureAuthority` — physical screen structure
  (perforation pitch/diameter/open-area, weave repeat, dominant spatial
  frequencies, pattern-axis orientation). Fields may remain ``None``:
  unknown microstructure stays unknown and is never reverse-engineered
  from marketing photos.
- :class:`MoireCompatibilityCondition` — projector instance + native
  imaging + pixel-shift + input/output raster + screen material +
  microstructure binding + orientation + image size + viewing position.
- :class:`MoireObservation` — ``visual`` / ``camera_analysis`` /
  ``laboratory`` observation records. Camera observations carry the
  camera chain so camera-sensor aliasing is distinguishable from real
  screen moiré; a suspected camera artifact is flagged, not promoted.

MVP scope: observation/import authority only. ``woven`` vs
``perforated`` never auto-decides safety, and no universal pitch ratio
or viewing distance is asserted.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .canonical_json import canonical_sha256 as _hash




ScreenStructureKind = Literal['perforated', 'woven', 'hybrid', 'unknown']
MicrostructureEvidence = Literal['measured', 'manufacturer', 'unknown']
PixelShiftMode = Literal['off', '2x', '4x', 'e_shift', 'other', 'unknown']
MoireMethod = Literal['visual', 'camera_analysis', 'laboratory']
MoireArtifact = Literal[
    'none', 'beat_pattern', 'camera_aliasing_ambiguous', 'other'
]


class ScreenMicrostructureAuthority(BaseModel):
    """Physical screen microstructure evidence (#1017 §2)."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['screen-microstructure-1'] = (
        'screen-microstructure-1'
    )
    microstructure_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    screen_optical_profile_id: str | None = None
    screen_optical_profile_version: str | None = None
    screen_optical_profile_sha256: str | None = None
    structure_kind: ScreenStructureKind = 'unknown'
    perforation_pitch_mm: float | None = Field(default=None, gt=0.0)
    perforation_diameter_mm: float | None = Field(default=None, gt=0.0)
    open_area_fraction: float | None = Field(
        default=None, ge=0.0, le=1.0
    )
    weave_repeat_mm: float | None = Field(default=None, gt=0.0)
    dominant_spatial_frequency_cyc_mm: tuple[float, ...] = ()
    pattern_axis_deg: float | None = None
    evidence_kind: MicrostructureEvidence = 'unknown'
    measurement_asset_sha256: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    microstructure_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'microstructure_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'ScreenMicrostructureAuthority':
        triple = (
            self.screen_optical_profile_id,
            self.screen_optical_profile_version,
            self.screen_optical_profile_sha256,
        )
        if (None in triple) and any(v is not None for v in triple):
            raise ValueError(
                'screen optical profile id/version/sha256 must be '
                'supplied together or not at all'
            )
        structural = (
            self.perforation_pitch_mm is not None
            or self.perforation_diameter_mm is not None
            or self.open_area_fraction is not None
            or self.weave_repeat_mm is not None
            or bool(self.dominant_spatial_frequency_cyc_mm)
        )
        if structural and self.evidence_kind == 'unknown':
            raise ValueError(
                'microstructure figures require a declared evidence_kind '
                '— structure is never inferred from marketing material'
            )
        expected = _hash(self.semantic_payload())
        if expected != self.microstructure_sha256:
            raise ValueError('screen microstructure hash mismatch')
        return self


class MoireCompatibilityCondition(BaseModel):
    """Exact projector × screen × geometry configuration (#1017 §1)."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['moire-condition-1'] = (
        'moire-condition-1'
    )
    condition_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    projector_instance_id: str | None = None
    projector_specification_id: str | None = None
    projector_specification_version: str | None = None
    projector_specification_sha256: str | None = None
    native_resolution_w: int | None = Field(default=None, gt=0)
    native_resolution_h: int | None = Field(default=None, gt=0)
    pixel_shift_mode: PixelShiftMode = 'unknown'
    input_raster_label: str | None = None
    output_raster_label: str | None = None
    # Exact raster/imaging semantics known — without this, image_width /
    # horizontal_pixels is NOT a physical pixel pitch (#1017 §3).
    raster_semantics_exact: bool = False
    screen_optical_profile_id: str | None = None
    screen_optical_profile_version: str | None = None
    screen_optical_profile_sha256: str | None = None
    microstructure_id: str | None = None
    microstructure_version: str | None = None
    microstructure_sha256: str | None = None
    screen_rotation_deg: float | None = None
    image_width_m: float | None = Field(default=None, gt=0.0)
    image_height_m: float | None = Field(default=None, gt=0.0)
    throw_distance_m: float | None = Field(default=None, gt=0.0)
    scaling_state: str | None = None
    focus_state: str | None = None
    viewing_distance_m: float | None = Field(default=None, gt=0.0)
    test_pattern: str | None = None
    condition_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'condition_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'MoireCompatibilityCondition':
        for triple, name in (
            (
                (
                    self.projector_specification_id,
                    self.projector_specification_version,
                    self.projector_specification_sha256,
                ),
                'projector specification',
            ),
            (
                (
                    self.screen_optical_profile_id,
                    self.screen_optical_profile_version,
                    self.screen_optical_profile_sha256,
                ),
                'screen optical profile',
            ),
            (
                (
                    self.microstructure_id,
                    self.microstructure_version,
                    self.microstructure_sha256,
                ),
                'microstructure',
            ),
        ):
            if (None in triple) and any(v is not None for v in triple):
                raise ValueError(
                    f'{name} id/version/sha256 must be supplied together '
                    'or not at all'
                )
        expected = _hash(self.semantic_payload())
        if expected != self.condition_sha256:
            raise ValueError('moiré condition hash mismatch')
        return self


class MoireCameraProfile(BaseModel):
    """Camera chain identity — a camera sensor has its own pixel grid and
    can manufacture moiré that is screen- or projector-independent
    (#1017 §5)."""

    model_config = ConfigDict(frozen=True)

    camera_model: str = Field(min_length=1)
    lens: str | None = None
    sensor_resolution_mp: float | None = Field(default=None, gt=0.0)
    sampling_note: str | None = None


class MoireObservation(BaseModel):
    """One observation of moiré under an exact condition (#1017 §5)."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['moire-observation-1'] = (
        'moire-observation-1'
    )
    observation_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    condition_id: str = Field(min_length=1)
    condition_version: str = Field(min_length=1)
    condition_sha256: str = Field(min_length=16)
    method: MoireMethod
    observed_artifact: MoireArtifact = 'other'
    camera_profile: MoireCameraProfile | None = None
    camera_aliasing_suspected: bool = False
    spatial_period_mm: float | None = Field(default=None, gt=0.0)
    screen_region: str | None = None
    severity: str | None = None
    raw_asset_sha256: str | None = None
    observed_at_utc: str | None = None
    note: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    observation_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'observation_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'MoireObservation':
        if self.method == 'camera_analysis' and (
            self.camera_profile is None
        ):
            raise ValueError(
                'camera-analysis moiré observations require a '
                'camera_profile so sensor aliasing stays separable'
            )
        expected = _hash(self.semantic_payload())
        if expected != self.observation_sha256:
            raise ValueError('moiré observation hash mismatch')
        return self


def derive_projected_pixel_pitch_mm(
    condition: MoireCompatibilityCondition,
) -> float | None:
    """Physical on-screen pixel pitch — only when the imaging raster is
    exactly known (#1017 §3). Pixel-shift, scaling, overscan, anamorphic
    or warp states all defeat this, as does input resolution alone."""
    if not condition.raster_semantics_exact:
        return None
    if condition.pixel_shift_mode != 'off':
        # shifting/subpixel modes change the apparent spatial structure
        return None
    if (
        condition.image_width_m is None
        or condition.native_resolution_w is None
    ):
        return None
    return (
        condition.image_width_m / condition.native_resolution_w * 1000.0
    )


def build_screen_microstructure(**kwargs) -> ScreenMicrostructureAuthority:
    probe = ScreenMicrostructureAuthority.model_construct(
        microstructure_sha256='x' * 64, **kwargs
    )
    digest = _hash(probe.semantic_payload())
    return ScreenMicrostructureAuthority(
        **probe.model_dump(
            mode='python', exclude={'microstructure_sha256', 'schema_version'}
        ),
        microstructure_sha256=digest,
    )


def build_moire_condition(**kwargs) -> MoireCompatibilityCondition:
    probe = MoireCompatibilityCondition.model_construct(
        condition_sha256='x' * 64, **kwargs
    )
    digest = _hash(probe.semantic_payload())
    return MoireCompatibilityCondition(
        **probe.model_dump(
            mode='python', exclude={'condition_sha256', 'schema_version'}
        ),
        condition_sha256=digest,
    )


def build_moire_observation(
    *, condition: MoireCompatibilityCondition, **kwargs
) -> MoireObservation:
    probe = MoireObservation.model_construct(
        condition_id=condition.condition_id,
        condition_version=condition.version,
        condition_sha256=condition.condition_sha256,
        observation_sha256='x' * 64,
        **kwargs,
    )
    digest = _hash(probe.semantic_payload())
    return MoireObservation(
        **probe.model_dump(
            mode='python', exclude={'observation_sha256', 'schema_version'}
        ),
        observation_sha256=digest,
    )
