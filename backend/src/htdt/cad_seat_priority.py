"""Seat-priority (listening population) authority (#513).

An explicit immutable profile binds an ordered set of seat entity IDs to one
exact SceneRevision. Members carry a semantic role (primary/secondary/
diagnostic), a required flag (hard-constraint membership) and a raw
non-negative weight. Weights are relative importance only — never
probabilities — and normalize deterministically by ``sum_to_one``.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
import sqlite3
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_repository import SceneRepository
from .cad_schema import ensure_native_schema, require_native_tables
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest


SEAT_PRIORITY_SCHEMA_VERSION = 1
SEAT_PRIORITY_AUTHORITY_VERSION = 'seat-priority-profile-1'
SEAT_PRIORITY_NORMALIZATION = 'sum_to_one'
SEAT_PRIORITY_NORMALIZATION_VERSION = 'normalize-sum-to-one-v1'

SeatPriorityRole = Literal['primary', 'secondary', 'diagnostic']






def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}:{digest[:24]}'


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SeatPriorityMember(BaseModel):
    """One seat's semantic priority inside a listening population."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    seat_entity_id: str = Field(min_length=1)
    seat_role: SeatPriorityRole
    # ``required`` marks hard-constraint membership: a required seat keeps
    # worst-seat/minimum semantics regardless of its soft weight.
    required: bool = True
    # Raw relative importance; evaluators normalize deterministically.
    weight: float = Field(default=1.0, gt=0.0)

    @field_validator('weight')
    @classmethod
    def finite_weight(cls, value: float) -> float:
        number = float(value)
        if not isfinite(number):
            raise ValueError('seat priority weight must be finite')
        return number


