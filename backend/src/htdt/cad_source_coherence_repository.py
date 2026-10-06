"""Append-only persistence for the multi-source coherence authority
(#690, REV58-AUDIOMODEL).

Two tables:

* ``cad_source_coherence_profiles`` — sealed member pins + correlation
  relations.
* ``cad_source_combination_qualifications`` — sealed combination-mode
  verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_source_coherence_authority import (
    SourceCoherenceProfile,
    SourceCombinationQualification,
)


class SourceCoherenceConflictError(ValueError):
    """A source-coherence save violated append-only identity rules."""


class SourceCoherenceIntegrityError(ValueError):
    """A stored source-coherence row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise SourceCoherenceIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise SourceCoherenceIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadSourceCoherenceRepository:
    """Native storage for the #690 multi-source coherence authority."""

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
                'cad_source_coherence_profiles',
                'cad_source_combination_qualifications',
            )

    # ------------------------------------------------------------------
    # Coherence profiles

    def save_profile(self, profile: SourceCoherenceProfile) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise SourceCoherenceConflictError(
                'source coherence profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_source_coherence_profiles (
                    profile_id, profile_sha256, document_id,
                    group_label, member_count, relation_count,
                    default_relation, declared_combination_mode,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.group_label,
                    len(profile.members),
                    len(profile.relations),
                    profile.default_relation,
                    profile.declared_combination_mode,
                    profile.declared_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str
    ) -> SourceCoherenceProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_source_coherence_profiles '
                'WHERE profile_id=?',
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = SourceCoherenceProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.group_label != row['group_label']
            or len(profile.members) != row['member_count']
            or len(profile.relations) != row['relation_count']
            or profile.default_relation != row['default_relation']
            or profile.declared_combination_mode
            != row['declared_combination_mode']
            or profile.declared_at_utc != row['declared_at_utc']
        ):
            raise SourceCoherenceIntegrityError(
                'stored source coherence profile disagrees with its '
                'payload'
            )
        return profile

    def list_profiles(
        self, document_id: str | None = None
    ) -> tuple[SourceCoherenceProfile, ...]:
        query = 'SELECT payload_json FROM cad_source_coherence_profiles'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            SourceCoherenceProfile.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Combination qualifications

    def save_qualification(
        self, qualification: SourceCombinationQualification
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
            raise SourceCoherenceConflictError(
                'source combination qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_source_combination_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    profile_ref_id, verdict, requested_mode,
                    effective_mode, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.profile_ref.ref_id,
                    qualification.verdict,
                    qualification.requested_mode,
                    qualification.effective_mode,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> SourceCombinationQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_source_combination_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = SourceCombinationQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.profile_ref.ref_id != row['profile_ref_id']
            or qualification.verdict != row['verdict']
            or qualification.requested_mode != row['requested_mode']
            or qualification.effective_mode != row['effective_mode']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise SourceCoherenceIntegrityError(
                'stored source combination qualification disagrees '
                'with its payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str | None = None
    ) -> tuple[SourceCombinationQualification, ...]:
        query = (
            'SELECT payload_json FROM '
            'cad_source_combination_qualifications'
        )
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            SourceCombinationQualification.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )
