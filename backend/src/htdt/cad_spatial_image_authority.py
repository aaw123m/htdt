"""Spatial projection-image qualification authority (#619, REV57-PROJ).

Center-screen color/luminance calibration does not prove that the whole
projected image is uniform from the actual seating area. This module owns
the installed-system spatial evidence:

- :class:`CadSpatialMeasurementPlan` — a versioned sampling plan: grid
  layout, explicit sample points with roles (center/edge/corner), the
  declared quantity set, measurement viewpoints, screen/projector state
  requirements and the bound evaluation-profile reference.
- :class:`CadSpatialMeasurementSet` — the sealed canonical evidence:
  embedded per-point observations (each binding point, viewpoint,
  quantity, stimulus profile and optional state snapshots). Raw
  per-point observations are canonical; no interpolated map can replace
  them.
- :class:`CadSpatialDerivedMap` — a *derived* artifact (heatmap) that
  must carry its interpolation algorithm/version, grid, extrapolation
  and smoothing provenance. It is never interchangeable with an
  observation set.
- :class:`CadImageUniformityEvaluation` + :func:`evaluate_spatial_uniformity`
  — the fail-closed per-quantity verdict. Uniformity is only evaluated
  where the plan actually sampled; a center-only set can never claim
  spatial uniformity, and a set with unmeasured edge/corner roles stays
  ``insufficient_coverage``.

Honesty rules baked in:

- ``predicted`` and ``field_measured`` evidence kinds are distinct and
  never silently comparable.
- Every quantity (white/black luminance, contrast, chromaticity, color
  error, EOTF tracking, focus/sharpness, convergence, project-defined
  diagnostics) is an independent axis — no opaque ``uniformity score``.
- Spatial criteria come from a bound external/project profile — never a
  hard-coded universal percentage across D-cinema, residential SDR, HDR
  and ALR-screen scenarios.
- When the plan requires thermal stabilization, an unstabilized or
  unknown projector state cannot produce profile-grade verdicts — a
  cold baseline is not a calibration comparison.
- Multi-viewpoint plans are evaluated per viewpoint; seats are never
  averaged into one result on an angularly selective screen.

Literature basis
----------------
- ANSI IT7.215/IT7.228 9-point average measurement lineage carried into
  IEC 61947-1:2002, IEC 62906-5-1:2021 and ISO 21118:2020 (light-output
  and uniformity sampling grids).
- ISO 26431-1:2008 (confirmed 2023) — D-cinema screen luminance,
  chromaticity and uniformity of the *installed* projector-room-screen
  system.
- SMPTE ST 431-1:2006 — position-dependent screen luminance and white
  chromaticity measurement semantics.
- SMPTE RP 431-2:2011 — reference-projector/environment conditions:
  thermal stabilization and stray-light control binding the
  measurement.
- AVIXA V202.01:2026 viewing-system context (residential scope caution:
  D-cinema thresholds are never silently imposed).
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


SPATIAL_AUTHORITY_SCHEMA_VERSION = 'proj-spatial-1'
SPATIAL_EVALUATION_VERSION = 'proj-spatial-eval-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _require_finite(value: float, label: str) -> None:
    if not isfinite(float(value)):
        raise ValueError(f'{label} must be finite')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


# ---------------------------------------------------------------------------
# Taxonomies (#619)
# ---------------------------------------------------------------------------

SpatialQuantity = Literal[
    'white_luminance',
    'black_luminance',
    'contrast',
    'white_chromaticity',
    'color_error',
    'eotf_gamma_tracking',
    'focus_sharpness',
    'convergence_fringe',
    'project_defined',
]
"""Independent spatial quantities — never folded into one score."""

MeasurementPointRole = Literal[
    'center', 'edge', 'corner', 'auxiliary', 'custom',
]

SpatialGridLayout = Literal[
    'ansi_9_point',
    'center_plus_corners',
    'center_plus_edges',
    'dense_research_grid',
    'project_defined',
    'adaptive_diagnostic',
    'center_only',
]
"""Versioned sampling layouts. ``ansi_9_point`` is the ANSI IT7 /
IEC 61947-1 3x3 lineage; ``center_only`` is an honest minimum that can
never carry a spatial-uniformity claim."""

SpatialEvidenceKind = Literal['field_measured', 'predicted']
"""Installed-system truth vs design-time prediction — kept distinct."""

StimulusProfile = Literal[
    'sdr_reference',
    'hdr_peak_window',
    'hdr_full_field_apl',
    'project_defined',
    'unknown',
]
"""Exact signal/picture profile the spatial campaign ran under."""

StabilizationState = Literal['stabilized', 'warming', 'unknown']

KeystoneCorrectionState = Literal['none', 'active', 'unknown']

ScreenCurvature = Literal['flat', 'curved', 'other', 'unknown']

RoomLightState = Literal[
    'dark', 'controlled', 'ambient_present', 'unknown',
]


# ---------------------------------------------------------------------------
# State snapshots bound into plans / sets
# ---------------------------------------------------------------------------


class CadMeasurementViewpoint(BaseModel):
    """One declared measurement/viewing position.

    A high-gain or ALR screen produces a materially different luminance
    distribution from different seats — the viewpoint is part of the
    measurement identity, never implicit.
    """

    model_config = ConfigDict(frozen=True)

    viewpoint_id: str = Field(min_length=1)
    seat_binding: str | None = None
    meter_xyz_m: tuple[float, float, float] | None = None
    distance_to_screen_m: float | None = None
    angle_to_screen_deg: float | None = None
    field_of_view_note: str | None = None

    @model_validator(mode='after')
    def valid_viewpoint(self) -> 'CadMeasurementViewpoint':
        if self.meter_xyz_m is not None:
            for component in self.meter_xyz_m:
                _require_finite(component, 'viewpoint meter_xyz_m')
        if self.distance_to_screen_m is not None:
            _require_finite(
                self.distance_to_screen_m, 'viewpoint distance_to_screen_m'
            )
            if self.distance_to_screen_m <= 0:
                raise ValueError('viewpoint distance must be positive')
        if self.angle_to_screen_deg is not None:
            _require_finite(
                self.angle_to_screen_deg, 'viewpoint angle_to_screen_deg'
            )
            if not 0 <= self.angle_to_screen_deg <= 180:
                raise ValueError('viewpoint angle must be within [0, 180]')
        return self


class CadScreenStateSnapshot(BaseModel):
    """The exact screen state a spatial measurement binds.

    Screen material/gain/curvature/masking/aging can all contribute to
    spatial variation — the snapshot keeps them attributable rather than
    blaming the projector by default.
    """

    model_config = ConfigDict(frozen=True)

    screen_ref: AuthorityRef | None = None
    material: str | None = None
    gain_model: str | None = None
    curvature: ScreenCurvature = 'unknown'
    masking_state: str | None = None
    condition_notes: str | None = None

    @model_validator(mode='after')
    def valid_screen_state(self) -> 'CadScreenStateSnapshot':
        if self.screen_ref is not None and self.screen_ref.ref_sha256 is None:
            raise ValueError('screen_ref must pin the evidence sha256')
        return self


class CadProjectorOpticalState(BaseModel):
    """The exact projector optical state a measurement binds.

    Spatial performance measured at one lens memory / zoom / shift /
    picture-mode state does not transfer to another; warm-up drift
    confounds before/after comparisons. Fields left unset stay UNKNOWN
    — they are never synthesized.
    """

    model_config = ConfigDict(frozen=True)

    projector_ref: AuthorityRef | None = None
    picture_mode: str | None = None
    light_source_mode: str | None = None
    lens_memory_id: str | None = None
    zoom_ratio: float | None = None
    focus_state: str | None = None
    lens_shift_h: float | None = None
    lens_shift_v: float | None = None
    keystone_correction: KeystoneCorrectionState = 'unknown'
    anamorphic_state: str | None = None
    warmup_seconds_on: float | None = None
    stabilization_state: StabilizationState = 'unknown'

    @model_validator(mode='after')
    def valid_optical_state(self) -> 'CadProjectorOpticalState':
        if self.projector_ref is not None and (
            self.projector_ref.ref_sha256 is None
        ):
            raise ValueError('projector_ref must pin the spec sha256')
        for label, value in (
            ('zoom_ratio', self.zoom_ratio),
            ('lens_shift_h', self.lens_shift_h),
            ('lens_shift_v', self.lens_shift_v),
            ('warmup_seconds_on', self.warmup_seconds_on),
        ):
            if value is not None:
                _require_finite(value, f'projector state {label}')
        if self.zoom_ratio is not None and self.zoom_ratio <= 0:
            raise ValueError('zoom_ratio must be positive')
        if self.warmup_seconds_on is not None and self.warmup_seconds_on < 0:
            raise ValueError('warmup_seconds_on must be non-negative')
        return self


class CadRoomLightState(BaseModel):
    """Room / stray-light state composing with #263/#270/#573."""

    model_config = ConfigDict(frozen=True)

    room_light_state: RoomLightState = 'unknown'
    wall_ceiling_reflectance: str | None = None
    opening_state: str | None = None
    other_emitters: str | None = None
    projector_off_black_observed: bool | None = None


