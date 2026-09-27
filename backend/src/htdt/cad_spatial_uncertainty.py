"""Spatial observation uncertainty authority (#1023).

Keeps *metric* observation/transform uncertainty as first-class evidence,
separate from RoomPlan-style classification confidence (which says how sure
the scanner is that a surface is a wall — never how accurate the geometry
is). No Gaussian distributions are invented: every uncertainty statement
records its representation kind and provenance, and UNKNOWN stays UNKNOWN.

- :class:`SpatialObservationUncertainty` — uncertainty bound on one exact
  observation/transform evidence ref (positional, angular, dimensional,
  optional covariance when actually supplied).
- :class:`ObservedDimension` — a scalar dimension reading that keeps
  instrument resolution, manufacturer accuracy and declared uncertainty as
  separate quantities.
- :class:`ObservedPoint` — one point observation with a bound
  representation in an exact coordinate frame.
- :func:`evaluate_dimension_decision` — propagate uncertainty into a
  geometry decision: an installation/conformance claim may only PASS when
  the declared tolerance exceeds the observation uncertainty; UNKNOWN when
  the uncertainty is unknown or exceeds the tolerance.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_video_geometry import EvaluationStatus
from .canonical_json import canonical_sha256 as _hash




UncertaintyRepresentationKind = Literal[
    'bounded_interval',
    'per_axis_bound',
    'covariance',
    'empirical_residual_distribution',
    'repeated_observation_spread',
    'manufacturer_accuracy_statement',
    'survey_control_residual',
    'unknown',
]
"""How the uncertainty is expressed. ``unknown`` is honest absence — the
authority still records that the observation exists, but no bound is
claimed."""

UncertaintyDomain = Literal[
    'position',
    'orientation',
    'dimension',
    'registration_transform',
    'surface_plane_fit',
    'other',
]

ComponentKind = Literal['systematic', 'random', 'combined', 'unknown']


class UncertaintyBound(BaseModel):
    """One numeric bound under a stated representation kind.

    All quantities are optional: a bound may only state an isotropic
    radius, or only a covariance. What is not recorded is not assumed.
    """

    model_config = ConfigDict(frozen=True)

    representation: UncertaintyRepresentationKind
    radial_bound_m: float | None = Field(default=None, ge=0.0)
    axis_bounds_m: tuple[float, float, float] | None = None
    dimensional_bound_m: float | None = Field(default=None, ge=0.0)
    angular_bound_deg: float | None = Field(default=None, ge=0.0)
    covariance_m2: tuple[float, ...] | None = None
    component: ComponentKind = 'unknown'

    @model_validator(mode='after')
    def _check(self) -> 'UncertaintyBound':
        if self.representation != 'unknown':
            declared = (
                self.radial_bound_m is not None
                or self.axis_bounds_m is not None
                or self.dimensional_bound_m is not None
                or self.angular_bound_deg is not None
                or self.covariance_m2 is not None
            )
            if not declared:
                raise ValueError(
                    'a non-unknown uncertainty representation must carry '
                    'at least one recorded quantity'
                )
        if self.covariance_m2 is not None and len(self.covariance_m2) not in (
            6,
            9,
        ):
            raise ValueError(
                'covariance_m2 must be a flat 3x3 (9) or upper-triangle (6) '
                'matrix as actually supplied'
            )
        return self

    def worst_case_linear_bound_m(self) -> float | None:
        """A conservative linear bound for comparisons, or None if the
        representation carries no usable linear quantity."""
        if self.radial_bound_m is not None:
            return self.radial_bound_m
        if self.dimensional_bound_m is not None:
            return self.dimensional_bound_m
        if self.axis_bounds_m is not None:
            return max(self.axis_bounds_m)
        return None


class SpatialObservationUncertainty(BaseModel):
    """Uncertainty authority bound to one exact observation/transform ref."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['spatial-observation-uncertainty-1'] = (
        'spatial-observation-uncertainty-1'
    )
    uncertainty_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    observation_ref_kind: str = Field(min_length=1)
    observation_ref_id: str = Field(min_length=1)
    domain: UncertaintyDomain
    coordinate_frame_id: str | None = None
    bound: UncertaintyBound
    classification_confidence: Literal['high', 'medium', 'low'] | None = None
    source_method: str | None = None
    applicability: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    uncertainty_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'uncertainty_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'SpatialObservationUncertainty':
        if self.uncertainty_sha256 != _hash(self.semantic_payload()):
            raise ValueError('spatial uncertainty semantic hash mismatch')
        return self


