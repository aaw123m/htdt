"""R160 residual — union-band numerical stitching and automatic crossover.

PR #254/#262 established the typed foundation but restricted the composed
response to the wave/GA *intersection* band with an explicit-only crossover.
This module adds the bounded residual:

- ``HybridBandStitchPlanAuthority`` — an audited plan that assigns every
  requested output-grid frequency to exactly one provenance region
  (``wave_only`` / ``crossover_blend`` / ``ga_only``) and records uncovered
  intervals as explicit ``gap_domains`` instead of silently joining them.
- ``HybridAutomaticCrossoverSelection`` — evidence-driven crossover bounds
  derived from wave/GA complex agreement over the common band. Selection is
  fail-closed: ambiguous or violated agreement evidence yields
  ``selection_state='UNSUPPORTED'`` and no crossover authority is produced.
- ``StitchedHybridCompositionSpec`` / ``StitchedHybridResponseArtifact`` —
  the composed broadband response over the union band, still bound to the
  exact R130 result/payload/candidate input/excitation and exact R150 path
  responses, still under the common exp(+i*omega*t) convention.

Nothing here widens a production claim: stitched samples record per-point
provenance, gap domains stay gaps, and no phase/magnitude is synthesized
outside the bands the inputs actually cover.
"""

from collections.abc import Sequence
from math import atan2, cos, isclose, isfinite, sin
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_acoustic_solver_result import AcousticSolverResultEnvelope
from .cad_candidate_wave_contracts import CandidateWaveExecutionInput
from .cad_geometric_acoustics_response import (
    DeterministicPathFrequencyResponseArtifact,
    TRANSFER_QUANTITY,
    TRANSFER_UNIT,
)
from .cad_hybrid_grid_reconciliation import (
    GridReconciliationMethod,
    HybridCrossoverConfigurationAuthority,
    HybridNumericalCompositionError,
    HybridNumericalFailureCode,
    HybridWeightLaw,
    build_hybrid_crossover_configuration_authority,
    reconcile_complex_series_with_method,
    validate_frequency_grid,
)
from .cad_hybrid_numerical_composition import (
    COMMON_ANALYSIS_FOURIER_KERNEL,
    COMMON_PHASOR_CONVENTION,
    COMMON_SOURCE_NORMALIZATION,
    COMMON_TIME_ORIGIN,
    AggregatedGaComplexResponse,
    ExactCandidateInputIdentity,
    ExactSolverResultIdentity,
    HybridConventionNormalizationAuthority,
    HybridNumericalCapability,
    aggregate_ga_paths_on_grid,
    build_hybrid_convention_normalization_authority,
    compute_native_wave_transfer,
    _complex_pressure_manifest,
    _excitation_ref,
    _phase,
    _ref_key,
    _response_ref,
    _validate_wave_inputs,
    _weights,
)
from .cad_wave_excitation import AcousticWaveExcitationAuthority
from .cad_wave_source_model import WaveSourceModelCompatibility
from ...r120_geometry_compiler import ExactExternalAuthorityRef
from ...canonical_json import canonical_sha256 as _semantic_hash

R160_AUTO_CROSSOVER_AUTHORITY_VERSION = 'r160-automatic-crossover-selection-1'
R160_BAND_STITCH_AUTHORITY_VERSION = 'r160-band-stitch-plan-1'
R160_STITCHED_SPEC_AUTHORITY_VERSION = 'r160-stitched-composition-spec-1'
R160_STITCHED_ARTIFACT_AUTHORITY_VERSION = 'r160-stitched-hybrid-response-1'
R160_STITCHED_SCHEMA_VERSION = 1

HybridStitchRegionKind = Literal['wave_only', 'crossover_blend', 'ga_only']
HybridStitchState = Literal['CONTINUOUS', 'GAP_PRESERVED']
CrossoverSelectionState = Literal['SELECTED', 'UNSUPPORTED']

class CrossoverAgreementSample(BaseModel):
    """One evaluation-grid point of the recorded wave/GA agreement evidence."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    frequency_hz: float = Field(gt=0.0)
    wave_real_pa_per_m3_s: float
    wave_imag_pa_per_m3_s: float
    ga_real_pa_per_m3_s: float
    ga_imag_pa_per_m3_s: float
    relative_discrepancy: float = Field(ge=0.0)
    within_tolerance: bool

    @model_validator(mode='after')
    def consistent(self) -> 'CrossoverAgreementSample':
        for label, value in (
            ('wave real', self.wave_real_pa_per_m3_s),
            ('wave imag', self.wave_imag_pa_per_m3_s),
            ('GA real', self.ga_real_pa_per_m3_s),
            ('GA imag', self.ga_imag_pa_per_m3_s),
            ('discrepancy', self.relative_discrepancy),
        ):
            if not isfinite(float(value)):
                raise ValueError(f'R160 crossover {label} must be finite')
        return self

class CrossoverBandCandidate(BaseModel):
    """One contiguous candidate transition band, accepted or rejected."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    lower_hz: float = Field(gt=0.0)
    upper_hz: float = Field(gt=0.0)
    sample_count: int = Field(ge=1)
    max_relative_discrepancy: float = Field(ge=0.0)
    mean_relative_discrepancy: float = Field(ge=0.0)
    accepted: bool
    rejected_reason: str | None = None

    @model_validator(mode='after')
    def consistent(self) -> 'CrossoverBandCandidate':
        if self.lower_hz > self.upper_hz:
            raise ValueError('R160 crossover candidate requires lower <= upper')
        if self.accepted and self.rejected_reason is not None:
            raise ValueError('accepted crossover candidate cannot carry a reason')
        if not self.accepted and self.rejected_reason is None:
            raise ValueError('rejected crossover candidate requires a reason')
        return self

