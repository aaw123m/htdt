"""Append-only persistence for electrical playback qualifications (#593)."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_electrical_compatibility import ElectricalPlaybackQualification
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite


class ElectricalQualificationConflictError(ValueError):
    """A qualification save violated append-only identity rules."""


class CadElectricalCompatibilityRepository:
    """Native storage for sealed ElectricalPlaybackQualification records.

    ``qualification_id`` is saved exactly once — a re-evaluation appends a
    new sealed record, never an UPDATE. Stored rows can be replayed by
    scenario hash so a drifted scenario always yields a different verdict.
    """

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_electrical_qualifications')

    def save_qualification(
        self, qualification: ElectricalPlaybackQualification
    ) -> None:
        if self.get_qualification(qualification.qualification_id) is not None:
            raise ElectricalQualificationConflictError(
                'ElectricalPlaybackQualification is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_electrical_qualifications (
                    qualification_id, document_id, scenario_sha256,
                    equipment_sha256, verdict, capability_class,
                    qualification_sha256, payload_json, evaluated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.document_id,
                    qualification.scenario.scenario_sha256,
                    qualification.scenario.equipment.semantic_sha256,
                    qualification.verdict,
                    qualification.capability_class,
                    qualification.qualification_sha256,
                    qualification.model_dump_json(),
                    qualification.evaluated_at_utc,
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> ElectricalPlaybackQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_electrical_qualifications
                WHERE qualification_id=?
                """,
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        return ElectricalPlaybackQualification.model_validate_json(
            row['payload_json']
        )

    def get_qualification_by_hash(
        self, qualification_sha256: str
    ) -> ElectricalPlaybackQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_electrical_qualifications
                WHERE qualification_sha256=?
                """,
                (qualification_sha256,),
            ).fetchone()
        if row is None:
            return None
        return ElectricalPlaybackQualification.model_validate_json(
            row['payload_json']
        )

    def list_qualifications(
        self, document_id: str
    ) -> tuple[ElectricalPlaybackQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_electrical_qualifications
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            ElectricalPlaybackQualification.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    def list_for_scenario(
        self, scenario_sha256: str
    ) -> tuple[ElectricalPlaybackQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_electrical_qualifications
                WHERE scenario_sha256=?
                ORDER BY seq ASC
                """,
                (scenario_sha256,),
            ).fetchall()
        return tuple(
            ElectricalPlaybackQualification.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )


__all__ = [
    'CadElectricalCompatibilityRepository',
    'ElectricalQualificationConflictError',
]
