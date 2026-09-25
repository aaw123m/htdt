"""Occupancy-conditioned room acoustics authority (#1038).

Occupancy is an exact simulation/measurement condition, not implicit
metadata: a seat's permanent geometry/material authority is never mutated
when a person sits in it, and a ``ListenerPose`` never implies OCCUPIED.

- :class:`SeatOccupancyBinding` — occupancy state of one seat/zone under
  one condition (empty / occupied / unknown / not_applicable), plus the
  optional bounded occupant acoustic proxy (OCC20) with its own provenance
  and validity domain.
- :class:`RoomOccupancyAcousticState` — immutable condition binding the
  exact scene revision and room operating state.
- :func:`evaluate_occupancy_compatibility` — whether a measurement and a
  prediction may be compared: mismatched occupancy is visible
  (INCOMPATIBLE-style FAIL) unless an explicit policy declares the
  observable insensitive; never a hidden normalization.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_video_geometry import EvaluationStatus, _combine_status


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


SeatOccupancy = Literal['empty', 'occupied', 'unknown', 'not_applicable']

OccupantRepresentation = Literal[
    'state_only',
    'bounded_acoustic_proxy',
    'solver_specific_geometry',
]
"""Capability ladder: OCC10 records exact state only; OCC20 allows a
validated bounded proxy (equivalent absorption/scattering); OCC30 is
solver-specific geometry. No human-body solver is implemented here."""


class OccupantAcousticProxy(BaseModel):
    """A bounded occupant acoustic representation — only valid inside its
    declared domain and only with recorded provenance."""

    model_config = ConfigDict(frozen=True)

    representation: OccupantRepresentation
    absorption_profile_ref: str | None = None
    validity_domain: str | None = None
    solver_capability_id: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'OccupantAcousticProxy':
        if self.representation == 'bounded_acoustic_proxy':
            if self.absorption_profile_ref is None:
                raise ValueError(
                    'a bounded acoustic proxy requires an explicit '
                    'absorption/scattering profile ref'
                )
            if self.validity_domain is None:
                raise ValueError(
                    'a bounded acoustic proxy requires a validity domain'
                )
        return self


class SeatOccupancyBinding(BaseModel):
    """Occupancy of one seat/zone entity under one condition."""

    model_config = ConfigDict(frozen=True)

    seat_entity_id: str = Field(min_length=1)
    occupancy: SeatOccupancy
    listener_pose_ref: str | None = None
    occupant_proxy: OccupantAcousticProxy | None = None

    @model_validator(mode='after')
    def _check(self) -> 'SeatOccupancyBinding':
        if self.occupancy != 'occupied' and self.occupant_proxy is not None:
            raise ValueError(
                'an occupant acoustic proxy requires occupancy=occupied'
            )
        return self


class RoomOccupancyAcousticState(BaseModel):
    """Immutable occupancy condition bound to exact scene + operating
    state. Predictions consuming occupancy must include this state's hash
    in their semantic/cache identity."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['room-occupancy-state-1'] = (
        'room-occupancy-state-1'
    )
    state_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    operating_state_id: str | None = None
    seats: tuple[SeatOccupancyBinding, ...] = ()
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    limitations: str | None = None
    state_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'state_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'RoomOccupancyAcousticState':
        seats = [s.seat_entity_id for s in self.seats]
        if len(set(seats)) != len(seats):
            raise ValueError('each seat may bind at most once')
        if self.state_sha256 != _hash(self.semantic_payload()):
            raise ValueError('occupancy state semantic hash mismatch')
        return self


def build_room_occupancy_state(
    *,
    state_id: str,
    version: str,
    scene_revision_id: str,
    scene_content_hash: str,
    operating_state_id: str | None = None,
    seats: tuple[SeatOccupancyBinding, ...] = (),
    provenance: tuple[EquipmentDataProvenance, ...] = (),
    limitations: str | None = None,
) -> RoomOccupancyAcousticState:
    probe = RoomOccupancyAcousticState.model_construct(
        state_id=state_id,
        version=version,
        scene_revision_id=scene_revision_id,
        scene_content_hash=scene_content_hash,
        operating_state_id=operating_state_id,
        seats=tuple(seats),
        provenance=tuple(provenance),
        limitations=limitations,
        state_sha256='',
    )
    return RoomOccupancyAcousticState(
        **probe.model_dump(mode='python', exclude={'state_sha256'}),
        state_sha256=_hash(probe.semantic_payload()),
    )


class OccupancyCompatibilityResult(BaseModel):
    """Whether one measurement and one prediction share an equivalent
    occupancy condition."""

    model_config = ConfigDict(frozen=True)

    status: Literal['compatible', 'incompatible', 'limited', 'unknown']
    reason: str


def evaluate_occupancy_compatibility(
    *,
    prediction_occupancy: RoomOccupancyAcousticState | None,
    measurement_occupancy: RoomOccupancyAcousticState | None,
    observable_declared_insensitive: bool = False,
    insensitivity_policy_id: str | None = None,
) -> OccupancyCompatibilityResult:
    """An old empty-room measurement must not silently validate an
    occupied prediction — and vice versa.

    ``observable_declared_insensitive`` is only honored with an explicit
    ``insensitivity_policy_id``; otherwise it is ignored.
    """
    if prediction_occupancy is None or measurement_occupancy is None:
        return OccupancyCompatibilityResult(
            status='unknown',
            reason='one side binds no occupancy state',
        )
    if (
        prediction_occupancy.state_sha256
        == measurement_occupancy.state_sha256
    ):
        return OccupancyCompatibilityResult(
            status='compatible',
            reason='identical occupancy state',
        )
    if (
        observable_declared_insensitive
        and insensitivity_policy_id is not None
    ):
        return OccupancyCompatibilityResult(
            status='limited',
            reason=f'occupancy differs but the observable is declared '
            f'insensitive under policy {insensitivity_policy_id}',
        )
    return OccupancyCompatibilityResult(
        status='incompatible',
        reason='prediction and measurement bind different occupancy '
        'states; comparison is not valid without an insensitivity policy',
    )


class OccupancyCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    status: EvaluationStatus
    reason: str | None = None


def evaluate_occupancy_state(
    state: RoomOccupancyAcousticState,
    *,
    solver_supports_occupancy: bool,
) -> tuple[OccupancyCheckResult, ...]:
    """Does the chosen solver path actually consume this occupancy state?
    Unsupported occupancy physics stays explicit — never silently
    ignored."""
    checks = [
        OccupancyCheckResult(
            check='occupancy_recorded',
            status='PASS' if state.seats else 'UNKNOWN',
            reason=(
                f'{len(state.seats)} seat bindings recorded'
                if state.seats
                else 'no seat occupancy bindings recorded'
            ),
        ),
        OccupancyCheckResult(
            check='solver_participation',
            status='PASS' if solver_supports_occupancy else 'UNKNOWN',
            reason=(
                'solver consumes occupancy state in prediction identity'
                if solver_supports_occupancy
                else 'solver does not consume occupancy; the recorded '
                'state is preserved but its acoustic effect is UNKNOWN'
            ),
        ),
    ]
    return tuple(checks)
