from __future__ import annotations

from itertools import product
import json
from math import isfinite
from typing import Any, Callable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_constraint_models import (
    CadAllowedRegionConstraint,
    CadConstraintPoint2D,
    CadConstraintSet,
    CadExclusionRegionConstraint,
    CadPlacementConstraint,
)
from .cad_constraints import build_g10_constraint_request, scene_to_g10_context
from .cad_extended_search import (
    CadExtendedCandidate,
    aim_horizontal_yaw_deg,
    direction_with_aim_pitch,
    extended_candidate_preview_document,
)
from .cad_orientation_constraints import orientation_constraint_rejections
from .cad_repository import SceneRevision
from .cad_scene import Direction3, SceneDocument, scene_content_hash
from .cad_search import candidate_preview_document
from .cad_search_models import CadCandidate, CadSearchAxis
from .cad_system_variant import (
    ProposedEntitySpec,
    SystemVariant,
    VariantProvenanceItem,
    build_system_variant,
    materialize_system_variant,
)
from .cad_topology_space import (
    TopologySearchSpec,
    require_topology_option,
)
from .placement_constraints import (
    AxisConstraint,
    ConstraintSetCreate,
    LinkedPlacementConstraint,
    PlacementEvaluationRequest,
    PreparedConstraintContext,
    evaluate_constraint_set,
    evaluate_prepared_constraint_set,
    prepare_constraint_context,
    prepare_constraint_spec,
    validate_constraint_set_for_context,
)
from .search_space import (
    MAX_SEARCH_PAGE_SIZE,
    GridAxis,
    LinkedDerivation,
    SearchSpecCreate,
    generate_search_space,
    grid_values,
    validate_search_spec,
)
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest


TOPOLOGY_SEARCH_SCHEMA_VERSION = 1
TOPOLOGY_SEARCH_AUTHORITY_VERSION = 'o100b-virtual-placement-1'
TOPOLOGY_SEARCH_ALGORITHM_VERSION_V1 = 'o100b-o10-o80-grid-1'
TOPOLOGY_SEARCH_ALGORITHM_VERSION = 'o100b-o10-o80-grid-2'
TOPOLOGY_SEARCH_SYSTEM_MAX_CANDIDATES = 50_000

AngleParameter = Literal['aim_yaw_deg', 'aim_pitch_deg', 'body_yaw_deg']
LinkRelation = Literal[
    'mirror_x',
    'equal_x',
    'equal_y',
    'equal_z',
    'equal_delta_x',
    'equal_delta_y',
    'equal_delta_z',
]






class PlacementAngleAxis(BaseModel):
    model_config = ConfigDict(frozen=True)

    parameter: AngleParameter
    min_deg: float
    max_deg: float
    step_deg: float = Field(gt=0.0)

    @model_validator(mode='after')
    def valid_range(self) -> 'PlacementAngleAxis':
        values = (self.min_deg, self.max_deg, self.step_deg)
        if not all(isfinite(float(value)) for value in values):
            raise ValueError('placement angle axis values must be finite')
        if self.max_deg < self.min_deg:
            raise ValueError('placement angle axis max_deg must be >= min_deg')
        if self.parameter == 'aim_pitch_deg':
            if self.min_deg <= -90.0 or self.max_deg >= 90.0:
                raise ValueError('aim pitch must remain strictly within -90..90 degrees')
        elif self.min_deg < -180.0 or self.max_deg > 180.0:
            raise ValueError('yaw parameters must remain within -180..180 degrees')
        return self


class ProposedExclusionRegion(BaseModel):
    model_config = ConfigDict(frozen=True)

    region_id: str = Field(min_length=1)
    vertices: tuple[CadConstraintPoint2D, ...] = Field(min_length=3)
    region_role: Literal['exclusion', 'walkway'] = 'exclusion'


class ProposedPlacementSpec(BaseModel):
    """Role-bound installation zone and search axes for one proposed speaker."""

    model_config = ConfigDict(frozen=True)

    entity_id: str = Field(min_length=1)
    role_id: str = Field(min_length=1)
    zone_id: str = Field(min_length=1)
    allowed_region: tuple[CadConstraintPoint2D, ...] = Field(min_length=3)
    exclusion_regions: tuple[ProposedExclusionRegion, ...] = ()
    min_z_m: float | None = None
    max_z_m: float | None = None
    xyz_axes: tuple[CadSearchAxis, ...] = ()
    angle_axes: tuple[PlacementAngleAxis, ...] = ()

    @model_validator(mode='after')
    def valid_placement(self) -> 'ProposedPlacementSpec':
        if (
            self.min_z_m is not None
            and self.max_z_m is not None
            and self.max_z_m < self.min_z_m
        ):
            raise ValueError('placement zone max_z_m must be >= min_z_m')
        if any(
            value is not None and not isfinite(float(value))
            for value in (self.min_z_m, self.max_z_m)
        ):
            raise ValueError('placement zone height bounds must be finite')
        exclusion_ids = [item.region_id for item in self.exclusion_regions]
        if len(exclusion_ids) != len(set(exclusion_ids)):
            raise ValueError('placement exclusion region IDs must be unique')
        if any(axis.entity_id != self.entity_id for axis in self.xyz_axes):
            raise ValueError('placement XYZ axes must target their ProposedPlacementSpec entity')
        xyz_keys = [axis.axis for axis in self.xyz_axes]
        if len(xyz_keys) != len(set(xyz_keys)):
            raise ValueError('placement XYZ axes must be unique per coordinate')
        angle_keys = [axis.parameter for axis in self.angle_axes]
        if len(angle_keys) != len(set(angle_keys)):
            raise ValueError('placement angle axes must be unique per parameter')
        return self


