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
    'OperatingOpeningState',
    'OPERATING_STATE_AUTHORITY_VERSION',
    'OperatingStateFreshness',
    'OperatingStateFreshnessStatus',
    'RoomOperatingState',
    'build_operating_state',
    'evaluate_operating_state_freshness',
]
