"""Solver-neutral spatial acoustic field authority (#517).

Separates the *field result* contract from point-receiver prediction results
and from any display interpolation. Canonical field data live on an exact
sampled regular grid at one exact frequency; slice/probe products are derived
views with exact-vs-interpolated flags — display resampling never mutates the
canonical solver data.

SPL is shown only when the result carries absolute pressure-reference
authority; phase only when complex authority exists. Phase masking is display
configuration, not a mutation of the field.
"""

from __future__ import annotations

from hashlib import sha256
import json
import math
from math import atan2, isfinite, log10, pi
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_equipment import FrequencyDomain
from .cad_scene import Position3


SPATIAL_FIELD_SCHEMA_VERSION = 1
SPATIAL_FIELD_REQUEST_AUTHORITY_VERSION = 'spatial-field-request-1'
SPATIAL_FIELD_RESULT_AUTHORITY_VERSION = 'spatial-field-result-1'

FieldQuantity = Literal[
    'complex_pressure',
    'pressure_magnitude_pa',
    'spl_db',
    'phase_deg',
]
FieldRepresentation = Literal['complex_pressure', 'magnitude_only']
AxisPlane = Literal['xy', 'xz', 'yz']
FieldInterpolation = Literal['exact_samples', 'trilinear']

MAX_FIELD_SAMPLES = 4_000_000


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