# ---------------------------------------------------------------------------
# Sampling plan
# ---------------------------------------------------------------------------


class CadSpatialSamplePoint(BaseModel):
    """One declared sample point in exact screen coordinates."""

    model_config = ConfigDict(frozen=True)

    point_id: str = Field(min_length=1)
    role: MeasurementPointRole
    x_fraction: float
    y_fraction: float
    physical_xyz_m: tuple[float, float, float] | None = None

    @model_validator(mode='after')
    def valid_point(self) -> 'CadSpatialSamplePoint':
        for label, value in (
            ('x_fraction', self.x_fraction),
            ('y_fraction', self.y_fraction),
        ):
            _require_finite(value, f'sample point {label}')
            if not 0.0 <= value <= 1.0:
                raise ValueError(f'sample point {label} must be within [0, 1]')
        if self.physical_xyz_m is not None:
            for component in self.physical_xyz_m:
                _require_finite(component, 'sample point physical_xyz_m')
        return self


def ansi_9_point_grid() -> tuple[CadSpatialSamplePoint, ...]:
    """The ANSI IT7 / IEC 61947-1 3x3 sample grid.

    Eight surrounding points at the centers of the tic-tac-toe cells
    plus the screen center — roles are explicit so an evaluation knows
    which points carry edge/corner evidence.
    """
    points: list[CadSpatialSamplePoint] = [
        CadSpatialSamplePoint(
            point_id='center', role='center',
            x_fraction=0.5, y_fraction=0.5,
        )
    ]
    for row, y in (('top', 1.0 / 6.0), ('middle', 0.5), ('bottom', 5.0 / 6.0)):
        for col, x in (('left', 1.0 / 6.0), ('center', 0.5), ('right', 5.0 / 6.0)):
            if row == 'middle' and col == 'center':
                continue
            role = 'corner' if col != 'center' and row != 'middle' else 'edge'
            points.append(
                CadSpatialSamplePoint(
                    point_id=f'{row}-{col}', role=role,
                    x_fraction=x, y_fraction=y,
                )
            )
    return tuple(points)


