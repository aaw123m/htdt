"""Projection spatial image fidelity authority (#1010).

Measures focus/MTF-like sharpness, panel convergence and geometric
distortion across exact optical conditions — two geometrically valid
placements can produce different image quality.

Authorities:

- :class:`ProjectionOpticalCondition` — projector instance + lens
  identity + throw/zoom/focus/lens-shift + keystone state + pixel-shift +
  picture/light state + warmup. A sharpness result with unknown lens
  state is not reusable authority.
- :class:`SpatialImageQualityMeasurement` — pattern × method ×
  camera chain × spatial grid × derived metric results. Method tiers:
  ``visual_inspection`` (ordinal evidence), ``camera_image`` /
  ``slanted_edge_camera`` (camera-chain MTF — instrument limitation
  recorded, not hidden), ``laboratory_otf``.

Honesty rules:

- ``mtf50``/``mtfx`` results require a camera or laboratory method AND
  an exact ``algorithm_version``; a subjective photo is never "MTF",
  and values from different algorithms are not exact peers.
- Digital keystone/warping resamples the image — a condition with
  active digital correction must not silently claim native optical
  resolution (recorded via ``keystone_correction`` on the condition).
- Camera-derived results carry the camera chain profile; the projector
  is never credited with resolution beyond the instrument's own MTF.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance


def _hash(payload) -> str:
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()


SpatialQualityMethod = Literal[
    'visual_inspection',
    'camera_image',
    'slanted_edge_camera',
    'laboratory_otf',
]
SpatialGridPoint = Literal[
    'center',
    'corner_tl',
    'corner_tr',
    'corner_bl',
    'corner_br',
    'edge_t',
    'edge_b',
    'edge_l',
    'edge_r',
    'custom',
]
SpatialMetricKind = Literal[
    'mtf50',
    'mtfx',
    'subjective_sharpness',
    'convergence_offset_px',
    'geometric_distortion_pct',
    'focus_uniformity',
]
_MTF_KINDS = frozenset(('mtf50', 'mtfx'))
_CAMERA_METHODS = frozenset(
    ('camera_image', 'slanted_edge_camera', 'laboratory_otf')
)


class ProjectionOpticalCondition(BaseModel):
    """Exact optical setup a spatial result binds to (#1010 §1)."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['projection-optical-condition-1'] = (
        'projection-optical-condition-1'
    )
    condition_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    projector_instance_id: str | None = None
    projector_specification_id: str | None = None
    projector_specification_version: str | None = None
    projector_specification_sha256: str | None = None
    lens_identity: str | None = None
    throw_distance_m: float | None = Field(default=None, gt=0.0)
    throw_ratio: float | None = Field(default=None, gt=0.0)
    zoom_state: float | None = None
    focus_state: str | None = None
    lens_shift_h_fraction: float | None = None
    lens_shift_v_fraction: float | None = None
    optical_axis_deg: float | None = None
    keystone_correction: Literal[
        'none', 'horizontal', 'vertical', 'both', 'warp', 'unknown'
    ] = 'unknown'
    native_resolution_w: int | None = Field(default=None, gt=0)
    native_resolution_h: int | None = Field(default=None, gt=0)
    input_resolution_w: int | None = Field(default=None, gt=0)
    input_resolution_h: int | None = Field(default=None, gt=0)
    pixel_shift_mode: Literal[
        'off', '2x', '4x', 'e_shift', 'other', 'unknown'
    ] = 'unknown'
    picture_mode: str | None = None
    light_aperture_state: str | None = None
    screen_optical_profile_id: str | None = None
    screen_optical_profile_version: str | None = None
    screen_optical_profile_sha256: str | None = None
    image_width_m: float | None = Field(default=None, gt=0.0)
    image_height_m: float | None = Field(default=None, gt=0.0)
    presentation_profile_id: str | None = None
    warmup_minutes: float | None = Field(default=None, ge=0.0)
    condition_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'condition_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'ProjectionOpticalCondition':
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
            raise ValueError('projection optical condition hash mismatch')
        return self


