"""Append-only persistence for the external standards registry (#599).

Five authorities live here:

* ``cad_external_standard_documents`` — immutable document identities
  keyed by ``registry_key`` (``standard_id@edition``). Re-saving an
  identical row is a no-op; a divergent row for the same key is a
  conflict — a registered edition is never silently revised. A lifecycle
  correction is recorded by appending a
  :class:`StandardLifecycleObservation`, never by rewriting the document
  row.
* ``cad_standard_lifecycle_observations`` — source-tiered lifecycle
  claims; conflicts stay visible.
* ``cad_standard_profile_mappings`` — HTDT mapping revisions per
  (standard_id, edition); a mapping must reference a persisted document.
* ``cad_standard_evaluation_pins`` — sealed evaluation pins (historical
  immutability).
* ``cad_standard_revision_diffs`` — structured edition-to-edition diffs.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_schema import require_native_tables, connect_sqlite
from .cad_external_standards import (
    ExternalStandardDocument,
    StandardLifecycleObservation,
    StandardProfileMapping,
    StandardsEvaluationPin,
    StandardsRevisionDiff,
)
from .cad_repository import SceneRepository


class ExternalStandardsConflictError(ValueError):
    """A registry save violated append-only identity rules."""


class ExternalStandardsIntegrityError(ValueError):
    """A stored row disagreed with its payload or references."""


class CadExternalStandardsRepository:
    """Native storage for external-standard documents, lifecycle
    observations, profile mappings, evaluation pins and revision diffs."""

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
                'cad_external_standard_documents',
                'cad_standard_lifecycle_observations',
                'cad_standard_profile_mappings',
                'cad_standard_evaluation_pins',
                'cad_standard_revision_diffs',
            )

    # ------------------------------------------------------------------
    # Documents

    def save_document(self, document: ExternalStandardDocument) -> None:
        existing = self.get_document(
            document.standard_id, document.edition
        )
        if existing is not None:
            if existing.document_sha == document.document_sha:
                return
            raise ExternalStandardsConflictError(
                'external standard documents are append-only — a '
                'lifecycle/status correction is recorded by a new '
                'lifecycle observation, never by rewriting the '
                'registered edition'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_external_standard_documents (
                    registry_key, standard_id, edition, document_number,
                    publisher, lifecycle, admission, rights, replaced_by,
                    registered_at_utc, document_sha, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document.registry_key,
                    document.standard_id,
                    document.edition,
                    document.document_number,
                    document.publisher,
                    document.lifecycle,
                    document.admission,
                    document.rights,
                    document.replaced_by,
                    document.registered_at_utc,
                    document.document_sha,
                    document.model_dump_json(),
                ),
            )

    def get_document(
        self, standard_id: str, edition: str
    ) -> ExternalStandardDocument | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT registry_key, standard_id, edition, lifecycle,
                       admission, payload_json
                FROM cad_external_standard_documents
                WHERE registry_key=?
                """,
                (f'{standard_id}@{edition}',),
            ).fetchone()
        if row is None:
            return None
        return self._document_from_row(row)

    def get_document_by_key(
        self, registry_key: str
    ) -> ExternalStandardDocument | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT registry_key, standard_id, edition, lifecycle,
                       admission, payload_json
                FROM cad_external_standard_documents
                WHERE registry_key=?
                """,
                (registry_key,),
            ).fetchone()
        if row is None:
            return None
        return self._document_from_row(row)

    def list_documents(
        self, standard_id: str | None = None
    ) -> tuple[ExternalStandardDocument, ...]:
        sql = (
            'SELECT registry_key, standard_id, edition, lifecycle, '
            'admission, payload_json FROM cad_external_standard_documents'
        )
        params: tuple[str, ...] = ()
        if standard_id is not None:
            sql += ' WHERE standard_id=?'
            params = (standard_id,)
        sql += ' ORDER BY standard_id, edition'
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, params).fetchall()
        return tuple(self._document_from_row(row) for row in rows)

    def _document_from_row(
        self, row: sqlite3.Row
    ) -> ExternalStandardDocument:
        document = ExternalStandardDocument.model_validate_json(
            row['payload_json']
        )
        if (
            document.registry_key != row['registry_key']
            or document.standard_id != row['standard_id']
            or document.edition != row['edition']
            or document.lifecycle != row['lifecycle']
            or document.admission != row['admission']
        ):
            raise ExternalStandardsIntegrityError(
                'external standard document row disagrees with its payload'
            )
        return document

    # ------------------------------------------------------------------
    # Lifecycle observations

    def save_observation(
        self, observation: StandardLifecycleObservation
    ) -> None:
        existing = self.get_observation(observation.observation_id)
        if existing is not None:
            if existing.observation_sha256 == observation.observation_sha256:
                return
            raise ExternalStandardsConflictError(
                'lifecycle observations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_standard_lifecycle_observations (
                    observation_id, observation_sha256, standard_id,
                    edition, claimed_lifecycle, source_tier,
                    observed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.observation_sha256,
                    observation.standard_id,
                    observation.edition,
                    observation.claimed_lifecycle,
                    observation.source_tier,
                    observation.observed_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_observation(
        self, observation_id: str
    ) -> StandardLifecycleObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT observation_id, observation_sha256, standard_id,
                       edition, claimed_lifecycle, payload_json
                FROM cad_standard_lifecycle_observations
                WHERE observation_id=?
                """,
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        return self._observation_from_row(row)

    def observations_for(
        self, standard_id: str, edition: str | None = None
    ) -> tuple[StandardLifecycleObservation, ...]:
        sql = (
            'SELECT observation_id, observation_sha256, standard_id, '
            'edition, claimed_lifecycle, payload_json FROM '
            'cad_standard_lifecycle_observations WHERE standard_id=?'
        )
        params: tuple[str, ...] = (standard_id,)
        if edition is not None:
            sql += ' AND edition=?'
            params = (standard_id, edition)
        sql += ' ORDER BY observed_at_utc, observation_id'
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, params).fetchall()
        return tuple(self._observation_from_row(row) for row in rows)

    def _observation_from_row(
        self, row: sqlite3.Row
    ) -> StandardLifecycleObservation:
        observation = StandardLifecycleObservation.model_validate_json(
            row['payload_json']
        )
        if (
            observation.observation_id != row['observation_id']
            or observation.observation_sha256 != row['observation_sha256']
            or observation.standard_id != row['standard_id']
            or observation.edition != row['edition']
            or observation.claimed_lifecycle != row['claimed_lifecycle']
        ):
            raise ExternalStandardsIntegrityError(
                'lifecycle observation row disagrees with its payload'
            )
        return observation

    # ------------------------------------------------------------------
    # Profile mappings

    def save_mapping(self, mapping: StandardProfileMapping) -> None:
        existing = self.get_mapping(mapping.mapping_id)
        if existing is not None:
            if existing.mapping_sha256 == mapping.mapping_sha256:
                return
            raise ExternalStandardsConflictError(
                'profile mappings are append-only'
            )
        if (
            self.get_document(mapping.standard_id, mapping.edition)
            is None
        ):
            raise ExternalStandardsIntegrityError(
                'a profile mapping must reference a persisted document — '
                'mapping an unregistered edition is not allowed'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_standard_profile_mappings (
                    mapping_id, mapping_sha256, standard_id, edition,
                    mapping_version, calculation_version, created_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mapping.mapping_id,
                    mapping.mapping_sha256,
                    mapping.standard_id,
                    mapping.edition,
                    mapping.mapping_version,
                    mapping.calculation_version,
                    mapping.created_at_utc,
                    mapping.model_dump_json(),
                ),
            )

    def get_mapping(
        self, mapping_id: str
    ) -> StandardProfileMapping | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT mapping_id, mapping_sha256, standard_id, edition,
                       mapping_version, payload_json
                FROM cad_standard_profile_mappings
                WHERE mapping_id=?
                """,
                (mapping_id,),
            ).fetchone()
        if row is None:
            return None
        return self._mapping_from_row(row)

    def mapping_for(
        self, standard_id: str, edition: str
    ) -> StandardProfileMapping | None:
        """Latest mapping for one edition (by creation order)."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT mapping_id, mapping_sha256, standard_id, edition,
                       mapping_version, payload_json
                FROM cad_standard_profile_mappings
                WHERE standard_id=? AND edition=?
                ORDER BY created_at_utc DESC, mapping_id DESC
                LIMIT 1
                """,
                (standard_id, edition),
            ).fetchone()
        if row is None:
            return None
        return self._mapping_from_row(row)

    def list_mappings(
        self, standard_id: str | None = None
    ) -> tuple[StandardProfileMapping, ...]:
        sql = (
            'SELECT mapping_id, mapping_sha256, standard_id, edition, '
            'mapping_version, payload_json FROM '
            'cad_standard_profile_mappings'
        )
        params: tuple[str, ...] = ()
        if standard_id is not None:
            sql += ' WHERE standard_id=?'
            params = (standard_id,)
        sql += ' ORDER BY standard_id, edition, created_at_utc'
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, params).fetchall()
        return tuple(self._mapping_from_row(row) for row in rows)

    def _mapping_from_row(
        self, row: sqlite3.Row
    ) -> StandardProfileMapping:
        mapping = StandardProfileMapping.model_validate_json(
            row['payload_json']
        )
        if (
            mapping.mapping_id != row['mapping_id']
            or mapping.mapping_sha256 != row['mapping_sha256']
            or mapping.standard_id != row['standard_id']
            or mapping.edition != row['edition']
            or mapping.mapping_version != row['mapping_version']
        ):
            raise ExternalStandardsIntegrityError(
                'profile mapping row disagrees with its payload'
            )
        return mapping

    # ------------------------------------------------------------------
    # Evaluation pins

    def save_pin(self, pin: StandardsEvaluationPin) -> None:
        existing = self.get_pin(pin.pin_id)
        if existing is not None:
            if existing.pin_sha256 == pin.pin_sha256:
                return
            raise ExternalStandardsConflictError(
                'evaluation pins are append-only'
            )
        if (
            self.get_document(pin.standard_id, pin.edition) is None
        ):
            raise ExternalStandardsIntegrityError(
                'an evaluation pin must reference a persisted document — '
                'pinning an unregistered edition is not allowed'
            )
        if pin.mapping_id is not None:
            mapping = self.get_mapping(pin.mapping_id)
            if mapping is None:
                raise ExternalStandardsIntegrityError(
                    'an evaluation pin must reference a persisted mapping'
                )
            if (
                mapping.standard_id != pin.standard_id
                or mapping.edition != pin.edition
            ):
                raise ExternalStandardsIntegrityError(
                    'pin mapping does not match the pinned edition'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_standard_evaluation_pins (
                    pin_id, pin_sha256, document_id, standard_id,
                    edition, mapping_id, result, evaluated_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pin.pin_id,
                    pin.pin_sha256,
                    pin.document_id,
                    pin.standard_id,
                    pin.edition,
                    pin.mapping_id,
                    pin.result,
                    pin.evaluated_at_utc,
                    pin.model_dump_json(),
                ),
            )

    def get_pin(
        self, pin_id: str
    ) -> StandardsEvaluationPin | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT pin_id, pin_sha256, document_id, standard_id,
                       edition, result, payload_json
                FROM cad_standard_evaluation_pins
                WHERE pin_id=?
                """,
                (pin_id,),
            ).fetchone()
        if row is None:
            return None
        return self._pin_from_row(row)

    def pins_for_standard(
        self, standard_id: str, edition: str | None = None
    ) -> tuple[StandardsEvaluationPin, ...]:
        sql = (
            'SELECT pin_id, pin_sha256, document_id, standard_id, '
            'edition, result, payload_json FROM '
            'cad_standard_evaluation_pins WHERE standard_id=?'
        )
        params: tuple[str, ...] = (standard_id,)
        if edition is not None:
            sql += ' AND edition=?'
            params = (standard_id, edition)
        sql += ' ORDER BY evaluated_at_utc, pin_id'
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, params).fetchall()
        return tuple(self._pin_from_row(row) for row in rows)

    def list_pins(
        self, document_id: str
    ) -> tuple[StandardsEvaluationPin, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT pin_id, pin_sha256, document_id, standard_id,
                       edition, result, payload_json
                FROM cad_standard_evaluation_pins
                WHERE document_id=?
                ORDER BY evaluated_at_utc, pin_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._pin_from_row(row) for row in rows)

    def _pin_from_row(
        self, row: sqlite3.Row
    ) -> StandardsEvaluationPin:
        pin = StandardsEvaluationPin.model_validate_json(
            row['payload_json']
        )
        if (
            pin.pin_id != row['pin_id']
            or pin.pin_sha256 != row['pin_sha256']
            or pin.document_id != row['document_id']
            or pin.standard_id != row['standard_id']
            or pin.edition != row['edition']
            or pin.result != row['result']
        ):
            raise ExternalStandardsIntegrityError(
                'evaluation pin row disagrees with its payload'
            )
        return pin

    # ------------------------------------------------------------------
    # Revision diffs

    def save_revision_diff(self, diff: StandardsRevisionDiff) -> None:
        existing = self.get_revision_diff(diff.diff_id)
        if existing is not None:
            if existing.diff_sha256 == diff.diff_sha256:
                return
            raise ExternalStandardsConflictError(
                'revision diffs are append-only'
            )
        for key, edition in (
            (diff.from_standard_id, diff.from_edition),
            (diff.to_standard_id, diff.to_edition),
        ):
            if self.get_document(key, edition) is None:
                raise ExternalStandardsIntegrityError(
                    'a revision diff must reference persisted documents '
                    f'— {key}@{edition} is not registered'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_standard_revision_diffs (
                    diff_id, diff_sha256, from_standard_id, from_edition,
                    to_standard_id, to_edition, recorded_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    diff.diff_id,
                    diff.diff_sha256,
                    diff.from_standard_id,
                    diff.from_edition,
                    diff.to_standard_id,
                    diff.to_edition,
                    diff.recorded_at_utc,
                    diff.model_dump_json(),
                ),
            )

    def get_revision_diff(
        self, diff_id: str
    ) -> StandardsRevisionDiff | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT diff_id, diff_sha256, from_standard_id,
                       from_edition, to_standard_id, to_edition,
                       payload_json
                FROM cad_standard_revision_diffs
                WHERE diff_id=?
                """,
                (diff_id,),
            ).fetchone()
        if row is None:
            return None
        return self._diff_from_row(row)

    def list_revision_diffs(
        self, standard_id: str | None = None
    ) -> tuple[StandardsRevisionDiff, ...]:
        sql = (
            'SELECT diff_id, diff_sha256, from_standard_id, '
            'from_edition, to_standard_id, to_edition, payload_json '
            'FROM cad_standard_revision_diffs'
        )
        params: tuple[str, ...] = ()
        if standard_id is not None:
            sql += (
                ' WHERE from_standard_id=? OR to_standard_id=?'
            )
            params = (standard_id, standard_id)
        sql += ' ORDER BY recorded_at_utc, diff_id'
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, params).fetchall()
        return tuple(self._diff_from_row(row) for row in rows)

    def _diff_from_row(
        self, row: sqlite3.Row
    ) -> StandardsRevisionDiff:
        diff = StandardsRevisionDiff.model_validate_json(
            row['payload_json']
        )
        if (
            diff.diff_id != row['diff_id']
            or diff.diff_sha256 != row['diff_sha256']
            or diff.from_standard_id != row['from_standard_id']
            or diff.from_edition != row['from_edition']
            or diff.to_standard_id != row['to_standard_id']
            or diff.to_edition != row['to_edition']
        ):
            raise ExternalStandardsIntegrityError(
                'revision diff row disagrees with its payload'
            )
        return diff


__all__ = [
    'CadExternalStandardsRepository',
    'ExternalStandardsConflictError',
    'ExternalStandardsIntegrityError',
]