class LinkedPlacementRule(BaseModel):
    """Explicit O10/G10 derivation for a proposed pair or group relation."""

    model_config = ConfigDict(frozen=True)

    constraint_id: str = Field(min_length=1, max_length=100)
    master_entity_id: str = Field(min_length=1)
    slave_entity_id: str = Field(min_length=1)
    relation: LinkRelation
    mirror_axis_x_m: float | None = None
    tolerance_m: float = Field(default=1e-6, ge=0.0)

    @model_validator(mode='after')
    def valid_rule(self) -> 'LinkedPlacementRule':
        if self.master_entity_id == self.slave_entity_id:
            raise ValueError('linked placement requires two different proposed entities')
        if self.relation != 'mirror_x' and self.mirror_axis_x_m is not None:
            raise ValueError('mirror_axis_x_m is valid only for mirror_x')
        if self.mirror_axis_x_m is not None and not isfinite(float(self.mirror_axis_x_m)):
            raise ValueError('linked placement mirror axis must be finite')
        return self


class TopologyPlacementSearchSpec(BaseModel):
    """Immutable O100B search definition bound to one O100A topology template."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = TOPOLOGY_SEARCH_SCHEMA_VERSION
    authority_version: Literal['o100b-virtual-placement-1'] = TOPOLOGY_SEARCH_AUTHORITY_VERSION
    search_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    baseline_revision_id: str = Field(min_length=1)
    baseline_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    template_variant_id: str = Field(min_length=1)
    template_variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    template_scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    topology_search_id: str = Field(min_length=1)
    topology_search_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    topology_option_id: str = Field(min_length=1)
    placement_specs: tuple[ProposedPlacementSpec, ...] = Field(min_length=1)
    linked_rules: tuple[LinkedPlacementRule, ...] = ()
    constraint_snapshot_json: str = Field(min_length=2)
    constraint_snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    g10_constraint_spec_json: str = Field(min_length=2)
    g10_constraint_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    o10_search_spec_json: str = Field(min_length=2)
    o10_raw_candidate_count: int = Field(ge=1)
    orientation_combination_count: int = Field(ge=1)
    candidate_limit: int = Field(
        ge=1,
        le=TOPOLOGY_SEARCH_SYSTEM_MAX_CANDIDATES,
    )
    algorithm_version: Literal[
        'o100b-o10-o80-grid-1',
        'o100b-o10-o80-grid-2',
    ] = TOPOLOGY_SEARCH_ALGORITHM_VERSION
    created_at_utc: str = Field(min_length=1)
    search_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_identity(self) -> 'TopologyPlacementSearchSpec':
        placement_ids = [item.entity_id for item in self.placement_specs]
        if len(placement_ids) != len(set(placement_ids)):
            raise ValueError('topology placement entity ids must be unique')
        link_ids = [item.constraint_id for item in self.linked_rules]
        if len(link_ids) != len(set(link_ids)):
            raise ValueError('topology linked constraint ids must be unique')
        if _digest(json.loads(self.constraint_snapshot_json)) != self.constraint_snapshot_sha256:
            raise ValueError('topology constraint snapshot hash mismatch')
        if _digest(json.loads(self.g10_constraint_spec_json)) != self.g10_constraint_spec_sha256:
            raise ValueError('topology G10 constraint spec hash mismatch')
        if _digest(self.identity_payload()) != self.search_sha256:
            raise ValueError('topology placement search identity hash mismatch')
        if self.search_id != 'tps-' + self.search_sha256[:20]:
            raise ValueError('topology placement search_id is not deterministic')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'baseline_revision_id': self.baseline_revision_id,
            'baseline_content_hash': self.baseline_content_hash,
            'template_variant_id': self.template_variant_id,
            'template_variant_sha256': self.template_variant_sha256,
            'template_scene_content_hash': self.template_scene_content_hash,
            'topology_search_id': self.topology_search_id,
            'topology_search_sha256': self.topology_search_sha256,
            'topology_option_id': self.topology_option_id,
            'placement_specs': [
                item.model_dump(mode='json') for item in self.placement_specs
            ],
            'linked_rules': [
                item.model_dump(mode='json') for item in self.linked_rules
            ],
            'constraint_snapshot_sha256': self.constraint_snapshot_sha256,
            'g10_constraint_spec_sha256': self.g10_constraint_spec_sha256,
            'o10_search_spec': json.loads(self.o10_search_spec_json),
            'o10_raw_candidate_count': self.o10_raw_candidate_count,
            'orientation_combination_count': self.orientation_combination_count,
            'candidate_limit': self.candidate_limit,
            'algorithm_version': self.algorithm_version,
        }


class TopologyPlacementCandidate(BaseModel):
    """One exact feasible placement. Identity is content-derived and reproducible."""

    model_config = ConfigDict(frozen=True)

    candidate_id: str = Field(min_length=1)
    candidate_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    search_id: str = Field(min_length=1)
    search_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    template_variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    topology_search_id: str = Field(min_length=1)
    topology_search_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    topology_option_id: str = Field(min_length=1)
    o10_candidate_id: str = Field(min_length=1)
    raw_index: int = Field(ge=0)
    feasible_index: int = Field(ge=0)
    positions: dict[str, dict[str, float]]
    aim_yaw_deg: dict[str, float] = Field(default_factory=dict)
    aim_pitch_deg: dict[str, float] = Field(default_factory=dict)
    body_yaw_deg: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode='after')
    def valid_candidate(self) -> 'TopologyPlacementCandidate':
        for entity_id, position in self.positions.items():
            if not entity_id or set(position) != {'x_m', 'y_m', 'z_m'}:
                raise ValueError('topology candidate positions require x_m/y_m/z_m')
            if not all(isfinite(float(value)) for value in position.values()):
                raise ValueError('topology candidate positions must be finite')
        for mapping, label in (
            (self.aim_yaw_deg, 'aim yaw'),
            (self.body_yaw_deg, 'body yaw'),
        ):
            for entity_id, value in mapping.items():
                if not entity_id or not isfinite(float(value)) or not -180.0 <= float(value) <= 180.0:
                    raise ValueError(f'topology candidate {label} is invalid')
        for entity_id, value in self.aim_pitch_deg.items():
            if not entity_id or not isfinite(float(value)) or not -90.0 < float(value) < 90.0:
                raise ValueError('topology candidate aim pitch is invalid')
        if self.candidate_sha256 != _digest(self.identity_payload()):
            raise ValueError('topology placement candidate hash mismatch')
        if self.candidate_id != 'tpc-' + self.candidate_sha256[:20]:
            raise ValueError('topology placement candidate_id is not deterministic')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'search_id': self.search_id,
            'search_sha256': self.search_sha256,
            'template_variant_sha256': self.template_variant_sha256,
            'topology_search_id': self.topology_search_id,
            'topology_search_sha256': self.topology_search_sha256,
            'topology_option_id': self.topology_option_id,
            'o10_candidate_id': self.o10_candidate_id,
            'positions': self.positions,
            'aim_yaw_deg': self.aim_yaw_deg,
            'aim_pitch_deg': self.aim_pitch_deg,
            'body_yaw_deg': self.body_yaw_deg,
        }


class TopologyPlacementCandidateSetPage(BaseModel):
    """One deterministic window over the canonical O100B candidate set.

    The wrapper asserts only the internal invariants every generated page
    must satisfy; it cannot prove membership by itself. Persisting a page
    requires exact equality with a fresh
    generate_topology_placement_candidates replay over the persisted search
    authority (see CadTopologySearchRepository.save_candidate_page).
    """

    model_config = ConfigDict(frozen=True)

    search_id: str = Field(min_length=1)
    search_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_set_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    raw_candidate_count: int = Field(ge=1)
    feasible_candidate_count: int = Field(ge=0)
    rejected_candidate_count: int = Field(ge=0)
    duplicate_candidate_count: int = Field(ge=0)
    rejection_counts: dict[str, int]
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=500)
    candidates: tuple[TopologyPlacementCandidate, ...]

    @model_validator(mode='after')
    def valid_page(self) -> 'TopologyPlacementCandidateSetPage':
        if (
            self.raw_candidate_count
            != self.feasible_candidate_count
            + self.rejected_candidate_count
            + self.duplicate_candidate_count
        ):
            raise ValueError('topology candidate page counts are inconsistent')
        if len(self.candidates) > self.limit:
            raise ValueError('topology candidate page exceeds its limit')
        if self.candidates:
            if self.offset + len(self.candidates) > self.feasible_candidate_count:
                raise ValueError(
                    'topology candidate page overruns the feasible set'
                )
            for index, candidate in enumerate(self.candidates):
                if (
                    candidate.search_id != self.search_id
                    or candidate.search_sha256 != self.search_sha256
                ):
                    raise ValueError(
                        'topology candidate page mixes search authority'
                    )
                if candidate.feasible_index != self.offset + index:
                    raise ValueError(
                        'topology candidate page feasible indices are not '
                        'contiguous'
                    )
                if candidate.raw_index >= self.raw_candidate_count:
                    raise ValueError(
                        'topology candidate page raw index outside the raw set'
                    )
        return self


def _zone_constraint_id(item: ProposedPlacementSpec) -> str:
    return 'o100b-zone-' + _digest({
        'entity_id': item.entity_id,
        'role_id': item.role_id,
        'zone_id': item.zone_id,
    })[:20]


def _exclusion_constraint_id(
    item: ProposedPlacementSpec,
    region: ProposedExclusionRegion,
) -> str:
    return 'o100b-exclusion-' + _digest({
        'entity_id': item.entity_id,
        'role_id': item.role_id,
        'zone_id': item.zone_id,
        'region_id': region.region_id,
    })[:20]


def _height_constraint_id(item: ProposedPlacementSpec) -> str:
    return 'o100b-height-' + _digest({
        'entity_id': item.entity_id,
        'zone_id': item.zone_id,
    })[:20]


def _placement_constraint_additions(
    placement_specs: Sequence[ProposedPlacementSpec],
) -> tuple[CadPlacementConstraint, ...]:
    """Deterministic zone/exclusion constraints derived from placement specs."""

    additions: list[CadPlacementConstraint] = [
        CadAllowedRegionConstraint(
            constraint_id=_zone_constraint_id(item),
            name=f'O100B {item.role_id} installation zone {item.zone_id}',
            entity_ids=(item.entity_id,),
            vertices=item.allowed_region,
        )
        for item in placement_specs
    ]
    for item in placement_specs:
        additions.extend(
            CadExclusionRegionConstraint(
                constraint_id=_exclusion_constraint_id(item, region),
                name=(
                    f'O100B {item.role_id} exclusion '
                    f'{item.zone_id}/{region.region_id}'
                ),
                entity_ids=(item.entity_id,),
                vertices=region.vertices,
                region_role=region.region_role,
            )
            for region in item.exclusion_regions
        )
    return tuple(additions)


def _effective_constraint_set(
    base: CadConstraintSet,
    placement_specs: Sequence[ProposedPlacementSpec],
) -> CadConstraintSet:
    return CadConstraintSet(
        document_id=base.document_id,
        constraints=tuple(base.constraints)
        + _placement_constraint_additions(placement_specs),
    )


def declared_base_constraint_set(
    spec: TopologyPlacementSearchSpec,
) -> CadConstraintSet:
    """Recover the declared base CadConstraintSet behind an O100B search spec.

    The embedded constraint snapshot is the canonical effective set: the
    caller-declared base constraints followed by the deterministic
    placement-derived zone/exclusion additions. Replaying
    build_topology_placement_search_spec requires the base half, so it is
    recovered by stripping exactly those canonical additions. A snapshot
    that does not end with them is not the product of the pinned compiler
    and fails closed.
    """

    try:
        effective = CadConstraintSet.model_validate(
            json.loads(spec.constraint_snapshot_json)
        )
    except ValueError as exc:
        raise ValueError(
            'topology placement constraint snapshot is not a CadConstraintSet'
        ) from exc
    additions = _placement_constraint_additions(spec.placement_specs)
    constraints = tuple(effective.constraints)
    if constraints[len(constraints) - len(additions):] != additions:
        raise ValueError(
            'topology placement constraint snapshot lacks the canonical '
            'placement-derived constraints'
        )
    return CadConstraintSet(
        document_id=effective.document_id,
        constraints=constraints[: len(constraints) - len(additions)],
    )


def _angle_values(axis: PlacementAngleAxis) -> tuple[float, ...]:
    # Reuse the O10 Decimal-backed deterministic grid stepping semantics.
    values = grid_values(GridAxis(
        entity_id=f'o100b-angle:{axis.parameter}',
        axis='x',
        min_m=axis.min_deg,
        max_m=axis.max_deg,
        step_m=axis.step_deg,
    ))
    return tuple(float(value) for value in values)


def _ordered_angle_axes(
    placement_specs: Sequence[ProposedPlacementSpec],
) -> tuple[tuple[str, PlacementAngleAxis], ...]:
    order = {'body_yaw_deg': 0, 'aim_yaw_deg': 1, 'aim_pitch_deg': 2}
    result = [
        (item.entity_id, axis)
        for item in placement_specs
        for axis in item.angle_axes
    ]
    return tuple(sorted(result, key=lambda pair: (pair[0], order[pair[1].parameter])))


def _validate_template(
    baseline: SceneRevision,
    template_variant: SystemVariant,
) -> SceneDocument:
    if template_variant.document_id != baseline.document_id:
        raise ValueError('topology template belongs to another document')
    if template_variant.baseline_revision_id != baseline.revision_id:
        raise ValueError('topology template baseline revision mismatch')
    if template_variant.baseline_content_hash != baseline.content_hash:
        raise ValueError('topology template baseline content hash mismatch')
    return materialize_system_variant(baseline, template_variant)


def build_topology_placement_search_spec(
    *,
    baseline: SceneRevision,
    template_variant: SystemVariant,
    topology_spec: TopologySearchSpec,
    topology_option_id: str,
    placement_specs: Sequence[ProposedPlacementSpec],
    constraint_set: CadConstraintSet,
    linked_rules: Sequence[LinkedPlacementRule] = (),
    candidate_limit: int = 10_000,
    created_at_utc: str,
) -> TopologyPlacementSearchSpec:
    """Build O100B search authority without saving or mutating a SceneRevision."""

    if constraint_set.document_id != baseline.document_id:
        raise ValueError('topology placement constraints belong to another document')
    if candidate_limit < 1 or candidate_limit > TOPOLOGY_SEARCH_SYSTEM_MAX_CANDIDATES:
        raise ValueError('topology placement candidate_limit is outside system bounds')

    require_topology_option(
        baseline=baseline,
        topology_spec=topology_spec,
        template_variant=template_variant,
        option_id=topology_option_id,
    )
    virtual_scene = _validate_template(baseline, template_variant)
    placements = tuple(sorted(placement_specs, key=lambda item: item.entity_id))
    if not placements:
        raise ValueError('topology placement search requires proposed placement specs')
    links = tuple(sorted(
        linked_rules,
        key=lambda item: (
            item.constraint_id,
            item.master_entity_id,
            item.slave_entity_id,
        ),
    ))

    proposed_by_id = {
        item.entity.entity_id: item
        for item in template_variant.proposed_entities
    }
    role_by_id = {
        item.role_id: item
        for item in template_variant.role_bindings
    }
    for item in placements:
        proposed = proposed_by_id.get(item.entity_id)
        if proposed is None:
            raise ValueError(
                f'topology placement entity is not ProposedEntitySpec: {item.entity_id}'
            )
        if proposed.role_binding_id != item.role_id:
            raise ValueError(
                f'topology placement role mismatch for {item.entity_id}'
            )
        if item.role_id not in role_by_id:
            raise ValueError(f'topology placement references unknown role: {item.role_id}')
        if item.angle_axes:
            entity = virtual_scene.entity(item.entity_id)
            if entity.kind != 'speaker' or entity.aim_xyz is None:
                raise ValueError(
                    f'orientation/aim search requires explicit proposed speaker aim: {item.entity_id}'
                )
            aim_horizontal_yaw_deg(entity.aim_xyz)

    placement_ids = {item.entity_id for item in placements}
    for rule in links:
        if (
            rule.master_entity_id not in placement_ids
            or rule.slave_entity_id not in placement_ids
        ):
            raise ValueError(
                f'linked placement rule {rule.constraint_id} must reference searched proposed entities'
            )

    effective_constraints = _effective_constraint_set(constraint_set, placements)
    context = scene_to_g10_context(virtual_scene)
    request = build_g10_constraint_request(
        virtual_scene,
        effective_constraints,
        additional_entity_ids=placement_ids,
    )
    request_payload = request.model_dump(mode='json')
    for item in placements:
        if item.min_z_m is None and item.max_z_m is None:
            continue
        request_payload['constraints'].append(
            AxisConstraint(
                constraint_id=_height_constraint_id(item),
                kind='axis_range',
                entity_id=item.entity_id,
                axis='z',
                min_m=item.min_z_m,
                max_m=item.max_z_m,
            ).model_dump(mode='json')
        )
    for rule in links:
        request_payload['constraints'].append(
            LinkedPlacementConstraint(
                constraint_id=rule.constraint_id,
                kind='linked_placement',
                entity_a=rule.master_entity_id,
                entity_b=rule.slave_entity_id,
                relation=rule.relation,
                mirror_axis_x_m=rule.mirror_axis_x_m,
                tolerance_m=rule.tolerance_m,
            ).model_dump(mode='json')
        )
    g10_request = ConstraintSetCreate.model_validate(request_payload)
    g10_spec = validate_constraint_set_for_context(g10_request, context)
    g10_sha = _digest(g10_spec)

    xyz_axes = tuple(
        axis
        for item in placements
        for axis in item.xyz_axes
    )
    if not xyz_axes:
        raise ValueError('topology placement search requires at least one XYZ grid axis')
    o10_request = SearchSpecCreate(
        constraint_set_id=f'o100b:{g10_sha[:20]}',
        axes=[
            GridAxis.model_validate(axis.model_dump(mode='json'))
            for axis in xyz_axes
        ],
        linked_derivations=[
            LinkedDerivation(
                constraint_id=rule.constraint_id,
                master_entity_id=rule.master_entity_id,
            )
            for rule in links
        ],
        candidate_limit=candidate_limit,
    )
    o10_spec, estimate = validate_search_spec(
        o10_request,
        context,
        context_id=f'o100b:{baseline.revision_id}:{template_variant.variant_sha256[:16]}',
        constraint_set_id=o10_request.constraint_set_id,
        constraint_set_spec=g10_spec,
        constraint_set_spec_sha256=g10_sha,
    )

    angle_axes = _ordered_angle_axes(placements)
    orientation_count = 1
    for _entity_id, axis in angle_axes:
        orientation_count *= len(_angle_values(axis))
    raw_count = int(estimate['raw_candidate_count']) * orientation_count
    if raw_count > candidate_limit:
        raise ValueError(
            f'topology raw candidate estimate {raw_count} exceeds '
            f'candidate_limit {candidate_limit}'
        )

    constraint_snapshot = effective_constraints.model_dump(mode='json')
    constraint_snapshot_json = _canonical(constraint_snapshot)
    constraint_snapshot_sha = _digest(constraint_snapshot)
    o10_search_spec_json = _canonical(o10_spec)
    # One canonical serialization feeds both the identity digest and the
    # returned spec field.
    virtual_scene_hash = scene_content_hash(virtual_scene)
    identity = {
        'schema_version': TOPOLOGY_SEARCH_SCHEMA_VERSION,
        'authority_version': TOPOLOGY_SEARCH_AUTHORITY_VERSION,
        'document_id': baseline.document_id,
        'baseline_revision_id': baseline.revision_id,
        'baseline_content_hash': baseline.content_hash,
        'template_variant_id': template_variant.variant_id,
        'template_variant_sha256': template_variant.variant_sha256,
        'template_scene_content_hash': virtual_scene_hash,
        'topology_search_id': topology_spec.topology_search_id,
        'topology_search_sha256': topology_spec.topology_search_sha256,
        'topology_option_id': topology_option_id,
        'placement_specs': [
            item.model_dump(mode='json') for item in placements
        ],
        'linked_rules': [
            item.model_dump(mode='json') for item in links
        ],
        'constraint_snapshot_sha256': constraint_snapshot_sha,
        'g10_constraint_spec_sha256': g10_sha,
        'o10_search_spec': o10_spec,
        'o10_raw_candidate_count': int(estimate['raw_candidate_count']),
        'orientation_combination_count': orientation_count,
        'candidate_limit': int(candidate_limit),
        'algorithm_version': TOPOLOGY_SEARCH_ALGORITHM_VERSION,
    }
    search_sha = _digest(identity)
    return TopologyPlacementSearchSpec(
        search_id='tps-' + search_sha[:20],
        document_id=baseline.document_id,
        baseline_revision_id=baseline.revision_id,
        baseline_content_hash=baseline.content_hash,
        template_variant_id=template_variant.variant_id,
        template_variant_sha256=template_variant.variant_sha256,
        template_scene_content_hash=virtual_scene_hash,
        topology_search_id=topology_spec.topology_search_id,
        topology_search_sha256=topology_spec.topology_search_sha256,
        topology_option_id=topology_option_id,
        placement_specs=placements,
        linked_rules=links,
        constraint_snapshot_json=constraint_snapshot_json,
        constraint_snapshot_sha256=constraint_snapshot_sha,
        g10_constraint_spec_json=_canonical(g10_spec),
        g10_constraint_spec_sha256=g10_sha,
        o10_search_spec_json=o10_search_spec_json,
        o10_raw_candidate_count=int(estimate['raw_candidate_count']),
        orientation_combination_count=orientation_count,
        candidate_limit=int(candidate_limit),
        algorithm_version=TOPOLOGY_SEARCH_ALGORITHM_VERSION,
        created_at_utc=created_at_utc,
        search_sha256=search_sha,
    )


def _validate_search_sources(
    baseline: SceneRevision,
    template_variant: SystemVariant,
    spec: TopologyPlacementSearchSpec,
) -> SceneDocument:
    if (
        baseline.document_id != spec.document_id
        or baseline.revision_id != spec.baseline_revision_id
        or baseline.content_hash != spec.baseline_content_hash
    ):
        raise ValueError('topology placement baseline SceneRevision authority mismatch')
    if (
        template_variant.variant_id != spec.template_variant_id
        or template_variant.variant_sha256 != spec.template_variant_sha256
    ):
        raise ValueError('topology placement template SystemVariant authority mismatch')
    virtual_scene = _validate_template(baseline, template_variant)
    if scene_content_hash(virtual_scene) != spec.template_scene_content_hash:
        raise ValueError('topology placement template scene hash mismatch')
    return virtual_scene


def _body_yaw_entity_ids(
    spec: TopologyPlacementSearchSpec,
) -> set[str]:
    return {
        entity_id
        for entity_id, axis in _ordered_angle_axes(spec.placement_specs)
        if axis.parameter == 'body_yaw_deg'
    }


def _o10_prefilter_constraint_spec(
    spec: TopologyPlacementSearchSpec,
) -> dict[str, Any]:
    """Return the deterministic XYZ prefilter for the selected O100B algorithm.

    v2 must not reject an XYZ point solely because the template footprint has
    the wrong yaw. Room/allowed/exclusion point checks remain necessary
    conditions, while wall and envelope-pair checks involving a searched body
    yaw are deferred to the exact final-pose evaluation.
    """

    raw = json.loads(spec.g10_constraint_spec_json)
    if spec.algorithm_version == TOPOLOGY_SEARCH_ALGORITHM_VERSION_V1:
        return raw

    rotated = _body_yaw_entity_ids(spec)
    if not rotated:
        return raw

    raw['entity_profiles'] = [
        item
        for item in raw.get('entity_profiles', [])
        if str(item['entity_id']) not in rotated
    ]
    constraints = []
    for item in raw.get('constraints', []):
        kind = str(item.get('kind'))
        if (
            kind == 'wall_clearance'
            and rotated.intersection(
                str(value) for value in item.get('entity_ids', [])
            )
        ):
            continue
        if (
            kind == 'pair_distance'
            and item.get('distance_reference') == 'envelope_clearance'
            and rotated.intersection(
                (str(item.get('entity_a')), str(item.get('entity_b')))
            )
        ):
            continue
        constraints.append(item)
    raw['constraints'] = constraints
    return raw


def _final_pose_constraint_rejections(
    *,
    preview: SceneDocument,
    spec: TopologyPlacementSearchSpec,
    positions: dict[str, dict[str, float]],
    constraint_set: CadConstraintSet,
    prepared_context: PreparedConstraintContext,
) -> tuple[str, ...]:
    """Evaluate canonical G10 constraints with exact final O80 footprints."""

    placement_ids = {
        item.entity_id
        for item in spec.placement_specs
    }
    final_request = build_g10_constraint_request(
        preview,
        constraint_set,
        additional_entity_ids=placement_ids,
    )
    final_profiles = final_request.model_dump(mode='json')['entity_profiles']

    # Context geometry is invariant across the base x orientation sweep; the
    # per-candidate spec still carries this candidate's exact final profiles.
    final_spec = json.loads(spec.g10_constraint_spec_json)
    final_spec['entity_profiles'] = final_profiles
    evaluation = evaluate_prepared_constraint_set(
        prepare_constraint_spec(prepared_context, final_spec),
        PlacementEvaluationRequest.model_validate({
            'positions': positions,
        }),
    )
    return tuple(
        str(item['constraint_id'])
        for item in evaluation['rejections']
    )


def _all_o10_candidates(
    *,
    context: dict[str, Any],
    spec: TopologyPlacementSearchSpec,
    cancelled: Callable[[], bool] | None,
) -> tuple[tuple[CadCandidate, ...], dict[str, Any]]:
    raw_o10_spec = json.loads(spec.o10_search_spec_json)
    raw_g10_spec = _o10_prefilter_constraint_spec(spec)
    raw_g10_sha = _digest(raw_g10_spec)
    raw_o10_spec['constraint_set_spec_sha256'] = raw_g10_sha
    page_limit = MAX_SEARCH_PAGE_SIZE
    first = generate_search_space(
        context,
        raw_o10_spec,
        search_spec_sha256=spec.search_sha256,
        constraint_set_spec=raw_g10_spec,
        constraint_set_spec_sha256=raw_g10_sha,
        offset=0,
        limit=page_limit,
        cancelled=cancelled,
    )
    result = [
        CadCandidate.model_validate(item)
        for item in first['candidates']
    ]
    offset = len(result)
    feasible_count = int(first['feasible_candidate_count'])
    while offset < feasible_count:
        page = generate_search_space(
            context,
            raw_o10_spec,
            search_spec_sha256=spec.search_sha256,
            constraint_set_spec=raw_g10_spec,
            constraint_set_spec_sha256=raw_g10_sha,
            offset=offset,
            limit=page_limit,
            cancelled=cancelled,
        )
        batch = [
            CadCandidate.model_validate(item)
            for item in page['candidates']
        ]
        if not batch:
            raise ValueError('O10 candidate pagination ended before feasible count')
        result.extend(batch)
        offset += len(batch)
    return tuple(result), first


def _orientation_maps(
    ordered_axes: Sequence[tuple[str, PlacementAngleAxis]],
    values: Sequence[float],
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    aim_yaw: dict[str, float] = {}
    aim_pitch: dict[str, float] = {}
    body_yaw: dict[str, float] = {}
    for (entity_id, axis), value in zip(ordered_axes, values, strict=True):
        target = {
            'aim_yaw_deg': aim_yaw,
            'aim_pitch_deg': aim_pitch,
            'body_yaw_deg': body_yaw,
        }[axis.parameter]
        target[entity_id] = round(float(value), 12)
    return aim_yaw, aim_pitch, body_yaw


def _candidate_document_from_parts(
    virtual_scene: SceneDocument,
    *,
    o10_candidate_id: str,
    raw_index: int,
    feasible_index: int,
    positions: dict[str, dict[str, float]],
    aim_yaw_deg: dict[str, float],
    aim_pitch_deg: dict[str, float],
    body_yaw_deg: dict[str, float],
) -> SceneDocument:
    base_candidate = CadCandidate(
        candidate_id=o10_candidate_id,
        raw_index=raw_index,
        feasible_index=feasible_index,
        positions=positions,
    )
    if aim_yaw_deg or body_yaw_deg:
        extended = CadExtendedCandidate(
            candidate_id='o100b-preview',
            base_candidate_id=o10_candidate_id,
            raw_index=raw_index,
            feasible_index=feasible_index,
            positions=positions,
            aim_yaw_deg=aim_yaw_deg,
            body_yaw_deg=body_yaw_deg,
        )
        preview = extended_candidate_preview_document(virtual_scene, extended)
    else:
        preview = candidate_preview_document(virtual_scene, base_candidate)

    if not aim_pitch_deg:
        return preview
    replacements = {}
    for entity_id, pitch_deg in aim_pitch_deg.items():
        entity = preview.entity(entity_id)
        if entity.kind != 'speaker' or entity.aim_xyz is None:
            raise ValueError(
                f'aim pitch target lacks explicit proposed speaker aim: {entity_id}'
            )
        replacements[entity_id] = entity.model_copy(update={
            'aim_xyz': direction_with_aim_pitch(entity.aim_xyz, pitch_deg),
        })
    return preview.model_copy(update={
        'entities': tuple(
            replacements.get(entity.entity_id, entity)
            for entity in preview.entities
        ),
    })


def _candidate_payload(
    *,
    spec: TopologyPlacementSearchSpec,
    o10_candidate_id: str,
    positions: dict[str, dict[str, float]],
    aim_yaw_deg: dict[str, float],
    aim_pitch_deg: dict[str, float],
    body_yaw_deg: dict[str, float],
) -> dict[str, Any]:
    return {
        'search_id': spec.search_id,
        'search_sha256': spec.search_sha256,
        'template_variant_sha256': spec.template_variant_sha256,
        'topology_search_id': spec.topology_search_id,
        'topology_search_sha256': spec.topology_search_sha256,
        'topology_option_id': spec.topology_option_id,
        'o10_candidate_id': o10_candidate_id,
        'positions': positions,
        'aim_yaw_deg': aim_yaw_deg,
        'aim_pitch_deg': aim_pitch_deg,
        'body_yaw_deg': body_yaw_deg,
    }


def generate_topology_placement_candidates(
    *,
    baseline: SceneRevision,
    template_variant: SystemVariant,
    spec: TopologyPlacementSearchSpec,
    offset: int = 0,
    limit: int = 100,
    cancelled: Callable[[], bool] | None = None,
) -> TopologyPlacementCandidateSetPage:
    """Generate deterministic O100B candidates using O10/G10 plus O80 body semantics."""

    if offset < 0:
        raise ValueError('topology placement offset must be >= 0')
    if limit < 1 or limit > 500:
        raise ValueError('topology placement limit must be between 1 and 500')

    virtual_scene = _validate_search_sources(baseline, template_variant, spec)
    context = scene_to_g10_context(virtual_scene)
    base_candidates, base_meta = _all_o10_candidates(
        context=context,
        spec=spec,
        cancelled=cancelled,
    )
    constraint_set = CadConstraintSet.model_validate(
        json.loads(spec.constraint_snapshot_json)
    )
    prepared_g10_context = prepare_constraint_context(context)

    ordered_angles = _ordered_angle_axes(spec.placement_specs)
    angle_value_lists = [
        _angle_values(axis)
        for _entity_id, axis in ordered_angles
    ]
    combinations = tuple(product(*angle_value_lists)) if angle_value_lists else ((),)
    if len(combinations) != spec.orientation_combination_count:
        raise ValueError('topology placement orientation grid no longer matches spec')

    candidate_ids: list[str] = []
    returned: list[TopologyPlacementCandidate] = []
    rejection_counts = {
        str(key): int(value) * spec.orientation_combination_count
        for key, value in base_meta['rejection_counts'].items()
    }
    duplicate_count = (
        int(base_meta['duplicate_candidate_count'])
        * spec.orientation_combination_count
    )

    for base_candidate in base_candidates:
        for orientation_index, values in enumerate(combinations):
            if cancelled is not None and cancelled():
                raise RuntimeError('topology placement generation cancelled')
            aim_yaw, aim_pitch, body_yaw = _orientation_maps(
                ordered_angles,
                values,
            )
            preview = _candidate_document_from_parts(
                virtual_scene,
                o10_candidate_id=base_candidate.candidate_id,
                raw_index=base_candidate.raw_index,
                feasible_index=base_candidate.feasible_index,
                positions=base_candidate.positions,
                aim_yaw_deg=aim_yaw,
                aim_pitch_deg=aim_pitch,
                body_yaw_deg=body_yaw,
            )
            if spec.algorithm_version == TOPOLOGY_SEARCH_ALGORITHM_VERSION_V1:
                rejections = (
                    orientation_constraint_rejections(
                        preview,
                        constraint_set,
                        changed_entity_ids=body_yaw,
                    )
                    if body_yaw
                    else ()
                )
            else:
                rejections = _final_pose_constraint_rejections(
                    preview=preview,
                    spec=spec,
                    positions=base_candidate.positions,
                    constraint_set=constraint_set,
                    prepared_context=prepared_g10_context,
                )
            if rejections:
                for constraint_id in rejections:
                    rejection_counts[constraint_id] = (
                        rejection_counts.get(constraint_id, 0) + 1
                    )
                continue

            payload = _candidate_payload(
                spec=spec,
                o10_candidate_id=base_candidate.candidate_id,
                positions=base_candidate.positions,
                aim_yaw_deg=aim_yaw,
                aim_pitch_deg=aim_pitch,
                body_yaw_deg=body_yaw,
            )
            candidate_sha = _digest(payload)
            feasible_index = len(candidate_ids)
            candidate = TopologyPlacementCandidate(
                candidate_id='tpc-' + candidate_sha[:20],
                candidate_sha256=candidate_sha,
                search_id=spec.search_id,
                search_sha256=spec.search_sha256,
                template_variant_sha256=spec.template_variant_sha256,
                topology_search_id=spec.topology_search_id,
                topology_search_sha256=spec.topology_search_sha256,
                topology_option_id=spec.topology_option_id,
                o10_candidate_id=base_candidate.candidate_id,
                raw_index=(
                    base_candidate.raw_index * spec.orientation_combination_count
                    + orientation_index
                ),
                feasible_index=feasible_index,
                positions=base_candidate.positions,
                aim_yaw_deg=aim_yaw,
                aim_pitch_deg=aim_pitch,
                body_yaw_deg=body_yaw,
            )
            candidate_ids.append(candidate.candidate_id)
            if offset <= feasible_index < offset + limit:
                returned.append(candidate)

    raw_count = int(base_meta['raw_candidate_count']) * spec.orientation_combination_count
    rejected_count = raw_count - len(candidate_ids) - duplicate_count
    if rejected_count < 0:
        raise ValueError('topology placement candidate accounting is inconsistent')
    return TopologyPlacementCandidateSetPage(
        search_id=spec.search_id,
        search_sha256=spec.search_sha256,
        candidate_set_sha256=_digest(candidate_ids),
        raw_candidate_count=raw_count,
        feasible_candidate_count=len(candidate_ids),
        rejected_candidate_count=rejected_count,
        duplicate_candidate_count=duplicate_count,
        rejection_counts=dict(sorted(rejection_counts.items())),
        offset=offset,
        limit=limit,
        candidates=tuple(returned),
    )


def topology_candidate_document(
    *,
    baseline: SceneRevision,
    template_variant: SystemVariant,
    spec: TopologyPlacementSearchSpec,
    candidate: TopologyPlacementCandidate,
) -> SceneDocument:
    virtual_scene = _validate_search_sources(baseline, template_variant, spec)
    if (
        candidate.search_id != spec.search_id
        or candidate.search_sha256 != spec.search_sha256
        or candidate.template_variant_sha256 != spec.template_variant_sha256
        or candidate.topology_search_id != spec.topology_search_id
        or candidate.topology_search_sha256 != spec.topology_search_sha256
        or candidate.topology_option_id != spec.topology_option_id
    ):
        raise ValueError('topology placement candidate search authority mismatch')
    expected_payload = _candidate_payload(
        spec=spec,
        o10_candidate_id=candidate.o10_candidate_id,
        positions=candidate.positions,
        aim_yaw_deg=candidate.aim_yaw_deg,
        aim_pitch_deg=candidate.aim_pitch_deg,
        body_yaw_deg=candidate.body_yaw_deg,
    )
    expected_sha = _digest(expected_payload)
    if (
        expected_sha != candidate.candidate_sha256
        or candidate.candidate_id != 'tpc-' + expected_sha[:20]
    ):
        raise ValueError('topology placement candidate identity mismatch')

    membership = generate_topology_placement_candidates(
        baseline=baseline,
        template_variant=template_variant,
        spec=spec,
        offset=candidate.feasible_index,
        limit=1,
    )
    if not membership.candidates or membership.candidates[0] != candidate:
        raise ValueError(
            'topology placement candidate is not an exact deterministic search member'
        )

    preview = _candidate_document_from_parts(
        virtual_scene,
        o10_candidate_id=candidate.o10_candidate_id,
        raw_index=candidate.raw_index,
        feasible_index=candidate.feasible_index,
        positions=candidate.positions,
        aim_yaw_deg=candidate.aim_yaw_deg,
        aim_pitch_deg=candidate.aim_pitch_deg,
        body_yaw_deg=candidate.body_yaw_deg,
    )
    if spec.algorithm_version == TOPOLOGY_SEARCH_ALGORITHM_VERSION_V1:
        g10_evaluation = evaluate_constraint_set(
            scene_to_g10_context(virtual_scene),
            json.loads(spec.g10_constraint_spec_json),
            PlacementEvaluationRequest.model_validate({
                'positions': candidate.positions,
            }),
        )
        if not g10_evaluation['feasible']:
            rejection_ids = [
                str(item['constraint_id'])
                for item in g10_evaluation['rejections']
            ]
            raise ValueError(
                'topology placement candidate violates G10 hard constraints: '
                + ', '.join(rejection_ids)
            )
        if candidate.body_yaw_deg:
            constraints = CadConstraintSet.model_validate(
                json.loads(spec.constraint_snapshot_json)
            )
            rejections = orientation_constraint_rejections(
                preview,
                constraints,
                changed_entity_ids=candidate.body_yaw_deg,
            )
            if rejections:
                raise ValueError(
                    'topology placement candidate violates O80 hard constraints: '
                    + ', '.join(rejections)
                )
    else:
        rejections = _final_pose_constraint_rejections(
            preview=preview,
            spec=spec,
            positions=candidate.positions,
            constraint_set=CadConstraintSet.model_validate(
                json.loads(spec.constraint_snapshot_json)
            ),
            prepared_context=prepare_constraint_context(
                scene_to_g10_context(virtual_scene)
            ),
        )
        if rejections:
            raise ValueError(
                'topology placement candidate violates final-pose G10/O80 '
                'hard constraints: ' + ', '.join(rejections)
            )
    return preview


def topology_candidate_to_system_variant(
    *,
    baseline: SceneRevision,
    template_variant: SystemVariant,
    spec: TopologyPlacementSearchSpec,
    candidate: TopologyPlacementCandidate,
    created_at_utc: str,
    name: str | None = None,
) -> SystemVariant:
    """Convert one exact placement into an immutable O100A SystemVariant."""

    candidate_scene = topology_candidate_document(
        baseline=baseline,
        template_variant=template_variant,
        spec=spec,
        candidate=candidate,
    )
    proposal_specs = tuple(
        ProposedEntitySpec(
            spec_id='proposal-o100b-' + _digest({
                'template_spec_id': proposal.spec_id,
                'candidate_sha256': candidate.candidate_sha256,
            })[:20],
            entity=candidate_scene.entity(proposal.entity.entity_id),
            role_binding_id=proposal.role_binding_id,
            provenance=proposal.provenance,
        )
        for proposal in template_variant.proposed_entities
    )
    lifecycle_overrides = tuple(
        item
        for item in template_variant.entity_lifecycle
        if item.state != 'proposed'
    )
    remove_ids = tuple(
        item.entity_id
        for item in template_variant.diff
        if item.kind == 'remove'
    )
    o100b_provenance_keys = {
        'o100b.topology_search_sha256',
        'o100b.topology_option_id',
        'o100b.search_sha256',
        'o100b.algorithm_version',
        'o100b.candidate_id',
        'o100b.candidate_sha256',
    }
    provenance = tuple(
        item
        for item in template_variant.provenance
        if item.key not in o100b_provenance_keys
    ) + (
        VariantProvenanceItem(
            key='o100b.topology_search_sha256',
            value=spec.topology_search_sha256,
        ),
        VariantProvenanceItem(
            key='o100b.topology_option_id',
            value=spec.topology_option_id,
        ),
        VariantProvenanceItem(
            key='o100b.search_sha256',
            value=spec.search_sha256,
        ),
        VariantProvenanceItem(
            key='o100b.algorithm_version',
            value=spec.algorithm_version,
        ),
        VariantProvenanceItem(
            key='o100b.candidate_id',
            value=candidate.candidate_id,
        ),
        VariantProvenanceItem(
            key='o100b.candidate_sha256',
            value=candidate.candidate_sha256,
        ),
    )
    built = build_system_variant(
        baseline=baseline,
        name=name or f'{template_variant.name} / {candidate.candidate_id}',
        role_bindings=template_variant.role_bindings,
        proposed_entities=proposal_specs,
        remove_entity_ids=remove_ids,
        lifecycle_overrides=lifecycle_overrides,
        equipment_bindings=template_variant.equipment_bindings,
        proposal_evidence=template_variant.proposal_evidence,
        provenance=provenance,
        parent_variant_id=template_variant.variant_id,
        created_at_utc=created_at_utc,
    )
    deterministic_id = 'sv-o100b-' + built.variant_sha256[:20]
    payload = built.model_dump(mode='python')
    payload['variant_id'] = deterministic_id
    return SystemVariant.model_validate(payload)
