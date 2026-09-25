"""Room operating-state authority (#556).

Curtains, doors, HVAC and movable equipment change the acoustic/physical
meaning of "the room" between sessions. This module records one named,
versioned operating state — e.g. "evening movie, curtains closed" — as a
first-class authority pinned to an exact SceneRevision, so presets,
calibration, prediction and measurement can reference *which room
configuration* was in effect instead of assuming the static geometry is the
whole story.

Contract properties:

- state fields are explicit authority fields, never implicit defaults
  re-derived from elsewhere: each ``CurtainState`` / ``OpeningState`` /
  ``MovableConfiguration`` names its target by id and its condition;
  ``hvac`` is an explicit on/off fact;
- state is saved with an observed timestamp — "when this configuration was
  in effect" is part of the record;
- the record pins the SceneRevision content hash and references opening /
  configuration ids — :func:`evaluate_operating_state_freshness` reports
  ``current`` / ``stale`` / ``missing`` read-only, never mutating the saved
  record;
- presets and measurement sessions may reference a state by
  ``state_id`` + ``semantic_sha256`` (``PresetComponentRef`` kind
  ``room_operating_state``); nothing here silently binds states to outputs.
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


OPERATING_STATE_AUTHORITY_VERSION = 'room-operating-state-1'

OperatingStateFreshnessStatus = Literal['current', 'stale', 'missing']


def _canonical(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: Any) -> str:
    return sha256(_canonical(payload).encode('utf-8')).hexdigest()


class CurtainState(BaseModel):
    """Coverage of one named curtain/blind at observation time.

    ``opening_id`` may bind the covering to a wall opening it shades;
    ``coverage_fraction`` is 0 (fully open) → 1 (fully closed).
    """

    model_config = ConfigDict(frozen=True)

    curtain_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    coverage_fraction: float = Field(ge=0.0, le=1.0)
    opening_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def finite_coverage(self) -> 'CurtainState':
        if not isfinite(float(self.coverage_fraction)):
            raise ValueError('curtain coverage must be finite')
        return self


class OperatingOpeningState(BaseModel):
    """Observed state of one wall opening (door/window/passage)."""

    model_config = ConfigDict(frozen=True)

    opening_id: str = Field(min_length=1)
    is_open: bool


class HvacState(BaseModel):
    """Observed HVAC/air-handling condition — explicit, never assumed."""

    model_config = ConfigDict(frozen=True)

    is_on: bool
    description: str | None = None


class MovableConfiguration(BaseModel):
    """One named movable configuration in effect (e.g. deployable screen
    down, movable absorber panels in place, seating row extended)."""

    model_config = ConfigDict(frozen=True)

    configuration_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    in_effect: bool = True
    description: str | None = None


class RoomOperatingState(BaseModel):
    """Versioned room operating-state record pinned to one SceneRevision."""

    model_config = ConfigDict(frozen=True)

    authority_version: Literal[
        'room-operating-state-1'
    ] = OPERATING_STATE_AUTHORITY_VERSION
    state_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    curtains: tuple[CurtainState, ...] = ()
    openings: tuple[OperatingOpeningState, ...] = ()
    hvac: HvacState | None = None
    movable_configurations: tuple[MovableConfiguration, ...] = ()
    notes: str | None = None
    #: When this configuration was observed in effect.
    observed_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_state(self) -> 'RoomOperatingState':
        curtain_ids = [item.curtain_id for item in self.curtains]
        if len(set(curtain_ids)) != len(curtain_ids):
            raise ValueError('curtain ids must be unique within a state')
        opening_ids = [item.opening_id for item in self.openings]
        if len(set(opening_ids)) != len(opening_ids):
            raise ValueError('opening ids must be unique within a state')
        config_ids = [
            item.configuration_id for item in self.movable_configurations
        ]
        if len(set(config_ids)) != len(config_ids):
            raise ValueError(
                'movable configuration ids must be unique within a state'
            )
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('RoomOperatingState semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'state_id': self.state_id,
            'version': self.version,
            'document_id': self.document_id,
            'name': self.name,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'curtains': [
                item.model_dump(mode='json') for item in self.curtains
            ],
            'openings': [
                item.model_dump(mode='json') for item in self.openings
            ],
            'hvac': (
                None if self.hvac is None else self.hvac.model_dump(mode='json')
            ),
            'movable_configurations': [
                item.model_dump(mode='json')
                for item in self.movable_configurations
            ],
            'notes': self.notes,
            'observed_at_utc': self.observed_at_utc,
        }


def build_operating_state(
    *,
    document_id: str,
    scene_revision_id: str,
    scene_content_hash: str,
    name: str,
    observed_at_utc: str,
    state_id: str | None = None,
    version: str = '1',
    curtains: Sequence[CurtainState] = (),
    openings: Sequence[OperatingOpeningState] = (),
    hvac: HvacState | None = None,
    movable_configurations: Sequence[MovableConfiguration] = (),
    notes: str | None = None,
) -> RoomOperatingState:
    payload: dict[str, Any] = {
        'authority_version': OPERATING_STATE_AUTHORITY_VERSION,
        'state_id': state_id or str(uuid4()),
        'version': version,
        'document_id': document_id,
        'name': name,
        'scene_revision_id': scene_revision_id,
        'scene_content_hash': scene_content_hash,
        'curtains': tuple(curtains),
        'openings': tuple(openings),
        'hvac': hvac,
        'movable_configurations': tuple(movable_configurations),
        'notes': notes,
        'observed_at_utc': observed_at_utc,
    }
    provisional = RoomOperatingState.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return RoomOperatingState(
        **payload,
        semantic_sha256=_hash(provisional.semantic_payload()),
    )


OperatingStateDomainEffect = Literal[
    'portal_topology',
    'unbound_opening',
    'unknown_effect_no_material_authority',
    'no_transfer_change',
    'declared_not_consumed',
    'none_declared',
]


class OperatingOpeningConsumption(BaseModel):
    """How one declared opening state binds to the scene's wall topology."""

    model_config = ConfigDict(frozen=True)

    opening_id: str = Field(min_length=1)
    is_open: bool
    wall_opening_bound: bool
    persisted_is_open: bool | None = None
    effect: Literal['portal_topology', 'unbound_opening']