class CadSpatialMeasurementPlan(BaseModel):
    """Versioned spatial sampling plan for one installed system.

    Declares which screen positions are measured, from which
    viewpoints, for which quantities, under which stabilization
    criterion, and which external/project profile supplies the
    evaluation formula. The plan — not a single convenient center
    reading — defines the spatial domain.
    """

    model_config = ConfigDict(frozen=True)

    plan_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    layout: SpatialGridLayout
    points: tuple[CadSpatialSamplePoint, ...] = Field(min_length=1)
    quantities: tuple[SpatialQuantity, ...] = Field(min_length=1)
    viewpoints: tuple[CadMeasurementViewpoint, ...] = Field(min_length=1)
    screen_state: CadScreenStateSnapshot
    projector_state_requirement: CadProjectorOpticalState | None = None
    stabilization_required: bool = False
    evaluation_profile_ref: AuthorityRef | None = None
    criterion_formula: str | None = None
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_plan(self) -> 'CadSpatialMeasurementPlan':
        _require_iso8601(self.declared_at_utc, 'plan declared_at_utc')
        point_ids = [point.point_id for point in self.points]
        if len(point_ids) != len(set(point_ids)):
            raise ValueError('sample point ids must be unique')
        viewpoint_ids = [v.viewpoint_id for v in self.viewpoints]
        if len(viewpoint_ids) != len(set(viewpoint_ids)):
            raise ValueError('viewpoint ids must be unique')
        if len(set(self.quantities)) != len(self.quantities):
            raise ValueError('plan quantities must be unique')
        if self.layout == 'center_only' and (
            len(self.points) != 1 or self.points[0].role != 'center'
        ):
            raise ValueError(
                'a center_only layout must declare exactly one center point'
            )
        if self.layout == 'ansi_9_point' and len(self.points) != 9:
            raise ValueError(
                'ansi_9_point layout requires exactly 9 sample points'
            )
        if self.evaluation_profile_ref is not None and (
            self.evaluation_profile_ref.ref_sha256 is None
        ):
            raise ValueError('evaluation profile ref must pin its sha256')
        expected = _hash(self.identity_payload())
        if self.plan_sha256 != expected:
            raise ValueError('plan hash mismatch')
        if self.plan_id != _semantic_id('spplan', expected):
            raise ValueError('plan id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'layout': self.layout,
            'points': [
                point.model_dump(mode='json') for point in self.points
            ],
            'quantities': list(self.quantities),
            'viewpoints': [
                v.model_dump(mode='json') for v in self.viewpoints
            ],
            'screen_state': self.screen_state.model_dump(mode='json'),
            'projector_state_requirement': (
                self.projector_state_requirement.model_dump(mode='json')
                if self.projector_state_requirement is not None
                else None
            ),
            'stabilization_required': self.stabilization_required,
            'evaluation_profile_ref': (
                self.evaluation_profile_ref.model_dump(mode='json')
                if self.evaluation_profile_ref is not None
                else None
            ),
            'criterion_formula': self.criterion_formula,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }

    def spatial_roles_declared(self) -> frozenset[MeasurementPointRole]:
        return frozenset(point.role for point in self.points)


def plan_binding(plan: CadSpatialMeasurementPlan) -> AuthorityRef:
    return AuthorityRef(
        kind='spatial_measurement_plan',
        ref_id=plan.plan_id,
        ref_sha256=plan.plan_sha256,
    )


# ---------------------------------------------------------------------------
# Observations + measurement set
# ---------------------------------------------------------------------------


