"""Append-only persistence for the playback dynamics / limiter
authority (#649, REV58-MEASELEC).

Three tables:

* ``cad_playback_dynamics_states`` — sealed measurement-time dynamics
  states (renderer/preset/volume/output-mode + declared mechanism states).
* ``cad_level_sweep_observations`` — sealed level-sweep
  transfer-invariance diagnostics.
* ``cad_playback_dynamics_qualifications`` — sealed fail-closed verdicts
  binding a dynamics state to a measurement purpose.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_playback_dynamics import (
    CadLevelSweepObservation,
    CadPlaybackDynamicsQualification,
    CadPlaybackDynamicsState,
)


class PlaybackDynamicsAuthorityConflictError(ValueError):
    """A playback-dynamics save violated append-only identity rules."""


class PlaybackDynamicsAuthorityIntegrityError(ValueError):
    """A stored playback-dynamics row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise PlaybackDynamicsAuthorityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise PlaybackDynamicsAuthorityIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadPlaybackDynamicsRepository:
    """Native storage for the #649 playback-dynamics authority records."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_playback_dynamics_states',
                'cad_level_sweep_observations',
                'cad_playback_dynamics_qualifications',
            )

    # ------------------------------------------------------------------
    # Dynamics states

    def save_state(self, state: CadPlaybackDynamicsState) -> None:
        _assert_sealed(state, 'state_sha256', 'state_id')
        existing = self.get_state(state.state_id)
        if existing is not None:
            if existing.state_sha256 == state.state_sha256:
                return
            raise PlaybackDynamicsAuthorityConflictError(
                'dynamics states are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_playback_dynamics_states (
                    state_id, state_sha256, document_id,
                    device, output_mode, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state.state_id,
                    state.state_sha256,
                    state.document_id,
                    state.device,
                    state.output_mode,
                    state.declared_at_utc,
                    state.model_dump_json(),
                ),
            )

    def get_state(
        self, state_id: str
    ) -> CadPlaybackDynamicsState | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_playback_dynamics_states '
                'WHERE state_id=?',
                (state_id,),
            ).fetchone()
        if row is None:
            return None
        state = CadPlaybackDynamicsState.model_validate_json(
            row['payload_json']
        )
        if (
            state.state_id != row['state_id']
            or state.state_sha256 != row['state_sha256']
            or state.document_id != row['document_id']
            or state.device != row['device']
            or state.output_mode != row['output_mode']
            or state.declared_at_utc != row['declared_at_utc']
        ):
            raise PlaybackDynamicsAuthorityIntegrityError(
                'dynamics state row disagrees with payload'
            )
        return state

    def list_states(
        self, document_id: str
    ) -> tuple[CadPlaybackDynamicsState, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_playback_dynamics_states '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadPlaybackDynamicsState.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Level-sweep observations

    def save_level_sweep(
        self, observation: CadLevelSweepObservation
    ) -> None:
        _assert_sealed(
            observation, 'observation_sha256', 'observation_id'
        )
        existing = self.get_level_sweep(observation.observation_id)
        if existing is not None:
            if existing.observation_sha256 == observation.observation_sha256:
                return
            raise PlaybackDynamicsAuthorityConflictError(
                'level-sweep observations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_level_sweep_observations (
                    observation_id, observation_sha256, document_id,
                    stimulus_ref_id, verdict, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.observation_sha256,
                    observation.document_id,
                    observation.stimulus_ref.ref_id,
                    observation.verdict,
                    observation.declared_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_level_sweep(
        self, observation_id: str
    ) -> CadLevelSweepObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_level_sweep_observations '
                'WHERE observation_id=?',
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        observation = CadLevelSweepObservation.model_validate_json(
            row['payload_json']
        )
        if (
            observation.observation_id != row['observation_id']
            or observation.observation_sha256 != row['observation_sha256']
            or observation.document_id != row['document_id']
            or observation.stimulus_ref.ref_id != row['stimulus_ref_id']
            or observation.verdict != row['verdict']
            or observation.declared_at_utc != row['declared_at_utc']
        ):
            raise PlaybackDynamicsAuthorityIntegrityError(
                'level-sweep observation row disagrees with payload'
            )
        return observation

    def list_level_sweeps(
        self, document_id: str
    ) -> tuple[CadLevelSweepObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_level_sweep_observations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadLevelSweepObservation.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Dynamics qualifications

    def save_qualification(
        self, qualification: CadPlaybackDynamicsQualification
    ) -> None:
        _assert_sealed(
            qualification, 'qualification_sha256', 'qualification_id'
        )
        existing = self.get_qualification(qualification.qualification_id)
        if existing is not None:
            if (
                existing.qualification_sha256
                == qualification.qualification_sha256
            ):
                return
            raise PlaybackDynamicsAuthorityConflictError(
                'dynamics qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_playback_dynamics_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    dynamics_state_ref_id, purpose, state,
                    evaluation_version, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    (
                        qualification.dynamics_state_ref.ref_id
                        if qualification.dynamics_state_ref is not None
                        else None
                    ),
                    qualification.purpose,
                    qualification.state,
                    qualification.evaluation_version,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> CadPlaybackDynamicsQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_playback_dynamics_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = (
            CadPlaybackDynamicsQualification.model_validate_json(
                row['payload_json']
            )
        )
        state_ref_id = (
            qualification.dynamics_state_ref.ref_id
            if qualification.dynamics_state_ref is not None
            else None
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or state_ref_id != row['dynamics_state_ref_id']
            or qualification.purpose != row['purpose']
            or qualification.state != row['state']
            or qualification.evaluation_version
            != row['evaluation_version']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise PlaybackDynamicsAuthorityIntegrityError(
                'dynamics qualification row disagrees with payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str
    ) -> tuple[CadPlaybackDynamicsQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_playback_dynamics_qualifications '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadPlaybackDynamicsQualification.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadPlaybackDynamicsRepository',
    'PlaybackDynamicsAuthorityConflictError',
    'PlaybackDynamicsAuthorityIntegrityError',
]