class CameraChainProfile(BaseModel):
    """Measurement-chain camera identity (#1010 §4) — the instrument's own
    MTF bounds what a camera-derived result may claim."""

    model_config = ConfigDict(frozen=True)

    camera_model: str = Field(min_length=1)
    lens: str | None = None
    sensor_resolution_mp: float | None = Field(default=None, gt=0.0)
    sensor_mtf_note: str | None = None
    aperture: str | None = None
    focus_distance_m: float | None = None
    sampling_note: str | None = None


class SpatialQualityResult(BaseModel):
    """One derived metric at one grid point (#1010 §2-5).

    ``instrument_limited`` marks results where the measurement chain
    (usually the camera MTF) is the binding constraint — the projector
    is credited with no more than the instrument can resolve.
    """

    model_config = ConfigDict(frozen=True)

    grid_point: SpatialGridPoint
    grid_label: str | None = None
    metric_kind: SpatialMetricKind
    value: float
    unit: str = Field(min_length=1)
    algorithm_version: str | None = None
    instrument_limited: bool = False
    note: str | None = None


class SpatialImageQualityMeasurement(BaseModel):
    """Pattern × method × camera × grid measurement set (#1010 §2)."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['spatial-image-quality-measurement-1'] = (
        'spatial-image-quality-measurement-1'
    )
    measurement_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    condition_id: str = Field(min_length=1)
    condition_version: str = Field(min_length=1)
    condition_sha256: str = Field(min_length=16)
    pattern_identity: str | None = None
    pattern_version: str | None = None
    signal_path_id: str | None = None
    method: SpatialQualityMethod
    camera_profile: CameraChainProfile | None = None
    grid: tuple[SpatialGridPoint, ...] = ()
    raw_asset_sha256: tuple[str, ...] = ()
    results: tuple[SpatialQualityResult, ...] = ()
    measured_at_utc: str | None = None
    uncertainty: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    measurement_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'measurement_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'SpatialImageQualityMeasurement':
        camera_method = self.method in _CAMERA_METHODS
        if camera_method and self.method != 'laboratory_otf' and (
            self.camera_profile is None
        ):
            raise ValueError(
                'camera-derived spatial measurements require a '
                'camera_profile — the measurement chain bounds the claim'
            )
        for result in self.results:
            if result.metric_kind in _MTF_KINDS:
                if not camera_method:
                    raise ValueError(
                        f'{result.metric_kind} results require a camera or '
                        'laboratory method — a subjective photo is not MTF'
                    )
                if result.algorithm_version is None:
                    raise ValueError(
                        f'{result.metric_kind} results require an exact '
                        'algorithm_version — incompatible algorithms are '
                        'not exact peers'
                    )
        expected = _hash(self.semantic_payload())
        if expected != self.measurement_sha256:
            raise ValueError(
                'spatial image quality measurement hash mismatch'
            )
        return self


def build_projection_optical_condition(**kwargs) -> ProjectionOpticalCondition:
    probe = ProjectionOpticalCondition.model_construct(
        condition_sha256='x' * 64, **kwargs
    )
    digest = _hash(probe.semantic_payload())
    return ProjectionOpticalCondition(
        **probe.model_dump(
            mode='python', exclude={'condition_sha256', 'schema_version'}
        ),
        condition_sha256=digest,
    )


def build_spatial_image_quality_measurement(
    *, condition: ProjectionOpticalCondition, **kwargs
) -> SpatialImageQualityMeasurement:
    probe = SpatialImageQualityMeasurement.model_construct(
        condition_id=condition.condition_id,
        condition_version=condition.version,
        condition_sha256=condition.condition_sha256,
        measurement_sha256='x' * 64,
        **kwargs,
    )
    digest = _hash(probe.semantic_payload())
    return SpatialImageQualityMeasurement(
        **probe.model_dump(
            mode='python', exclude={'measurement_sha256', 'schema_version'}
        ),
        measurement_sha256=digest,
    )
