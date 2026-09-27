from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence
from enum import StrEnum
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .r120_geometry_compiler import ExactExternalAuthorityRef
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _semantic_hash


R160_GRID_RECONCILIATION_AUTHORITY_VERSION = 'r160-frequency-grid-reconciliation-1'
R160_CROSSOVER_AUTHORITY_VERSION = 'r160-fixed-bounded-crossover-1'

GridReconciliationMethod = Literal['exact_bin_identity_v1', 'cartesian_linear_v1']
InterpolationDomain = Literal['none', 'linear_frequency_hz']
HybridWeightLaw = Literal['linear_frequency_complementary_v1']


class HybridNumericalFailureCode(StrEnum):
    INVALID_GRID = 'INVALID_GRID'
    NON_MONOTONIC_GRID = 'NON_MONOTONIC_GRID'
    DUPLICATE_FREQUENCY = 'DUPLICATE_FREQUENCY'
    OUT_OF_VALID_BAND = 'OUT_OF_VALID_BAND'
    EXTRAPOLATION_REQUIRED = 'EXTRAPOLATION_REQUIRED'
    PHASE_INTERPOLATION_UNSUPPORTED = 'PHASE_INTERPOLATION_UNSUPPORTED'
    OVERLAP_INVALID = 'OVERLAP_INVALID'
    INPUT_CAPABILITY_MISMATCH = 'INPUT_CAPABILITY_MISMATCH'
    ARTIFACT_HASH_MISMATCH = 'ARTIFACT_HASH_MISMATCH'


class HybridNumericalCompositionError(ValueError):
    def __init__(self, code: HybridNumericalFailureCode, message: str) -> None:
        self.code = code
        super().__init__(f'{code.value}: {message}')






def validate_frequency_grid(
    values: Sequence[float],
    *,
    label: str,
    tolerance_hz: float = 0.0,
) -> tuple[float, ...]:
    tolerance = float(tolerance_hz)
    if not isfinite(tolerance) or tolerance < 0.0:
        raise HybridNumericalCompositionError(
            HybridNumericalFailureCode.INVALID_GRID,
            f'{label} tolerance must be finite and non-negative',
        )
    grid = tuple(float(item) for item in values)
    if len(grid) < 2 or any(not isfinite(item) or item <= 0.0 for item in grid):
        raise HybridNumericalCompositionError(
            HybridNumericalFailureCode.INVALID_GRID,
            f'{label} must contain at least two finite positive frequencies',
        )
    for previous, current in zip(grid, grid[1:]):
        if current < previous:
            raise HybridNumericalCompositionError(
                HybridNumericalFailureCode.NON_MONOTONIC_GRID,
                f'{label} must be strictly increasing',
            )
        if current - previous <= tolerance:
            raise HybridNumericalCompositionError(
                HybridNumericalFailureCode.DUPLICATE_FREQUENCY,
                f'{label} contains duplicate/ambiguous bins within tolerance',
            )
    return grid


def _band(grid: Sequence[float]) -> tuple[float, float]:
    values = tuple(float(item) for item in grid)
    return values[0], values[-1]


def _exact_index(
    grid: tuple[float, ...],
    frequency_hz: float,
    tolerance_hz: float,
) -> int | None:
    index = bisect_left(grid, frequency_hz)
    candidates = []
    if index < len(grid):
        candidates.append(index)
    if index > 0:
        candidates.append(index - 1)
    for candidate in candidates:
        if abs(grid[candidate] - frequency_hz) <= tolerance_hz:
            return candidate
    return None