class CadSpatialObservation(BaseModel):
    """One measured spatial observation — canonical evidence.

    ``value`` carries scalar quantities; ``white_chromaticity`` instead
    carries the ``chromaticity_x``/``chromaticity_y`` pair. Optional
    per-observation state snapshots override the set-level bindings when
    the operator recorded a more exact state at that point.
    """

    model_config = ConfigDict(frozen=True)

    point_id: str = Field(min_length=1)
    viewpoint_id: str = Field(min_length=1)
    quantity: SpatialQuantity
    value: float | None = None
    units: str | None = None
    chromaticity_x: float | None = None
    chromaticity_y: float | None = None
    instrument_ref: AuthorityRef | None = None
    instrument_identity: str | None = None
    uncertainty: float | None = None
    projector_state: CadProjectorOpticalState | None = None
    screen_state: CadScreenStateSnapshot | None = None
    room_state: CadRoomLightState | None = None
    stimulus_profile: StimulusProfile = 'unknown'
    observed_at_utc: str | None = None
    note: str | None = None

    @model_validator(mode='after')
    def valid_observation(self) -> 'CadSpatialObservation':
        if self.value is not None:
            _require_finite(self.value, 'observation value')
            if self.units is None:
                raise ValueError(
                    'a measured value requires its units — '
                    'a bare number is not evidence'
                )
        if self.chromaticity_x is not None or self.chromaticity_y is not None:
            if self.chromaticity_x is None or self.chromaticity_y is None:
                raise ValueError(
                    'chromaticity requires both x and y'
                )
            for value in (self.chromaticity_x, self.chromaticity_y):
                _require_finite(value, 'chromaticity coordinate')
                if not 0.0 <= value <= 1.0:
                    raise ValueError('chromaticity coordinate outside [0, 1]')
        if self.quantity == 'white_chromaticity' and (
            self.chromaticity_x is None
        ):
            raise ValueError(
                'white_chromaticity observations carry the x/y pair'
            )
        if self.value is None and self.chromaticity_x is None:
            raise ValueError(
                'an observation requires a measured value or a '
                'chromaticity pair — a point label is not a measurement'
            )
        if self.uncertainty is not None:
            _require_finite(self.uncertainty, 'observation uncertainty')
            if self.uncertainty < 0:
                raise ValueError('uncertainty must be non-negative')
        if self.instrument_ref is not None and (
            self.instrument_ref.ref_sha256 is None
        ):
            raise ValueError('instrument_ref must pin its sha256')
        if self.observed_at_utc is not None:
            _require_iso8601(
                self.observed_at_utc, 'observation observed_at_utc'
            )
        return self