class OperatingStateConsumption(BaseModel):
    """Typed view of which domains of a ``RoomOperatingState`` a solver
    actually consumes (#941).

    Domain policy is explicit and fail-closed:

    - ``openings`` bind onto ``document.wall_topology`` openings: a matched
      door/passage open/closed state is a real portal/topology input
      (``portal_topology``); an opening id absent from the wall topology is
      ``unbound_opening`` rather than silently ignored;
    - ``curtains`` carry coverage only — with no exact material/placement
      authority their acoustic effect is ``unknown_effect_no_material_authority``;
    - ``hvac`` is recorded but never alters deterministic transfer: its
      effect is ``no_transfer_change``;
    - ``movable_configurations`` are ``declared_not_consumed`` until a
      treatment/placement authority exists to compose them.
    """

    model_config = ConfigDict(frozen=True)

    state_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    name: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    observed_at_utc: str = Field(min_length=1)
    openings: tuple[OperatingOpeningConsumption, ...] = ()
    curtain_effect: OperatingStateDomainEffect = 'none_declared'
    hvac_effect: OperatingStateDomainEffect = 'none_declared'
    movable_effect: OperatingStateDomainEffect = 'none_declared'
    consumed_domains: tuple[str, ...] = ()


def compile_operating_state_consumption(
    state: RoomOperatingState,
    document: Any,
) -> OperatingStateConsumption:
    """Resolve which parts of an operating state a prediction consumes.

    ``document`` is the ``SceneDocument`` of the exact SceneRevision the
    state pins — callers must check the pinned revision themselves. Openings
    are matched against ``document.wall_topology``; everything else reports
    its declared domain effect without inventing physics.
    """

    topology = getattr(document, 'wall_topology', None)
    known_openings = (
        {}
        if topology is None
        else {opening.opening_id: opening for opening in topology.openings}
    )
    opening_consumptions: list[OperatingOpeningConsumption] = []
    for opening in state.openings:
        wall_opening = known_openings.get(opening.opening_id)
        opening_consumptions.append(
            OperatingOpeningConsumption(
                opening_id=opening.opening_id,
                is_open=opening.is_open,
                wall_opening_bound=wall_opening is not None,
                persisted_is_open=(
                    None if wall_opening is None else wall_opening.is_open
                ),
                effect=(
                    'portal_topology'
                    if wall_opening is not None
                    else 'unbound_opening'
                ),
            )
        )
    consumed_domains: tuple[str, ...] = (
        ('openings',)
        if any(item.wall_opening_bound for item in opening_consumptions)
        else ()
    )
    return OperatingStateConsumption(
        state_id=state.state_id,
        version=state.version,
        semantic_sha256=state.semantic_sha256,
        name=state.name,
        document_id=state.document_id,
        scene_revision_id=state.scene_revision_id,
        observed_at_utc=state.observed_at_utc,
        openings=tuple(opening_consumptions),
        curtain_effect=(
            'unknown_effect_no_material_authority'
            if state.curtains
            else 'none_declared'
        ),
        hvac_effect=(
            'no_transfer_change' if state.hvac is not None else 'none_declared'
        ),
        movable_effect=(
            'declared_not_consumed'
            if state.movable_configurations
            else 'none_declared'
        ),
        consumed_domains=consumed_domains,
    )