def _finite(value: float, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


class FieldPlaneRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    axis_plane: AxisPlane
    coordinate_m: float

    @field_validator('coordinate_m')
    @classmethod
    def finite_coordinate(cls, value: float) -> float:
        return _finite(value, field_name='plane coordinate')


class FieldProbeSetRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    points: tuple[Position3, ...] = Field(min_length=1)


class FieldVolumeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    origin: Position3
    size_m: Position3
    stride_m: float = Field(gt=0.0)

    @field_validator('stride_m')
    @classmethod
    def finite_stride(cls, value: float) -> float:
        return _finite(value, field_name='stride')


class SpatialFieldRequestSpec(BaseModel):
    """Immutable request: what the user asked the provider/UI to resolve.

    A provider may satisfy a plane/probe request directly without ever
    materializing a full volume — the request is first-class.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = SPATIAL_FIELD_SCHEMA_VERSION
    authority_version: Literal[
        'spatial-field-request-1'
    ] = SPATIAL_FIELD_REQUEST_AUTHORITY_VERSION
    request_id: str = Field(pattern=r'^spatial-field-request:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    acoustic_scene_snapshot_id: str = Field(min_length=1)
    acoustic_scene_snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_scenario_id: str = Field(min_length=1)
    source_scenario_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    solver_result_id: str = Field(min_length=1)
    solver_result_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    provider_id: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)

    quantity: FieldQuantity
    frequency_hz: float = Field(gt=0.0)
    plane: FieldPlaneRequest | None = None
    probe_set: FieldProbeSetRequest | None = None
    volume: FieldVolumeRequest | None = None
    requested_interpolation: FieldInterpolation = 'exact_samples'
    display_lod: Literal['full', 'decimated_2', 'decimated_4'] = 'full'

    @field_validator('frequency_hz')
    @classmethod
    def finite_frequency(cls, value: float) -> float:
        return _finite(value, field_name='frequency')

    @model_validator(mode='after')
    def validate_request(self) -> 'SpatialFieldRequestSpec':
        kinds = [
            self.plane is not None,
            self.probe_set is not None,
            self.volume is not None,
        ]
        if sum(kinds) != 1:
            raise ValueError('exactly one of plane/probe_set/volume is required')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('spatial field request semantic hash mismatch')
        if self.request_id != f'spatial-field-request:{expected}':
            raise ValueError('spatial field request id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'request_id', 'semantic_sha256'},
        )


def build_spatial_field_request(**kwargs: Any) -> SpatialFieldRequestSpec:
    probe = SpatialFieldRequestSpec.model_construct(
        request_id='spatial-field-request:' + '0' * 64,
        semantic_sha256='0' * 64,
        **kwargs,
    )
    digest = _digest(probe.semantic_payload())
    return SpatialFieldRequestSpec(
        request_id=f'spatial-field-request:{digest}',
        semantic_sha256=digest,
        **kwargs,
    )


SCENE_COORDINATE_FRAME_ID = 'htdt-scene-axes'
SCENE_COORDINATE_FRAME_VERSION = 'x-right-y-rear-z-up-right-handed-1'
"""Canonical spatial-field coordinate frame (#1027): the right-handed HTDT
scene frame (+X right / +Y rear / +Z up) every built result is stamped with.
"""

FieldIncompatibility = Literal[
    'coordinate_frame',
    'source_scenario',
    'grid_axes',
    'frequency',
    'pressure_reference',
    'absolute_reference_authority',
    'representation',
    'phasor_convention',
]
"""Typed reasons a field pair cannot be differenced (#1027). Order of
evaluation is fixed: frame identity first, then source/excitation
scenario, then the grid and value semantics."""

FieldComparisonPolicy = Literal['same_scenario', 'different_scenario']
"""A/B comparison policy (#1027): ``same_scenario`` (default) requires the
source/excitation scenario triple to be identical; ``different_scenario``
is the explicit opt-in for intentional A/B across scenarios."""


class RegularGridAxis(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    name: Literal['x_m', 'y_m', 'z_m']
    origin_m: float
    spacing_m: float = Field(gt=0.0)
    count: int = Field(gt=0)

    @field_validator('origin_m', 'spacing_m')
    @classmethod
    def finite_axis(cls, value: float) -> float:
        return _finite(value, field_name='grid axis')

    def coordinate(self, index: int) -> float:
        return self.origin_m + index * self.spacing_m


class SpatialFieldResult(BaseModel):
    """Canonical solver field on one exact regular grid at one frequency."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = SPATIAL_FIELD_SCHEMA_VERSION
    authority_version: Literal[
        'spatial-field-result-1'
    ] = SPATIAL_FIELD_RESULT_AUTHORITY_VERSION
    result_id: str = Field(pattern=r'^spatial-field-result:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    request_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    acoustic_scene_snapshot_id: str = Field(min_length=1)
    acoustic_scene_snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    solver_result_id: str = Field(min_length=1)
    solver_result_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    provider_id: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)

    # A/B-compatibility authorities (#1027). Present on every result built
    # through ``build_spatial_field_result``; absent only on payloads
    # persisted before the contract existed (excluded from the identity
    # hash then, so legacy payloads still revalidate).
    source_scenario_id: str | None = Field(default=None, min_length=1)
    source_scenario_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    coordinate_frame_id: str | None = Field(default=None, min_length=1)
    coordinate_frame_version: str | None = Field(
        default=None, min_length=1
    )

    frequency_hz: float = Field(gt=0.0)
    axes: tuple[RegularGridAxis, RegularGridAxis, RegularGridAxis]
    representation: FieldRepresentation
    pressure_real: tuple[float, ...] | None = None
    pressure_imag: tuple[float, ...] | None = None
    pressure_magnitude_pa: tuple[float, ...] | None = None
    pressure_unit: Literal['Pa'] = 'Pa'
    absolute_pressure_reference: bool
    pressure_reference_pa: float = 20.0e-6
    phasor_convention: str | None = None
    valid_frequency_domain: FrequencyDomain

    @field_validator('frequency_hz')
    @classmethod
    def finite_frequency(cls, value: float) -> float:
        return _finite(value, field_name='frequency')

    @model_validator(mode='after')
    def validate_result(self) -> 'SpatialFieldResult':
        names = [axis.name for axis in self.axes]
        if names != ['x_m', 'y_m', 'z_m']:
            raise ValueError('field grid axes must be ordered x_m, y_m, z_m')
        count = 1
        for axis in self.axes:
            count *= axis.count
        if count > MAX_FIELD_SAMPLES:
            raise ValueError('spatial field exceeds the bounded sample contract')
        if not self.valid_frequency_domain.contains(self.frequency_hz):
            raise ValueError('result frequency lies outside the valid domain')
        complex_parts = (self.pressure_real is None) + (self.pressure_imag is None)
        if self.representation == 'complex_pressure':
            if complex_parts != 0:
                raise ValueError('complex field requires real and imaginary parts')
            if len(self.pressure_real) != count or len(self.pressure_imag) != count:
                raise ValueError('field arrays must match the grid sample count')
            if self.phasor_convention is None:
                raise ValueError('complex field requires a phasor convention')
            if self.pressure_magnitude_pa is not None:
                raise ValueError('complex field cannot carry magnitude-only data')
        else:
            if complex_parts != 2:
                raise ValueError('magnitude-only field cannot carry complex parts')
            if self.pressure_magnitude_pa is None or len(self.pressure_magnitude_pa) != count:
                raise ValueError('magnitude-only field requires magnitude data')
            if any(
                not isfinite(float(v)) or float(v) < 0.0
                for v in self.pressure_magnitude_pa
            ):
                raise ValueError('magnitude field values must be finite non-negative')
        if self.representation == 'complex_pressure':
            for values in (self.pressure_real, self.pressure_imag):
                if any(not isfinite(float(v)) for v in values):
                    raise ValueError('complex field values must be finite')
        if (self.source_scenario_id is None) != (
            self.source_scenario_sha256 is None
        ):
            raise ValueError(
                'source scenario id/hash must be supplied together'
            )
        if (self.coordinate_frame_id is None) != (
            self.coordinate_frame_version is None
        ):
            raise ValueError(
                'coordinate frame id/version must be supplied together'
            )
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('spatial field result semantic hash mismatch')
        if self.result_id != f'spatial-field-result:{expected}':
            raise ValueError('spatial field result id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            mode='json',
            exclude={'result_id', 'semantic_sha256'},
        )
        for key in (
            'source_scenario_id',
            'source_scenario_sha256',
            'coordinate_frame_id',
            'coordinate_frame_version',
        ):
            if payload.get(key) is None:
                payload.pop(key)
        return payload

    @property
    def is_complex(self) -> bool:
        return self.representation == 'complex_pressure'

    def supports_quantity(self, quantity: FieldQuantity) -> tuple[bool, str | None]:
        if quantity == 'spl_db' and not self.absolute_pressure_reference:
            return False, 'SPL requires an absolute pressure reference authority'
        if quantity in ('complex_pressure', 'phase_deg') and not self.is_complex:
            return False, f'{quantity} requires complex pressure authority'
        return True, None


def build_spatial_field_result(
    *,
    request: SpatialFieldRequestSpec,
    axes: tuple[RegularGridAxis, RegularGridAxis, RegularGridAxis],
    representation: FieldRepresentation,
    pressure_real: tuple[float, ...] | None = None,
    pressure_imag: tuple[float, ...] | None = None,
    pressure_magnitude_pa: tuple[float, ...] | None = None,
    absolute_pressure_reference: bool,
    phasor_convention: str | None = None,
    valid_frequency_domain: FrequencyDomain,
    coordinate_frame_id: str | None = SCENE_COORDINATE_FRAME_ID,
    coordinate_frame_version: str | None = SCENE_COORDINATE_FRAME_VERSION,
) -> SpatialFieldResult:
    if not valid_frequency_domain.contains(request.frequency_hz):
        raise ValueError('request frequency is outside the valid domain')
    payload = {
        'schema_version': SPATIAL_FIELD_SCHEMA_VERSION,
        'authority_version': SPATIAL_FIELD_RESULT_AUTHORITY_VERSION,
        'request_semantic_sha256': request.semantic_sha256,
        'acoustic_scene_snapshot_id': request.acoustic_scene_snapshot_id,
        'acoustic_scene_snapshot_sha256': request.acoustic_scene_snapshot_sha256,
        'solver_result_id': request.solver_result_id,
        'solver_result_sha256': request.solver_result_sha256,
        'provider_id': request.provider_id,
        'provider_version': request.provider_version,
        'source_scenario_id': request.source_scenario_id,
        'source_scenario_sha256': request.source_scenario_sha256,
        'coordinate_frame_id': coordinate_frame_id,
        'coordinate_frame_version': coordinate_frame_version,
        'frequency_hz': request.frequency_hz,
        'axes': [axis.model_dump(mode='json') for axis in axes],
        'representation': representation,
        'pressure_real': list(pressure_real) if pressure_real is not None else None,
        'pressure_imag': list(pressure_imag) if pressure_imag is not None else None,
        'pressure_magnitude_pa': (
            list(pressure_magnitude_pa)
            if pressure_magnitude_pa is not None
            else None
        ),
        'pressure_unit': 'Pa',
        'absolute_pressure_reference': absolute_pressure_reference,
        'pressure_reference_pa': 20.0e-6,
        'phasor_convention': phasor_convention,
        'valid_frequency_domain': valid_frequency_domain.model_dump(mode='json'),
    }
    digest = _digest(payload)
    return SpatialFieldResult(
        request_semantic_sha256=request.semantic_sha256,
        acoustic_scene_snapshot_id=request.acoustic_scene_snapshot_id,
        acoustic_scene_snapshot_sha256=request.acoustic_scene_snapshot_sha256,
        solver_result_id=request.solver_result_id,
        solver_result_sha256=request.solver_result_sha256,
        provider_id=request.provider_id,
        provider_version=request.provider_version,
        source_scenario_id=request.source_scenario_id,
        source_scenario_sha256=request.source_scenario_sha256,
        coordinate_frame_id=coordinate_frame_id,
        coordinate_frame_version=coordinate_frame_version,
        frequency_hz=request.frequency_hz,
        axes=axes,
        representation=representation,
        pressure_real=pressure_real,
        pressure_imag=pressure_imag,
        pressure_magnitude_pa=pressure_magnitude_pa,
        absolute_pressure_reference=absolute_pressure_reference,
        phasor_convention=phasor_convention,
        valid_frequency_domain=valid_frequency_domain,
        result_id=f'spatial-field-result:{digest}',
        semantic_sha256=digest,
    )


def _index(result: SpatialFieldResult, ix: int, iy: int, iz: int) -> int:
    nx, ny, _ = (axis.count for axis in result.axes)
    return (iz * ny + iy) * nx + ix


_PLANE_AXES: dict[AxisPlane, tuple[int, int, int]] = {
    'xy': (0, 1, 2),
    'xz': (0, 2, 1),
    'yz': (1, 2, 0),
}


class FieldSliceView(BaseModel):
    """Derived display product; canonical samples unchanged."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    result_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    plane: FieldPlaneRequest
    quantity: FieldQuantity
    unit: str = Field(min_length=1)
    rows: tuple[tuple[float, ...], ...] = Field(min_length=1)
    row_axis: Literal['x_m', 'y_m', 'z_m']
    column_axis: Literal['x_m', 'y_m', 'z_m']
    row_coordinates_m: tuple[float, ...] = Field(min_length=1)
    column_coordinates_m: tuple[float, ...] = Field(min_length=1)
    sample_state: Literal['exact', 'interpolated']
    masked_positions: tuple[tuple[int, int], ...] = ()
    cache_key_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def validate_view(self) -> 'FieldSliceView':
        if len(self.rows) != len(self.row_coordinates_m):
            raise ValueError('slice row count must match row coordinates')
        for row in self.rows:
            if len(row) != len(self.column_coordinates_m):
                raise ValueError('slice columns must match column coordinates')
        if self.sample_state == 'exact' and self.masked_positions:
            for row_index, column_index in self.masked_positions:
                if not (
                    0 <= row_index < len(self.rows)
                    and 0 <= column_index < len(self.rows[0])
                ):
                    raise ValueError('masked position outside slice extent')
        return self


def field_slice_cache_key(
    result: SpatialFieldResult,
    plane: FieldPlaneRequest,
    quantity: FieldQuantity,
    interpolation: FieldInterpolation,
    display_lod: str = 'full',
    phase_mask_min_magnitude_pa: float | None = None,
) -> str:
    """Derived-cache key: exact upstream result + view configuration."""
    return _digest(
        {
            'kind': 'field-slice-cache',
            'result_semantic_sha256': result.semantic_sha256,
            'plane': plane.model_dump(mode='json'),
            'quantity': quantity,
            'interpolation': interpolation,
            'display_lod': display_lod,
            'phase_mask_min_magnitude_pa': phase_mask_min_magnitude_pa,
        }
    )


def _plane_indices(
    result: SpatialFieldResult,
    plane: FieldPlaneRequest,
    *,
    tolerance_m: float = 1e-9,
) -> tuple[int, int, int] | None:
    """Return the exact grid index of the plane coordinate, or None."""
    fixed_axis = _PLANE_AXES[plane.axis_plane][2]
    axis = result.axes[fixed_axis]
    for index in range(axis.count):
        if abs(axis.coordinate(index) - plane.coordinate_m) <= tolerance_m:
            return index
    return None


def _quantity_value(
    result: SpatialFieldResult,
    flat_index: int,
    quantity: FieldQuantity,
) -> float:
    if quantity == 'complex_pressure':
        return complex(
            result.pressure_real[flat_index], result.pressure_imag[flat_index]
        )
    if quantity in ('pressure_magnitude_pa', 'spl_db', 'phase_deg'):
        if result.is_complex:
            magnitude = abs(
                complex(
                    result.pressure_real[flat_index],
                    result.pressure_imag[flat_index],
                )
            )
        else:
            magnitude = result.pressure_magnitude_pa[flat_index]
        if quantity == 'pressure_magnitude_pa':
            return magnitude
        if quantity == 'spl_db':
            if magnitude <= 0.0:
                return -400.0
            return 20.0 * log10(magnitude / result.pressure_reference_pa)
        return atan2(
            result.pressure_imag[flat_index],
            result.pressure_real[flat_index],
        ) * 180.0 / pi
    raise ValueError(f'unsupported quantity: {quantity}')


# Physical acoustic energy density needs density, sound speed and particle
# velocity authorities this contract does not carry (#952); a dimensionless
# pressure proxy must never be mislabeled as J/m3, so the quantity is absent.
_QUANTITY_UNIT: dict[FieldQuantity, str] = {
    'complex_pressure': 'Pa',
    'pressure_magnitude_pa': 'Pa',
    'spl_db': 'dB SPL',
    'phase_deg': 'deg',
}


def extract_field_slice(
    result: SpatialFieldResult,
    plane: FieldPlaneRequest,
    quantity: FieldQuantity,
    *,
    phase_mask_min_magnitude_pa: float | None = None,
) -> FieldSliceView:
    """Extract one axis-aligned slice of canonical samples.

    Slices are exact-sample only: a plane that does not coincide with a grid
    plane fails closed, and the caller must request interpolation explicitly
    through a separate derived product (not silently applied here).
    """
    supported, reason = result.supports_quantity(quantity)
    if not supported:
        raise ValueError(f'field does not support requested quantity: {reason}')
    if quantity == 'complex_pressure':
        raise ValueError(
            'complex_pressure needs a two-component view; scalar slice '
            'quantities are magnitude, SPL and phase'
        )
    if quantity == 'phase_deg' and not result.is_complex:
        raise ValueError('phase view requires complex pressure authority')

    fixed_index = _plane_indices(result, plane)
    if fixed_index is None:
        raise ValueError(
            'requested plane does not coincide with the canonical grid; '
            'exact-sample slices never interpolate silently'
        )

    axis_row, axis_col, axis_fixed = _PLANE_AXES[plane.axis_plane]
    row_axis = result.axes[axis_row]
    col_axis = result.axes[axis_col]

    rows: list[tuple[float, ...]] = []
    masked: list[tuple[int, int]] = []
    for row_i in range(row_axis.count):
        row_values: list[float] = []
        for col_i in range(col_axis.count):
            indices = [0, 0, 0]
            indices[axis_row] = row_i
            indices[axis_col] = col_i
            indices[axis_fixed] = fixed_index
            flat = _index(result, indices[0], indices[1], indices[2])
            value = _quantity_value(result, flat, quantity)
            if (
                quantity == 'phase_deg'
                and phase_mask_min_magnitude_pa is not None
            ):
                magnitude = _quantity_value(result, flat, 'pressure_magnitude_pa')
                if magnitude < phase_mask_min_magnitude_pa:
                    masked.append((row_i, col_i))
            row_values.append(value)
        rows.append(tuple(row_values))

    return FieldSliceView(
        result_semantic_sha256=result.semantic_sha256,
        plane=plane,
        quantity=quantity,
        unit=_QUANTITY_UNIT[quantity],
        rows=tuple(rows),
        row_axis=row_axis.name,
        column_axis=col_axis.name,
        row_coordinates_m=tuple(
            row_axis.coordinate(i) for i in range(row_axis.count)
        ),
        column_coordinates_m=tuple(
            col_axis.coordinate(i) for i in range(col_axis.count)
        ),
        sample_state='exact',
        masked_positions=tuple(masked),
        cache_key_sha256=field_slice_cache_key(
            result,
            plane,
            quantity,
            'exact_samples',
            phase_mask_min_magnitude_pa=phase_mask_min_magnitude_pa,
        ),
    )


class FieldProbeValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    result_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    position: Position3
    quantity: FieldQuantity
    unit: str = Field(min_length=1)
    value: float
    # 'exact': grid-node sample. 'interpolated': true trilinear blend of the
    # 8 bounding samples. 'nearest_sample': the nearest node was returned —
    # no interpolation ever happened (#951).
    sample_state: Literal['exact', 'interpolated', 'nearest_sample']
    requested_position: Position3
    sampled_position: Position3
    distance_m: float = Field(ge=0.0)
    nearest_sample_index: tuple[int, int, int]

    @field_validator('value', 'distance_m')
    @classmethod
    def finite_value(cls, value: float) -> float:
        return _finite(value, field_name='probe value')


def _probe_complex_at(result: SpatialFieldResult, flat_index: int) -> complex:
    if result.is_complex:
        return complex(
            result.pressure_real[flat_index], result.pressure_imag[flat_index]
        )
    return complex(result.pressure_magnitude_pa[flat_index], 0.0)


def _quantity_from_complex(
    value: complex,
    magnitude_only: float | None,
    quantity: FieldQuantity,
    result: SpatialFieldResult,
) -> float:
    """Derive the scalar quantity AFTER any interpolation (#951).

    Complex probes interpolate real/imag components and derive magnitude,
    SPL and phase from the interpolated pressure — wrapped phase is never
    interpolated directly.
    """
    magnitude = abs(value) if result.is_complex else float(magnitude_only)
    if quantity == 'pressure_magnitude_pa':
        return magnitude
    if quantity == 'spl_db':
        if magnitude <= 0.0:
            return -400.0
        return 20.0 * log10(magnitude / result.pressure_reference_pa)
    return atan2(value.imag, value.real) * 180.0 / pi


def probe_field(
    result: SpatialFieldResult,
    position: Position3,
    quantity: FieldQuantity,
    *,
    interpolation: FieldInterpolation = 'exact_samples',
    snap_tolerance_m: float = 1e-9,
) -> FieldProbeValue:
    """Probe the field at an arbitrary position with explicit semantics.

    ``exact_samples`` returns the nearest node unchanged and reports
    ``nearest_sample``; ``trilinear`` performs true trilinear interpolation
    over the 8 bounding samples. Off-domain positions fail closed.
    """
    supported, reason = result.supports_quantity(quantity)
    if not supported:
        raise ValueError(f'field does not support requested quantity: {reason}')
    if quantity == 'complex_pressure':
        raise ValueError(
            'complex_pressure needs a two-component view; scalar probe '
            'quantities are magnitude, SPL and phase'
        )

    coords = (float(position.x_m), float(position.y_m), float(position.z_m))
    # Fail closed outside the sampled domain on any axis.
    for axis, coordinate in zip(result.axes, coords):
        lo = axis.coordinate(0)
        hi = axis.coordinate(axis.count - 1)
        if coordinate < lo - snap_tolerance_m or (
            coordinate > hi + snap_tolerance_m
        ):
            raise ValueError(
                'probe position lies outside the sampled field domain'
            )

    # Nearest node (always computed — it is the reported fallback/state).
    nearest: list[int] = []
    exact = True
    for axis, coordinate in zip(result.axes, coords):
        best = 0
        best_distance = float('inf')
        for index in range(axis.count):
            distance = abs(axis.coordinate(index) - coordinate)
            if distance < best_distance:
                best = index
                best_distance = distance
        if best_distance > snap_tolerance_m:
            exact = False
        nearest.append(best)
    nearest_flat = _index(result, nearest[0], nearest[1], nearest[2])
    sampled_position = Position3(
        x_m=result.axes[0].coordinate(nearest[0]),
        y_m=result.axes[1].coordinate(nearest[1]),
        z_m=result.axes[2].coordinate(nearest[2]),
    )
    distance_m = math.sqrt(
        sum(
            (coordinate - sampled)
            ** 2
            for coordinate, sampled in zip(
                coords,
                (
                    float(sampled_position.x_m),
                    float(sampled_position.y_m),
                    float(sampled_position.z_m),
                ),
            )
        )
    )

    if exact or interpolation == 'exact_samples':
        value = _quantity_value(result, nearest_flat, quantity)
        return FieldProbeValue(
            result_semantic_sha256=result.semantic_sha256,
            position=position,
            quantity=quantity,
            unit=_QUANTITY_UNIT[quantity],
            value=value,
            sample_state='exact' if exact else 'nearest_sample',
            requested_position=position,
            sampled_position=sampled_position,
            distance_m=distance_m,
            nearest_sample_index=(nearest[0], nearest[1], nearest[2]),
        )

    # True trilinear interpolation over the 8 bounding nodes.
    lower: list[int] = []
    upper: list[int] = []
    fraction: list[float] = []
    for axis, coordinate, node in zip(result.axes, coords, nearest):
        node_coordinate = axis.coordinate(node)
        if abs(coordinate - node_coordinate) <= snap_tolerance_m:
            lower.append(node)
            upper.append(node)
            fraction.append(0.0)
            continue
        if axis.count < 2:
            raise ValueError(
                'cannot interpolate along a single-sample axis'
            )
        if coordinate < node_coordinate:
            lo_index, hi_index = node - 1, node
        else:
            lo_index, hi_index = node, min(node + 1, axis.count - 1)
        lo_coordinate = axis.coordinate(lo_index)
        hi_coordinate = axis.coordinate(hi_index)
        lower.append(lo_index)
        upper.append(hi_index)
        fraction.append(
            (coordinate - lo_coordinate) / (hi_coordinate - lo_coordinate)
        )

    complex_sum = 0j
    magnitude_sum = 0.0
    for dx in (0, 1):
        for dy in (0, 1):
            for dz in (0, 1):
                weight = (
                    (fraction[0] if dx else 1.0 - fraction[0])
                    * (fraction[1] if dy else 1.0 - fraction[1])
                    * (fraction[2] if dz else 1.0 - fraction[2])
                )
                flat = _index(
                    result,
                    upper[0] if dx else lower[0],
                    upper[1] if dy else lower[1],
                    upper[2] if dz else lower[2],
                )
                sample = _probe_complex_at(result, flat)
                complex_sum += weight * sample
                magnitude_sum += weight * abs(sample)
    value = _quantity_from_complex(
        complex_sum, magnitude_sum, quantity, result
    )
    return FieldProbeValue(
        result_semantic_sha256=result.semantic_sha256,
        position=position,
        quantity=quantity,
        unit=_QUANTITY_UNIT[quantity],
        value=value,
        sample_state='interpolated',
        requested_position=position,
        sampled_position=position,
        distance_m=distance_m,
        nearest_sample_index=(nearest[0], nearest[1], nearest[2]),
    )


def check_field_compatibility(
    left: SpatialFieldResult,
    right: SpatialFieldResult,
    *,
    comparison_policy: FieldComparisonPolicy = 'same_scenario',
) -> tuple[FieldIncompatibility, ...]:
    """Typed A/B compatibility verdict (#1027).

    Coordinate-frame identity is checked BEFORE grid equality: two fields
    sampled on equal axes in different frames are not subtractable. The
    source/excitation scenario triple must be identical unless the caller
    explicitly opts into a ``different_scenario`` comparison — an
    intentional A/B across scenarios still requires the same frame and
    the same value semantics.
    """
    reasons: list[FieldIncompatibility] = []
    if (
        left.coordinate_frame_id != right.coordinate_frame_id
        or left.coordinate_frame_version != right.coordinate_frame_version
    ):
        reasons.append('coordinate_frame')
    if (
        comparison_policy == 'same_scenario'
        and (
            left.source_scenario_id != right.source_scenario_id
            or left.source_scenario_sha256 != right.source_scenario_sha256
        )
    ):
        reasons.append('source_scenario')
    if left.axes != right.axes:
        reasons.append('grid_axes')
    if left.frequency_hz != right.frequency_hz:
        reasons.append('frequency')
    if left.pressure_reference_pa != right.pressure_reference_pa:
        reasons.append('pressure_reference')
    if (
        left.absolute_pressure_reference
        != right.absolute_pressure_reference
    ):
        reasons.append('absolute_reference_authority')
    if left.representation != right.representation:
        reasons.append('representation')
    return tuple(reasons)


def field_difference(
    left: SpatialFieldResult,
    right: SpatialFieldResult,
    *,
    difference_semantics: Literal['db_delta', 'complex_difference', 'magnitude_ratio'],
    comparison_policy: FieldComparisonPolicy = 'same_scenario',
) -> list[float] | list[complex]:
    """A/B field difference with fail-closed compatibility checks (#1027)."""
    incompatible = check_field_compatibility(
        left, right, comparison_policy=comparison_policy
    )
    if incompatible and not (
        left.semantic_sha256 == right.semantic_sha256
    ):
        raise ValueError(
            'field difference incompatible: ' + ', '.join(incompatible)
        )
    if left.semantic_sha256 == right.semantic_sha256:
        count = len(
            left.pressure_real
            if left.is_complex
            else left.pressure_magnitude_pa
        )
        if difference_semantics == 'complex_difference':
            return [0j] * count
        return [0.0] * count
    if difference_semantics == 'complex_difference':
        if not left.is_complex:
            raise ValueError(
                'field difference incompatible: representation'
            )
        if left.phasor_convention != right.phasor_convention:
            raise ValueError(
                'field difference incompatible: phasor_convention'
            )
        return [
            complex(lr, li) - complex(rr, ri)
            for lr, li, rr, ri in zip(
                left.pressure_real,
                left.pressure_imag,
                right.pressure_real,
                right.pressure_imag,
            )
        ]
    if left.is_complex:
        left_mag = [
            abs(complex(r, i))
            for r, i in zip(left.pressure_real, left.pressure_imag)
        ]
        right_mag = [
            abs(complex(r, i))
            for r, i in zip(right.pressure_real, right.pressure_imag)
        ]
    else:
        left_mag = list(left.pressure_magnitude_pa)
        right_mag = list(right.pressure_magnitude_pa)
    if difference_semantics == 'db_delta':
        if not (
            left.absolute_pressure_reference
            and right.absolute_pressure_reference
        ):
            raise ValueError('dB delta requires absolute reference authority')
        return [
            20.0 * log10(l / r) if l > 0.0 and r > 0.0 else 0.0
            for l, r in zip(left_mag, right_mag)
        ]
    return [
        (l / r) if r > 0.0 else 0.0
        for l, r in zip(left_mag, right_mag)
    ]


def build_rectangular_mode_field(
    *,
    request: SpatialFieldRequestSpec,
    room_size_m: tuple[float, float, float],
    mode_indices: tuple[int, int, int],
    axes: tuple[RegularGridAxis, RegularGridAxis, RegularGridAxis],
    amplitude_pa: float,
    valid_frequency_domain: FrequencyDomain,
    absolute_pressure_reference: bool = True,
) -> SpatialFieldResult:
    """Analytical rigid rectangular-room pressure mode fixture.

    p(x,y,z) = A * cos(nx*pi*x/Lx) * cos(ny*pi*y/Ly) * cos(nz*pi*z/Lz)
    purely real in the e^{+iwt} convention — nodal planes at known
    positions; used by tests to verify slice/probe semantics.
    """
    from math import cos

    lx, ly, lz = room_size_m
    nx_mode, ny_mode, nz_mode = mode_indices
    real: list[float] = []
    imag: list[float] = []
    for iz in range(axes[2].count):
        z = axes[2].coordinate(iz)
        for iy in range(axes[1].count):
            y = axes[1].coordinate(iy)
            for ix in range(axes[0].count):
                x = axes[0].coordinate(ix)
                value = (
                    amplitude_pa
                    * cos(nx_mode * pi * x / lx)
                    * cos(ny_mode * pi * y / ly)
                    * cos(nz_mode * pi * z / lz)
                )
                real.append(value)
                imag.append(0.0)
    return build_spatial_field_result(
        request=request,
        axes=axes,
        representation='complex_pressure',
        pressure_real=tuple(real),
        pressure_imag=tuple(imag),
        absolute_pressure_reference=absolute_pressure_reference,
        phasor_convention='exp(+i*omega*t)',
        valid_frequency_domain=valid_frequency_domain,
    )
