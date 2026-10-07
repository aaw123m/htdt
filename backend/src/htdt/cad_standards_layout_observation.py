"""Scene-layout-derived criterion observations for standards evaluations.

Where a criterion's declared inputs can be computed honestly from the exact
evaluation target's scene geometry — seat listening references, speaker
channel roles, and the room footprint — this lane derives the quantity,
retains it as a ``StandardsObservationAuthority`` (``predicted`` basis,
``scene-layout-derivation-v1`` method), and feeds the evaluator an explicit
``CriterionObservation``.

Derivation conventions are declared, not inferred:

- The listener reference is each seat's ``acoustic_reference_position``;
  seats without one and non-seat receivers never supply listener geometry.
- Listener forward is the seat's local +Y axis rotated into world space
  (the authored "seat faces -Y at yaw 180" convention).
- Signed azimuth is 0 along listener forward, positive toward the
  listener's right — matching the HTDT/Dolby mapping documented in the
  built-in profiles.
- Adjacent surround or upper speakers are consecutive in azimuth order
  around the listener; the reported angle is the smaller arc between the
  pair (and, for RP22 Parameter 9, only same-side row pairs are adjacent —
  Voice-of-God / height-center roles are excluded by the family set).
- Upper-layer speakers are the HTDT ``T``-prefixed top roles.
- Multi-seat scenes report the value with the greatest criterion-rule
  violation (the binding seat); when nothing violates, the value with the
  smallest margin is reported.

Quantities whose honest source is not scene geometry — recommended-zone
membership, upfiring rendering mode, wide/AURO layer membership where
HTDT declares no convention, and every SPL/headroom input — are never
derived: the criterion stays ``UNKNOWN``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from shapely.geometry import Point

from .cad_scene import (
    Position3,
    SceneDocument,
    SceneEntity,
    acoustic_reference_position,
    is_unassigned_speaker_role,
    quaternion_to_matrix3,
    room_vertices,
)
from .cad_standards import (
    CriterionDefinition,
    CriterionObservation,
    ObservedScalar,
    StandardsEvaluationTarget,
    StandardsProfile,
    _normalize_numeric,
)
from .cad_standards_evidence import (
    StandardsObservationAuthority,
    build_standards_observation_authority,
)
from .cad_system_variant import SystemVariant
from .geometry import polygon_from_vertices

if TYPE_CHECKING:
    from .cad_standards_repository import CadStandardsRepository


SCENE_LAYOUT_DERIVATION_METHOD = 'scene-layout-derivation-v1'

# HTDT channel-role families used by the derivation. Only roles whose layer
# membership is unambiguous in HTDT convention are classified; every other
# role id is left unclassified and never contributes an observation.
SURROUND_LAYER_ROLES = frozenset({'SL', 'SR', 'SBL', 'SBR'})
UPPER_LAYER_ROLES = frozenset(
    {'TFL', 'TFR', 'TML', 'TMR', 'TRL', 'TRR', 'TSL', 'TSR'}
)
UPPER_LAYER_LEFT_ROLES = frozenset({'TFL', 'TML', 'TRL', 'TSL'})
UPPER_LAYER_RIGHT_ROLES = frozenset({'TFR', 'TMR', 'TRR', 'TSR'})

_DOLBY_ROLE_BY_CRITERION = {
    'dolby.5.1.2.front-left-azimuth': 'FL',
    'dolby.5.1.2.front-right-azimuth': 'FR',
    'dolby.5.1.2.surround-left-azimuth': 'SL',
    'dolby.5.1.2.surround-right-azimuth': 'SR',
}

# REV63 #805 — elevation criteria measure per-speaker elevation from the
# listener reference over the roles each criterion names.
_DOLBY_ELEVATION_ROLES_BY_CRITERION = {
    'dolby.5.1.2.top-middle-overhead-elevation': frozenset({'TML', 'TMR'}),
}

# Quantity/unit each published criterion id names and this lane measures.
# A criterion reusing one of these ids while declaring a different quantity
# or unit is never served: the lane would otherwise mint an observation
# authority claiming a quantity it did not measure.
_DERIVED_QUANTITY_UNIT = {
    'rp22.p01.listener-boundary-distance': (
        'listener_head_to_nearest_room_boundary',
        'm',
    ),
    'dolby.5.1.2.front-left-azimuth': ('speaker_azimuth_from_mlp', 'deg'),
    'dolby.5.1.2.front-right-azimuth': ('speaker_azimuth_from_mlp', 'deg'),
    'dolby.5.1.2.surround-left-azimuth': ('speaker_azimuth_from_mlp', 'deg'),
    'dolby.5.1.2.surround-right-azimuth': ('speaker_azimuth_from_mlp', 'deg'),
    'dolby.5.1.2.top-middle-overhead-elevation': (
        'overhead_speaker_elevation_from_mlp',
        'deg',
    ),
    'rp22.p05.max-adjacent-surround-horizontal-angle': (
        'adjacent_surround_speaker_horizontal_angle',
        'deg',
    ),
    'rp22.p09.max-adjacent-upper-vertical-angle': (
        'adjacent_upper_speaker_vertical_angle',
        'deg',
    ),
}

# A listener forward direction whose XY norm falls below this is pointing
# essentially straight up/down — its projection is quaternion round-off
# noise, not a facing direction, so no azimuth can be derived from it.
_MIN_FORWARD_XY_NORM = 1e-6

# A speaker whose XY delta from the ear falls below this is co-located with
# the listener: atan2 of a (near-)zero vector reports a fabricated angle.
_MIN_SPEAKER_LISTENER_XY_M = 1e-9


@dataclass(frozen=True, slots=True)
class _ListenerReference:
    """One seat listening reference: ear position plus facing direction."""

    entity_id: str
    position: Position3
    forward_x: float
    forward_y: float


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One candidate observed value with its exact entity binding."""

    value: float
    entity_ids: tuple[str, ...]