class SeatPriorityProfile(BaseModel):
    """Versioned immutable listening-population authority.

    ``members`` order is part of the identity. ``normalized_weights`` is the
    soft-objective projection: it covers primary/secondary (non-diagnostic)
    members regardless of ``required`` — soft importance is independent of
    hard-constraint membership (#975). Diagnostic members never enter the
    soft weights but may still appear in per-seat evidence; the hard floor
    is governed by ``required_seat_entity_ids`` alone.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = SEAT_PRIORITY_SCHEMA_VERSION
    authority_version: Literal[
        'seat-priority-profile-1'
    ] = SEAT_PRIORITY_AUTHORITY_VERSION
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    members: tuple[SeatPriorityMember, ...] = Field(min_length=1)
    weight_normalization: Literal['sum_to_one'] = SEAT_PRIORITY_NORMALIZATION
    normalization_version: Literal[
        'normalize-sum-to-one-v1'
    ] = SEAT_PRIORITY_NORMALIZATION_VERSION
    profile_id: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_profile(self) -> 'SeatPriorityProfile':
        ids = [item.seat_entity_id for item in self.members]
        if ids != sorted(set(ids)):
            raise ValueError('seat priority members must be unique and sorted')
        if not any(item.required for item in self.members):
            raise ValueError('seat priority profile requires at least one required seat')
        digest = _digest(self.identity_payload())
        if self.profile_sha256 != digest:
            raise ValueError('seat priority profile semantic hash mismatch')
        if self.profile_id != _semantic_id('seat-priority', digest):
            raise ValueError('seat priority profile ID does not match semantic hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'members': [item.model_dump(mode='json') for item in self.members],
            'weight_normalization': self.weight_normalization,
            'normalization_version': self.normalization_version,
        }

    @property
    def seat_entity_ids(self) -> tuple[str, ...]:
        return tuple(item.seat_entity_id for item in self.members)

    @property
    def required_seat_entity_ids(self) -> tuple[str, ...]:
        """Hard-constraint membership: seats the worst-seat floor covers."""
        return tuple(
            item.seat_entity_id for item in self.members if item.required
        )

    @property
    def soft_objective_seat_entity_ids(self) -> tuple[str, ...]:
        """Soft-weighted objective population: non-diagnostic members only,
        independent of ``required`` (#975)."""
        return tuple(
            item.seat_entity_id
            for item in self.members
            if item.seat_role != 'diagnostic'
        )

    @property
    def diagnostic_seat_entity_ids(self) -> tuple[str, ...]:
        return tuple(
            item.seat_entity_id
            for item in self.members
            if item.seat_role == 'diagnostic'
        )

    def member(self, seat_entity_id: str) -> SeatPriorityMember | None:
        return next(
            (item for item in self.members if item.seat_entity_id == seat_entity_id),
            None,
        )

    def normalized_weights(self) -> dict[str, float]:
        """Deterministic relative importance over the soft-objective
        population (primary/secondary members; #975).

        ``required=False`` never zeroes soft importance — an optional
        secondary seat still compromises the weighted objective. Diagnostic
        members are evidence-only and are always excluded; the result sums
        to 1.0. This is a weighting policy — never a probability
        distribution.
        """
        soft = [item for item in self.members if item.seat_role != 'diagnostic']
        total = sum(float(item.weight) for item in soft)
        if total <= 0.0 or not isfinite(total):
            raise ValueError(
                'seat priority profile has no soft-objective members'
            )
        return {
            item.seat_entity_id: float(item.weight) / total
            for item in soft
        }


def build_seat_priority_profile(
    *,
    scene_repository: SceneRepository,
    document_id: str,
    members: Sequence[SeatPriorityMember],
) -> SeatPriorityProfile:
    """Bind an explicit ordered seat population to the document's head revision."""
    revision = scene_repository.current_head(document_id)
    if revision is None:
        raise ValueError('seat priority profile requires a saved scene revision')
    entity_kinds = {
        entity.entity_id: entity.kind for entity in revision.document.entities
    }
    member_tuple = tuple(members)
    for member in member_tuple:
        kind = entity_kinds.get(member.seat_entity_id)
        if kind is None:
            raise ValueError(
                f'seat priority member {member.seat_entity_id} is missing from '
                'the current scene revision'
            )
        if kind != 'seat':
            raise ValueError(
                f'seat priority member {member.seat_entity_id} must be a seat '
                'entity'
            )
    identity = {
        'schema_version': SEAT_PRIORITY_SCHEMA_VERSION,
        'authority_version': SEAT_PRIORITY_AUTHORITY_VERSION,
        'document_id': document_id,
        'scene_revision_id': revision.revision_id,
        'scene_content_hash': revision.content_hash,
        'members': [item.model_dump(mode='json') for item in member_tuple],
        'weight_normalization': SEAT_PRIORITY_NORMALIZATION,
        'normalization_version': SEAT_PRIORITY_NORMALIZATION_VERSION,
    }
    digest = _digest(identity)
    return SeatPriorityProfile(
        document_id=document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        members=member_tuple,
        profile_id=_semantic_id('seat-priority', digest),
        profile_sha256=digest,
    )


class CadSeatPriorityProfileRepository:
    """Append-only seat-priority profile store with replay validation."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = Path(scene_repository.path)
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            require_native_tables(
                connection,
                'cad_seat_priority_profiles',
            )


    def _validate(self, profile: SeatPriorityProfile) -> SeatPriorityProfile:
        profile = SeatPriorityProfile.model_validate(
            profile.model_dump(mode='python')
        )
        revision = self.scene_repository.get(profile.scene_revision_id)
        if revision is None:
            raise ValueError('seat priority profile SceneRevision disappeared')
        if revision.document_id != profile.document_id:
            raise ValueError('seat priority profile document mismatch')
        if revision.content_hash != profile.scene_content_hash:
            raise ValueError('seat priority profile SceneRevision hash mismatch')
        entity_kinds = {
            entity.entity_id: entity.kind
            for entity in revision.document.entities
        }
        for member in profile.members:
            if entity_kinds.get(member.seat_entity_id) != 'seat':
                raise ValueError(
                    'seat priority member is missing from the bound '
                    'SceneRevision or is not a seat'
                )
        return profile

    def save(self, profile: SeatPriorityProfile) -> SeatPriorityProfile:
        profile = self._validate(profile)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT payload_json FROM cad_seat_priority_profiles '
                'WHERE profile_id=?',
                (profile.profile_id,),
            ).fetchone()
            if existing is not None:
                persisted = SeatPriorityProfile.model_validate_json(
                    existing['payload_json']
                )
                if persisted != profile:
                    raise ValueError(
                        'seat priority profile id has different semantics'
                    )
                return self._validate(persisted)
            recorded_at = _utc_now()
            connection.execute(
                """
                INSERT INTO cad_seat_priority_profiles(
                    profile_id, profile_sha256, document_id,
                    scene_revision_id, payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.scene_revision_id,
                    profile.model_dump_json(),
                    recorded_at,
                ),
            )
        return profile

    def get(self, profile_id: str) -> SeatPriorityProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_seat_priority_profiles '
                'WHERE profile_id=?',
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate(
            SeatPriorityProfile.model_validate_json(row['payload_json'])
        )

    def list_for_document(
        self,
        document_id: str,
    ) -> tuple[SeatPriorityProfile, ...]:
        with closing(self._connect()) as connection:
            if not self._table_exists(connection):
                return ()
            rows = connection.execute(
                'SELECT payload_json FROM cad_seat_priority_profiles '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            self._validate(
                SeatPriorityProfile.model_validate_json(row['payload_json'])
            )
            for row in rows
        )

    def _table_exists(self, connection: sqlite3.Connection) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            ('cad_seat_priority_profiles',),
        ).fetchone() is not None