class HybridAutomaticCrossoverSelection(BaseModel):
    """Audited evidence-driven selection of the bounded crossover region."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r160-automatic-crossover-selection-1'
    ] = R160_AUTO_CROSSOVER_AUTHORITY_VERSION
    selection_id: str = Field(
        pattern=r'^r160-automatic-crossover:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    selection_rule: Literal[
        'widest_contiguous_agreement_band_v1'
    ] = 'widest_contiguous_agreement_band_v1'
    wave_validity_band_hz: tuple[float, float]
    ga_validity_band_hz: tuple[float, float]
    common_band_hz: tuple[float, float]
    evaluation_grid_hz: tuple[float, ...] = Field(min_length=1)
    max_relative_discrepancy: float = Field(ge=0.0)
    discrepancy_floor_pa_per_m3_s: float = Field(ge=0.0)
    minimum_band_samples: int = Field(ge=2)
    wave_series_ref: ExactExternalAuthorityRef | None = None
    ga_series_ref: ExactExternalAuthorityRef | None = None

    samples: tuple[CrossoverAgreementSample, ...] = Field(min_length=1)
    candidates: tuple[CrossoverBandCandidate, ...] = ()
    selection_state: CrossoverSelectionState
    unsupported_reasons: tuple[str, ...] = ()
    selected_lower_hz: float | None = None
    selected_upper_hz: float | None = None
    crossover_configuration: HybridCrossoverConfigurationAuthority | None = None

    @model_validator(mode='after')
    def contract(self) -> 'HybridAutomaticCrossoverSelection':
        grid = tuple(float(item) for item in self.evaluation_grid_hz)
        if tuple(item.frequency_hz for item in self.samples) != grid:
            raise ValueError(
                'R160 automatic crossover samples must cover the evaluation grid'
            )
        common = (
            max(float(self.wave_validity_band_hz[0]),
                float(self.ga_validity_band_hz[0])),
            min(float(self.wave_validity_band_hz[1]),
                float(self.ga_validity_band_hz[1])),
        )
        if tuple(float(item) for item in self.common_band_hz) != common:
            raise ValueError('R160 crossover common band must equal the intersection')
        selected = self.selection_state == 'SELECTED'
        values = (
            self.selected_lower_hz,
            self.selected_upper_hz,
            self.crossover_configuration,
        )
        if selected and any(value is None for value in values):
            raise ValueError(
                'SELECTED crossover requires bounds and a crossover authority'
            )
        if not selected and any(value is not None for value in values):
            raise ValueError(
                'UNSUPPORTED crossover selection cannot emit crossover bounds'
            )
        if selected and self.unsupported_reasons:
            raise ValueError('SELECTED crossover cannot carry failure reasons')
        if not selected and not self.unsupported_reasons:
            raise ValueError('UNSUPPORTED crossover selection requires reasons')
        if selected:
            crossover = self.crossover_configuration
            assert crossover is not None
            if (
                float(crossover.overlap_lower_hz) != float(self.selected_lower_hz)
                or float(crossover.overlap_upper_hz)
                != float(self.selected_upper_hz)
            ):
                raise ValueError(
                    'R160 selected bounds must equal the crossover authority'
                )
            if (
                tuple(float(item) for item in crossover.wave_validity_band_hz)
                != tuple(float(item) for item in self.wave_validity_band_hz)
                or tuple(float(item) for item in crossover.ga_validity_band_hz)
                != tuple(float(item) for item in self.ga_validity_band_hz)
            ):
                raise ValueError(
                    'R160 selected crossover validity bands are inconsistent'
                )
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('R160 automatic crossover semantic hash mismatch')
        if self.selection_id != f'r160-automatic-crossover:{expected}':
            raise ValueError('R160 automatic crossover id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'selection_id', 'semantic_sha256'},
        )

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.selection_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )

def build_automatic_crossover_selection(
    *,
    wave_values: dict[float, complex] | Sequence[tuple[float, complex]],
    ga_values: dict[float, complex] | Sequence[tuple[float, complex]],
    wave_validity_band_hz: tuple[float, float],
    ga_validity_band_hz: tuple[float, float],
    max_relative_discrepancy: float,
    discrepancy_floor_pa_per_m3_s: float,
    minimum_band_samples: int = 2,
    wave_series_ref: ExactExternalAuthorityRef | None = None,
    ga_series_ref: ExactExternalAuthorityRef | None = None,
) -> HybridAutomaticCrossoverSelection:
    """Select the crossover band from recorded wave/GA agreement evidence.

    The widest contiguous run of evaluation frequencies whose relative complex
    discrepancy stays inside the declared bound becomes the transition. Equal
    widest runs, violated bounds, and sample starvation are all UNSUPPORTED —
    the gap is kept rather than silently joined.
    """

    wave_map = {float(f): complex(v) for f, v in dict(wave_values).items()}
    ga_map = {float(f): complex(v) for f, v in dict(ga_values).items()}
    if set(wave_map) != set(ga_map):
        raise HybridNumericalCompositionError(
            HybridNumericalFailureCode.INVALID_GRID,
            'R160 crossover evaluation requires identical wave/GA grids',
        )
    grid = tuple(sorted(wave_map))
    if not grid:
        raise HybridNumericalCompositionError(
            HybridNumericalFailureCode.INVALID_GRID,
            'R160 crossover evaluation grid is empty',
        )
    if any(
        not isfinite(item) or item <= 0.0 for item in grid
    ) or any(
        current <= previous for previous, current in zip(grid, grid[1:])
    ):
        raise HybridNumericalCompositionError(
            HybridNumericalFailureCode.INVALID_GRID,
            'R160 crossover evaluation grid must be finite/positive/unique',
        )
    tolerance = float(max_relative_discrepancy)
    floor = float(discrepancy_floor_pa_per_m3_s)
    if not isfinite(tolerance) or tolerance < 0.0:
        raise ValueError('R160 crossover discrepancy bound must be finite')
    if not isfinite(floor) or floor < 0.0:
        raise ValueError('R160 crossover discrepancy floor must be finite')
    wave_band = tuple(float(item) for item in wave_validity_band_hz)
    ga_band = tuple(float(item) for item in ga_validity_band_hz)
    common = (max(wave_band[0], ga_band[0]), min(wave_band[1], ga_band[1]))
    reasons: list[str] = []
    if common[0] >= common[1]:
        reasons.append(
            'wave and GA validity bands are disjoint; no common band exists '
            'to evaluate agreement — the gap is preserved'
        )
    else:
        outside = [
            frequency
            for frequency in grid
            if frequency < common[0] or frequency > common[1]
        ]
        if outside:
            raise HybridNumericalCompositionError(
                HybridNumericalFailureCode.OUT_OF_VALID_BAND,
                'R160 crossover evaluation points lie outside the common '
                f'validity band: {outside}',
            )

    samples: list[dict[str, Any]] = []
    within: list[bool] = []
    for frequency in grid:
        wave = wave_map[frequency]
        ga = ga_map[frequency]
        discrepancy = abs(wave - ga) / max(abs(wave) + abs(ga), floor)
        ok = discrepancy <= tolerance
        within.append(ok)
        samples.append(
            {
                'frequency_hz': frequency,
                'wave_real_pa_per_m3_s': wave.real,
                'wave_imag_pa_per_m3_s': wave.imag,
                'ga_real_pa_per_m3_s': ga.real,
                'ga_imag_pa_per_m3_s': ga.imag,
                'relative_discrepancy': discrepancy,
                'within_tolerance': ok,
            }
        )

    minimum = int(minimum_band_samples)
    if minimum < 2:
        raise ValueError('R160 crossover requires at least two band samples')

    runs: list[tuple[int, int]] = []
    if not reasons:
        start: int | None = None
        for index, ok in enumerate(within + [False]):
            if ok and start is None:
                start = index
            elif not ok and start is not None:
                runs.append((start, index - 1))
                start = None

    candidates: list[dict[str, Any]] = []
    for first, last in runs:
        run = grid[first : last + 1]
        discrepancies = [
            float(samples[index]['relative_discrepancy'])
            for index in range(first, last + 1)
        ]
        if len(run) >= minimum:
            accepted = True
            rejected_reason = None
        else:
            accepted = False
            rejected_reason = (
                f'candidate band holds {len(run)} evaluation points; the '
                f'declared minimum is {minimum}'
            )
        candidates.append(
            {
                'lower_hz': run[0],
                'upper_hz': run[-1],
                'sample_count': len(run),
                'max_relative_discrepancy': max(discrepancies),
                'mean_relative_discrepancy': (
                    sum(discrepancies) / len(discrepancies)
                ),
                'accepted': accepted,
                'rejected_reason': rejected_reason,
            }
        )

    accepted_candidates = [
        candidate for candidate in candidates if candidate['accepted']
    ]
    crossover: HybridCrossoverConfigurationAuthority | None = None
    selected: tuple[float, float] | None = None
    if not accepted_candidates:
        reasons.append(
            'no contiguous evaluation band satisfies the declared agreement '
            'bound and minimum sample count — the gap is preserved'
        )
    else:
        widest = max(
            float(item['upper_hz']) - float(item['lower_hz'])
            for item in accepted_candidates
        )
        winners = [
            item
            for item in accepted_candidates
            if float(item['upper_hz']) - float(item['lower_hz']) == widest
        ]
        if len(winners) != 1:
            reasons.append(
                'multiple disjoint candidate bands tie for widest agreement — '
                'selection is ambiguous, so no crossover is emitted'
            )
            for item in accepted_candidates:
                item['accepted'] = False
                item['rejected_reason'] = (
                    'ambiguous equal-width candidate band'
                )
        else:
            winner = winners[0]
            selected = (
                float(winner['lower_hz']),
                float(winner['upper_hz']),
            )
            if selected[0] >= selected[1]:
                reasons.append(
                    'selected agreement band has zero width; a single point '
                    'cannot bound a blend transition'
                )
                winner['accepted'] = False
                winner['rejected_reason'] = 'zero-width selected band'
                selected = None
            else:
                crossover = build_hybrid_crossover_configuration_authority(
                    overlap_lower_hz=selected[0],
                    overlap_upper_hz=selected[1],
                    wave_validity_band_hz=wave_band,
                    ga_validity_band_hz=ga_band,
                )

    core: dict[str, Any] = {
        'authority_version': R160_AUTO_CROSSOVER_AUTHORITY_VERSION,
        'selection_rule': 'widest_contiguous_agreement_band_v1',
        'wave_validity_band_hz': list(wave_band),
        'ga_validity_band_hz': list(ga_band),
        'common_band_hz': [common[0], common[1]],
        'evaluation_grid_hz': list(grid),
        'max_relative_discrepancy': tolerance,
        'discrepancy_floor_pa_per_m3_s': floor,
        'minimum_band_samples': minimum,
        'wave_series_ref': (
            None if wave_series_ref is None
            else wave_series_ref.model_dump(mode='json')
        ),
        'ga_series_ref': (
            None if ga_series_ref is None
            else ga_series_ref.model_dump(mode='json')
        ),
        'samples': samples,
        'candidates': candidates,
        'selection_state': 'SELECTED' if crossover is not None else 'UNSUPPORTED',
        'unsupported_reasons': (
            [] if crossover is not None else sorted(set(reasons))
        ),
        'selected_lower_hz': None if selected is None else selected[0],
        'selected_upper_hz': None if selected is None else selected[1],
        'crossover_configuration': (
            None
            if crossover is None
            else crossover.model_dump(mode='json')
        ),
    }
    digest = _semantic_hash(core)
    return HybridAutomaticCrossoverSelection(
        selection_id=f'r160-automatic-crossover:{digest}',
        semantic_sha256=digest,
        **core,
    )

class HybridStitchRegion(BaseModel):
    """One maximal output-grid segment served by a single provenance."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    region: HybridStitchRegionKind
    lower_hz: float = Field(gt=0.0)
    upper_hz: float = Field(gt=0.0)
    output_grid_hz: tuple[float, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def consistent(self) -> 'HybridStitchRegion':
        grid = tuple(float(item) for item in self.output_grid_hz)
        if grid != tuple(sorted(set(grid))):
            raise ValueError('R160 stitch region grid must be sorted/unique')
        if grid[0] < float(self.lower_hz) or grid[-1] > float(self.upper_hz):
            raise ValueError('R160 stitch region grid exceeds its bounds')
        if float(self.lower_hz) > float(self.upper_hz):
            raise ValueError('R160 stitch region requires lower <= upper')
        return self

class HybridStitchGap(BaseModel):
    """One uncovered interval inside the requested output span."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    lower_hz: float = Field(gt=0.0)
    upper_hz: float = Field(gt=0.0)
    reason: str = Field(min_length=1)

    @model_validator(mode='after')
    def consistent(self) -> 'HybridStitchGap':
        if float(self.lower_hz) >= float(self.upper_hz):
            raise ValueError('R160 stitch gap requires lower < upper')
        return self

class HybridBandStitchPlanAuthority(BaseModel):
    """Versioned union-band stitching plan with explicit gap accounting."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r160-band-stitch-plan-1'
    ] = R160_BAND_STITCH_AUTHORITY_VERSION
    plan_id: str = Field(pattern=r'^r160-band-stitch-plan:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    wave_valid_input_band_hz: tuple[float, float]
    ga_valid_input_band_hz: tuple[float, float]
    crossover_configuration: HybridCrossoverConfigurationAuthority | None = None
    automatic_crossover_selection_ref: ExactExternalAuthorityRef | None = None
    reconciliation_method: GridReconciliationMethod
    tolerance_hz: float = Field(ge=0.0)
    requested_output_frequency_grid_hz: tuple[float, ...] = Field(min_length=2)
    regions: tuple[HybridStitchRegion, ...] = Field(min_length=1)
    gap_domains: tuple[HybridStitchGap, ...] = ()
    stitch_state: HybridStitchState
    blend_law: HybridWeightLaw = 'linear_frequency_complementary_v1'
    algorithm_identity: Literal[
        'htdt.r160.band-stitch'
    ] = 'htdt.r160.band-stitch'
    algorithm_version: Literal['1'] = '1'

    @model_validator(mode='after')
    def contract(self) -> 'HybridBandStitchPlanAuthority':
        grid = tuple(float(item) for item in self.requested_output_frequency_grid_hz)
        assigned = [
            frequency
            for region in self.regions
            for frequency in region.output_grid_hz
        ]
        if tuple(assigned) != grid:
            raise ValueError(
                'R160 stitch regions must partition the requested output grid'
            )
        kinds = [region.region for region in self.regions]
        if 'crossover_blend' in kinds and self.crossover_configuration is None:
            raise ValueError(
                'R160 crossover_blend region requires a crossover authority'
            )
        if self.crossover_configuration is not None:
            crossover = self.crossover_configuration
            if 'crossover_blend' not in kinds:
                raise ValueError(
                    'R160 crossover authority requires a crossover_blend region'
                )
            if (
                tuple(float(item) for item in crossover.wave_validity_band_hz)
                != tuple(float(item) for item in self.wave_valid_input_band_hz)
                or tuple(float(item) for item in crossover.ga_validity_band_hz)
                != tuple(float(item) for item in self.ga_valid_input_band_hz)
            ):
                raise ValueError(
                    'R160 stitch crossover validity bands are inconsistent'
                )
        if (
            self.automatic_crossover_selection_ref is not None
            and self.crossover_configuration is None
        ):
            raise ValueError(
                'R160 stitch selection ref requires a crossover authority'
            )
        if bool(self.gap_domains) != (self.stitch_state == 'GAP_PRESERVED'):
            raise ValueError(
                'R160 stitch_state must be GAP_PRESERVED exactly when gaps exist'
            )
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('R160 band stitch plan semantic hash mismatch')
        if self.plan_id != f'r160-band-stitch-plan:{expected}':
            raise ValueError('R160 band stitch plan id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'plan_id', 'semantic_sha256'},
        )

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.plan_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )

    def region_for(self, frequency_hz: float) -> HybridStitchRegion | None:
        for region in self.regions:
            if float(region.lower_hz) <= frequency_hz <= float(region.upper_hz):
                return region
        return None