class OperatingStateFreshness(BaseModel):
    """Read-only drift check for one persisted state (never rewrites it)."""

    model_config = ConfigDict(frozen=True)

    state_id: str
    version: str
    status: OperatingStateFreshnessStatus
    reasons: tuple[str, ...] = ()


def evaluate_operating_state_freshness(
    state: RoomOperatingState,
    *,
    scene_content_hash: str,
    present_opening_ids: Sequence[str],
) -> OperatingStateFreshness:
    """Report whether a saved state still maps onto the current room.

    ``stale`` = the pinned scene hash moved on; ``missing`` = a referenced
    opening id is absent. The saved record is never rewritten — consumers
    surface UNKNOWN rather than extrapolate a stale configuration.
    """

    reasons: list[str] = []
    status: OperatingStateFreshnessStatus = 'current'
    if state.scene_content_hash != scene_content_hash:
        status = 'stale'
        reasons.append(
            'scene content hash changed since the state was recorded'
        )
    opening_ids = set(present_opening_ids)
    for opening in state.openings:
        if opening.opening_id not in opening_ids:
            status = 'missing'
            reasons.append(f'opening absent: {opening.opening_id}')
    for curtain in state.curtains:
        if (
            curtain.opening_id is not None
            and curtain.opening_id not in opening_ids
        ):
            status = 'missing'
            reasons.append(
                f'curtain opening absent: {curtain.opening_id}'
            )
    return OperatingStateFreshness(
        state_id=state.state_id,
        version=state.version,
        status=status,
        reasons=tuple(reasons),
    )


__all__ = [
    'CurtainState',
    'HvacState',
    'MovableConfiguration',
    'OperatingOpeningConsumption',
    'OperatingOpeningState',
    'OPERATING_STATE_AUTHORITY_VERSION',
    'OperatingStateConsumption',
    'OperatingStateDomainEffect',
    'OperatingStateFreshness',
    'OperatingStateFreshnessStatus',
    'RoomOperatingState',
    'build_operating_state',
    'compile_operating_state_consumption',
    'evaluate_operating_state_freshness',
]
