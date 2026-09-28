"""Laser-projection speckle authority (#1012 / SPKLE10).

Speckle is a coherence-driven spatial/color artifact bound to a
**projector + screen + geometry + viewing condition** — never a
projector-only equipment truth, and never derived from screen gain or
color metrics.

Authorities:

- :class:`LaserProjectionSpeckleCondition` — projector instance +
  light-engine mode + speckle-reduction state + screen
  material/installation + throw/UST class + image size + viewing
  position/direction + test wavelength/pattern.
- :class:`LaserProjectionSpeckleMeasurement` — one metric kind
  (``monochromatic_contrast`` / ``colour_speckle`` / ``image_quality`` /
  ``visual_observation`` — kept separate per IEC 62906-5-6 and CIE
  semantics, never folded into one ``speckle_score``) × one method.

Honesty rules:

- A measurement binds the projector spec triple AND the screen optical
  profile triple together — it is pair evidence, not single-device
  truth. Two screens with the same nominal gain need not share speckle
  behavior; UST and long-throw conditions are not interchangeable.
- ``camera_derived`` results require a :class:`SpeckleCameraProfile`
  (optics/sampling) — camera-speckle values from different optical
  sampling methods are not comparable peers.
- Manufacturer "speckle reduction" is device capability metadata until
  measured — the condition records the *state*, never an inferred
  improvement.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload




LightEngineMode = Literal[
    'rgb_laser', 'single_laser_phosphor', 'hybrid', 'lamp', 'led', 'unknown'
]
SpeckleReductionState = Literal[
    'off', 'on', 'mechanical', 'temporal', 'unknown'
]
ThrowClass = Literal['ust', 'short', 'standard', 'long', 'unknown']
TestWavelength = Literal[
    'red', 'green', 'blue', 'white', 'rgb_combined', 'other', 'unknown'
]
SpeckleMetricKind = Literal[
    'monochromatic_contrast',
    'colour_speckle',
    'image_quality',
    'visual_observation',
]
SpeckleMethod = Literal[
    'iec_62906_5_6',
    'cie_colour_speckle',
    'camera_derived',
    'visual',
    'other',
]
VisualObservation = Literal['not_noticed', 'visible', 'objectionable']


class LaserProjectionSpeckleCondition(BaseModel):
    """Exact projector+screen+viewing configuration (#1012 §1)."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['laser-speckle-condition-1'] = (
        'laser-speckle-condition-1'
    )
    condition_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    projector_instance_id: str | None = None
    projector_specification_id: str | None = None
    projector_specification_version: str | None = None
    projector_specification_sha256: str | None = None
    light_engine_mode: LightEngineMode = 'unknown'
    speckle_reduction_state: SpeckleReductionState = 'unknown'
    screen_optical_profile_id: str | None = None
    screen_optical_profile_version: str | None = None
    screen_optical_profile_sha256: str | None = None
    screen_surface_state: str | None = None
    throw_class: ThrowClass = 'unknown'
    throw_distance_m: float | None = Field(default=None, gt=0.0)
    image_width_m: float | None = Field(default=None, gt=0.0)
    image_height_m: float | None = Field(default=None, gt=0.0)
    viewing_distance_m: float | None = Field(default=None, gt=0.0)
    viewing_angle_deg: float | None = None
    focus_state: str | None = None
    test_wavelength: TestWavelength = 'unknown'
    test_pattern: str | None = None
    picture_light_mode: str | None = None
    ambient_observation_id: str | None = None
    condition_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'condition_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'LaserProjectionSpeckleCondition':
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
        ):
            if (None in triple) and any(v is not None for v in triple):
                raise ValueError(
                    f'{name} id/version/sha256 must be supplied together '
                    'or not at all'
                )
        expected = _hash(self.semantic_payload())
        if expected != self.condition_sha256:
            raise ValueError('laser speckle condition hash mismatch')
        return self


class SpeckleCameraProfile(BaseModel):
    """Camera optical/sampling identity for camera-derived speckle —
    different sampling methods produce non-comparable contrast values
    (#1012 §5)."""

    model_config = ConfigDict(frozen=True)

    camera_model: str = Field(min_length=1)
    lens: str | None = None
    aperture: str | None = None
    exposure_seconds: float | None = Field(default=None, gt=0.0)
    sensor_resolution_mp: float | None = Field(default=None, gt=0.0)
    sampling_note: str | None = None


class LaserProjectionSpeckleMeasurement(BaseModel):
    """One speckle metric under one method for one exact condition."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['laser-speckle-measurement-1'] = (
        'laser-speckle-measurement-1'
    )
    measurement_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    condition_id: str = Field(min_length=1)
    condition_version: str = Field(min_length=1)
    condition_sha256: str = Field(min_length=16)
    metric_kind: SpeckleMetricKind
    method: SpeckleMethod
    method_version: str | None = None
    value_fraction: float | None = Field(default=None, ge=0.0)
    visual_observation: VisualObservation | None = None
    camera_profile: SpeckleCameraProfile | None = None
    integration_seconds: float | None = Field(default=None, gt=0.0)
    raw_asset_sha256: str | None = None
    measured_at_utc: str | None = None
    instrument: str | None = None
    uncertainty: str | None = None
    note: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    measurement_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'measurement_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'LaserProjectionSpeckleMeasurement':
        if self.metric_kind == 'visual_observation':
            if self.visual_observation is None:
                raise ValueError(
                    'visual_observation metric requires a declared '
                    'observation (not_noticed/visible/objectionable)'
                )
        elif self.visual_observation is not None:
            raise ValueError(
                'visual_observation is only valid on the '
                'visual_observation metric — ordinal evidence is never '
                'mixed into instrumented metrics'
            )
        if self.method == 'camera_derived' and self.camera_profile is None:
            raise ValueError(
                'camera-derived speckle requires a camera_profile — '
                'optical sampling bounds the value'
            )
        if self.metric_kind != 'visual_observation' and (
            self.value_fraction is None
        ):
            raise ValueError(
                f'{self.metric_kind} measurement requires value_fraction'
            )
        expected = _hash(self.semantic_payload())
        if expected != self.measurement_sha256:
            raise ValueError('laser speckle measurement hash mismatch')
        return self


def build_laser_speckle_condition(**kwargs) -> LaserProjectionSpeckleCondition:
    probe = LaserProjectionSpeckleCondition.model_construct(**canonicalize_payload(LaserProjectionSpeckleCondition, dict(
        condition_sha256='x' * 64, **kwargs
    )))
    digest = _hash(probe.semantic_payload())
    return LaserProjectionSpeckleCondition(
        **probe.model_dump(
            mode='python', exclude={'condition_sha256', 'schema_version'}
        ),
        condition_sha256=digest,
    )


def build_laser_speckle_measurement(
    *, condition: LaserProjectionSpeckleCondition, **kwargs
) -> LaserProjectionSpeckleMeasurement:
    probe = LaserProjectionSpeckleMeasurement.model_construct(**canonicalize_payload(LaserProjectionSpeckleMeasurement, dict(
        condition_id=condition.condition_id,
        condition_version=condition.version,
        condition_sha256=condition.condition_sha256,
        measurement_sha256='x' * 64,
        **kwargs,
    )))
    digest = _hash(probe.semantic_payload())
    return LaserProjectionSpeckleMeasurement(
        **probe.model_dump(
            mode='python', exclude={'measurement_sha256', 'schema_version'}
        ),
        measurement_sha256=digest,
    )