def _classify_stitch_frequency(
    frequency_hz: float,
    *,
    wave_band: tuple[float, float],
    ga_band: tuple[float, float],
    transition: tuple[float, float] | None,
) -> HybridStitchRegionKind | None:
    in_wave = wave_band[0] <= frequency_hz <= wave_band[1]
    in_ga = ga_band[0] <= frequency_hz <= ga_band[1]
    if transition is not None:
        if transition[0] <= frequency_hz <= transition[1]:
            if not (in_wave and in_ga):
                raise HybridNumericalCompositionError(
                    HybridNumericalFailureCode.OVERLAP_INVALID,
                    'R160 transition extends outside an input validity band',
                )
            return 'crossover_blend'
        if in_wave and in_ga:
            return 'wave_only' if frequency_hz < transition[0] else 'ga_only'
    elif in_wave and in_ga:
        return None  # ambiguous: both bands cover, no crossover to resolve it
    if in_wave:
        return 'wave_only'
    if in_ga:
        return 'ga_only'
    return None

def build_hybrid_band_stitch_plan(
    *,
    wave_valid_input_band_hz: tuple[float, float],
    ga_valid_input_band_hz: tuple[float, float],
    requested_output_frequency_grid_hz: Sequence[float],
    crossover_configuration: HybridCrossoverConfigurationAuthority | None = None,
    automatic_crossover_selection: HybridAutomaticCrossoverSelection | None = None,
    reconciliation_method: str = 'exact_bin_identity_v1',
    tolerance_hz: float = 0.0,
) -> HybridBandStitchPlanAuthority:
    wave_band = tuple(float(item) for item in wave_valid_input_band_hz)
    ga_band = tuple(float(item) for item in ga_valid_input_band_hz)
    if (
        len(wave_band) != 2
        or len(ga_band) != 2
        or any(not isfinite(item) or item <= 0.0 for item in wave_band)
        or any(not isfinite(item) or item <= 0.0 for item in ga_band)
        or wave_band[0] >= wave_band[1]
        or ga_band[0] >= ga_band[1]
    ):
        raise HybridNumericalCompositionError(
            HybridNumericalFailureCode.INVALID_GRID,
            'R160 stitch input bands must be finite ordered pairs',
        )
    if reconciliation_method not in {
        'exact_bin_identity_v1',
        'cartesian_linear_v1',
    }:
        raise HybridNumericalCompositionError(
            HybridNumericalFailureCode.PHASE_INTERPOLATION_UNSUPPORTED,
            'only exact-bin or Cartesian real/imag linear interpolation '
            'is authorized',
        )
    grid = validate_frequency_grid(
        requested_output_frequency_grid_hz,
        label='R160 stitched output grid',
        tolerance_hz=tolerance_hz,
    )
    crossover = crossover_configuration
    selection_ref: ExactExternalAuthorityRef | None = None
    if automatic_crossover_selection is not None:
        selection = HybridAutomaticCrossoverSelection.model_validate(
            automatic_crossover_selection.model_dump(mode='python')
        )
        if selection.selection_state != 'SELECTED':
            raise HybridNumericalCompositionError(
                HybridNumericalFailureCode.OVERLAP_INVALID,
                'R160 stitch cannot consume an UNSUPPORTED crossover selection',
            )
        if (
            tuple(float(item) for item in selection.wave_validity_band_hz)
            != wave_band
            or tuple(float(item) for item in selection.ga_validity_band_hz)
            != ga_band
        ):
            raise HybridNumericalCompositionError(
                HybridNumericalFailureCode.INPUT_CAPABILITY_MISMATCH,
                'R160 crossover selection validity bands are stale',
            )
        selection_ref = selection.as_external_ref()
        if crossover is None:
            crossover = selection.crossover_configuration
        elif crossover != selection.crossover_configuration:
            raise ValueError(
                'R160 explicit crossover does not match the selection record'
            )
    if crossover is not None:
        crossover = HybridCrossoverConfigurationAuthority.model_validate(
            crossover.model_dump(mode='python')
        )
        for label, band in (('wave', wave_band), ('GA', ga_band)):
            if (
                float(crossover.overlap_lower_hz) < band[0]
                or float(crossover.overlap_upper_hz) > band[1]
            ):
                raise HybridNumericalCompositionError(
                    HybridNumericalFailureCode.OVERLAP_INVALID,
                    f'R160 transition is outside the {label} validity band',
                )
        transition: tuple[float, float] | None = (
            float(crossover.overlap_lower_hz),
            float(crossover.overlap_upper_hz),
        )
    else:
        transition = None

    kinds: list[HybridStitchRegionKind | None] = [
        _classify_stitch_frequency(
            frequency,
            wave_band=wave_band,
            ga_band=ga_band,
            transition=transition,
        )
        for frequency in grid
    ]
    uncovered = [
        frequency
        for frequency, kind in zip(grid, kinds)
        if kind is None
    ]
    if uncovered:
        raise HybridNumericalCompositionError(
            HybridNumericalFailureCode.OUT_OF_VALID_BAND,
            'R160 stitched output grid contains frequencies with no '
            f'unambiguous backend coverage: {uncovered}',
        )

    regions: list[dict[str, Any]] = []
    run_kind = kinds[0]
    run_start = grid[0]
    run_points = [grid[0]]
    for frequency, kind in zip(grid[1:], kinds[1:]):
        assert kind is not None
        if kind == run_kind:
            run_points.append(frequency)
            continue
        regions.append(
            {
                'region': run_kind,
                'lower_hz': run_start,
                'upper_hz': run_points[-1],
                'output_grid_hz': run_points,
            }
        )
        run_kind = kind
        run_start = frequency
        run_points = [frequency]
    regions.append(
        {
            'region': run_kind,
            'lower_hz': run_start,
            'upper_hz': run_points[-1],
            'output_grid_hz': run_points,
        }
    )

    # Exact capability-uncovered intervals inside the requested span: the
    # complement of the validity-band union plus ambiguous overlaps that no
    # crossover resolves. Covered-but-unsampled stretches between region
    # points are not gaps; they simply hold no requested output points.
    span = (float(grid[0]), float(grid[-1]))
    breakpoints = sorted(
        {
            span[0],
            span[1],
            *(
                min(max(bound, span[0]), span[1])
                for bound in (
                    wave_band[0],
                    wave_band[1],
                    ga_band[0],
                    ga_band[1],
                    *(transition if transition is not None else ()),
                )
            ),
        }
    )
    gaps: list[dict[str, Any]] = []
    for lower, upper in zip(breakpoints, breakpoints[1:]):
        if upper <= lower:
            continue
        midpoint = 0.5 * (lower + upper)
        in_wave = wave_band[0] <= midpoint <= wave_band[1]
        in_ga = ga_band[0] <= midpoint <= ga_band[1]
        if transition is None and in_wave and in_ga:
            reason = (
                'both validity bands cover this interval but no crossover '
                'authority resolves ownership; the ambiguous interval stays '
                'unjoined'
            )
        elif not in_wave and not in_ga:
            reason = (
                'no validity band covers this interval; the band gap is '
                'preserved rather than bridged'
            )
        else:
            continue
        if gaps and gaps[-1]['reason'] == reason and gaps[-1]['upper_hz'] == lower:
            gaps[-1]['upper_hz'] = upper
        else:
            gaps.append({'lower_hz': lower, 'upper_hz': upper, 'reason': reason})

    core = {
        'authority_version': R160_BAND_STITCH_AUTHORITY_VERSION,
        'wave_valid_input_band_hz': list(wave_band),
        'ga_valid_input_band_hz': list(ga_band),
        'crossover_configuration': (
            None
            if crossover is None
            else crossover.model_dump(mode='json')
        ),
        'automatic_crossover_selection_ref': (
            None
            if selection_ref is None
            else selection_ref.model_dump(mode='json')
        ),
        'reconciliation_method': reconciliation_method,
        'tolerance_hz': float(tolerance_hz),
        'requested_output_frequency_grid_hz': list(grid),
        'regions': regions,
        'gap_domains': gaps,
        'stitch_state': 'GAP_PRESERVED' if gaps else 'CONTINUOUS',
        'blend_law': 'linear_frequency_complementary_v1',
        'algorithm_identity': 'htdt.r160.band-stitch',
        'algorithm_version': '1',
    }
    digest = _semantic_hash(core)
    return HybridBandStitchPlanAuthority(
        plan_id=f'r160-band-stitch-plan:{digest}',
        semantic_sha256=digest,
        **core,
    )