class FrequencyGridReconciliationAuthority(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r160-frequency-grid-reconciliation-1'
    ] = R160_GRID_RECONCILIATION_AUTHORITY_VERSION
    authority_id: str = Field(
        pattern=r'^r160-frequency-grid-reconciliation:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    original_wave_frequency_grid_hz: tuple[float, ...] = Field(min_length=2)
    original_ga_frequency_grid_hz: tuple[float, ...] = Field(min_length=2)
    requested_output_frequency_grid_hz: tuple[float, ...] = Field(min_length=2)
    reconciliation_method: GridReconciliationMethod
    interpolation_domain: InterpolationDomain
    complex_interpolation: Literal[
        'exact_complex_sample',
        'cartesian_real_imag_piecewise_linear',
    ]
    extrapolation_policy: Literal['forbidden'] = 'forbidden'
    wave_valid_input_band_hz: tuple[float, float]
    ga_valid_input_band_hz: tuple[float, float]
    valid_output_band_hz: tuple[float, float]
    tolerance_hz: float = Field(ge=0.0)
    algorithm_identity: Literal[
        'htdt.r160.frequency-grid-reconciliation'
    ] = 'htdt.r160.frequency-grid-reconciliation'
    algorithm_version: Literal['1'] = '1'

    @model_validator(mode='after')
    def contract(self) -> 'FrequencyGridReconciliationAuthority':
        wave = validate_frequency_grid(
            self.original_wave_frequency_grid_hz,
            label='R160 original wave grid',
            tolerance_hz=self.tolerance_hz,
        )
        ga = validate_frequency_grid(
            self.original_ga_frequency_grid_hz,
            label='R160 original GA grid',
            tolerance_hz=self.tolerance_hz,
        )
        output = validate_frequency_grid(
            self.requested_output_frequency_grid_hz,
            label='R160 requested output grid',
            tolerance_hz=self.tolerance_hz,
        )
        common = (max(wave[0], ga[0]), min(wave[-1], ga[-1]))
        if common[0] >= common[1]:
            raise HybridNumericalCompositionError(
                HybridNumericalFailureCode.OUT_OF_VALID_BAND,
                'wave and GA grids do not share a usable band',
            )
        if self.wave_valid_input_band_hz != _band(wave):
            raise ValueError('wave valid band does not bind original wave grid')
        if self.ga_valid_input_band_hz != _band(ga):
            raise ValueError('GA valid band does not bind original GA grid')
        if self.valid_output_band_hz != common:
            raise ValueError('valid output band must equal the input-band intersection')
        if output[0] < common[0] or output[-1] > common[1]:
            raise HybridNumericalCompositionError(
                HybridNumericalFailureCode.OUT_OF_VALID_BAND,
                'requested output grid lies outside the common valid band',
            )
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('grid reconciliation semantic hash mismatch')
        if self.authority_id != f'r160-frequency-grid-reconciliation:{expected}':
            raise ValueError('grid reconciliation authority id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'authority_id', 'semantic_sha256'},
        )

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.authority_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


def build_frequency_grid_reconciliation_authority(
    *,
    original_wave_frequency_grid_hz: Sequence[float],
    original_ga_frequency_grid_hz: Sequence[float],
    requested_output_frequency_grid_hz: Sequence[float],
    reconciliation_method: str = 'exact_bin_identity_v1',
    tolerance_hz: float = 0.0,
) -> FrequencyGridReconciliationAuthority:
    if reconciliation_method not in {
        'exact_bin_identity_v1',
        'cartesian_linear_v1',
    }:
        raise HybridNumericalCompositionError(
            HybridNumericalFailureCode.PHASE_INTERPOLATION_UNSUPPORTED,
            'only exact-bin or Cartesian real/imag linear interpolation is authorized',
        )
    wave = validate_frequency_grid(
        original_wave_frequency_grid_hz,
        label='R160 original wave grid',
        tolerance_hz=tolerance_hz,
    )
    ga = validate_frequency_grid(
        original_ga_frequency_grid_hz,
        label='R160 original GA grid',
        tolerance_hz=tolerance_hz,
    )
    output = validate_frequency_grid(
        requested_output_frequency_grid_hz,
        label='R160 requested output grid',
        tolerance_hz=tolerance_hz,
    )
    common = (max(wave[0], ga[0]), min(wave[-1], ga[-1]))
    if common[0] >= common[1] or output[0] < common[0] or output[-1] > common[1]:
        raise HybridNumericalCompositionError(
            HybridNumericalFailureCode.OUT_OF_VALID_BAND,
            'output grid requires data outside the common valid input band',
        )
    if reconciliation_method == 'exact_bin_identity_v1':
        for label, grid in (('wave', wave), ('GA', ga)):
            missing = [
                frequency for frequency in output
                if _exact_index(grid, frequency, float(tolerance_hz)) is None
            ]
            if missing:
                raise HybridNumericalCompositionError(
                    HybridNumericalFailureCode.INVALID_GRID,
                    f'exact-bin mode has no {label} sample for {missing}',
                )
        interpolation_domain = 'none'
        complex_interpolation = 'exact_complex_sample'
    else:
        interpolation_domain = 'linear_frequency_hz'
        complex_interpolation = 'cartesian_real_imag_piecewise_linear'
    core = {
        'authority_version': R160_GRID_RECONCILIATION_AUTHORITY_VERSION,
        'original_wave_frequency_grid_hz': list(wave),
        'original_ga_frequency_grid_hz': list(ga),
        'requested_output_frequency_grid_hz': list(output),
        'reconciliation_method': reconciliation_method,
        'interpolation_domain': interpolation_domain,
        'complex_interpolation': complex_interpolation,
        'extrapolation_policy': 'forbidden',
        'wave_valid_input_band_hz': list(_band(wave)),
        'ga_valid_input_band_hz': list(_band(ga)),
        'valid_output_band_hz': list(common),
        'tolerance_hz': float(tolerance_hz),
        'algorithm_identity': 'htdt.r160.frequency-grid-reconciliation',
        'algorithm_version': '1',
    }
    digest = _semantic_hash(core)
    return FrequencyGridReconciliationAuthority(
        authority_id=f'r160-frequency-grid-reconciliation:{digest}',
        semantic_sha256=digest,
        **core,
    )


class HybridCrossoverConfigurationAuthority(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r160-fixed-bounded-crossover-1'
    ] = R160_CROSSOVER_AUTHORITY_VERSION
    authority_id: str = Field(
        pattern=r'^r160-crossover-configuration:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    overlap_lower_hz: float = Field(gt=0.0)
    overlap_upper_hz: float = Field(gt=0.0)
    blend_law: HybridWeightLaw = 'linear_frequency_complementary_v1'
    wave_validity_band_hz: tuple[float, float]
    ga_validity_band_hz: tuple[float, float]

    @model_validator(mode='after')
    def contract(self) -> 'HybridCrossoverConfigurationAuthority':
        if self.overlap_lower_hz >= self.overlap_upper_hz:
            raise HybridNumericalCompositionError(
                HybridNumericalFailureCode.OVERLAP_INVALID,
                'overlap lower bound must be less than upper bound',
            )
        for label, band in (
            ('wave', self.wave_validity_band_hz),
            ('GA', self.ga_validity_band_hz),
        ):
            if (
                band[0] >= band[1]
                or self.overlap_lower_hz < band[0]
                or self.overlap_upper_hz > band[1]
            ):
                raise HybridNumericalCompositionError(
                    HybridNumericalFailureCode.OVERLAP_INVALID,
                    f'overlap is outside {label} validity band',
                )
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('crossover configuration semantic hash mismatch')
        if self.authority_id != f'r160-crossover-configuration:{expected}':
            raise ValueError('crossover configuration id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'authority_id', 'semantic_sha256'},
        )


def build_hybrid_crossover_configuration_authority(
    *,
    overlap_lower_hz: float,
    overlap_upper_hz: float,
    wave_validity_band_hz: tuple[float, float],
    ga_validity_band_hz: tuple[float, float],
) -> HybridCrossoverConfigurationAuthority:
    lower = float(overlap_lower_hz)
    upper = float(overlap_upper_hz)
    wave_band = tuple(float(item) for item in wave_validity_band_hz)
    ga_band = tuple(float(item) for item in ga_validity_band_hz)
    if (
        not isfinite(lower)
        or not isfinite(upper)
        or lower <= 0.0
        or upper <= 0.0
        or lower >= upper
    ):
        raise HybridNumericalCompositionError(
            HybridNumericalFailureCode.OVERLAP_INVALID,
            'overlap bounds must be finite, positive, and lower < upper',
        )
    for label, band in (('wave', wave_band), ('GA', ga_band)):
        if (
            len(band) != 2
            or any(not isfinite(item) or item <= 0.0 for item in band)
            or band[0] >= band[1]
            or lower < band[0]
            or upper > band[1]
        ):
            raise HybridNumericalCompositionError(
                HybridNumericalFailureCode.OVERLAP_INVALID,
                f'overlap is outside {label} validity band',
            )
    core = {
        'authority_version': R160_CROSSOVER_AUTHORITY_VERSION,
        'overlap_lower_hz': lower,
        'overlap_upper_hz': upper,
        'blend_law': 'linear_frequency_complementary_v1',
        'wave_validity_band_hz': list(wave_band),
        'ga_validity_band_hz': list(ga_band),
    }
    digest = _semantic_hash(core)
    return HybridCrossoverConfigurationAuthority(
        authority_id=f'r160-crossover-configuration:{digest}',
        semantic_sha256=digest,
        **core,
    )


def reconcile_complex_series(
    *,
    original_grid_hz: Sequence[float],
    values: Sequence[complex],
    output_grid_hz: Sequence[float],
    authority: FrequencyGridReconciliationAuthority,
    label: str,
) -> dict[float, complex]:
    grid = tuple(float(item) for item in original_grid_hz)
    output = tuple(float(item) for item in output_grid_hz)
    samples = tuple(complex(item) for item in values)
    if len(grid) != len(samples):
        raise HybridNumericalCompositionError(
            HybridNumericalFailureCode.INVALID_GRID,
            f'{label} grid/value length mismatch',
        )
    result: dict[float, complex] = {}
    for frequency in output:
        exact = _exact_index(grid, frequency, authority.tolerance_hz)
        if exact is not None:
            result[frequency] = samples[exact]
            continue
        if authority.reconciliation_method == 'exact_bin_identity_v1':
            raise HybridNumericalCompositionError(
                HybridNumericalFailureCode.INVALID_GRID,
                f'{label} has no exact sample at {frequency} Hz',
            )
        insertion = bisect_left(grid, frequency)
        if insertion == 0 or insertion == len(grid):
            raise HybridNumericalCompositionError(
                HybridNumericalFailureCode.EXTRAPOLATION_REQUIRED,
                f'{label} would require forbidden extrapolation at {frequency} Hz',
            )
        f0, f1 = grid[insertion - 1], grid[insertion]
        alpha = (frequency - f0) / (f1 - f0)
        z0, z1 = samples[insertion - 1], samples[insertion]
        result[frequency] = complex(
            (1.0 - alpha) * z0.real + alpha * z1.real,
            (1.0 - alpha) * z0.imag + alpha * z1.imag,
        )
    return result