class CadSpatialMeasurementSet(BaseModel):
    """The sealed canonical spatial evidence for one campaign.

    Embeds every raw observation. ``evidence_kind`` keeps design-time
    predicted maps separate from installed-system field measurements —
    a catalogue brightness figure never promotes to measured screen
    uniformity.
    """

    model_config = ConfigDict(frozen=True)

    set_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    plan_ref: AuthorityRef
    evidence_kind: SpatialEvidenceKind = 'field_measured'
    stimulus_profile: StimulusProfile = 'unknown'
    projector_state: CadProjectorOpticalState | None = None
    screen_state: CadScreenStateSnapshot | None = None
    room_state: CadRoomLightState | None = None
    observations: tuple[CadSpatialObservation, ...] = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    set_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_set(self) -> 'CadSpatialMeasurementSet':
        _require_iso8601(self.declared_at_utc, 'set declared_at_utc')
        if self.plan_ref.ref_sha256 is None:
            raise ValueError('measurement sets must pin the plan sha256')
        keys = [
            (o.point_id, o.viewpoint_id, o.quantity)
            for o in self.observations
        ]
        if len(keys) != len(set(keys)):
            raise ValueError(
                'duplicate (point, viewpoint, quantity) observations — '
                'a re-measurement is a new set, not an overwrite'
            )
        expected = _hash(self.identity_payload())
        if self.set_sha256 != expected:
            raise ValueError('measurement set hash mismatch')
        if self.set_id != _semantic_id('spset', expected):
            raise ValueError('set id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'plan_ref': self.plan_ref.model_dump(mode='json'),
            'evidence_kind': self.evidence_kind,
            'stimulus_profile': self.stimulus_profile,
            'projector_state': (
                self.projector_state.model_dump(mode='json')
                if self.projector_state is not None
                else None
            ),
            'screen_state': (
                self.screen_state.model_dump(mode='json')
                if self.screen_state is not None
                else None
            ),
            'room_state': (
                self.room_state.model_dump(mode='json')
                if self.room_state is not None
                else None
            ),
            'observations': [
                o.model_dump(mode='json') for o in self.observations
            ],
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def set_binding(measurement_set: CadSpatialMeasurementSet) -> AuthorityRef:
    return AuthorityRef(
        kind='spatial_measurement_set',
        ref_id=measurement_set.set_id,
        ref_sha256=measurement_set.set_sha256,
    )


# ---------------------------------------------------------------------------
# Derived maps (never canonical evidence)
# ---------------------------------------------------------------------------


class CadSpatialDerivedMap(BaseModel):
    """An interpolated spatial map — a derived artifact with provenance.

    Requires its source set, interpolation algorithm/version, grid and
    extrapolation state, so a smooth heatmap can never masquerade as
    measured corner evidence.
    """

    model_config = ConfigDict(frozen=True)

    map_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    source_set_ref: AuthorityRef
    quantity: SpatialQuantity
    viewpoint_id: str | None = None
    interpolation_algorithm: str = Field(min_length=1)
    algorithm_version: str = Field(min_length=1)
    grid_resolution: str = Field(min_length=1)
    extrapolation: bool = False
    smoothing: str | None = None
    uncertainty: float | None = None
    rendered_artifact_ref: AuthorityRef | None = None
    declared_at_utc: str = Field(min_length=1)
    map_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_map(self) -> 'CadSpatialDerivedMap':
        _require_iso8601(self.declared_at_utc, 'map declared_at_utc')
        if self.source_set_ref.ref_sha256 is None:
            raise ValueError('derived maps must pin the source set sha256')
        if self.uncertainty is not None:
            _require_finite(self.uncertainty, 'map uncertainty')
            if self.uncertainty < 0:
                raise ValueError('map uncertainty must be non-negative')
        expected = _hash(self.identity_payload())
        if self.map_sha256 != expected:
            raise ValueError('derived map hash mismatch')
        if self.map_id != _semantic_id('spmap', expected):
            raise ValueError('map id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'source_set_ref': self.source_set_ref.model_dump(mode='json'),
            'quantity': self.quantity,
            'viewpoint_id': self.viewpoint_id,
            'interpolation_algorithm': self.interpolation_algorithm,
            'algorithm_version': self.algorithm_version,
            'grid_resolution': self.grid_resolution,
            'extrapolation': self.extrapolation,
            'smoothing': self.smoothing,
            'uncertainty': self.uncertainty,
            'rendered_artifact_ref': (
                self.rendered_artifact_ref.model_dump(mode='json')
                if self.rendered_artifact_ref is not None
                else None
            ),
            'declared_at_utc': self.declared_at_utc,
        }


def map_binding(derived_map: CadSpatialDerivedMap) -> AuthorityRef:
    return AuthorityRef(
        kind='spatial_derived_map',
        ref_id=derived_map.map_id,
        ref_sha256=derived_map.map_sha256,
    )


# ---------------------------------------------------------------------------
# Uniformity evaluation
# ---------------------------------------------------------------------------

SpatialCriterionMetric = Literal[
    'min_over_max_ratio',
    'center_deviation_pct',
    'max_xy_distance',
]
"""Metrics this evaluator can honestly compute. Anything else stays
``not_evaluable`` — a profile-defined formula HTDT cannot evaluate is
never silently approximated."""


class CadSpatialCriterion(BaseModel):
    """One profile-bound spatial criterion for one quantity.

    The criterion comes from the bound external/project profile — the
    evaluator never invents thresholds.
    """

    model_config = ConfigDict(frozen=True)

    quantity: SpatialQuantity
    metric: SpatialCriterionMetric
    operator: Literal['>=', '<=']
    limit: float

    @model_validator(mode='after')
    def valid_criterion(self) -> 'CadSpatialCriterion':
        _require_finite(self.limit, 'criterion limit')
        return self


QuantityState = Literal[
    'within_profile',
    'outside_profile',
    'criterion_unbound',
    'insufficient_coverage',
    'insufficient_evidence',
    'not_evaluable',
]


class CadSpatialQuantityVerdict(BaseModel):
    """Per-quantity spatial verdict — one axis of the evaluation."""

    model_config = ConfigDict(frozen=True)

    quantity: SpatialQuantity
    state: QuantityState
    per_viewpoint: tuple[tuple[str, QuantityState], ...] = ()
    expected_points: int = Field(ge=0)
    observed_points: int = Field(ge=0)
    missing_roles: tuple[MeasurementPointRole, ...] = ()
    center_value: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    spread: float | None = None
    spread_metric: str | None = None
    reasons: tuple[str, ...] = ()


class CadImageUniformityEvaluation(BaseModel):
    """Sealed spatial-uniformity verdict for one measurement set.

    Reports every declared plan quantity — an omitted quantity would
    silently read as fine. ``coverage_state`` records whether the plan's
    declared spatial domain was actually sampled: a ``center_only`` or
    partially covered set can never claim installed-system uniformity.
    """

    model_config = ConfigDict(frozen=True)

    evaluation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    plan_ref: AuthorityRef
    set_ref: AuthorityRef
    evidence_kind: SpatialEvidenceKind
    coverage_state: Literal[
        'full_spatial_coverage',
        'partial_spatial_coverage',
        'center_only',
        'empty',
    ]
    quantity_verdicts: tuple[CadSpatialQuantityVerdict, ...]
    unbound_observations: int = Field(ge=0, default=0)
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_evaluation(self) -> 'CadImageUniformityEvaluation':
        _require_iso8601(
            self.evaluated_at_utc, 'evaluation evaluated_at_utc'
        )
        for ref, label in (
            (self.plan_ref, 'plan'),
            (self.set_ref, 'set'),
        ):
            if ref.ref_sha256 is None:
                raise ValueError(f'evaluation must pin the {label} sha256')
        if not self.quantity_verdicts:
            raise ValueError(
                'an evaluation must report every declared quantity — '
                'an omitted quantity would silently read as valid'
            )
        expected = _hash(self.identity_payload())
        if self.evaluation_sha256 != expected:
            raise ValueError('evaluation hash mismatch')
        if self.evaluation_id != _semantic_id('speval', expected):
            raise ValueError('evaluation id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'plan_ref': self.plan_ref.model_dump(mode='json'),
            'set_ref': self.set_ref.model_dump(mode='json'),
            'evidence_kind': self.evidence_kind,
            'coverage_state': self.coverage_state,
            'quantity_verdicts': [
                v.model_dump(mode='json') for v in self.quantity_verdicts
            ],
            'unbound_observations': self.unbound_observations,
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    def verdict_for(self, quantity: SpatialQuantity) -> QuantityState:
        for verdict in self.quantity_verdicts:
            if verdict.quantity == quantity:
                return verdict.state
        return 'insufficient_evidence'


def evaluation_binding(
    evaluation: CadImageUniformityEvaluation,
) -> AuthorityRef:
    return AuthorityRef(
        kind='spatial_uniformity_evaluation',
        ref_id=evaluation.evaluation_id,
        ref_sha256=evaluation.evaluation_sha256,
    )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _seal_model(model, payload: dict[str, Any], id_field: str,
                sha_field: str, prefix: str):
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{id_field: _semantic_id(prefix, digest), sha_field: digest},
    )


def build_spatial_measurement_plan(
    *,
    document_id: str,
    layout: SpatialGridLayout,
    points: tuple[CadSpatialSamplePoint, ...],
    quantities: tuple[SpatialQuantity, ...],
    viewpoints: tuple[CadMeasurementViewpoint, ...],
    screen_state: CadScreenStateSnapshot,
    projector_state_requirement: CadProjectorOpticalState | None = None,
    stabilization_required: bool = False,
    evaluation_profile_ref: AuthorityRef | None = None,
    criterion_formula: str | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadSpatialMeasurementPlan:
    """Seal a spatial sampling plan."""
    payload = dict(
        document_id=document_id,
        layout=layout,
        points=points,
        quantities=quantities,
        viewpoints=viewpoints,
        screen_state=screen_state,
        projector_state_requirement=projector_state_requirement,
        stabilization_required=stabilization_required,
        evaluation_profile_ref=evaluation_profile_ref,
        criterion_formula=criterion_formula,
        authority_version=SPATIAL_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadSpatialMeasurementPlan, payload,
        'plan_id', 'plan_sha256', 'spplan',
    )


def build_spatial_measurement_set(
    *,
    document_id: str,
    plan: CadSpatialMeasurementPlan | AuthorityRef,
    observations: tuple[CadSpatialObservation, ...],
    evidence_kind: SpatialEvidenceKind = 'field_measured',
    stimulus_profile: StimulusProfile = 'unknown',
    projector_state: CadProjectorOpticalState | None = None,
    screen_state: CadScreenStateSnapshot | None = None,
    room_state: CadRoomLightState | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadSpatialMeasurementSet:
    """Seal the canonical spatial measurement set."""
    plan_ref = (
        plan_binding(plan) if isinstance(plan, CadSpatialMeasurementPlan)
        else plan
    )
    payload = dict(
        document_id=document_id,
        plan_ref=plan_ref,
        evidence_kind=evidence_kind,
        stimulus_profile=stimulus_profile,
        projector_state=projector_state,
        screen_state=screen_state,
        room_state=room_state,
        observations=observations,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadSpatialMeasurementSet, payload,
        'set_id', 'set_sha256', 'spset',
    )


def build_spatial_derived_map(
    *,
    document_id: str,
    source_set: CadSpatialMeasurementSet | AuthorityRef,
    quantity: SpatialQuantity,
    interpolation_algorithm: str,
    algorithm_version: str,
    grid_resolution: str,
    viewpoint_id: str | None = None,
    extrapolation: bool = False,
    smoothing: str | None = None,
    uncertainty: float | None = None,
    rendered_artifact_ref: AuthorityRef | None = None,
    declared_at_utc: str | None = None,
) -> CadSpatialDerivedMap:
    """Seal a derived spatial map — never canonical evidence."""
    set_ref = (
        set_binding(source_set)
        if isinstance(source_set, CadSpatialMeasurementSet)
        else source_set
    )
    payload = dict(
        document_id=document_id,
        source_set_ref=set_ref,
        quantity=quantity,
        viewpoint_id=viewpoint_id,
        interpolation_algorithm=interpolation_algorithm,
        algorithm_version=algorithm_version,
        grid_resolution=grid_resolution,
        extrapolation=extrapolation,
        smoothing=smoothing,
        uncertainty=uncertainty,
        rendered_artifact_ref=rendered_artifact_ref,
        declared_at_utc=declared_at_utc or _utc_now(),
    )
    return _seal_model(
        CadSpatialDerivedMap, payload,
        'map_id', 'map_sha256', 'spmap',
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _observation_scalar(observation: CadSpatialObservation) -> float | None:
    if observation.quantity == 'white_chromaticity':
        return None
    return observation.value


def _chromaticity_distance(
    left: CadSpatialObservation, right: CadSpatialObservation
) -> float:
    dx = (left.chromaticity_x or 0.0) - (right.chromaticity_x or 0.0)
    dy = (left.chromaticity_y or 0.0) - (right.chromaticity_y or 0.0)
    return (dx * dx + dy * dy) ** 0.5


def _metric_value(
    criterion: CadSpatialCriterion,
    observations: list[CadSpatialObservation],
    role_by_point: dict[str, MeasurementPointRole],
) -> float | None:
    """Compute the criterion metric over one viewpoint's observations."""
    if criterion.metric == 'max_xy_distance':
        best: float | None = None
        for index, obs in enumerate(observations):
            for other in observations[index + 1:]:
                distance = _chromaticity_distance(obs, other)
                if best is None or distance > best:
                    best = distance
        return best
    values = [
        _observation_scalar(obs)
        for obs in observations
        if _observation_scalar(obs) is not None
    ]
    if not values:
        return None
    if criterion.metric == 'min_over_max_ratio':
        maximum = max(values)
        if maximum <= 0:
            return None
        return min(values) / maximum
    if criterion.metric == 'center_deviation_pct':
        center = next(
            (
                _observation_scalar(obs)
                for obs in observations
                if role_by_point.get(obs.point_id) == 'center'
                and _observation_scalar(obs) is not None
            ),
            None,
        )
        if center is None or center == 0:
            return None
        return max(abs(v - center) for v in values) / abs(center) * 100.0
    return None


def _passes(operator: str, value: float, limit: float) -> bool:
    return value >= limit if operator == '>=' else value <= limit


def evaluate_spatial_uniformity(
    *,
    document_id: str,
    plan: CadSpatialMeasurementPlan,
    measurement_set: CadSpatialMeasurementSet,
    criteria: Mapping[SpatialQuantity, CadSpatialCriterion] | None = None,
    evaluated_at_utc: str | None = None,
) -> CadImageUniformityEvaluation:
    """Fail-closed spatial-uniformity verdict for one measurement set.

    A quantity is only ``within_profile``/``outside_profile`` when the
    plan's spatial domain was actually sampled for that quantity *and* a
    bound criterion exists. Missing coverage is reported, never filled
    in; unbound criteria yield ``criterion_unbound``, never an invented
    threshold.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    criteria = dict(criteria or {})
    reasons: list[str] = []

    if measurement_set.plan_ref.ref_id != plan.plan_id or (
        measurement_set.plan_ref.ref_sha256 != plan.plan_sha256
    ):
        raise ValueError(
            'measurement set does not bind this exact plan — '
            'evaluating against a different spatial domain would be a '
            'silent scope change'
        )

    role_by_point = {p.point_id: p.role for p in plan.points}
    valid_points = set(role_by_point)
    valid_viewpoints = {v.viewpoint_id for v in plan.viewpoints}
    unbound = [
        obs for obs in measurement_set.observations
        if obs.point_id not in valid_points
        or obs.viewpoint_id not in valid_viewpoints
    ]
    if unbound:
        reasons.append(
            f'{len(unbound)} observation(s) reference points/viewpoints '
            'outside the declared plan — excluded from evaluation'
        )

    stabilization_blocked = False
    if plan.stabilization_required:
        state = measurement_set.projector_state
        if state is None or state.stabilization_state != 'stabilized':
            stabilization_blocked = True
            reasons.append(
                'plan requires thermal stabilization but the set does not '
                'bind a stabilized projector state — an unstabilized '
                'baseline cannot carry a profile-grade spatial verdict'
            )

    # Coverage over the declared spatial domain (per quantity).
    observations_by_key: dict[tuple[str, str], list[CadSpatialObservation]] = {}
    for obs in measurement_set.observations:
        if obs in unbound:
            continue
        observations_by_key.setdefault(
            (obs.quantity, obs.viewpoint_id), []
        ).append(obs)

    any_coverage = any(
        len(v) == len(plan.points) for v in observations_by_key.values()
    )
    if plan.layout == 'center_only' or (
        role_by_point and set(role_by_point.values()) == {'center'}
    ):
        coverage_state = 'center_only'
    elif not measurement_set.observations:
        coverage_state = 'empty'
    elif any_coverage:
        coverage_state = 'full_spatial_coverage'
    else:
        coverage_state = 'partial_spatial_coverage'

    verdicts: list[CadSpatialQuantityVerdict] = []
    for quantity in plan.quantities:
        criterion = criteria.get(quantity)
        per_viewpoint: list[tuple[str, QuantityState]] = []
        quantity_reasons: list[str] = []
        all_values: list[float] = []
        chromaticity_max: float | None = None
        observed_total = 0
        missing_roles: set[MeasurementPointRole] = set()
        center_value = min_value = max_value = None
        spread: float | None = None
        spread_metric: str | None = None

        for viewpoint in plan.viewpoints:
            obs_list = [
                obs for obs in measurement_set.observations
                if obs.quantity == quantity
                and obs.viewpoint_id == viewpoint.viewpoint_id
                and obs.point_id in valid_points
            ]
            observed_total += len(obs_list)
            covered_points = {obs.point_id for obs in obs_list}
            missing_roles.update(
                point.role for point in plan.points
                if point.point_id not in covered_points
            )
            if not obs_list:
                per_viewpoint.append(
                    (viewpoint.viewpoint_id, 'insufficient_evidence')
                )
                continue
            if len(obs_list) < len(plan.points):
                per_viewpoint.append(
                    (viewpoint.viewpoint_id, 'insufficient_coverage')
                )
            elif stabilization_blocked:
                per_viewpoint.append(
                    (viewpoint.viewpoint_id, 'insufficient_evidence')
                )
            elif criterion is None:
                per_viewpoint.append(
                    (viewpoint.viewpoint_id, 'criterion_unbound')
                )
            else:
                metric_value = _metric_value(
                    criterion, obs_list, role_by_point
                )
                if metric_value is None:
                    per_viewpoint.append(
                        (viewpoint.viewpoint_id, 'not_evaluable')
                    )
                else:
                    per_viewpoint.append(
                        (
                            viewpoint.viewpoint_id,
                            'within_profile'
                            if _passes(
                                criterion.operator,
                                metric_value,
                                criterion.limit,
                            )
                            else 'outside_profile',
                        )
                    )

            for obs in obs_list:
                scalar = _observation_scalar(obs)
                if scalar is not None:
                    all_values.append(scalar)
            if quantity == 'white_chromaticity':
                for index, obs in enumerate(obs_list):
                    for other in obs_list[index + 1:]:
                        d = _chromaticity_distance(obs, other)
                        if chromaticity_max is None or d > chromaticity_max:
                            chromaticity_max = d

        if all_values:
            center_obs = next(
                (
                    o for o in measurement_set.observations
                    if o.quantity == quantity
                    and o.point_id in valid_points
                    and role_by_point.get(o.point_id) == 'center'
                    and _observation_scalar(o) is not None
                ),
                None,
            )
            center_value = (
                _observation_scalar(center_obs)
                if center_obs is not None else None
            )
            min_value = min(all_values)
            max_value = max(all_values)
            if max_value > 0:
                spread = min_value / max_value
                spread_metric = 'min_over_max_ratio'
        if chromaticity_max is not None:
            spread = chromaticity_max
            spread_metric = 'max_xy_distance'

        state_order = {
            'outside_profile': 0,
            'insufficient_evidence': 1,
            'insufficient_coverage': 2,
            'not_evaluable': 3,
            'criterion_unbound': 4,
            'within_profile': 5,
        }
        aggregate = min(
            (state for _, state in per_viewpoint),
            key=lambda s: state_order[s],
            default='insufficient_evidence',
        )
        if stabilization_blocked and aggregate == 'within_profile':
            aggregate = 'insufficient_evidence'
        verdicts.append(
            CadSpatialQuantityVerdict(
                quantity=quantity,
                state=aggregate,
                per_viewpoint=tuple(per_viewpoint),
                expected_points=len(plan.points) * len(plan.viewpoints),
                observed_points=observed_total,
                missing_roles=tuple(sorted(missing_roles)),
                center_value=center_value,
                min_value=min_value,
                max_value=max_value,
                spread=spread,
                spread_metric=spread_metric,
                reasons=tuple(quantity_reasons),
            )
        )

    if coverage_state == 'center_only':
        reasons.append(
            'center-only sampling cannot represent the projected surface — '
            'spatial uniformity claims require edge/corner evidence'
        )
    if measurement_set.evidence_kind == 'predicted':
        reasons.append(
            'predicted spatial evidence: design-time estimate, not '
            'installed-system qualification'
        )

    payload = dict(
        document_id=document_id,
        plan_ref=plan_binding(plan),
        set_ref=set_binding(measurement_set),
        evidence_kind=measurement_set.evidence_kind,
        coverage_state=coverage_state,
        quantity_verdicts=tuple(verdicts),
        unbound_observations=len(unbound),
        reasons=tuple(reasons),
        evaluation_version=SPATIAL_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadImageUniformityEvaluation, payload,
        'evaluation_id', 'evaluation_sha256', 'speval',
    )


__all__ = [
    'SPATIAL_AUTHORITY_SCHEMA_VERSION',
    'SPATIAL_EVALUATION_VERSION',
    'CadImageUniformityEvaluation',
    'CadMeasurementViewpoint',
    'CadProjectorOpticalState',
    'CadRoomLightState',
    'CadScreenStateSnapshot',
    'CadSpatialCriterion',
    'CadSpatialDerivedMap',
    'CadSpatialMeasurementPlan',
    'CadSpatialMeasurementSet',
    'CadSpatialObservation',
    'CadSpatialQuantityVerdict',
    'CadSpatialSamplePoint',
    'MeasurementPointRole',
    'QuantityState',
    'SpatialEvidenceKind',
    'SpatialGridLayout',
    'SpatialQuantity',
    'StimulusProfile',
    'ansi_9_point_grid',
    'build_spatial_derived_map',
    'build_spatial_measurement_plan',
    'build_spatial_measurement_set',
    'evaluate_spatial_uniformity',
    'evaluation_binding',
    'map_binding',
    'plan_binding',
    'set_binding',
]
