"""REV56-CAMPPROFILE (#581): spatial measurement campaign design authority.

A ``SpatialCampaignDesign`` is a self-sealed, first-class immutable authority
that declares *where and why* measurements are sampled before capture:
the listening area being sampled, each point's role (reference /
optimization / spatial holdout / repeatability / diagnostic / boundary /
standards-required), source-channel coverage, declared weights, capture
order, and the preplanned-vs-adaptive provenance of every point. A campaign
may not make a ``multi_position_area`` / ``seat_to_seat_consistency`` /
``whole_listening_area`` claim without defensible spatial sampling.

Literature basis
----------------
- Dirac Live measurement guidance (helpdesk.dirac.com, primary): the first
  measurement is the main/sweet-spot position used for gains and delays;
  measurement points should be at least ~30 cm (12 in) apart; too small a
  measured region causes over-compensation; points are distributed across
  the intended region including different heights, with some points
  deliberately a little outside the listening boundary. The 30 cm value is
  a vendor *workflow* recommendation, not a universal physics threshold —
  this module therefore carries spacing expectations as declared plan
  policy fields, never as hidden constants.
- Trinnov Optimizer guidance (kb.trinnov.com, primary): the Reference
  Seating Position anchors localization/alignment; secondary points should
  represent the listening area as a whole and add *acoustic diversity*,
  not repeated copies of the same information; multipoint optimization is
  joint analysis of all positions, not a simple average — averaging hides
  location-specific problems.
- Welti & Devantier (JAES 54(5) 2006, pp. 347-364; Devantier & Welti AES
  115 conv. paper 5942, 2003; Welti AES 133 conv. paper 8748, 2012):
  low-frequency response varies strongly from seat to seat, so correction
  has to be designed and verified over the seating *area* — a defensible
  sampling plan must span the intended domain and report per-point
  outcomes.
- Holdout independence: REV55 ``assert_partition_disjoint`` (#564)
  mechanically forbids the same measurement serving as both calibration
  and holdout evidence; this module binds that discipline to declared
  point roles (``assert_campaign_partition_disjoint``) so a filter-design
  point can never also stand as independent holdout evidence.

Design rules encoded
--------------------
- The intended listening area is an explicit authority (zones / bounds /
  RSP / head-height range) — it is never inferred from the convex hull of
  whatever points happened to be measured.
- ``optimization`` and ``spatial_holdout`` roles are mutually exclusive on
  a point, and an ``adaptive`` point may never carry ``spatial_holdout``
  (adaptive development points can never be relabelled as locked
  validation evidence).
- Repeatability captures do not inflate spatial coverage: points carrying
  only ``repeatability``/``diagnostic`` roles never count toward the
  spatial sample count.
- Sample density must not act as hidden weighting: coverage metrics expose
  the density-implied share of each zone next to its declared weight, and
  the design verdict warns when they diverge past the declared alert ratio.
- Acoustic-diversity analysis is a post-hoc diagnostic only — it can flag
  redundancy but never rewrites holdout membership after results are seen.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_scene import Position3, Quaternion4
from .canonical_json import canonical_sha256 as _digest


SPATIAL_CAMPAIGN_SCHEMA_VERSION = 1
SPATIAL_CAMPAIGN_AUTHORITY_VERSION = 'rev56-spatial-campaign-1'
CAMPAIGN_EVALUATION_ALGORITHM_VERSION = 'rev56-spatial-campaign-eval-1'
CAMPAIGN_BINDING_AUTHORITY_VERSION = 'rev56-spatial-campaign-binding-1'
DIVERSITY_ALGORITHM_VERSION = 'rev56-acoustic-diversity-1'
TEMPLATE_INSTANTIATION_VERSION = 'rev56-campaign-template-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'

#: Two different planned positions closer than this are the *same spot*
#: (near-duplicate identity guard), not a physics threshold. Declared
#: clustering/spacing expectations live on ``SamplingExpectation``.
DEFAULT_DUPLICATE_TOLERANCE_M = 0.01

MeasurementPointRole = Literal[
    'reference_alignment',
    'optimization',
    'spatial_holdout',
    'repeatability',
    'diagnostic',
    'boundary_stress',
    'standards_required',
]

PointSelectionProvenance = Literal['preplanned', 'adaptive']

ChannelCoverageKind = Literal[
    'all_channels',
    'selected_channels',
    'reference_source_only',
    'subwoofers_only',
    'bass_management_paths',
    'spatial_array',
    'unknown',
]

CaptureOrderStrategy = Literal[
    'declared_sequence',
    'interleaved_pairs',
    'randomized',
    'unplanned',
]

CampaignClaimKind = Literal[
    'single_position',
    'multi_position_area',
    'seat_to_seat_consistency',
    'whole_listening_area',
]

ListeningZoneKind = Literal[
    'seat',
    'row',
    'priority',
    'excluded',
    'challenge_region',
    'custom',
]

CampaignDesignState = Literal['valid', 'valid_with_warnings', 'invalid']

#: Roles that contribute a distinct spatial sample. ``repeatability`` and
#: ``diagnostic`` never count toward spatial coverage.
SPATIAL_COVERAGE_ROLES = frozenset(
    {
        'reference_alignment',
        'optimization',
        'spatial_holdout',
        'boundary_stress',
        'standards_required',
    }
)

#: Role combinations that are impossible on one point: a point cannot be
#: both a filter-design input and independent holdout evidence, a repeat
#: capture at one XYZ is never independent spatial evidence, and a
#: diagnostic point cannot double as locked holdout.
FORBIDDEN_ROLE_PAIRS: tuple[tuple[str, str], ...] = (
    ('optimization', 'spatial_holdout'),
    ('repeatability', 'spatial_holdout'),
    ('diagnostic', 'spatial_holdout'),
)

#: Channel coverages that must name their channel/source entities.
_CHANNEL_SCOPES_REQUIRING_ENTITIES = frozenset(
    {
        'selected_channels',
        'reference_source_only',
        'subwoofers_only',
        'bass_management_paths',
        'spatial_array',
    }
)


def _require_iso8601(value: str, label: str) -> None:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _finite(value: float, label: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f'{label} must be finite')
    return result


def _unique(values: tuple[str, ...], label: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f'{label} must be unique')


def _distance_m(a: Position3, b: Position3) -> float:
    return math.sqrt(
        (a.x_m - b.x_m) ** 2 + (a.y_m - b.y_m) ** 2 + (a.z_m - b.z_m) ** 2
    )


def _position_key(position: Position3) -> tuple[float, float, float]:
    """Millimetre-rounded identity for 'same spot' comparisons."""

    return (
        round(position.x_m, 3),
        round(position.y_m, 3),
        round(position.z_m, 3),
    )


# --- listening area --------------------------------------------------------


class ListeningZone(BaseModel):
    """One named region of the intended listening domain.

    ``excluded`` zones veto membership even when another zone's bounds
    overlap them; ``challenge_region`` zones are declared stress areas where
    holdout points may legitimately sit outside the primary listening area.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    zone_id: str = Field(min_length=1)
    kind: ListeningZoneKind = 'custom'
    entity_ids: tuple[str, ...] = ()
    bounds_min: Position3
    bounds_max: Position3
    declared_weight: float = Field(default=1.0, ge=0.0)
    note: str | None = Field(default=None, min_length=1)

    @field_validator('entity_ids')
    @classmethod
    def canonical_entities(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        _unique(values, 'zone entity ids')
        if any(not value for value in values):
            raise ValueError('zone entity ids must not be empty')
        return tuple(sorted(values))

    @field_validator('declared_weight')
    @classmethod
    def finite_weight(cls, value: float) -> float:
        return _finite(value, 'zone declared_weight')

    @model_validator(mode='after')
    def valid_zone(self) -> 'ListeningZone':
        for axis in ('x_m', 'y_m', 'z_m'):
            low = getattr(self.bounds_min, axis)
            high = getattr(self.bounds_max, axis)
            if not high > low:
                raise ValueError(
                    f'zone {self.zone_id} bounds must be increasing on {axis}'
                )
        if self.kind == 'excluded' and self.declared_weight > 0.0:
            raise ValueError('excluded zones cannot carry a declared weight')
        if self.kind != 'excluded' and self.declared_weight <= 0.0:
            raise ValueError('non-excluded zones require a positive declared weight')
        return self

    def contains(self, position: Position3) -> bool:
        return (
            self.bounds_min.x_m <= position.x_m <= self.bounds_max.x_m
            and self.bounds_min.y_m <= position.y_m <= self.bounds_max.y_m
            and self.bounds_min.z_m <= position.z_m <= self.bounds_max.z_m
        )


class ListeningAreaSpec(BaseModel):
    """The exact intended domain a campaign samples (issue #581 section 1).

    Coverage is always judged against this declared area — never against the
    hull of measured points.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    area_label: str = Field(min_length=1)
    zones: tuple[ListeningZone, ...] = Field(min_length=1)
    rsp_entity_id: str | None = Field(default=None, min_length=1)
    head_height_range_m: tuple[float, float] | None = None
    intended_listeners: int = Field(default=1, ge=1)
    description: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_area(self) -> 'ListeningAreaSpec':
        zone_ids = [zone.zone_id for zone in self.zones]
        if len(zone_ids) != len(set(zone_ids)):
            raise ValueError('listening area zone ids must be unique')
        if not any(zone.kind != 'excluded' for zone in self.zones):
            raise ValueError('listening area requires a non-excluded zone')
        if self.head_height_range_m is not None:
            low, high = self.head_height_range_m
            if not (
                math.isfinite(low)
                and math.isfinite(high)
                and 0.0 <= low < high
            ):
                raise ValueError('head height range must be a finite increasing pair')
        return self

    def zone(self, zone_id: str) -> ListeningZone | None:
        return next((z for z in self.zones if z.zone_id == zone_id), None)

    def zone_for(self, position: Position3) -> ListeningZone | None:
        """Membership: excluded zones veto, then first containing zone."""

        if any(
            zone.kind == 'excluded' and zone.contains(position)
            for zone in self.zones
        ):
            return None
        for zone in self.zones:
            if zone.kind != 'excluded' and zone.contains(position):
                return zone
        return None

    def is_inside(self, position: Position3) -> bool:
        return self.zone_for(position) is not None

    def non_excluded_zones(self) -> tuple[ListeningZone, ...]:
        return tuple(zone for zone in self.zones if zone.kind != 'excluded')

    def challenge_zones(self) -> tuple[ListeningZone, ...]:
        return tuple(
            zone for zone in self.zones if zone.kind == 'challenge_region'
        )

    def aggregate_bounds(self) -> tuple[Position3, Position3]:
        zones = self.non_excluded_zones()
        mins = Position3(
            x_m=min(zone.bounds_min.x_m for zone in zones),
            y_m=min(zone.bounds_min.y_m for zone in zones),
            z_m=min(zone.bounds_min.z_m for zone in zones),
        )
        maxs = Position3(
            x_m=max(zone.bounds_max.x_m for zone in zones),
            y_m=max(zone.bounds_max.y_m for zone in zones),
            z_m=max(zone.bounds_max.z_m for zone in zones),
        )
        return mins, maxs


# --- points ----------------------------------------------------------------


class CampaignPoint(BaseModel):
    """One declared sampling position (issue #581 sections 2-3, 8, 10-11)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    point_id: str = Field(min_length=1)
    position: Position3
    orientation: Quaternion4 | None = None
    roles: tuple[MeasurementPointRole, ...] = Field(min_length=1)
    weight: float = Field(default=1.0, ge=0.0)
    channel_coverage: ChannelCoverageKind = 'all_channels'
    channel_entity_ids: tuple[str, ...] = ()
    provenance: PointSelectionProvenance = 'preplanned'
    adaptive_algorithm: str | None = Field(default=None, min_length=1)
    adaptive_reason: str | None = Field(default=None, min_length=1)
    zone_id: str | None = Field(default=None, min_length=1)
    note: str | None = Field(default=None, min_length=1)

    @field_validator('channel_entity_ids')
    @classmethod
    def canonical_channels(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        _unique(values, 'channel entity ids')
        if any(not value for value in values):
            raise ValueError('channel entity ids must not be empty')
        return tuple(sorted(values))

    @field_validator('weight')
    @classmethod
    def finite_weight(cls, value: float) -> float:
        return _finite(value, 'point weight')

    @model_validator(mode='after')
    def valid_point(self) -> 'CampaignPoint':
        _unique(self.roles, 'point roles')
        role_set = set(self.roles)
        for a, b in FORBIDDEN_ROLE_PAIRS:
            if a in role_set and b in role_set:
                raise ValueError(
                    f'roles {a!r} and {b!r} are mutually exclusive on one point'
                )
        if self.provenance == 'adaptive':
            if not self.adaptive_algorithm or not self.adaptive_reason:
                raise ValueError(
                    'adaptive points must record the selection algorithm and reason'
                )
            if 'spatial_holdout' in role_set:
                raise ValueError(
                    'an adaptively selected point can never be declared '
                    'spatial holdout evidence'
                )
        else:
            if self.adaptive_algorithm is not None or self.adaptive_reason is not None:
                raise ValueError(
                    'preplanned points cannot carry adaptive provenance fields'
                )
        if self.channel_coverage in _CHANNEL_SCOPES_REQUIRING_ENTITIES:
            if not self.channel_entity_ids:
                raise ValueError(
                    f'{self.channel_coverage} coverage requires channel entity ids'
                )
        elif self.channel_entity_ids:
            raise ValueError(
                f'{self.channel_coverage} coverage cannot name channel entities'
            )
        return self

    def has_role(self, role: MeasurementPointRole) -> bool:
        return role in self.roles

    def contributes_spatial_coverage(self) -> bool:
        return bool(SPATIAL_COVERAGE_ROLES.intersection(self.roles))


class InterleavedPair(BaseModel):
    """A declared A/B pair for paired/interleaved capture order (#581 §12)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    pair_id: str = Field(min_length=1)
    first_point_id: str = Field(min_length=1)
    second_point_id: str = Field(min_length=1)

    @model_validator(mode='after')
    def distinct(self) -> 'InterleavedPair':
        if self.first_point_id == self.second_point_id:
            raise ValueError('an interleaved pair needs two distinct points')
        return self


class CaptureOrderPlan(BaseModel):
    """Declared measurement order / drift-confounding strategy (§12)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    strategy: CaptureOrderStrategy = 'unplanned'
    sequence: tuple[str, ...] = ()
    interleaved_pairs: tuple[InterleavedPair, ...] = ()
    randomization_seed: str | None = Field(default=None, min_length=1)
    note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_plan(self) -> 'CaptureOrderPlan':
        _unique(self.sequence, 'capture sequence point ids')
        if self.strategy == 'declared_sequence' and not self.sequence:
            raise ValueError('declared_sequence order requires the sequence')
        if self.strategy == 'interleaved_pairs' and not self.interleaved_pairs:
            raise ValueError('interleaved_pairs order requires pair records')
        if self.strategy == 'randomized' and self.randomization_seed is None:
            raise ValueError(
                'randomized order requires a recorded randomization seed'
            )
        if self.strategy == 'unplanned' and (
            self.sequence or self.interleaved_pairs or self.randomization_seed
        ):
            raise ValueError('unplanned order cannot carry sequence detail')
        pair_ids = [pair.pair_id for pair in self.interleaved_pairs]
        if len(pair_ids) != len(set(pair_ids)):
            raise ValueError('interleaved pair ids must be unique')
        return self


class SamplingExpectation(BaseModel):
    """Declared coverage policy the design is evaluated against (§4, §9).

    Every bound is plan policy — none of these is a universal physics
    threshold. Values left ``None`` disable the corresponding check rather
    than applying a hidden default.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    min_axis_span_m: float | None = Field(default=None, gt=0.0)
    min_pairwise_spacing_m: float | None = Field(default=None, gt=0.0)
    duplicate_tolerance_m: float = Field(
        default=DEFAULT_DUPLICATE_TOLERANCE_M, gt=0.0
    )
    required_zone_ids: tuple[str, ...] = ()
    min_holdout_count: int = Field(default=0, ge=0)
    density_weight_alert_ratio: float = Field(default=1.5, gt=0.0)

    @field_validator(
        'min_axis_span_m', 'min_pairwise_spacing_m', 'duplicate_tolerance_m'
    )
    @classmethod
    def finite_bound(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, 'sampling expectation bound')

    @field_validator('required_zone_ids')
    @classmethod
    def canonical_required(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        _unique(values, 'required zone ids')
        if any(not value for value in values):
            raise ValueError('required zone ids must not be empty')
        return tuple(sorted(values))

    @field_validator('density_weight_alert_ratio')
    @classmethod
    def finite_ratio(cls, value: float) -> float:
        return _finite(value, 'density weight alert ratio')


class CampaignTemplateRef(BaseModel):
    """Provenance for a compatibility/workflow template (§14).

    Templates are labelled workflow compatibility aids — they never claim
    vendor algorithm equivalence.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    template_id: str = Field(min_length=1)
    template_version: str = Field(min_length=1)
    source_label: str = Field(min_length=1)
    source_uri: str | None = Field(default=None, min_length=1)
    compatibility_note: str = Field(min_length=1)


# --- the sealed design ------------------------------------------------------


class SpatialCampaignDesign(BaseModel):
    """Immutable spatial sampling authority (issue #581 goal).

    ``declared_at_utc`` records when the design was declared — the durable
    "declared before measurement" property comes from repository commit
    order (bindings cannot reference an unpersisted design), not from this
    timestamp alone.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = SPATIAL_CAMPAIGN_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-spatial-campaign-1'
    ] = SPATIAL_CAMPAIGN_AUTHORITY_VERSION

    design_id: str = Field(pattern=r'^spatial-campaign:[0-9a-f]{64}$')
    design_sha256: str = Field(pattern=_SHA256_PATTERN)

    document_id: str = Field(min_length=1)
    scene_revision_id: str | None = Field(default=None, min_length=1)
    scene_content_hash: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )

    listening_area: ListeningAreaSpec
    points: tuple[CampaignPoint, ...] = Field(min_length=1)
    order_plan: CaptureOrderPlan = Field(default_factory=CaptureOrderPlan)
    expectations: SamplingExpectation = Field(
        default_factory=SamplingExpectation
    )

    observable_scope: tuple[str, ...] = ()
    frequency_scope_hz: tuple[float, float] | None = None
    claim_kinds: tuple[CampaignClaimKind, ...] = ()
    template_ref: CampaignTemplateRef | None = None
    purpose: str | None = Field(default=None, min_length=1)
    declared_at_utc: str = Field(min_length=1)

    @field_validator('observable_scope')
    @classmethod
    def canonical_scope(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        _unique(values, 'observable scope')
        if any(not value for value in values):
            raise ValueError('observable scope entries must not be empty')
        return tuple(sorted(values))

    @model_validator(mode='after')
    def valid_design(self) -> 'SpatialCampaignDesign':
        point_ids = [point.point_id for point in self.points]
        if len(point_ids) != len(set(point_ids)):
            raise ValueError('campaign point ids must be unique')
        _require_iso8601(self.declared_at_utc, 'campaign declared_at_utc')
        if (self.scene_revision_id is None) != (self.scene_content_hash is None):
            raise ValueError(
                'scene revision id/content hash must be supplied together'
            )
        zone_ids = {zone.zone_id for zone in self.listening_area.zones}
        for point in self.points:
            if point.zone_id is not None and point.zone_id not in zone_ids:
                raise ValueError(
                    f'point {point.point_id} binds an unknown zone '
                    f'{point.zone_id!r}'
                )
        known = set(point_ids)
        for point_id in self.order_plan.sequence:
            if point_id not in known:
                raise ValueError(
                    f'capture sequence references unknown point {point_id!r}'
                )
        for pair in self.order_plan.interleaved_pairs:
            for member in (pair.first_point_id, pair.second_point_id):
                if member not in known:
                    raise ValueError(
                        f'interleaved pair {pair.pair_id} references unknown '
                        f'point {member!r}'
                    )
        if self.order_plan.strategy == 'declared_sequence' and set(
            self.order_plan.sequence
        ) != known:
            raise ValueError(
                'declared_sequence must cover every declared point exactly once'
            )
        if self.frequency_scope_hz is not None:
            low, high = self.frequency_scope_hz
            if not (
                math.isfinite(low)
                and math.isfinite(high)
                and 0.0 < low < high
            ):
                raise ValueError(
                    'frequency scope must be a finite increasing pair'
                )
        expected = _digest(self.semantic_payload())
        if self.design_sha256 != expected:
            raise ValueError('SpatialCampaignDesign semantic hash mismatch')
        if self.design_id != f'spatial-campaign:{expected}':
            raise ValueError('SpatialCampaignDesign id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'design_id', 'design_sha256'}
        )

    # -- point views ------------------------------------------------------

    def point(self, point_id: str) -> CampaignPoint | None:
        return next(
            (point for point in self.points if point.point_id == point_id),
            None,
        )

    def points_with_role(
        self, role: MeasurementPointRole
    ) -> tuple[CampaignPoint, ...]:
        return tuple(
            point for point in self.points if role in point.roles
        )

    def spatial_points(self) -> tuple[CampaignPoint, ...]:
        return tuple(
            point
            for point in self.points
            if point.contributes_spatial_coverage()
        )

    def spatial_sample_count(self) -> int:
        """Distinct XYZ positions among spatial-coverage points.

        Repeatability captures at one coordinate never inflate this count.
        """

        return len(
            {
                _position_key(point.position)
                for point in self.spatial_points()
            }
        )


def build_spatial_campaign_design(
    *,
    document_id: str,
    listening_area: ListeningAreaSpec,
    points: Sequence[CampaignPoint],
    declared_at_utc: str,
    scene_revision_id: str | None = None,
    scene_content_hash: str | None = None,
    order_plan: CaptureOrderPlan | None = None,
    expectations: SamplingExpectation | None = None,
    observable_scope: Sequence[str] = (),
    frequency_scope_hz: tuple[float, float] | None = None,
    claim_kinds: Sequence[CampaignClaimKind] = (),
    template_ref: CampaignTemplateRef | None = None,
    purpose: str | None = None,
) -> SpatialCampaignDesign:
    """Build a sealed spatial campaign design."""

    payload: dict[str, Any] = {
        'schema_version': SPATIAL_CAMPAIGN_SCHEMA_VERSION,
        'authority_version': SPATIAL_CAMPAIGN_AUTHORITY_VERSION,
        'document_id': document_id,
        'scene_revision_id': scene_revision_id,
        'scene_content_hash': scene_content_hash,
        'listening_area': listening_area.model_dump(mode='json'),
        'points': [point.model_dump(mode='json') for point in points],
        'order_plan': (
            order_plan or CaptureOrderPlan()
        ).model_dump(mode='json'),
        'expectations': (
            expectations or SamplingExpectation()
        ).model_dump(mode='json'),
        'observable_scope': tuple(sorted(set(observable_scope))),
        'frequency_scope_hz': frequency_scope_hz,
        'claim_kinds': tuple(sorted(set(claim_kinds))),
        'template_ref': (
            template_ref.model_dump(mode='json') if template_ref else None
        ),
        'purpose': purpose,
        'declared_at_utc': declared_at_utc,
    }
    digest = _digest(payload)
    return SpatialCampaignDesign(
        schema_version=SPATIAL_CAMPAIGN_SCHEMA_VERSION,
        authority_version=SPATIAL_CAMPAIGN_AUTHORITY_VERSION,
        design_id=f'spatial-campaign:{digest}',
        design_sha256=digest,
        document_id=document_id,
        scene_revision_id=scene_revision_id,
        scene_content_hash=scene_content_hash,
        listening_area=listening_area,
        points=tuple(points),
        order_plan=order_plan or CaptureOrderPlan(),
        expectations=expectations or SamplingExpectation(),
        observable_scope=tuple(sorted(set(observable_scope))),
        frequency_scope_hz=frequency_scope_hz,
        claim_kinds=tuple(sorted(set(claim_kinds))),
        template_ref=template_ref,
        purpose=purpose,
        declared_at_utc=declared_at_utc,
    )


# --- coverage metrics --------------------------------------------------------


class SpatialCoverageMetrics(BaseModel):
    """Descriptive coverage diagnostics (issue #581 section 4).

    These metrics describe sampling geometry; they never by themselves
    prove acoustic independence.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    spatial_point_count: int = Field(ge=0)
    distinct_spatial_positions: int = Field(ge=0)
    optimization_count: int = Field(ge=0)
    holdout_count: int = Field(ge=0)
    repeatability_count: int = Field(ge=0)
    diagnostic_count: int = Field(ge=0)
    nearest_neighbor_m: dict[str, float]
    min_spacing_m: float | None = None
    max_spacing_m: float | None = None
    median_spacing_m: float | None = None
    axis_span_m: tuple[float, float, float]
    height_span_m: float
    zone_spatial_counts: dict[str, int]
    unsampled_zone_ids: tuple[str, ...]
    outside_area_point_ids: tuple[str, ...]
    near_duplicate_pairs: tuple[tuple[str, str], ...]
    declared_weight_share: dict[str, float]
    density_implied_share: dict[str, float]
    density_weight_max_ratio: float | None = None


def compute_coverage_metrics(
    design: SpatialCampaignDesign,
) -> SpatialCoverageMetrics:
    """Compute descriptive sampling-geometry diagnostics for a design."""

    area = design.listening_area
    spatial = design.spatial_points()
    positions = {
        point.point_id: point.position for point in spatial
    }

    nearest: dict[str, float] = {}
    duplicates: list[tuple[str, str]] = []
    tolerance = design.expectations.duplicate_tolerance_m
    ids = sorted(positions)
    for index, point_id in enumerate(ids):
        best: float | None = None
        for other in ids[index + 1 :]:
            delta = _distance_m(positions[point_id], positions[other])
            if delta <= tolerance:
                duplicates.append((point_id, other))
            if best is None or delta < best:
                best = delta
            current_other = nearest.get(other)
            if current_other is None or delta < current_other:
                nearest[other] = delta
        if best is not None:
            nearest[point_id] = best
        elif len(ids) == 1:
            nearest[point_id] = 0.0

    spacings = sorted(nearest.values())
    if spacings:
        min_spacing = spacings[0]
        max_spacing = spacings[-1]
        middle = len(spacings) // 2
        median_spacing = (
            spacings[middle]
            if len(spacings) % 2 == 1
            else (spacings[middle - 1] + spacings[middle]) / 2.0
        )
    else:
        min_spacing = max_spacing = median_spacing = None

    if spatial:
        axis_span = (
            max(p.position.x_m for p in spatial)
            - min(p.position.x_m for p in spatial),
            max(p.position.y_m for p in spatial)
            - min(p.position.y_m for p in spatial),
            max(p.position.z_m for p in spatial)
            - min(p.position.z_m for p in spatial),
        )
    else:
        axis_span = (0.0, 0.0, 0.0)

    zone_counts: dict[str, int] = {}
    outside: list[str] = []
    for point in spatial:
        zone = area.zone_for(point.position)
        if zone is None:
            outside.append(point.point_id)
        else:
            zone_counts[zone.zone_id] = zone_counts.get(zone.zone_id, 0) + 1

    unsampled = tuple(
        sorted(
            zone.zone_id
            for zone in area.non_excluded_zones()
            if zone_counts.get(zone.zone_id, 0) == 0
        )
    )

    declared_total = sum(
        zone.declared_weight for zone in area.non_excluded_zones()
    )
    declared_share: dict[str, float] = {
        zone.zone_id: zone.declared_weight / declared_total
        for zone in area.non_excluded_zones()
    }
    total_spatial = len(spatial)
    density_share: dict[str, float] = {
        zone.zone_id: zone_counts.get(zone.zone_id, 0) / total_spatial
        for zone in area.non_excluded_zones()
    }
    ratios = [
        density_share[zone.zone_id] / declared_share[zone.zone_id]
        for zone in area.non_excluded_zones()
        if declared_share[zone.zone_id] > 0.0
        and zone_counts.get(zone.zone_id, 0) > 0
    ]
    max_ratio = max(ratios) if ratios else None

    return SpatialCoverageMetrics(
        spatial_point_count=len(spatial),
        distinct_spatial_positions=design.spatial_sample_count(),
        optimization_count=len(design.points_with_role('optimization')),
        holdout_count=len(design.points_with_role('spatial_holdout')),
        repeatability_count=len(
            design.points_with_role('repeatability')
        ),
        diagnostic_count=len(design.points_with_role('diagnostic')),
        nearest_neighbor_m=nearest,
        min_spacing_m=min_spacing,
        max_spacing_m=max_spacing,
        median_spacing_m=median_spacing,
        axis_span_m=axis_span,
        height_span_m=axis_span[2],
        zone_spatial_counts=zone_counts,
        unsampled_zone_ids=unsampled,
        outside_area_point_ids=tuple(sorted(outside)),
        near_duplicate_pairs=tuple(sorted(duplicates)),
        declared_weight_share=declared_share,
        density_implied_share=density_share,
        density_weight_max_ratio=max_ratio,
    )


# --- design evaluation -------------------------------------------------------


class CampaignDesignEvaluation(BaseModel):
    """Sealed, fail-closed evaluation of one exact design.

    ``reasons`` are hard rejections; ``warnings`` are advisory findings the
    operator must be able to see. Neither may silently reshape the plan.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'rev56-spatial-campaign-eval-1'
    ] = CAMPAIGN_EVALUATION_ALGORITHM_VERSION
    evaluator_version: str = CAMPAIGN_EVALUATION_ALGORITHM_VERSION

    evaluation_id: str = Field(
        pattern=r'^spatial-campaign-evaluation:[0-9a-f]{64}$'
    )
    evaluation_sha256: str = Field(pattern=_SHA256_PATTERN)
    design_id: str = Field(pattern=r'^spatial-campaign:[0-9a-f]{64}$')
    design_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)

    state: CampaignDesignState
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    metrics: SpatialCoverageMetrics
    evaluated_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_evaluation(self) -> 'CampaignDesignEvaluation':
        if self.state == 'invalid' and not self.reasons:
            raise ValueError('invalid evaluations require reason codes')
        if self.state == 'valid_with_warnings' and not self.warnings:
            raise ValueError('valid_with_warnings requires warnings')
        if self.state == 'valid' and (self.reasons or self.warnings):
            raise ValueError('valid evaluations carry no reasons/warnings')
        _unique(self.reasons, 'evaluation reasons')
        _unique(self.warnings, 'evaluation warnings')
        _require_iso8601(self.evaluated_at_utc, 'evaluation timestamp')
        expected = _digest(self.semantic_payload())
        if self.evaluation_sha256 != expected:
            raise ValueError('CampaignDesignEvaluation semantic hash mismatch')
        if self.evaluation_id != f'spatial-campaign-evaluation:{expected}':
            raise ValueError('CampaignDesignEvaluation id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'evaluation_id', 'evaluation_sha256'},
        )


def evaluate_campaign_design(
    design: SpatialCampaignDesign,
    *,
    evaluated_at_utc: str,
) -> CampaignDesignEvaluation:
    """Fail-closed evaluation of a design against its declared policy.

    Hard reasons reject the design for area/claim use; warnings surface
    clustering, hidden density weighting, out-of-area sampling, and other
    findings the issue requires to be visible rather than folded into an
    opaque score.
    """

    metrics = compute_coverage_metrics(design)
    area = design.listening_area
    expectations = design.expectations
    reasons: list[str] = []
    warnings: list[str] = []

    if metrics.spatial_point_count == 0:
        reasons.append('no_spatial_coverage_points')

    # Holdouts must sit inside the declared area — including declared
    # challenge regions, which are non-excluded zones. A holdout outside
    # every declared zone is not independent evidence *for the claimed
    # domain*.
    holdout_misplaced = False
    holdout_coincident = False
    optimization_keys = {
        _position_key(point.position)
        for point in design.points_with_role('optimization')
    } | {
        _position_key(point.position)
        for point in design.points_with_role('reference_alignment')
    }
    for point in design.points_with_role('spatial_holdout'):
        if area.zone_for(point.position) is None:
            holdout_misplaced = True
        if _position_key(point.position) in optimization_keys:
            holdout_coincident = True
    if holdout_misplaced:
        reasons.append('holdout_outside_declared_area')
    if holdout_coincident:
        reasons.append('holdout_coincident_with_design_point')

    spatial_claims = {
        'multi_position_area',
        'seat_to_seat_consistency',
        'whole_listening_area',
    }
    if (
        spatial_claims.intersection(design.claim_kinds)
        and metrics.holdout_count == 0
    ):
        reasons.append('spatial_claim_without_holdout')
    if metrics.holdout_count < expectations.min_holdout_count:
        reasons.append('min_holdout_count_unmet')

    missing_required = tuple(
        zone_id
        for zone_id in expectations.required_zone_ids
        if metrics.zone_spatial_counts.get(zone_id, 0) == 0
    )
    if missing_required:
        reasons.append('required_zone_unsampled')

    if expectations.min_axis_span_m is not None and (
        max(metrics.axis_span_m) < expectations.min_axis_span_m
    ):
        reasons.append('spatial_span_below_declared_minimum')

    if metrics.near_duplicate_pairs:
        reasons.append('near_duplicate_positions')
    if (
        expectations.min_pairwise_spacing_m is not None
        and metrics.min_spacing_m is not None
        and metrics.min_spacing_m < expectations.min_pairwise_spacing_m
    ):
        warnings.append('points_below_declared_spacing')
    if (
        metrics.density_weight_max_ratio is not None
        and metrics.density_weight_max_ratio
        > expectations.density_weight_alert_ratio
    ):
        warnings.append('hidden_density_weighting')
    if metrics.outside_area_point_ids:
        non_holdout_outside = tuple(
            point_id
            for point_id in metrics.outside_area_point_ids
            if 'spatial_holdout'
            not in (design.point(point_id).roles if design.point(point_id) else ())
        )
        if non_holdout_outside:
            warnings.append('points_outside_listening_area')
    if metrics.unsampled_zone_ids:
        warnings.append('zones_unsampled')
    if any(
        'diagnostic' in point.roles and point.weight != 0.0
        for point in design.points
    ):
        warnings.append('diagnostic_point_carries_weight')
    if design.order_plan.strategy == 'unplanned' and (
        spatial_claims.intersection(design.claim_kinds)
    ):
        warnings.append('capture_order_undeclared')
    if not design.points_with_role('reference_alignment') and (
        spatial_claims.intersection(design.claim_kinds)
    ):
        warnings.append('no_reference_alignment_point')
    if any(point.provenance == 'adaptive' for point in design.points):
        warnings.append('adaptive_points_not_holdout_eligible')

    if reasons:
        state: CampaignDesignState = 'invalid'
    elif warnings:
        state = 'valid_with_warnings'
    else:
        state = 'valid'

    payload: dict[str, Any] = {
        'authority_version': CAMPAIGN_EVALUATION_ALGORITHM_VERSION,
        'evaluator_version': CAMPAIGN_EVALUATION_ALGORITHM_VERSION,
        'design_id': design.design_id,
        'design_sha256': design.design_sha256,
        'document_id': design.document_id,
        'state': state,
        'reasons': tuple(sorted(set(reasons))),
        'warnings': tuple(sorted(set(warnings))),
        'metrics': metrics.model_dump(mode='json'),
        'evaluated_at_utc': evaluated_at_utc,
    }
    digest = _digest(payload)
    return CampaignDesignEvaluation(
        authority_version=CAMPAIGN_EVALUATION_ALGORITHM_VERSION,
        evaluator_version=CAMPAIGN_EVALUATION_ALGORITHM_VERSION,
        evaluation_id=f'spatial-campaign-evaluation:{digest}',
        evaluation_sha256=digest,
        design_id=design.design_id,
        design_sha256=design.design_sha256,
        document_id=design.document_id,
        state=state,
        reasons=tuple(sorted(set(reasons))),
        warnings=tuple(sorted(set(warnings))),
        metrics=metrics,
        evaluated_at_utc=evaluated_at_utc,
    )


# --- planned vs observed -----------------------------------------------------


class CampaignPointBinding(BaseModel):
    """Binds one capture to a planned point (issue #581 section 3).

    ``observed_*`` records where the microphone actually was; ``deviation_m``
    is the planned-vs-observed distance. A missing observed position is
    UNKNOWN — never silently equal to plan.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'rev56-spatial-campaign-binding-1'
    ] = CAMPAIGN_BINDING_AUTHORITY_VERSION

    binding_id: str = Field(
        pattern=r'^spatial-campaign-binding:[0-9a-f]{64}$'
    )
    binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    design_id: str = Field(pattern=r'^spatial-campaign:[0-9a-f]{64}$')
    design_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)

    point_id: str = Field(min_length=1)
    measurement_id: str = Field(min_length=1)
    measurement_sha256: str = Field(pattern=_SHA256_PATTERN)

    observed_position: Position3 | None = None
    observed_orientation: Quaternion4 | None = None
    position_uncertainty_m: float | None = Field(default=None, ge=0.0)
    deviation_m: float | None = Field(default=None, ge=0.0)
    captured_at_utc: str = Field(min_length=1)
    capture_index: int | None = Field(default=None, ge=0)

    @field_validator('position_uncertainty_m', 'deviation_m')
    @classmethod
    def finite_distance(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, 'binding distance metric')

    @model_validator(mode='after')
    def valid_binding(self) -> 'CampaignPointBinding':
        _require_iso8601(self.captured_at_utc, 'binding captured_at_utc')
        expected = _digest(self.semantic_payload())
        if self.binding_sha256 != expected:
            raise ValueError('CampaignPointBinding semantic hash mismatch')
        if self.binding_id != f'spatial-campaign-binding:{expected}':
            raise ValueError('CampaignPointBinding id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'binding_id', 'binding_sha256'}
        )


def build_point_binding(
    *,
    design: SpatialCampaignDesign,
    point_id: str,
    measurement_id: str,
    measurement_sha256: str,
    captured_at_utc: str,
    observed_position: Position3 | None = None,
    observed_orientation: Quaternion4 | None = None,
    position_uncertainty_m: float | None = None,
    capture_index: int | None = None,
) -> CampaignPointBinding:
    """Seal a capture-to-plan binding against an exact design.

    Fail-closed: the point must exist in the design, and the design must
    already have been declared (the caller binds to its persisted identity).
    """

    point = design.point(point_id)
    if point is None:
        raise ValueError(f'campaign design has no point {point_id!r}')
    deviation = (
        _distance_m(point.position, observed_position)
        if observed_position is not None
        else None
    )
    payload: dict[str, Any] = {
        'authority_version': CAMPAIGN_BINDING_AUTHORITY_VERSION,
        'design_id': design.design_id,
        'design_sha256': design.design_sha256,
        'document_id': design.document_id,
        'point_id': point_id,
        'measurement_id': measurement_id,
        'measurement_sha256': measurement_sha256,
        'observed_position': (
            observed_position.model_dump(mode='json')
            if observed_position is not None
            else None
        ),
        'observed_orientation': (
            observed_orientation.model_dump(mode='json')
            if observed_orientation is not None
            else None
        ),
        'position_uncertainty_m': position_uncertainty_m,
        'deviation_m': deviation,
        'captured_at_utc': captured_at_utc,
        'capture_index': capture_index,
    }
    digest = _digest(payload)
    return CampaignPointBinding(
        authority_version=CAMPAIGN_BINDING_AUTHORITY_VERSION,
        binding_id=f'spatial-campaign-binding:{digest}',
        binding_sha256=digest,
        design_id=design.design_id,
        design_sha256=design.design_sha256,
        document_id=design.document_id,
        point_id=point_id,
        measurement_id=measurement_id,
        measurement_sha256=measurement_sha256,
        observed_position=observed_position,
        observed_orientation=observed_orientation,
        position_uncertainty_m=position_uncertainty_m,
        deviation_m=deviation,
        captured_at_utc=captured_at_utc,
        capture_index=capture_index,
    )


class PlacementAssessment(BaseModel):
    """Derived planned-vs-observed summary over a design's bindings."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    design_id: str
    binding_count: int = Field(ge=0)
    uncaptured_spatial_point_ids: tuple[str, ...]
    unbound_measurement_ids: tuple[str, ...]
    max_deviation_m: float | None = None
    deviation_flags: tuple[str, ...]
    order_flags: tuple[str, ...]


def evaluate_placements(
    design: SpatialCampaignDesign,
    bindings: Sequence[CampaignPointBinding],
    *,
    deviation_tolerance_m: float | None = None,
) -> PlacementAssessment:
    """Assess captured placements against the plan (§3, §12, §15).

    Flags: captures bound to points not in the design, spatial points left
    uncaptured, deviations above the declared tolerance, undeclared position
    uncertainty, and capture order that diverges from the declared sequence.
    """

    flags: list[str] = []
    order_flags: list[str] = []
    design_point_ids = {point.point_id for point in design.points}
    spatial_ids = {
        point.point_id for point in design.spatial_points()
    }
    bound_points = {binding.point_id for binding in bindings}
    for binding in bindings:
        if binding.design_id != design.design_id:
            raise ValueError('binding references a different campaign design')
        if binding.point_id not in design_point_ids:
            raise ValueError(
                f'binding names unknown design point {binding.point_id!r}'
            )
        if binding.observed_position is not None and (
            binding.position_uncertainty_m is None
        ):
            flags.append('position_uncertainty_undeclared')
        if (
            deviation_tolerance_m is not None
            and binding.deviation_m is not None
            and binding.deviation_m > deviation_tolerance_m
        ):
            flags.append('deviation_above_declared_tolerance')
    uncaptured = tuple(sorted(spatial_ids - bound_points))
    if uncaptured:
        flags.append('spatial_points_uncaptured')
    if (
        design.order_plan.strategy == 'declared_sequence'
        and design.order_plan.sequence
    ):
        declared = list(design.order_plan.sequence)
        observed_order = [
            binding.point_id
            for binding in sorted(
                (b for b in bindings if b.capture_index is not None),
                key=lambda b: b.capture_index,
            )
        ]
        for index, point_id in enumerate(observed_order):
            if index < len(declared) and declared[index] != point_id:
                order_flags.append('capture_order_diverged')
                break

    deviations = [
        binding.deviation_m
        for binding in bindings
        if binding.deviation_m is not None
    ]
    measurement_ids = [binding.measurement_id for binding in bindings]
    return PlacementAssessment(
        design_id=design.design_id,
        binding_count=len(bindings),
        uncaptured_spatial_point_ids=uncaptured,
        unbound_measurement_ids=tuple(sorted(set(measurement_ids))),
        max_deviation_m=max(deviations) if deviations else None,
        deviation_flags=tuple(sorted(set(flags))),
        order_flags=tuple(sorted(set(order_flags))),
    )


# --- post-capture acoustic diversity (diagnostic only) ------------------------


class AcousticDiversitySpec(BaseModel):
    """Declared parameters a diversity report was computed under (§5).

    Algorithm/band/window/metric are explicit — similarity output computed
    under a different spec is a different diagnostic, not a rerun of this
    one.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    algorithm_version: str = DIVERSITY_ALGORITHM_VERSION
    metric: Literal['pearson_db_magnitude'] = 'pearson_db_magnitude'
    band_hz: tuple[float, float] | None = None
    redundancy_threshold: float = Field(default=0.95, gt=0.0, lt=1.0)

    @model_validator(mode='after')
    def valid_spec(self) -> 'AcousticDiversitySpec':
        if self.band_hz is not None:
            low, high = self.band_hz
            if not (
                math.isfinite(low) and math.isfinite(high) and 0.0 < low < high
            ):
                raise ValueError('diversity band must be a finite increasing pair')
        return self


class PointResponseInput(BaseModel):
    """One point's measured magnitude response on a shared grid."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    point_id: str = Field(min_length=1)
    frequency_hz: tuple[float, ...] = Field(min_length=2)
    magnitude_db: tuple[float, ...] = Field(min_length=2)

    @model_validator(mode='after')
    def valid_response(self) -> 'PointResponseInput':
        if len(self.frequency_hz) != len(self.magnitude_db):
            raise ValueError('frequency/magnitude grids must align')
        freqs = list(self.frequency_hz)
        if any(not math.isfinite(f) or f <= 0.0 for f in freqs):
            raise ValueError('frequency grid must be finite positive')
        if freqs != sorted(freqs):
            raise ValueError('frequency grid must be ascending')
        if any(not math.isfinite(v) for v in self.magnitude_db):
            raise ValueError('magnitude values must be finite')
        return self


class PairwiseSimilarity(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    point_a: str = Field(min_length=1)
    point_b: str = Field(min_length=1)
    correlation: float


class AcousticDiversityReport(BaseModel):
    """Post-hoc similarity diagnostic (§5) — never rewrites roles.

    The report flags suspicious similarity but is deliberately not wired to
    any mutation path: holdout membership stays exactly as predeclared.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'rev56-acoustic-diversity-1'
    ] = DIVERSITY_ALGORITHM_VERSION

    report_id: str = Field(
        pattern=r'^spatial-campaign-diversity:[0-9a-f]{64}$'
    )
    report_sha256: str = Field(pattern=_SHA256_PATTERN)
    design_id: str = Field(pattern=r'^spatial-campaign:[0-9a-f]{64}$')
    design_sha256: str = Field(pattern=_SHA256_PATTERN)
    spec: AcousticDiversitySpec
    similarities: tuple[PairwiseSimilarity, ...]
    redundant_pair_ids: tuple[tuple[str, str], ...]
    skipped_pairs: tuple[tuple[str, str], ...]
    diagnostic_only: Literal[True] = True
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_report(self) -> 'AcousticDiversityReport':
        _require_iso8601(self.created_at_utc, 'diversity report timestamp')
        expected = _digest(self.semantic_payload())
        if self.report_sha256 != expected:
            raise ValueError('AcousticDiversityReport semantic hash mismatch')
        if self.report_id != f'spatial-campaign-diversity:{expected}':
            raise ValueError('AcousticDiversityReport id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'report_id', 'report_sha256'}
        )


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    syy = sum((y - mean_y) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def acoustic_diversity_report(
    design: SpatialCampaignDesign,
    responses: Sequence[PointResponseInput],
    *,
    spec: AcousticDiversitySpec | None = None,
    created_at_utc: str,
) -> AcousticDiversityReport:
    """Pairwise magnitude-similarity diagnostic over spatial points.

    Responses on mismatched grids are skipped per pair
    (``skipped_pairs``) rather than resampled silently.
    """

    spec = spec or AcousticDiversitySpec()
    known = {point.point_id for point in design.spatial_points()}
    usable = [r for r in responses if r.point_id in known]
    missing = [r.point_id for r in responses if r.point_id not in known]
    if missing:
        raise ValueError(
            'responses reference points outside the design spatial set: '
            + ', '.join(sorted(missing))
        )

    def _band_values(response: PointResponseInput) -> tuple[float, ...]:
        if spec.band_hz is None:
            return response.magnitude_db
        low, high = spec.band_hz
        values = tuple(
            value
            for freq, value in zip(
                response.frequency_hz, response.magnitude_db
            )
            if low <= freq <= high
        )
        return values

    similarities: list[PairwiseSimilarity] = []
    redundant: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    ordered = sorted(usable, key=lambda r: r.point_id)
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            pair = (left.point_id, right.point_id)
            left_values = _band_values(left)
            right_values = _band_values(right)
            if (
                left.frequency_hz != right.frequency_hz
                or len(left_values) != len(right_values)
                or len(left_values) < 2
            ):
                skipped.append(pair)
                continue
            corr = _pearson(left_values, right_values)
            if corr is None:
                skipped.append(pair)
                continue
            similarities.append(
                PairwiseSimilarity(
                    point_a=pair[0], point_b=pair[1], correlation=corr
                )
            )
            if corr >= spec.redundancy_threshold:
                redundant.append(pair)

    payload: dict[str, Any] = {
        'authority_version': DIVERSITY_ALGORITHM_VERSION,
        'design_id': design.design_id,
        'design_sha256': design.design_sha256,
        'spec': spec.model_dump(mode='json'),
        'similarities': [s.model_dump(mode='json') for s in similarities],
        'redundant_pair_ids': redundant,
        'skipped_pairs': skipped,
        'diagnostic_only': True,
        'created_at_utc': created_at_utc,
    }
    digest = _digest(payload)
    return AcousticDiversityReport(
        authority_version=DIVERSITY_ALGORITHM_VERSION,
        report_id=f'spatial-campaign-diversity:{digest}',
        report_sha256=digest,
        design_id=design.design_id,
        design_sha256=design.design_sha256,
        spec=spec,
        similarities=tuple(similarities),
        redundant_pair_ids=tuple(sorted(redundant)),
        skipped_pairs=tuple(sorted(skipped)),
        diagnostic_only=True,
        created_at_utc=created_at_utc,
    )


# --- holdout/partition composition --------------------------------------------


def partition_for_point(point: CampaignPoint) -> str:
    """Map a declared point to the REV55 registration partition.

    ``spatial_holdout`` → ``holdout``; ``repeatability`` → ``repeatability``;
    filter-design/alignment/standards/boundary points feed fitting →
    ``calibration``; diagnostics stay ``unassigned`` so they can never feed
    an objective silently.
    """

    roles = set(point.roles)
    if 'spatial_holdout' in roles:
        return 'holdout'
    if 'repeatability' in roles:
        return 'repeatability'
    if roles.intersection(
        {'optimization', 'reference_alignment', 'boundary_stress', 'standards_required'}
    ):
        return 'calibration'
    return 'unassigned'


def assert_campaign_partition_disjoint(
    design: SpatialCampaignDesign,
    measurement_partitions: Iterable[tuple[str, str]],
) -> None:
    """Fail-closed calibration/holdout partition check (#581 §6 + #564).

    ``measurement_partitions`` pairs a measurement id with the partition it
    was filed under. A measurement may not appear as both ``calibration``
    and ``holdout`` evidence — same invariant as REV55
    ``assert_partition_disjoint``.
    """

    calibration_ids: set[str] = set()
    holdout_ids: set[str] = set()
    for measurement_id, partition in measurement_partitions:
        if partition == 'calibration':
            calibration_ids.add(measurement_id)
        elif partition == 'holdout':
            holdout_ids.add(measurement_id)
    overlap = calibration_ids & holdout_ids
    if overlap:
        raise ValueError(
            'measurements cannot be both calibration and holdout evidence: '
            + ', '.join(sorted(overlap))
        )


def check_binding_partition_consistency(
    design: SpatialCampaignDesign,
    bindings: Sequence[CampaignPointBinding],
    measurement_partitions: Iterable[tuple[str, str]],
) -> None:
    """Every bound measurement's filed partition must equal the point's.

    A measurement captured on an ``optimization`` point may not be filed as
    ``holdout`` (or vice versa) — the declared role drives the partition,
    never post-hoc inspection.
    """

    partition_by_measurement = dict(measurement_partitions)
    for binding in bindings:
        point = design.point(binding.point_id)
        if point is None:
            raise ValueError('binding references a point outside the design')
        expected = partition_for_point(point)
        actual = partition_by_measurement.get(binding.measurement_id)
        if actual is None:
            continue
        if actual != expected:
            raise ValueError(
                f'measurement {binding.measurement_id} filed as '
                f'{actual!r} but bound to {expected!r} point '
                f'{binding.point_id}'
            )


# --- compatibility/workflow templates (§14) -------------------------------------


class SpatialCampaignTemplate(BaseModel):
    """A named, versioned workflow template.

    Compatibility templates never claim vendor algorithm equivalence; they
    carry provenance so the operator can audit what behaviour is borrowed.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    template_id: str = Field(min_length=1)
    template_version: str = Field(min_length=1)
    name: str = Field(min_length=1)
    source_label: str = Field(min_length=1)
    source_uri: str | None = Field(default=None, min_length=1)
    compatibility_note: str = Field(min_length=1)
    optimization_count: int = Field(ge=1)
    holdout_count: int = Field(ge=0)
    repeatability_count: int = Field(ge=0)
    include_reference: bool = True
    outside_area_count: int = Field(ge=0, default=0)
    spacing_m: float = Field(gt=0.0)
    claim_kinds: tuple[CampaignClaimKind, ...] = ()


def _lattice_positions(
    bounds_min: Position3,
    bounds_max: Position3,
    spacing_m: float,
    count: int,
    offset_fraction: float = 0.0,
) -> list[Position3]:
    """Deterministic interior lattice inside one box.

    Walks a 3-D grid at ``spacing_m`` pitch (offset by ``offset_fraction``
    of the pitch), deterministically ordered from the box centre outward so
    the first ``count`` entries stay well-spread at any ``count``.
    """

    nx = max(
        1,
        int(
            math.floor(
                (bounds_max.x_m - bounds_min.x_m) / spacing_m
            )
        )
        + 1,
    )
    ny = max(
        1,
        int(
            math.floor(
                (bounds_max.y_m - bounds_min.y_m) / spacing_m
            )
        )
        + 1,
    )
    nz = max(
        1,
        int(
            math.floor(
                (bounds_max.z_m - bounds_min.z_m) / spacing_m
            )
        )
        + 1,
    )
    offset = spacing_m * offset_fraction
    centre = (
        (bounds_min.x_m + bounds_max.x_m) / 2.0,
        (bounds_min.y_m + bounds_max.y_m) / 2.0,
        (bounds_min.z_m + bounds_max.z_m) / 2.0,
    )
    candidates: list[Position3] = []
    for ix in range(nx):
        for iy in range(ny):
            for iz in range(nz):
                x = bounds_min.x_m + spacing_m * (ix + 0.5) + offset
                y = bounds_min.y_m + spacing_m * (iy + 0.5) + offset
                z = bounds_min.z_m + spacing_m * (iz + 0.5) + offset
                if x > bounds_max.x_m or y > bounds_max.y_m or z > bounds_max.z_m:
                    continue
                candidates.append(Position3(x_m=x, y_m=y, z_m=z))
    candidates.sort(
        key=lambda p: (
            (p.x_m - centre[0]) ** 2
            + (p.y_m - centre[1]) ** 2
            + (p.z_m - centre[2]) ** 2
        )
    )
    return candidates[:count]


def instantiate_campaign_template(
    template: SpatialCampaignTemplate,
    listening_area: ListeningAreaSpec,
    *,
    document_id: str,
    declared_at_utc: str,
    scene_revision_id: str | None = None,
    scene_content_hash: str | None = None,
    purpose: str | None = None,
) -> SpatialCampaignDesign:
    """Instantiate a declared layout from a template (§14).

    Deterministic: same (template, area) always yields the same layout.
    Lattices are generated per zone — never across the aggregate bounding
    box — so inter-zone gaps are never sampled as if they were listening
    area. The result is a *proposal* — it must still pass
    ``evaluate_campaign_design`` and operator review before capture.
    """

    zones = listening_area.non_excluded_zones()
    points: list[CampaignPoint] = []
    sequence: list[str] = []

    # The RSP anchor sits at the centre of the highest-weight zone.
    anchor_zone = max(zones, key=lambda z: z.declared_weight)
    centre = Position3(
        x_m=(anchor_zone.bounds_min.x_m + anchor_zone.bounds_max.x_m) / 2.0,
        y_m=(anchor_zone.bounds_min.y_m + anchor_zone.bounds_max.y_m) / 2.0,
        z_m=(anchor_zone.bounds_min.z_m + anchor_zone.bounds_max.z_m) / 2.0,
    )

    def _zone_lattice(
        zone: ListeningZone, count: int, offset_fraction: float = 0.0
    ) -> list[tuple[ListeningZone, Position3]]:
        return [
            (zone, position)
            for position in _lattice_positions(
                zone.bounds_min,
                zone.bounds_max,
                template.spacing_m,
                count,
                offset_fraction=offset_fraction,
            )
        ]

    def _distribute(
        count: int,
        offset_fraction: float = 0.0,
        reserved: Iterable[tuple[float, float, float]] = (),
    ) -> list[tuple[ListeningZone, Position3]]:
        """Round-robin lattice positions across zones so coverage spreads
        over the whole declared area rather than concentrating in one."""

        lattices = [
            _zone_lattice(zone, count, offset_fraction) for zone in zones
        ]
        picked: list[tuple[ListeningZone, Position3]] = []
        # The RSP anchor and every previously assigned point are occupied
        # — a lattice point landing on one is a near-duplicate (or worse,
        # a holdout coincident with a design point), not a distinct sample.
        seen: set[tuple[float, float, float]] = {
            _position_key(centre), *reserved
        }
        index = 0
        while len(picked) < count and any(lattices):
            candidates = lattices[index % len(lattices)]
            while candidates:
                candidate = candidates.pop(0)
                key = _position_key(candidate[1])
                if key in seen:
                    continue
                seen.add(key)
                picked.append(candidate)
                break
            index += 1
            if index > count * max(1, len(zones)) * 4:
                break
        return picked

    if template.include_reference:
        points.append(
            CampaignPoint(
                point_id='p-ref',
                position=centre,
                roles=('reference_alignment', 'optimization'),
                zone_id=anchor_zone.zone_id,
                note='reference/sweet-spot anchor (RSP)',
            )
        )
        sequence.append('p-ref')

    optimization_needed = template.optimization_count - (
        1 if template.include_reference else 0
    )
    for index, (zone, position) in enumerate(
        _distribute(max(optimization_needed, 1))[:optimization_needed]
    ):
        point_id = f'p-opt-{index:02d}'
        points.append(
            CampaignPoint(
                point_id=point_id,
                position=position,
                roles=('optimization',),
                zone_id=zone.zone_id,
            )
        )
        sequence.append(point_id)

    if template.holdout_count:
        occupied = {
            _position_key(point.position) for point in points
        }
        for index, (zone, position) in enumerate(
            _distribute(
                template.holdout_count,
                offset_fraction=0.5,
                reserved=occupied,
            )
        ):
            point_id = f'p-hold-{index:02d}'
            points.append(
                CampaignPoint(
                    point_id=point_id,
                    position=position,
                    roles=('spatial_holdout',),
                    zone_id=zone.zone_id,
                )
            )
            sequence.append(point_id)

    for index in range(template.repeatability_count):
        point_id = f'p-rep-{index:02d}'
        points.append(
            CampaignPoint(
                point_id=point_id,
                position=centre,
                roles=('repeatability',),
                weight=0.0,
            )
        )
        sequence.append(point_id)

    if template.outside_area_count:
        mins, maxs = listening_area.aggregate_bounds()
        for index in range(template.outside_area_count):
            point_id = f'p-out-{index:02d}'
            # Deliberately a little outside the aggregate box on
            # alternating sides (Dirac-style outside-area points), weight
            # zero so they can never silently alter the objective.
            side = -1.0 if index % 2 == 0 else 1.0
            position = Position3(
                x_m=mins.x_m + (maxs.x_m - mins.x_m) * ((index + 1) / (
                    template.outside_area_count + 1
                )),
                y_m=mins.y_m + side * template.spacing_m,
                z_m=centre.z_m,
            )
            points.append(
                CampaignPoint(
                    point_id=point_id,
                    position=position,
                    roles=('boundary_stress',),
                    weight=0.0,
                )
            )
            sequence.append(point_id)

    template_ref = CampaignTemplateRef(
        template_id=template.template_id,
        template_version=template.template_version,
        source_label=template.source_label,
        source_uri=template.source_uri,
        compatibility_note=template.compatibility_note,
    )
    return build_spatial_campaign_design(
        document_id=document_id,
        listening_area=listening_area,
        points=points,
        order_plan=CaptureOrderPlan(
            strategy='declared_sequence', sequence=tuple(sequence)
        ),
        claim_kinds=template.claim_kinds,
        template_ref=template_ref,
        scene_revision_id=scene_revision_id,
        scene_content_hash=scene_content_hash,
        purpose=purpose or f'template {template.template_id} instantiation',
        declared_at_utc=declared_at_utc,
    )


def builtin_campaign_templates() -> tuple[SpatialCampaignTemplate, ...]:
    """Versioned workflow templates with explicit provenance (§14)."""

    return (
        SpatialCampaignTemplate(
            template_id='dirac-like-focused',
            template_version='1.0',
            name='Dirac-like focused listening area',
            source_label='Dirac Live measurement-position guidance',
            source_uri=(
                'https://helpdesk.dirac.com/en/dirac-room-correction/'
                'In-what-order-should-I-measure-the-positions-cbe'
            ),
            compatibility_note=(
                'Workflow compatibility template only — not Dirac algorithm '
                'equivalence. Points spaced >=0.30 m apart inside the '
                'declared area per current public Dirac guidance.'
            ),
            optimization_count=8,
            holdout_count=3,
            repeatability_count=0,
            include_reference=True,
            spacing_m=0.30,
            claim_kinds=('multi_position_area',),
        ),
        SpatialCampaignTemplate(
            template_id='dirac-like-wide',
            template_version='1.0',
            name='Dirac-like wide listening area',
            source_label='Dirac Live measurement-position guidance',
            source_uri=(
                'https://helpdesk.dirac.com/en/dirac-room-correction/'
                'In-what-order-should-I-measure-the-positions-cbe'
            ),
            compatibility_note=(
                'Workflow compatibility template only — wider spatial spread '
                'plus deliberate just-outside points per current public Dirac '
                'guidance; not algorithm equivalence.'
            ),
            optimization_count=10,
            holdout_count=4,
            repeatability_count=0,
            include_reference=True,
            outside_area_count=2,
            spacing_m=0.45,
            claim_kinds=('multi_position_area', 'whole_listening_area'),
        ),
        SpatialCampaignTemplate(
            template_id='trinnov-like-rsp-multipoint',
            template_version='1.0',
            name='Trinnov-like RSP + multipoint',
            source_label='Trinnov Optimizer microphone-placement guidance',
            source_uri=(
                'https://kb.trinnov.com/en/knowledge-base/place-the-microphone'
            ),
            compatibility_note=(
                'Workflow compatibility template only — RSP anchor plus '
                'spread secondary points; the Trinnov joint optimizer is not '
                'averaging and is not reproduced here.'
            ),
            optimization_count=7,
            holdout_count=4,
            repeatability_count=1,
            include_reference=True,
            spacing_m=0.40,
            claim_kinds=('multi_position_area', 'seat_to_seat_consistency'),
        ),
        SpatialCampaignTemplate(
            template_id='htdt-research-holdout',
            template_version='1.0',
            name='HTDT research holdout campaign',
            source_label='HTDT spatial generalization practice (#581)',
            source_uri=None,
            compatibility_note=(
                'HTDT research template — holds back roughly one third of '
                'spatial samples as locked holdout evidence.'
            ),
            optimization_count=8,
            holdout_count=4,
            repeatability_count=2,
            include_reference=True,
            spacing_m=0.40,
            claim_kinds=('multi_position_area', 'whole_listening_area'),
        ),
        SpatialCampaignTemplate(
            template_id='rp22-verification',
            template_version='1.0',
            name='RP22-style verification sampling',
            source_label='HTDT RP22 verification workflow (#579/#585)',
            source_uri=None,
            compatibility_note=(
                'HTDT verification template — a standards-required point set '
                'plus holdout coverage; no claim of CEDIA RP32 clause '
                'equivalence until the exact source is mapped.'
            ),
            optimization_count=6,
            holdout_count=4,
            repeatability_count=1,
            include_reference=True,
            spacing_m=0.50,
            claim_kinds=('whole_listening_area',),
        ),
    )


__all__ = [
    'AcousticDiversityReport',
    'AcousticDiversitySpec',
    'CAMPAIGN_BINDING_AUTHORITY_VERSION',
    'CAMPAIGN_EVALUATION_ALGORITHM_VERSION',
    'CampaignClaimKind',
    'CampaignDesignEvaluation',
    'CampaignDesignState',
    'CampaignPoint',
    'CampaignPointBinding',
    'CampaignTemplateRef',
    'CaptureOrderPlan',
    'CaptureOrderStrategy',
    'ChannelCoverageKind',
    'DEFAULT_DUPLICATE_TOLERANCE_M',
    'DIVERSITY_ALGORITHM_VERSION',
    'FORBIDDEN_ROLE_PAIRS',
    'InterleavedPair',
    'ListeningAreaSpec',
    'ListeningZone',
    'ListeningZoneKind',
    'MeasurementPointRole',
    'PairwiseSimilarity',
    'PlacementAssessment',
    'PointResponseInput',
    'PointSelectionProvenance',
    'SPATIAL_CAMPAIGN_AUTHORITY_VERSION',
    'SPATIAL_CAMPAIGN_SCHEMA_VERSION',
    'SPATIAL_COVERAGE_ROLES',
    'SamplingExpectation',
    'SpatialCampaignDesign',
    'SpatialCampaignTemplate',
    'SpatialCoverageMetrics',
    'TEMPLATE_INSTANTIATION_VERSION',
    'acoustic_diversity_report',
    'assert_campaign_partition_disjoint',
    'build_point_binding',
    'build_spatial_campaign_design',
    'builtin_campaign_templates',
    'check_binding_partition_consistency',
    'compute_coverage_metrics',
    'evaluate_campaign_design',
    'evaluate_placements',
    'instantiate_campaign_template',
    'partition_for_point',
]