class StitchedHybridCompositionSpec(BaseModel):
    """Union-band composition request bound to the same exact inputs."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r160-stitched-composition-spec-1'
    ] = R160_STITCHED_SPEC_AUTHORITY_VERSION
    composition_spec_id: str = Field(
        pattern=r'^r160-stitched-composition-spec:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    r130_result: ExactSolverResultIdentity
    r130_complex_pressure_artifact_ref: ExactExternalAuthorityRef
    r130_candidate_input: ExactCandidateInputIdentity
    wave_excitation_ref: ExactExternalAuthorityRef
    r150_response_refs: tuple[ExactExternalAuthorityRef, ...] = Field(
        min_length=1
    )
    wave_source_model: WaveSourceModelCompatibility | None = None

    source_entity_id: str = Field(min_length=1)
    receiver_id: str = Field(min_length=1)
    exact_frequency_grid_hz: tuple[float, ...] = Field(min_length=2)

    quantity: Literal[
        'complex_acoustic_pressure_per_volume_velocity'
    ] = TRANSFER_QUANTITY
    unit: Literal['Pa/(m3/s)'] = TRANSFER_UNIT
    common_phasor_convention: Literal[
        'exp(+i*omega*t)'
    ] = COMMON_PHASOR_CONVENTION
    common_analysis_fourier_kernel: Literal[
        'exp(-i*omega*t)'
    ] = COMMON_ANALYSIS_FOURIER_KERNEL
    source_normalization: Literal[
        'unit_volume_velocity_m3_s'
    ] = COMMON_SOURCE_NORMALIZATION
    time_origin: Literal['source_t0'] = COMMON_TIME_ORIGIN

    normalization_authority_ref: ExactExternalAuthorityRef
    stitch_plan: HybridBandStitchPlanAuthority
    crossover_selection: HybridAutomaticCrossoverSelection | None = None
    weight_law: HybridWeightLaw = 'linear_frequency_complementary_v1'

    @model_validator(mode='after')
    def contract(self) -> 'StitchedHybridCompositionSpec':
        grid = tuple(float(item) for item in self.exact_frequency_grid_hz)
        if grid != tuple(sorted(set(grid))):
            raise ValueError('R160 stitched frequency grid must be sorted/unique')
        if not all(isfinite(item) and item > 0.0 for item in grid):
            raise ValueError(
                'R160 stitched frequency grid must be finite/positive'
            )
        if grid != self.stitch_plan.requested_output_frequency_grid_hz:
            raise ValueError('R160 stitched grid does not match stitch plan')
        if self.weight_law != self.stitch_plan.blend_law:
            raise ValueError('R160 stitched weight law does not match plan')
        if self.crossover_selection is not None:
            selection = self.crossover_selection
            if selection.selection_state != 'SELECTED':
                raise ValueError(
                    'R160 stitched spec cannot embed an UNSUPPORTED selection'
                )
            if (
                selection.crossover_configuration
                != self.stitch_plan.crossover_configuration
            ):
                raise ValueError(
                    'R160 stitched selection crossover does not match plan'
                )
            if (
                self.stitch_plan.automatic_crossover_selection_ref
                != selection.as_external_ref()
            ):
                raise ValueError(
                    'R160 stitch plan does not pin the exact selection record'
                )
        elif self.stitch_plan.automatic_crossover_selection_ref is not None:
            raise ValueError(
                'R160 stitch plan pins a selection the spec does not carry'
            )
        ref_keys = tuple(_ref_key(item) for item in self.r150_response_refs)
        if ref_keys != tuple(sorted(set(ref_keys))):
            raise ValueError('R160 stitched R150 refs must be unique/sorted')
        if self.wave_source_model is not None:
            model = self.wave_source_model
            if (
                model.excitation_semantic_sha256
                != self.wave_excitation_ref.semantic_hash_sha256
                or model.excitation_id != self.wave_excitation_ref.authority_id
            ):
                raise ValueError(
                    'R160 stitched wave source model does not pin the excitation'
                )
            if model.source_entity_id != self.source_entity_id:
                raise ValueError(
                    'R160 stitched wave source model source entity mismatch'
                )
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('R160 stitched spec semantic hash mismatch')
        if (
            self.composition_spec_id
            != f'r160-stitched-composition-spec:{expected}'
        ):
            raise ValueError('R160 stitched spec id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            mode='json',
            exclude={'composition_spec_id', 'semantic_sha256'},
        )
        if payload.get('wave_source_model') is None:
            payload.pop('wave_source_model')
        if payload.get('crossover_selection') is None:
            payload.pop('crossover_selection')
        return payload

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.composition_spec_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )

def build_stitched_hybrid_composition_spec(
    *,
    r130_result: AcousticSolverResultEnvelope,
    r130_artifact_payload: Any,
    r130_candidate_input: CandidateWaveExecutionInput,
    wave_excitation: AcousticWaveExcitationAuthority,
    r150_responses: Sequence[DeterministicPathFrequencyResponseArtifact],
    receiver_id: str,
    exact_frequency_grid_hz: Sequence[float],
    crossover_configuration: HybridCrossoverConfigurationAuthority | None = None,
    automatic_crossover_selection: HybridAutomaticCrossoverSelection | None = None,
    normalization_authority: HybridConventionNormalizationAuthority | None = None,
    reconciliation_method: str = 'exact_bin_identity_v1',
    frequency_tolerance_hz: float = 0.0,
    wave_source_model: WaveSourceModelCompatibility | None = None,
) -> StitchedHybridCompositionSpec:
    result = AcousticSolverResultEnvelope.model_validate(
        r130_result.model_dump(mode='python')
    )
    candidate = CandidateWaveExecutionInput.model_validate(
        r130_candidate_input.model_dump(mode='python')
    )
    excitation = AcousticWaveExcitationAuthority.model_validate(
        wave_excitation.model_dump(mode='python')
    )
    if wave_source_model is not None:
        wave_source_model = WaveSourceModelCompatibility.model_validate(
            wave_source_model.model_dump(mode='python')
        )
        if (
            wave_source_model.binding_semantic_sha256
            != candidate.wave_excitation_binding_sha256
            or wave_source_model.binding_id
            != candidate.wave_excitation_binding_id
        ):
            raise ValueError(
                'R160 stitched wave source model does not pin the candidate '
                "input's exact excitation binding"
            )
        if (
            wave_source_model.excitation_id != excitation.excitation_id
            or wave_source_model.excitation_semantic_sha256
            != excitation.semantic_sha256
        ):
            raise ValueError(
                'R160 stitched wave source model does not pin the exact wave '
                'excitation authority'
            )
        if wave_source_model.source_entity_id != candidate.source_entity_id:
            raise ValueError(
                'R160 stitched wave source model source identity mismatch'
            )
    responses = tuple(
        DeterministicPathFrequencyResponseArtifact.model_validate(
            item.model_dump(mode='python')
        )
        for item in r150_responses
    )
    if not responses:
        raise ValueError(
            'R160 stitched composition requires at least one R150 path response'
        )

    manifest = _complex_pressure_manifest(result)
    _, wave_grid = _validate_wave_inputs(
        result=result,
        payload=r130_artifact_payload,
        candidate_input=candidate,
        excitation=excitation,
        receiver_id=receiver_id,
    )
    grid = validate_frequency_grid(
        exact_frequency_grid_hz,
        label='R160 stitched output grid',
        tolerance_hz=frequency_tolerance_hz,
    )
    excitation_grid = validate_frequency_grid(
        tuple(float(item.frequency_hz) for item in excitation.samples),
        label='R160 wave excitation grid',
        tolerance_hz=frequency_tolerance_hz,
    )
    if excitation_grid != wave_grid:
        raise HybridNumericalCompositionError(
            HybridNumericalFailureCode.INPUT_CAPABILITY_MISMATCH,
            'R130 Q(f) grid must exactly bind the original wave grid before '
            'stitching',
        )

    source_ids = {item.source_entity_id for item in responses}
    receiver_ids = {item.receiver_id for item in responses}
    receiver_entity_ids = {item.receiver_entity_id for item in responses}
    candidate_receiver = next(
        (item for item in candidate.receivers if item.receiver_id == receiver_id),
        None,
    )
    if source_ids != {candidate.source_entity_id}:
        raise ValueError('R160 stitched R130/R150 source identity mismatch')
    if receiver_ids != {receiver_id} or candidate_receiver is None:
        raise ValueError('R160 stitched R130/R150 receiver identity mismatch')
    if receiver_entity_ids != {candidate_receiver.entity_id}:
        raise ValueError(
            'R160 stitched R130/R150 receiver entity identity mismatch'
        )

    for item in responses:
        if not any(
            ref.authority_id == candidate.compiled_geometry_id
            and ref.semantic_hash_sha256 == candidate.compiled_geometry_sha256
            for ref in item.dependency_refs
        ):
            raise ValueError(
                'R160 stitched R150 response does not bind the exact R130 '
                'compiled geometry'
            )
        if not any(
            ref.semantic_hash_sha256 == candidate.r110_compiled_source_sha256
            for ref in item.dependency_refs
        ):
            raise ValueError(
                'R160 stitched R150 response does not bind the exact R130 '
                'R110 source'
            )

    path_artifact_ids = {
        (item.deterministic_path_artifact_id, item.deterministic_path_artifact_sha256)
        for item in responses
    }
    if len(path_artifact_ids) != 1:
        raise ValueError(
            'R160 stitched R150 responses must bind one exact deterministic '
            'path artifact'
        )
    for item in responses:
        if (
            item.quantity != TRANSFER_QUANTITY
            or item.unit != TRANSFER_UNIT
            or item.source_normalization != COMMON_SOURCE_NORMALIZATION
            or item.phasor_convention != COMMON_PHASOR_CONVENTION
            or item.time_origin != COMMON_TIME_ORIGIN
        ):
            raise HybridNumericalCompositionError(
                HybridNumericalFailureCode.INPUT_CAPABILITY_MISMATCH,
                'R160 stitched R150 physical convention mismatch',
            )

    ga_grids = {
        tuple(float(frequency) for frequency in item.exact_frequency_grid_hz)
        for item in responses
    }
    if len(ga_grids) != 1:
        raise HybridNumericalCompositionError(
            HybridNumericalFailureCode.INPUT_CAPABILITY_MISMATCH,
            'R150 paths must share one original GA frequency grid before '
            'coherent summation',
        )
    ga_grid = validate_frequency_grid(
        next(iter(ga_grids)),
        label='R160 original GA grid',
        tolerance_hz=frequency_tolerance_hz,
    )
    plan = build_hybrid_band_stitch_plan(
        wave_valid_input_band_hz=(wave_grid[0], wave_grid[-1]),
        ga_valid_input_band_hz=(ga_grid[0], ga_grid[-1]),
        requested_output_frequency_grid_hz=grid,
        crossover_configuration=crossover_configuration,
        automatic_crossover_selection=automatic_crossover_selection,
        reconciliation_method=reconciliation_method,
        tolerance_hz=frequency_tolerance_hz,
    )
    selection = (
        None
        if automatic_crossover_selection is None
        else HybridAutomaticCrossoverSelection.model_validate(
            automatic_crossover_selection.model_dump(mode='python')
        )
    )

    normalization = (
        build_hybrid_convention_normalization_authority()
        if normalization_authority is None
        else HybridConventionNormalizationAuthority.model_validate(
            normalization_authority.model_dump(mode='python')
        )
    )
    response_refs = tuple(
        sorted((_response_ref(item) for item in responses), key=_ref_key)
    )
    core = {
        'authority_version': R160_STITCHED_SPEC_AUTHORITY_VERSION,
        'r130_result': {
            'result_id': result.result_id,
            'semantic_sha256': result.semantic_sha256,
        },
        'r130_complex_pressure_artifact_ref': manifest.artifact_authority.model_dump(
            mode='json'
        ),
        'r130_candidate_input': {
            'execution_input_id': candidate.execution_input_id,
            'authority_version': candidate.authority_version,
            'semantic_sha256': candidate.semantic_sha256,
        },
        'wave_excitation_ref': _excitation_ref(excitation).model_dump(mode='json'),
        'r150_response_refs': [
            item.model_dump(mode='json') for item in response_refs
        ],
        'source_entity_id': candidate.source_entity_id,
        'receiver_id': receiver_id,
        'exact_frequency_grid_hz': list(grid),
        'quantity': TRANSFER_QUANTITY,
        'unit': TRANSFER_UNIT,
        'common_phasor_convention': COMMON_PHASOR_CONVENTION,
        'common_analysis_fourier_kernel': COMMON_ANALYSIS_FOURIER_KERNEL,
        'source_normalization': COMMON_SOURCE_NORMALIZATION,
        'time_origin': COMMON_TIME_ORIGIN,
        'normalization_authority_ref': normalization.as_external_ref().model_dump(
            mode='json'
        ),
        'stitch_plan': plan.model_dump(mode='json'),
        'weight_law': 'linear_frequency_complementary_v1',
    }
    if wave_source_model is not None:
        core['wave_source_model'] = wave_source_model.model_dump(mode='json')
    if selection is not None:
        core['crossover_selection'] = selection.model_dump(mode='json')
    digest = _semantic_hash(core)
    return StitchedHybridCompositionSpec(
        composition_spec_id=f'r160-stitched-composition-spec:{digest}',
        semantic_sha256=digest,
        **core,
    )

class StitchedHybridResponseSample(BaseModel):
    """One output-grid sample with per-point provenance and honest absence."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    frequency_hz: float = Field(gt=0.0)
    region: HybridStitchRegionKind
    low_weight: float = Field(ge=0.0, le=1.0)
    high_weight: float = Field(ge=0.0, le=1.0)

    wave_complex_real_pa_per_m3_s: float | None = None
    wave_complex_imag_pa_per_m3_s: float | None = None
    ga_complex_real_pa_per_m3_s: float | None = None
    ga_complex_imag_pa_per_m3_s: float | None = None
    complex_real_pa_per_m3_s: float
    complex_imag_pa_per_m3_s: float
    magnitude_pa_per_m3_s: float = Field(ge=0.0)
    phase_rad: float

    @model_validator(mode='after')
    def consistent(self) -> 'StitchedHybridResponseSample':
        if not isclose(
            float(self.low_weight) + float(self.high_weight),
            1.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError('R160 stitched complementary weights must sum to one')
        wave = (
            None
            if self.wave_complex_real_pa_per_m3_s is None
            or self.wave_complex_imag_pa_per_m3_s is None
            else complex(
                self.wave_complex_real_pa_per_m3_s,
                self.wave_complex_imag_pa_per_m3_s,
            )
        )
        ga = (
            None
            if self.ga_complex_real_pa_per_m3_s is None
            or self.ga_complex_imag_pa_per_m3_s is None
            else complex(
                self.ga_complex_real_pa_per_m3_s,
                self.ga_complex_imag_pa_per_m3_s,
            )
        )
        if (self.wave_complex_real_pa_per_m3_s is None) != (
            self.wave_complex_imag_pa_per_m3_s is None
        ) or (self.ga_complex_real_pa_per_m3_s is None) != (
            self.ga_complex_imag_pa_per_m3_s is None
        ):
            raise ValueError('R160 stitched complex components must pair up')
        if wave is None and self.low_weight != 0.0:
            raise ValueError('R160 stitched missing wave value needs zero weight')
        if ga is None and self.high_weight != 0.0:
            raise ValueError('R160 stitched missing GA value needs zero weight')
        if self.region == 'crossover_blend' and (wave is None or ga is None):
            raise ValueError(
                'R160 crossover_blend sample requires both backends'
            )
        if self.region == 'wave_only' and wave is None:
            raise ValueError('R160 wave_only sample requires wave evidence')
        if self.region == 'ga_only' and ga is None:
            raise ValueError('R160 ga_only sample requires GA evidence')
        expected = (self.low_weight * (wave or 0.0)) + (
            self.high_weight * (ga or 0.0)
        )
        actual = complex(
            self.complex_real_pa_per_m3_s,
            self.complex_imag_pa_per_m3_s,
        )
        if abs(expected - actual) > max(1e-12, abs(expected) * 1e-10):
            raise ValueError(
                'R160 stitched sample does not equal complementary blend'
            )
        if not isclose(
            abs(actual),
            self.magnitude_pa_per_m3_s,
            rel_tol=1e-10,
            abs_tol=1e-12,
        ):
            raise ValueError('R160 stitched magnitude mismatch')
        delta = _phase(actual) - float(self.phase_rad)
        if abs(atan2(sin(delta), cos(delta))) > 1e-10:
            raise ValueError('R160 stitched phase mismatch')
        return self

class StitchedHybridResponseArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = R160_STITCHED_SCHEMA_VERSION
    authority_version: Literal[
        'r160-stitched-hybrid-response-1'
    ] = R160_STITCHED_ARTIFACT_AUTHORITY_VERSION
    artifact_id: str = Field(
        pattern=r'^r160-stitched-hybrid-response:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    composition_spec: StitchedHybridCompositionSpec
    exact_r130_result: ExactSolverResultIdentity
    exact_r130_artifact_ref: ExactExternalAuthorityRef
    exact_r150_response_refs: tuple[ExactExternalAuthorityRef, ...] = Field(
        min_length=1
    )
    exact_aggregated_ga_identity: ExactExternalAuthorityRef | None = None
    aggregated_ga: AggregatedGaComplexResponse | None = None
    stitch_plan: HybridBandStitchPlanAuthority
    stitch_state: HybridStitchState
    gap_domains: tuple[HybridStitchGap, ...] = ()
    crossover_selection: HybridAutomaticCrossoverSelection | None = None

    exact_frequency_grid_hz: tuple[float, ...] = Field(min_length=2)
    quantity: Literal[
        'complex_acoustic_pressure_per_volume_velocity'
    ] = TRANSFER_QUANTITY
    unit: Literal['Pa/(m3/s)'] = TRANSFER_UNIT
    common_phasor_convention: Literal[
        'exp(+i*omega*t)'
    ] = COMMON_PHASOR_CONVENTION
    common_analysis_fourier_kernel: Literal[
        'exp(-i*omega*t)'
    ] = COMMON_ANALYSIS_FOURIER_KERNEL
    source_normalization: Literal[
        'unit_volume_velocity_m3_s'
    ] = COMMON_SOURCE_NORMALIZATION
    time_origin: Literal['source_t0'] = COMMON_TIME_ORIGIN
    weight_law: HybridWeightLaw = 'linear_frequency_complementary_v1'

    capability_state: HybridNumericalCapability
    failure_codes: tuple[HybridNumericalFailureCode, ...] = ()
    unsupported_reasons: tuple[str, ...] = ()
    samples: tuple[StitchedHybridResponseSample, ...] = ()

    wave_source_model: WaveSourceModelCompatibility | None = None
    source_model_state: Literal[
        'compatible',
        'compatible_with_limitations',
        'unverified',
        'unsupported',
    ]

    @model_validator(mode='after')
    def contract(self) -> 'StitchedHybridResponseArtifact':
        if self.composition_spec.r130_result != self.exact_r130_result:
            raise ValueError('R160 stitched output R130 result identity mismatch')
        if (
            self.composition_spec.r130_complex_pressure_artifact_ref
            != self.exact_r130_artifact_ref
        ):
            raise ValueError('R160 stitched output R130 artifact mismatch')
        if (
            self.composition_spec.r150_response_refs
            != self.exact_r150_response_refs
        ):
            raise ValueError('R160 stitched output R150 identity mismatch')
        if (self.aggregated_ga is None) != (
            self.exact_aggregated_ga_identity is None
        ):
            raise ValueError(
                'R160 stitched GA aggregate and its identity must pair up'
            )
        if self.aggregated_ga is not None and (
            self.aggregated_ga.as_external_ref()
            != self.exact_aggregated_ga_identity
        ):
            raise ValueError('R160 stitched GA aggregate identity mismatch')
        if self.exact_frequency_grid_hz != self.composition_spec.exact_frequency_grid_hz:
            raise ValueError('R160 stitched output frequency grid mismatch')
        if self.stitch_plan != self.composition_spec.stitch_plan:
            raise ValueError('R160 stitched output plan mismatch')
        if self.stitch_state != self.stitch_plan.stitch_state:
            raise ValueError('R160 stitched state does not match plan')
        if self.gap_domains != self.stitch_plan.gap_domains:
            raise ValueError('R160 stitched gaps do not match plan')
        if (self.crossover_selection is None) != (
            self.composition_spec.crossover_selection is None
        ):
            raise ValueError('R160 stitched selection presence mismatch')
        if (
            self.crossover_selection is not None
            and self.crossover_selection
            != self.composition_spec.crossover_selection
        ):
            raise ValueError('R160 stitched selection mismatch')
        if self.weight_law != self.composition_spec.weight_law:
            raise ValueError('R160 stitched weight law mismatch')
        if self.capability_state == 'COMPLEX_SUPPORTED':
            if self.failure_codes or self.unsupported_reasons:
                raise ValueError(
                    'supported R160 stitched output cannot carry failure metadata'
                )
            if tuple(item.frequency_hz for item in self.samples) != self.exact_frequency_grid_hz:
                raise ValueError(
                    'supported R160 stitched output must cover exact grid'
                )
            regions = {
                float(item.frequency_hz): item.region for item in self.samples
            }
            for frequency in self.exact_frequency_grid_hz:
                region = self.stitch_plan.region_for(frequency)
                if region is None or regions[frequency] != region.region:
                    raise ValueError(
                        'R160 stitched sample region does not match plan'
                    )
        else:
            if not self.failure_codes or not self.unsupported_reasons or self.samples:
                raise ValueError(
                    'unsupported R160 stitched output requires failure '
                    'codes/reasons and no samples'
                )
        if self.wave_source_model is None:
            if self.source_model_state != 'unverified':
                raise ValueError(
                    'R160 stitched output without a wave source model must '
                    "record source_model_state='unverified'"
                )
        else:
            if self.wave_source_model != self.composition_spec.wave_source_model:
                raise ValueError(
                    'R160 stitched wave source model does not match the spec'
                )
            if self.source_model_state != self.wave_source_model.compatibility_state:
                raise ValueError(
                    'R160 stitched source_model_state mismatch'
                )
            if self.source_model_state == 'unsupported':
                raise ValueError(
                    'R160 stitched output cannot mark a source-model-'
                    'unsupported composition as produced'
                )
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('R160 stitched hybrid semantic hash mismatch')
        if self.artifact_id != f'r160-stitched-hybrid-response:{expected}':
            raise ValueError('R160 stitched hybrid id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            mode='json',
            exclude={'artifact_id', 'semantic_sha256'},
        )
        if payload.get('wave_source_model') is None:
            payload.pop('wave_source_model')
        if payload.get('crossover_selection') is None:
            payload.pop('crossover_selection')
        if payload.get('aggregated_ga') is None:
            payload.pop('aggregated_ga')
        if payload.get('exact_aggregated_ga_identity') is None:
            payload.pop('exact_aggregated_ga_identity')
        return payload

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.artifact_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )

def compose_stitched_hybrid_response(
    *,
    spec: StitchedHybridCompositionSpec,
    r130_result: AcousticSolverResultEnvelope,
    r130_artifact_payload: Any,
    r130_candidate_input: CandidateWaveExecutionInput,
    wave_excitation: AcousticWaveExcitationAuthority,
    r150_responses: Sequence[DeterministicPathFrequencyResponseArtifact],
    normalization_authority: HybridConventionNormalizationAuthority | None = None,
) -> StitchedHybridResponseArtifact:
    spec = StitchedHybridCompositionSpec.model_validate(
        spec.model_dump(mode='python')
    )
    normalization = (
        build_hybrid_convention_normalization_authority()
        if normalization_authority is None
        else HybridConventionNormalizationAuthority.model_validate(
            normalization_authority.model_dump(mode='python')
        )
    )
    if normalization.as_external_ref() != spec.normalization_authority_ref:
        raise ValueError('R160 stitched convention authority is stale')

    expected_spec = build_stitched_hybrid_composition_spec(
        r130_result=r130_result,
        r130_artifact_payload=r130_artifact_payload,
        r130_candidate_input=r130_candidate_input,
        wave_excitation=wave_excitation,
        r150_responses=r150_responses,
        receiver_id=spec.receiver_id,
        exact_frequency_grid_hz=spec.exact_frequency_grid_hz,
        crossover_configuration=spec.stitch_plan.crossover_configuration,
        automatic_crossover_selection=spec.crossover_selection,
        normalization_authority=normalization,
        reconciliation_method=spec.stitch_plan.reconciliation_method,
        frequency_tolerance_hz=spec.stitch_plan.tolerance_hz,
        wave_source_model=spec.wave_source_model,
    )
    if expected_spec != spec:
        raise ValueError(
            'R160 stitched composition spec is stale for exact inputs'
        )

    wave_points = tuple(
        frequency
        for frequency in spec.exact_frequency_grid_hz
        if spec.stitch_plan.region_for(frequency) is not None
        and spec.stitch_plan.region_for(frequency).region
        in ('wave_only', 'crossover_blend')
    )
    ga_points = tuple(
        frequency
        for frequency in spec.exact_frequency_grid_hz
        if spec.stitch_plan.region_for(frequency) is not None
        and spec.stitch_plan.region_for(frequency).region
        in ('ga_only', 'crossover_blend')
    )

    aggregate: AggregatedGaComplexResponse | None = None
    if ga_points:
        aggregate = aggregate_ga_paths_on_grid(
            responses=r150_responses,
            path_response_refs=spec.r150_response_refs,
            source_entity_id=spec.source_entity_id,
            receiver_id=spec.receiver_id,
            native_ga_grid_hz=tuple(
                float(item)
                for item in r150_responses[0].exact_frequency_grid_hz
            ),
            output_grid_hz=ga_points,
            grid_reconciliation_ref=spec.stitch_plan.as_external_ref(),
            reconciliation_method=spec.stitch_plan.reconciliation_method,
            tolerance_hz=spec.stitch_plan.tolerance_hz,
        )

    base = {
        'schema_version': R160_STITCHED_SCHEMA_VERSION,
        'authority_version': R160_STITCHED_ARTIFACT_AUTHORITY_VERSION,
        'composition_spec': spec.model_dump(mode='json'),
        'exact_r130_result': spec.r130_result.model_dump(mode='json'),
        'exact_r130_artifact_ref': (
            spec.r130_complex_pressure_artifact_ref.model_dump(mode='json')
        ),
        'exact_r150_response_refs': [
            item.model_dump(mode='json') for item in spec.r150_response_refs
        ],
        'stitch_plan': spec.stitch_plan.model_dump(mode='json'),
        'stitch_state': spec.stitch_plan.stitch_state,
        'gap_domains': [
            item.model_dump(mode='json')
            for item in spec.stitch_plan.gap_domains
        ],
        'exact_frequency_grid_hz': list(spec.exact_frequency_grid_hz),
        'quantity': TRANSFER_QUANTITY,
        'unit': TRANSFER_UNIT,
        'common_phasor_convention': COMMON_PHASOR_CONVENTION,
        'common_analysis_fourier_kernel': COMMON_ANALYSIS_FOURIER_KERNEL,
        'source_normalization': COMMON_SOURCE_NORMALIZATION,
        'time_origin': COMMON_TIME_ORIGIN,
        'weight_law': spec.weight_law,
        'source_model_state': (
            'unverified'
            if spec.wave_source_model is None
            else spec.wave_source_model.compatibility_state
        ),
    }
    if spec.wave_source_model is not None:
        base['wave_source_model'] = spec.wave_source_model.model_dump(mode='json')
    if spec.crossover_selection is not None:
        base['crossover_selection'] = spec.crossover_selection.model_dump(
            mode='json'
        )
    if aggregate is not None:
        base['aggregated_ga'] = aggregate.model_dump(mode='json')
        base['exact_aggregated_ga_identity'] = (
            aggregate.as_external_ref().model_dump(mode='json')
        )

    if (
        spec.wave_source_model is not None
        and spec.wave_source_model.collapse_state == 'collapse_unsupported'
    ):
        core = {
            **base,
            'capability_state': 'UNSUPPORTED',
            'failure_codes': [HybridNumericalFailureCode.INPUT_CAPABILITY_MISMATCH],
            'unsupported_reasons': [
                'R130 wave source model collapse is falsified: '
                + '; '.join(spec.wave_source_model.reasons)
            ],
            'samples': [],
        }
    elif (
        aggregate is not None
        and aggregate.capability_state != 'COMPLEX_SUPPORTED'
    ):
        core = {
            **base,
            'capability_state': 'UNSUPPORTED',
            'failure_codes': [HybridNumericalFailureCode.INPUT_CAPABILITY_MISMATCH],
            'unsupported_reasons': [
                'R150 coherent path aggregation unavailable: '
                + '; '.join(aggregate.unsupported_reasons)
            ],
            'samples': [],
        }
    else:
        assert isinstance(r130_artifact_payload, dict)
        native_grid, native_values = compute_native_wave_transfer(
            receiver_id=spec.receiver_id,
            payload=r130_artifact_payload,
            excitation=wave_excitation,
        )
        wave = reconcile_complex_series_with_method(
            original_grid_hz=native_grid,
            values=native_values,
            output_grid_hz=wave_points,
            reconciliation_method=spec.stitch_plan.reconciliation_method,
            tolerance_hz=spec.stitch_plan.tolerance_hz,
            label='R160 stitched wave transfer',
        )
        ga = (
            {}
            if aggregate is None
            else {
                item.frequency_hz: complex(
                    item.complex_real_pa_per_m3_s,
                    item.complex_imag_pa_per_m3_s,
                )
                for item in aggregate.samples
            }
        )
        samples: list[dict[str, Any]] = []
        for frequency in spec.exact_frequency_grid_hz:
            region = spec.stitch_plan.region_for(frequency)
            assert region is not None
            if region.region == 'crossover_blend':
                crossover = spec.stitch_plan.crossover_configuration
                assert crossover is not None
                low_weight, high_weight = _weights(
                    frequency,
                    start_hz=float(crossover.overlap_lower_hz),
                    end_hz=float(crossover.overlap_upper_hz),
                )
            elif region.region == 'wave_only':
                low_weight, high_weight = 1.0, 0.0
            else:
                low_weight, high_weight = 0.0, 1.0
            wave_value = wave.get(frequency)
            ga_value = ga.get(frequency)
            hybrid = (low_weight * (wave_value or 0.0)) + (
                high_weight * (ga_value or 0.0)
            )
            samples.append(
                {
                    'frequency_hz': frequency,
                    'region': region.region,
                    'low_weight': low_weight,
                    'high_weight': high_weight,
                    'wave_complex_real_pa_per_m3_s': (
                        None if wave_value is None else wave_value.real
                    ),
                    'wave_complex_imag_pa_per_m3_s': (
                        None if wave_value is None else wave_value.imag
                    ),
                    'ga_complex_real_pa_per_m3_s': (
                        None if ga_value is None else ga_value.real
                    ),
                    'ga_complex_imag_pa_per_m3_s': (
                        None if ga_value is None else ga_value.imag
                    ),
                    'complex_real_pa_per_m3_s': hybrid.real,
                    'complex_imag_pa_per_m3_s': hybrid.imag,
                    'magnitude_pa_per_m3_s': abs(hybrid),
                    'phase_rad': _phase(hybrid),
                }
            )
        core = {
            **base,
            'capability_state': 'COMPLEX_SUPPORTED',
            'failure_codes': [],
            'unsupported_reasons': [],
            'samples': samples,
        }

    digest = _semantic_hash(core)
    return StitchedHybridResponseArtifact(
        artifact_id=f'r160-stitched-hybrid-response:{digest}',
        semantic_sha256=digest,
        **core,
    )

StitchedCompositionSpecResolver = Callable[
    [str], StitchedHybridCompositionSpec | None
]
WaveResultResolver = Callable[[str], AcousticSolverResultEnvelope | None]
WaveArtifactPayloadResolver = Callable[[ExactExternalAuthorityRef], Any]
CandidateInputResolver = Callable[[str], CandidateWaveExecutionInput | None]
WaveExcitationResolver = Callable[[str], AcousticWaveExcitationAuthority | None]
R150ResponseResolver = Callable[
    [str], DeterministicPathFrequencyResponseArtifact | None
]
ConventionAuthorityResolver = Callable[
    [ExactExternalAuthorityRef], HybridConventionNormalizationAuthority | None
]

__all__ = [
    '',
    'CrossoverAgreementSample',
    'CrossoverBandCandidate',
    'CrossoverSelectionState',
    'HybridAutomaticCrossoverSelection',
    'HybridBandStitchPlanAuthority',
    'HybridStitchGap',
    'HybridStitchRegion',
    'HybridStitchRegionKind',
    'HybridStitchState',
    'R160_AUTO_CROSSOVER_AUTHORITY_VERSION',
    'R160_BAND_STITCH_AUTHORITY_VERSION',
    'R160_STITCHED_ARTIFACT_AUTHORITY_VERSION',
    'R160_STITCHED_SCHEMA_VERSION',
    'R160_STITCHED_SPEC_AUTHORITY_VERSION',
    'StitchedHybridCompositionSpec',
    'StitchedHybridResponseArtifact',
    'StitchedHybridResponseSample',
    'build_automatic_crossover_selection',
    'build_hybrid_band_stitch_plan',
    'build_stitched_hybrid_composition_spec',
    'compose_stitched_hybrid_response',
]