def _listener_references(document: SceneDocument) -> tuple[_ListenerReference, ...]:
    references: list[_ListenerReference] = []
    for entity in sorted(document.entities, key=lambda item: item.entity_id):
        if entity.kind != 'seat':
            continue
        reference = acoustic_reference_position(entity)
        if reference is None:
            continue
        matrix = quaternion_to_matrix3(entity.orientation)
        # Seat local +Y is the facing axis (authored seats face -Y at yaw 180).
        forward_x = matrix[0][1]
        forward_y = matrix[1][1]
        norm = math.hypot(forward_x, forward_y)
        if norm < _MIN_FORWARD_XY_NORM:
            continue
        references.append(
            _ListenerReference(
                entity_id=entity.entity_id,
                position=reference,
                forward_x=forward_x / norm,
                forward_y=forward_y / norm,
            )
        )
    return tuple(references)


def _speaker_reference_position(entity: SceneEntity) -> Position3:
    """Acoustic reference when declared; otherwise the placement anchor."""

    reference = acoustic_reference_position(entity)
    return reference if reference is not None else entity.position


def _assigned_speakers(
    document: SceneDocument,
    roles: frozenset[str] | None,
) -> tuple[SceneEntity, ...]:
    return tuple(
        entity
        for entity in sorted(
            document.entities, key=lambda item: item.entity_id
        )
        if entity.kind == 'speaker'
        and not is_unassigned_speaker_role(entity.speaker_role)
        and (roles is None or entity.speaker_role in roles)
    )


def _signed_azimuth_deg(
    reference: _ListenerReference,
    position: Position3,
) -> float | None:
    dx = position.x_m - reference.position.x_m
    dy = position.y_m - reference.position.y_m
    if math.hypot(dx, dy) < _MIN_SPEAKER_LISTENER_XY_M:
        # The speaker is co-located with the listener; its azimuth is
        # undefined, not zero.
        return None
    # Right is forward rotated -90° in the XY plane.
    right_x = -reference.forward_y
    right_y = reference.forward_x
    return math.degrees(
        math.atan2(
            dx * right_x + dy * right_y,
            dx * reference.forward_x + dy * reference.forward_y,
        )
    )


