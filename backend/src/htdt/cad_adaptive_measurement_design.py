"""Adaptive acoustic measurement design authority (#970).

The deterministic channel x target x repeat ``MeasurementPlan`` (#529) runs
a predeclared fixed matrix. This module adds the **experiment-design**
layer: given a declared candidate domain, eligible existing evidence, an
inference model identity and an acquisition function, propose the *next
most informative* physical microphone positions.

A proposal is never a measurement and never silently edits a preregistered
plan — ``AdaptiveMeasurementProposal`` is a sealed artifact a user (or a
campaign step) must explicitly accept into a plan. The first bounded
algorithm is a deterministic non-adaptive optimal-subset / sequential
max-min picker over the candidate domain; richer acquisition functions
retain their own versioned identity.
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite, sqrt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_scene import Position3


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: Any) -> str:
    return sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


_SHA256_PATTERN = r'^[0-9a-f]{64}$'


AdaptiveDesignPurpose = Literal[
    'spatial_field_reconstruction',
    'modal_identification',
    'model_calibration',
    'validation_coverage',
    'seat_neighborhood_characterization',
    'unknown',
]

AcquisitionFunctionKind = Literal[
    'maxmin_spacing',
    'uncertainty_max',
    'loo_error_max',
    'expected_information_gain',
    'random_baseline',
    'producer',
    'unknown',
]

AdaptiveStopCriterion = Literal[
    'measurement_budget',
    'uncertainty_threshold',
    'error_threshold',
    'spacing_exhausted',
    'manual',
    'unknown',
]

CandidateGenerationRule = Literal[
    'explicit_list',
    'regular_grid',
    'random_seeded',
    'imported',
    'unknown',
]

ProposalState = Literal['proposed', 'accepted', 'rejected', 'superseded']


class CandidateSpatialDomain(BaseModel):
    """Explicit physical domain candidates may be drawn from.

    Positions are constrained by geometry and workflow: an axis-aligned
    bounding region plus optional explicit candidate list, exclusion boxes
    (furniture/walls/user zones) and a minimum spacing rule. The optimizer
    can never propose a free-space point outside this declared domain.
    """

    model_config = ConfigDict(frozen=True)

    bounds_min: Position3
    bounds_max: Position3
    candidates: tuple[Position3, ...] | None = None
    generation_rule: CandidateGenerationRule = 'unknown'
    exclusion_min: tuple[Position3, ...] = ()
    exclusion_max: tuple[Position3, ...] = ()
    min_spacing_m: float = Field(default=0.0, ge=0.0)
    mic_height_min_m: float | None = None
    mic_height_max_m: float | None = None

    @model_validator(mode='after')
    def valid_domain(self) -> 'CandidateSpatialDomain':
        for axis in ('x_m', 'y_m', 'z_m'):
            low = getattr(self.bounds_min, axis)
            high = getattr(self.bounds_max, axis)
            if not (isfinite(low) and isfinite(high)) or high <= low:
                raise ValueError('domain bounds must satisfy min < max per axis')
        if len(self.exclusion_min) != len(self.exclusion_max):
            raise ValueError('exclusion_min and exclusion_max must pair up')
        for low_p, high_p in zip(self.exclusion_min, self.exclusion_max):
            for axis in ('x_m', 'y_m', 'z_m'):
                if getattr(high_p, axis) <= getattr(low_p, axis):
                    raise ValueError('exclusion bounds must satisfy min < max')
        if (
            self.mic_height_min_m is not None
            and self.mic_height_max_m is not None
            and self.mic_height_max_m <= self.mic_height_min_m
        ):
            raise ValueError('mic height range is invalid')
        if self.generation_rule == 'explicit_list' and not self.candidates:
            raise ValueError('explicit_list rule requires candidates')
        if self.candidates is not None:
            for point in self.candidates:
                self._require_inside(point)
        return self

    def _require_inside(self, point: Position3) -> None:
        if not self.contains(point):
            raise ValueError('declared candidate lies outside domain bounds')

    def contains(self, point: Position3) -> bool:
        """Whether ``point`` is inside the eligible domain."""
        inside = (
            self.bounds_min.x_m <= point.x_m <= self.bounds_max.x_m
            and self.bounds_min.y_m <= point.y_m <= self.bounds_max.y_m
            and self.bounds_min.z_m <= point.z_m <= self.bounds_max.z_m
        )
        if not inside:
            return False
        if self.mic_height_min_m is not None and point.z_m < self.mic_height_min_m:
            return False
        if self.mic_height_max_m is not None and point.z_m > self.mic_height_max_m:
            return False
        return True

    def excluded(self, point: Position3) -> bool:
        """Whether ``point`` falls inside a declared exclusion box."""
        for low_p, high_p in zip(self.exclusion_min, self.exclusion_max):
            if (
                low_p.x_m <= point.x_m <= high_p.x_m
                and low_p.y_m <= point.y_m <= high_p.y_m
                and low_p.z_m <= point.z_m <= high_p.z_m
            ):
                return True
        return False

    def eligible(self, point: Position3, taken: tuple[Position3, ...] = ()) -> bool:
        """Full candidate-domain authority: inside, unexcluded, spaced."""
        if not self.contains(point) or self.excluded(point):
            return False
        for existing in taken:
            if _distance(point, existing) < self.min_spacing_m - 1e-9:
                return False
        return True


def _distance(a: Position3, b: Position3) -> float:
    return sqrt(
        (a.x_m - b.x_m) ** 2 + (a.y_m - b.y_m) ** 2 + (a.z_m - b.z_m) ** 2
    )


class AdaptiveMeasurementDesignSpec(BaseModel):
    """Immutable experiment-design spec a proposal replays from."""

    model_config = ConfigDict(frozen=True)

    spec_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=_SHA256_PATTERN)
    system_variant_id: str | None = None
    purpose: AdaptiveDesignPurpose = 'unknown'
    target_observable: str | None = None
    target_band_hz: tuple[float, float] | None = None
    source_scenario_ref: str | None = None
    eligible_measurement_ids: tuple[str, ...] = ()
    domain: CandidateSpatialDomain
    microphone_orientation_policy: str | None = None
    inference_model_id: str | None = None
    inference_model_version: str | None = None
    prior_assumptions_json: str = '{}'
    noise_model_json: str = '{}'
    acquisition_function: AcquisitionFunctionKind = 'unknown'
    exploration_policy: str | None = None
    measurement_budget: int | None = Field(default=None, ge=1)
    stop_criterion: AdaptiveStopCriterion = 'unknown'
    deterministic_seed: int | None = None
    created_at_utc: str = Field(min_length=1)
    spec_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_spec(self) -> 'AdaptiveMeasurementDesignSpec':
        if self.purpose == 'unknown':
            raise ValueError('adaptive design requires an explicit purpose')
        if self.target_band_hz is not None:
            low, high = self.target_band_hz
            if not (isfinite(low) and isfinite(high)) or not (0 < low < high):
                raise ValueError('target_band_hz must satisfy 0 < low < high')
        if len(self.eligible_measurement_ids) != len(set(self.eligible_measurement_ids)):
            raise ValueError('eligible_measurement_ids must be unique')
        if self.acquisition_function == 'unknown':
            raise ValueError('adaptive design requires an explicit acquisition function')
        if self.spec_sha256 != _hash(self.identity_payload()):
            raise ValueError('adaptive design spec hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'spec_sha256'})


class ProposedMeasurementPoint(BaseModel):
    """One proposed sampling coordinate — proposal, never evidence."""

    model_config = ConfigDict(frozen=True)

    point_id: str = Field(min_length=1)
    position: Position3
    score: float | None = None
    score_semantics: str | None = None
    rationale: str | None = None

    @model_validator(mode='after')
    def valid_point(self) -> 'ProposedMeasurementPoint':
        if self.score is not None and not isfinite(float(self.score)):
            raise ValueError('score must be finite')
        return self


class AdaptiveMeasurementProposal(BaseModel):
    """Sealed set of proposed next positions for one exact spec.

    ``state`` records proposal lifecycle; a proposal is never a
    measurement and never mutates a persisted ``MeasurementPlan`` — an
    explicit accept step projects it into plan cells (#529).
    """

    model_config = ConfigDict(frozen=True)

    proposal_id: str = Field(min_length=1)
    spec_id: str = Field(min_length=1)
    spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    algorithm_version: str = Field(min_length=1)
    iteration: int = Field(ge=0)
    existing_positions: tuple[Position3, ...] = ()
    proposed_points: tuple[ProposedMeasurementPoint, ...] = ()
    state: ProposalState = 'proposed'
    stop_reached: bool = False
    created_at_utc: str = Field(min_length=1)
    proposal_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_proposal(self) -> 'AdaptiveMeasurementProposal':
        if not self.proposed_points and not self.stop_reached:
            raise ValueError(
                'a proposal must carry proposed points or declare stop_reached'
            )
        ids = [point.point_id for point in self.proposed_points]
        if len(ids) != len(set(ids)):
            raise ValueError('proposed point ids must be unique')
        if self.proposal_sha256 != _hash(self.identity_payload()):
            raise ValueError('adaptive proposal hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'proposal_sha256'})


ADAPTIVE_MAXMIN_ALGORITHM_VERSION = 'htdt_adaptive_maxmin_v1'


def build_adaptive_design_spec(**kwargs: Any) -> AdaptiveMeasurementDesignSpec:
    """Assemble and seal an :class:`AdaptiveMeasurementDesignSpec`."""
    payload = {'spec_sha256': '0' * 64, **kwargs}
    provisional = AdaptiveMeasurementDesignSpec.model_construct(**payload)
    payload['spec_sha256'] = _hash(provisional.identity_payload())
    return AdaptiveMeasurementDesignSpec(**payload)


def propose_maxmin_positions(
    spec: AdaptiveMeasurementDesignSpec,
    *,
    proposal_id: str,
    existing_positions: tuple[Position3, ...],
    count: int,
    iteration: int,
    created_at_utc: str,
) -> AdaptiveMeasurementProposal:
    """Deterministic max-min (farthest-point) next-position proposal.

    The first bounded algorithm: over the spec's declared candidate set it
    greedily picks the eligible candidate whose minimum distance to all
    already-taken positions is largest — a deterministic spacing-optimal
    subset with a fixed tie-break (lowest x, then y, then z). It fabricates
    no measurement and edits no plan; callers accept a proposal into a
    ``MeasurementPlan`` explicitly.
    """
    domain = spec.domain
    pool = domain.candidates
    if pool is None:
        raise ValueError(
            'maxmin proposal requires an explicit candidate set; other '
            'generation rules need their own versioned algorithm'
        )
    if count < 1:
        raise ValueError('count must be positive')
    taken = tuple(existing_positions)
    scored: list[tuple[Position3, float]] = []
    for candidate in pool:
        if not domain.eligible(candidate, taken):
            continue
        if taken:
            score = min(_distance(candidate, other) for other in taken)
        else:
            score = 0.0
        scored.append((candidate, score))
    scored.sort(
        key=lambda item: (
            -item[1], item[0].x_m, item[0].y_m, item[0].z_m
        )
    )
    chosen = scored[:count]
    points = tuple(
        ProposedMeasurementPoint(
            point_id=f'{proposal_id}-p{index + 1}',
            position=position,
            score=score,
            score_semantics='min_distance_to_existing_m',
            rationale='maxmin_spacing',
        )
        for index, (position, score) in enumerate(chosen)
    )
    return _seal_proposal(
        AdaptiveMeasurementProposal,
        proposal_id=proposal_id,
        spec_id=spec.spec_id,
        spec_sha256=spec.spec_sha256,
        document_id=spec.document_id,
        algorithm_version=ADAPTIVE_MAXMIN_ALGORITHM_VERSION,
        iteration=iteration,
        existing_positions=existing_positions,
        proposed_points=points,
        state='proposed',
        stop_reached=not chosen
        or len(chosen) < count
        or (
            spec.measurement_budget is not None
            and len(taken) + len(chosen) >= spec.measurement_budget
        ),
        created_at_utc=created_at_utc,
    )


def _seal_proposal(
    model: type[AdaptiveMeasurementProposal], **payload: Any
) -> AdaptiveMeasurementProposal:
    provisional = model.model_construct(proposal_sha256='0' * 64, **payload)
    return model(
        proposal_sha256=_hash(provisional.identity_payload()), **payload
    )