def build_spatial_observation_uncertainty(
    *,
    uncertainty_id: str,
    version: str,
    observation_ref_kind: str,
    observation_ref_id: str,
    domain: UncertaintyDomain,
    bound: UncertaintyBound,
    coordinate_frame_id: str | None = None,
    classification_confidence: Literal['high', 'medium', 'low'] | None = None,
    source_method: str | None = None,
    applicability: str | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> SpatialObservationUncertainty:
    probe = SpatialObservationUncertainty.model_construct(
        uncertainty_id=uncertainty_id,
        version=version,
        observation_ref_kind=observation_ref_kind,
        observation_ref_id=observation_ref_id,
        domain=domain,
        coordinate_frame_id=coordinate_frame_id,
        bound=bound,
        classification_confidence=classification_confidence,
        source_method=source_method,
        applicability=applicability,
        provenance=tuple(provenance),
        uncertainty_sha256='',
    )
    return SpatialObservationUncertainty(
        **probe.model_dump(mode='python', exclude={'uncertainty_sha256'}),
        uncertainty_sha256=_hash(probe.semantic_payload()),
    )


class ObservedDimension(BaseModel):
    """A scalar dimension observation.

    ``instrument_resolution_m`` (what the display shows), the
    manufacturer's accuracy statement and the operator's endpoint placement
    error are separate quantities — a 1 mm display resolution never proves
    ±1 mm installed accuracy.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    value_m: float = Field(gt=0.0)
    instrument: str | None = None
    instrument_resolution_m: float | None = Field(default=None, gt=0.0)
    declared_uncertainty_m: float | None = Field(default=None, ge=0.0)
    endpoint_semantics: str | None = None
    observed_at_utc: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()


class ObservedPoint(BaseModel):
    """One observed point with an explicit bound in an exact frame."""

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    coordinate_frame_id: str = Field(min_length=1)
    position_m: tuple[float, float, float]
    bound: UncertaintyBound
    observed_at_utc: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()


class GeometryDecisionResult(BaseModel):
    """Whether a tolerance decision is supported by the uncertainty."""

    model_config = ConfigDict(frozen=True)

    decision: str = Field(min_length=1)
    status: EvaluationStatus
    reason: str


def evaluate_dimension_decision(
    *,
    decision: str,
    allowed_tolerance_m: float,
    uncertainty: SpatialObservationUncertainty | None,
) -> GeometryDecisionResult:
    """Propagate observation uncertainty into a geometry decision.

    A claimed installation/conformance tolerance may only PASS when the
    uncertainty's worst-case linear bound is smaller than the allowed
    tolerance — never declare a 3 mm installation error when observation
    uncertainty is materially larger.
    """
    if allowed_tolerance_m <= 0.0:
        raise ValueError('allowed tolerance must be positive')
    if uncertainty is None:
        return GeometryDecisionResult(
            decision=decision,
            status='UNKNOWN',
            reason='no uncertainty authority bound to the observation',
        )
    bound = uncertainty.bound.worst_case_linear_bound_m()
    if bound is None:
        return GeometryDecisionResult(
            decision=decision,
            status='UNKNOWN',
            reason='uncertainty representation carries no linear bound '
            '(classification confidence is not metric accuracy)',
        )
    if bound >= allowed_tolerance_m:
        return GeometryDecisionResult(
            decision=decision,
            status='FAIL',
            reason=f'observation uncertainty {bound} m meets or exceeds '
            f'allowed tolerance {allowed_tolerance_m} m',
        )
    return GeometryDecisionResult(
        decision=decision,
        status='PASS',
        reason=f'observation uncertainty {bound} m is inside allowed '
        f'tolerance {allowed_tolerance_m} m',
    )