def _elevation_deg(reference: _ListenerReference, position: Position3) -> float:
    dx = position.x_m - reference.position.x_m
    dy = position.y_m - reference.position.y_m
    dz = position.z_m - reference.position.z_m
    return math.degrees(math.atan2(dz, math.hypot(dx, dy)))


def _boundary_distance_m(document: SceneDocument, position: Position3) -> float | None:
    if document.room is None:
        return None
    polygon = polygon_from_vertices(
        [(vertex.x_m, vertex.y_m) for vertex in room_vertices(document.room)]
    )
    point = Point(position.x_m, position.y_m)
    if not polygon.covers(point):
        # A listener outside the room footprint has no listener-to-boundary
        # distance: measuring to the wall would fabricate clearance.
        return None
    return point.distance(polygon.boundary)


def _pairwise_separation_deg(left_deg: float, right_deg: float) -> float:
    """Smaller angular separation between two directions/elevations."""

    difference = abs(left_deg - right_deg) % 360.0
    return min(difference, 360.0 - difference)


def _adjacent_pair_candidates(
    listeners: Sequence[_ListenerReference],
    speakers: Sequence[SceneEntity],
    *,
    per_side: bool,
    value_of,
) -> tuple[_Candidate, ...] | None:
    """Angle between azimuth-consecutive speaker pairs at each listener.

    ``per_side`` restricts adjacency to left/right row members (RP22
    Parameter 9 evaluates L-row and R-row pairs separately and excludes
    center-line upper roles).
    """

    candidates: list[_Candidate] = []
    for listener in listeners:
        measured = [
            (azimuth, speaker)
            for speaker in speakers
            for azimuth in (
                _signed_azimuth_deg(
                    listener, _speaker_reference_position(speaker)
                ),
            )
            if azimuth is not None
        ]
        if per_side:
            left = [item for item in measured if item[1].speaker_role in UPPER_LAYER_LEFT_ROLES]
            right = [item for item in measured if item[1].speaker_role in UPPER_LAYER_RIGHT_ROLES]
            groups = (left, right)
        else:
            groups = (measured,)
        for group in groups:
            ordered = sorted(group, key=lambda item: item[0])
            if len(ordered) < 2:
                continue
            if per_side:
                # L/R rows are linear chains, not rings: the two end
                # speakers of a row are not adjacent to each other.
                pair_indexes = [
                    (index, index + 1)
                    for index in range(len(ordered) - 1)
                ]
            else:
                pair_indexes = [
                    (index, (index + 1) % len(ordered))
                    for index in range(len(ordered))
                ]
                if len(ordered) == 2:
                    pair_indexes = [(0, 1)]
            seen: set[frozenset[str]] = set()
            for first_index, second_index in pair_indexes:
                first, second = ordered[first_index], ordered[second_index]
                key = frozenset(
                    (first[1].entity_id, second[1].entity_id)
                )
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    _Candidate(
                        value=value_of(listener, first, second),
                        entity_ids=tuple(
                            sorted(
                                (
                                    listener.entity_id,
                                    first[1].entity_id,
                                    second[1].entity_id,
                                )
                            )
                        ),
                    )
                )
    return tuple(candidates) if candidates else None


def _criterion_candidates(
    criterion: CriterionDefinition,
    document: SceneDocument,
) -> tuple[_Candidate, ...] | None:
    """Derive candidate observed values for criteria this lane supports."""

    listeners = _listener_references(document)
    criterion_id = criterion.criterion_id

    if criterion_id == 'rp22.p01.listener-boundary-distance':
        if document.room is None:
            return None
        candidates = [
            _Candidate(
                value=distance,
                entity_ids=(listener.entity_id,),
            )
            for listener in listeners
            for distance in (_boundary_distance_m(document, listener.position),)
            if distance is not None
        ]
        return tuple(candidates) if candidates else None

    if not listeners:
        return None

    role = _DOLBY_ROLE_BY_CRITERION.get(criterion_id)
    if role is not None:
        speakers = _assigned_speakers(document, frozenset({role}))
        if not speakers:
            return None
        candidates = [
            _Candidate(
                value=azimuth,
                entity_ids=tuple(
                    sorted((listener.entity_id, speaker.entity_id))
                ),
            )
            for listener in listeners
            for speaker in speakers
            for azimuth in (
                _signed_azimuth_deg(
                    listener, _speaker_reference_position(speaker)
                ),
            )
            if azimuth is not None
        ]
        return tuple(candidates) if candidates else None

    elevation_roles = _DOLBY_ELEVATION_ROLES_BY_CRITERION.get(criterion_id)
    if elevation_roles is not None:
        speakers = _assigned_speakers(document, elevation_roles)
        if not speakers:
            return None
        candidates = [
            _Candidate(
                value=_elevation_deg(
                    listener, _speaker_reference_position(speaker)
                ),
                entity_ids=tuple(
                    sorted((listener.entity_id, speaker.entity_id))
                ),
            )
            for listener in listeners
            for speaker in speakers
        ]
        return tuple(candidates) if candidates else None

    if criterion_id == 'rp22.p05.max-adjacent-surround-horizontal-angle':
        speakers = _assigned_speakers(document, SURROUND_LAYER_ROLES)
        if len(speakers) < 2:
            return None
        return _adjacent_pair_candidates(
            listeners,
            speakers,
            per_side=False,
            value_of=lambda listener, first, second: _pairwise_separation_deg(
                first[0], second[0]
            ),
        )

    if criterion_id == 'rp22.p09.max-adjacent-upper-vertical-angle':
        speakers = _assigned_speakers(document, UPPER_LAYER_ROLES)
        if len(speakers) < 2:
            return None
        return _adjacent_pair_candidates(
            listeners,
            speakers,
            per_side=True,
            value_of=lambda listener, first, second: abs(
                _elevation_deg(
                    listener, _speaker_reference_position(first[1])
                )
                - _elevation_deg(
                    listener, _speaker_reference_position(second[1])
                )
            ),
        )

    return None


def _rule_violation(criterion: CriterionDefinition, value: ObservedScalar) -> float:
    """Signed distance outside the rule interval (negative when inside)."""

    normalized = float(_normalize_numeric(value, criterion.rule))
    rule = criterion.rule
    if rule.operator == 'min':
        assert rule.minimum is not None
        return float(rule.minimum) - normalized
    if rule.operator == 'max':
        assert rule.maximum is not None
        return normalized - float(rule.maximum)
    if rule.operator == 'range':
        assert rule.minimum is not None and rule.maximum is not None
        return max(
            float(rule.minimum) - normalized,
            normalized - float(rule.maximum),
        )
    return float('-inf')


def _binding_candidate(
    criterion: CriterionDefinition,
    candidates: Sequence[_Candidate],
) -> _Candidate:
    """The candidate with the greatest rule violation — the binding seat.

    Deterministic tie-break by entity ids so identical layouts never pick a
    different record.
    """

    ordered = sorted(
        candidates,
        key=lambda candidate: (
            candidate.entity_ids,
            candidate.value,
        ),
    )
    return max(
        ordered,
        key=lambda candidate: _rule_violation(criterion, candidate.value),
    )


def _observation_identity_fields(
    authority: StandardsObservationAuthority,
) -> dict[str, object]:
    """Claim identity excluding the first-observation timestamp.

    Re-deriving the same quantity from the same exact target reuses the
    retained authority (and its original ``observed_at_utc``) instead of
    minting an equivalent record per evaluation run.
    """

    return authority.model_dump(
        mode='json',
        exclude={
            'authority_id',
            'semantic_hash_sha256',
            'observed_at_utc',
        },
    )


def _persist_observation_authority(
    repository: 'CadStandardsRepository',
    *,
    criterion: CriterionDefinition,
    bound: _Candidate,
    target: StandardsEvaluationTarget,
    observed_at_utc: str,
) -> StandardsObservationAuthority:
    for authority in repository.list_observation_authorities():
        if authority.method != SCENE_LAYOUT_DERIVATION_METHOD:
            continue
        fields = _observation_identity_fields(authority)
        if (
            fields['document_id'] == target.document_id
            and fields['scene_revision_id'] == target.scene_revision_id
            and fields['scene_content_hash'] == target.scene_content_hash
            and fields['system_variant_id'] == target.system_variant_id
            and fields['system_variant_sha256'] == target.system_variant_sha256
            and fields['quantity'] == criterion.quantity
            and fields['unit'] == criterion.unit
            and fields['entity_ids'] == sorted(bound.entity_ids)
            and authority.evidence_basis == 'predicted'
            and set(fields['provided_inputs']) == set(criterion.required_inputs)
            and set(fields['capabilities']) == set(criterion.required_capabilities)
            and authority.observed_value == bound.value
        ):
            return authority
    return repository.save_observation_authority(
        build_standards_observation_authority(
            document_id=target.document_id,
            scene_revision_id=target.scene_revision_id,
            scene_content_hash=target.scene_content_hash,
            system_variant_id=target.system_variant_id,
            system_variant_sha256=target.system_variant_sha256,
            quantity=criterion.quantity,
            unit=criterion.unit or '',
            observed_value=bound.value,
            evidence_basis='predicted',
            entity_ids=bound.entity_ids,
            provided_inputs=criterion.required_inputs,
            capabilities=criterion.required_capabilities,
            observed_at_utc=observed_at_utc,
            method=SCENE_LAYOUT_DERIVATION_METHOD,
            note='Derived from the exact scene layout geometry.',
        )
    )


def derive_layout_observations(
    *,
    repository: 'CadStandardsRepository',
    profile: StandardsProfile,
    target: StandardsEvaluationTarget,
    document: SceneDocument,
    observed_at_utc: str,
) -> tuple[CriterionObservation, ...]:
    """Derive layout-geometry observations for the exact evaluation target.

    Each derivable criterion gets one persisted
    ``StandardsObservationAuthority`` (predicted basis,
    ``scene-layout-derivation-v1`` method) bound to the exact
    SceneRevision/SystemVariant and the entities that produced the value,
    plus the ``CriterionObservation`` that consumes it. Underivable criteria
    are left unobserved so they evaluate ``UNKNOWN`` rather than borrowing
    a quantity this lane cannot honestly produce.
    """

    observations: list[CriterionObservation] = []
    for criterion in profile.criteria:
        if criterion.unit is None:
            continue
        if _DERIVED_QUANTITY_UNIT.get(criterion.criterion_id) != (
            criterion.quantity,
            criterion.unit,
        ):
            continue
        candidates = _criterion_candidates(criterion, document)
        if not candidates:
            continue
        bound = _binding_candidate(criterion, candidates)
        authority = _persist_observation_authority(
            repository,
            criterion=criterion,
            bound=bound,
            target=target,
            observed_at_utc=observed_at_utc,
        )
        observations.append(
            CriterionObservation(
                criterion_id=criterion.criterion_id,
                entity_ids=bound.entity_ids,
                observed_value=bound.value,
                unit=criterion.unit,
                evidence_basis='predicted',
                evidence_refs=(authority.ref(),),
                provided_inputs=criterion.required_inputs,
                capabilities=criterion.required_capabilities,
            )
        )
    return tuple(observations)


__all__ = [
    'SCENE_LAYOUT_DERIVATION_METHOD',
    'SURROUND_LAYER_ROLES',
    'UPPER_LAYER_LEFT_ROLES',
    'UPPER_LAYER_RIGHT_ROLES',
    'UPPER_LAYER_ROLES',
    'derive_layout_observations',
]
