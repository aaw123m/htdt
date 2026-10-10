"""Append-only persistence for the IFC interop authority (#578).

Six tables:

* ``cad_ifc_import_artifacts`` — sealed ``IfcImportArtifact`` records
  keyed by ``artifact_id``; the raw-file hash binding makes an artifact
  unfalsifiable after the fact.
* ``cad_ifc_entity_mappings`` — sealed ``IfcEntityMapping`` records; a
  mapping may only be persisted against a stored artifact whose sha
  matches — mappings can never float free of their import.
* ``cad_ifc_revision_deltas`` — sealed ``IfcRevisionDelta`` records;
  both artifacts must be stored and the matching shas verified.
* ``cad_ifc_intake_profiles`` — declared intake requirements (IDS-style
  selection contract) a document's imports are checked against.
* ``cad_ifc_intake_evaluations`` — sealed ``IfcIntakeEvaluation``
  records bound to a stored profile.
* ``cad_ifc_exports`` — sealed ``IfcExportPackage`` records; the emitted
  STEP text is hash-verified at persist time so a stored package can
  never disagree with its declared content hash.

Re-saving an identical row is a no-op; a divergent hash under the same
id is a conflict. Records are sealed before insert — a ``model_copy``
forgery is rejected at the repository boundary.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Iterable

from .cad_repository import SceneRepository
from .cad_ifc_diff import IfcDiffApply
from .cad_ifc_interop import (
    IfcEntityMapping,
    IfcExportPackage,
    IfcImportArtifact,
    IfcIntakeEvaluation,
    IfcIntakeProfile,
    IfcRevisionDelta,
)
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256


class IfcInteropConflictError(ValueError):
    """An IFC interop save violated append-only identity rules."""


class IfcInteropIntegrityError(ValueError):
    """A stored IFC interop row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha or getattr(record, id_field) != (
        getattr(record, id_field).split(':')[0] + ':' + sha
    ):
        raise IfcInteropIntegrityError(
            'record payload does not match its sealed identity'
        )


class CadIfcInteropRepository:
    """Native storage for IFC interop authorities."""

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
                'cad_ifc_import_artifacts',
                'cad_ifc_entity_mappings',
                'cad_ifc_revision_deltas',
                'cad_ifc_diff_applies',
                'cad_ifc_intake_profiles',
                'cad_ifc_intake_evaluations',
                'cad_ifc_exports',
            )

    # ------------------------------------------------------------------
    # Import artifacts

    def save_import_artifact(self, artifact: IfcImportArtifact) -> None:
        _assert_sealed(artifact, 'artifact_sha256', 'artifact_id')
        existing = self.get_import_artifact(artifact.artifact_id)
        if existing is not None:
            if existing.artifact_sha256 == artifact.artifact_sha256:
                return
            raise IfcInteropConflictError(
                'ifc import artifacts are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_ifc_import_artifacts (
                    artifact_id, artifact_sha256, document_id,
                    file_name, schema_identifier, imported_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    artifact.artifact_sha256,
                    artifact.document_id,
                    artifact.file_name,
                    artifact.schema_identifier,
                    artifact.imported_at_utc,
                    artifact.model_dump_json(),
                ),
            )

    def get_import_artifact(
        self, artifact_id: str
    ) -> IfcImportArtifact | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT artifact_id, artifact_sha256, document_id,
                       file_name, schema_identifier, imported_at_utc,
                       payload_json
                FROM cad_ifc_import_artifacts
                WHERE artifact_id=?
                """,
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        return self._artifact_from_row(row)

    def list_import_artifacts(
        self, document_id: str
    ) -> tuple[IfcImportArtifact, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT artifact_id, artifact_sha256, document_id,
                       file_name, schema_identifier, imported_at_utc,
                       payload_json
                FROM cad_ifc_import_artifacts
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._artifact_from_row(row) for row in rows)

    def _artifact_from_row(self, row: tuple) -> IfcImportArtifact:
        (
            artifact_id,
            artifact_sha256,
            document_id,
            file_name,
            schema_identifier,
            imported_at_utc,
            payload_json,
        ) = row
        artifact = IfcImportArtifact.model_validate_json(payload_json)
        if (
            artifact.artifact_id != artifact_id
            or artifact.artifact_sha256 != artifact_sha256
            or artifact.document_id != document_id
            or artifact.file_name != file_name
            or artifact.schema_identifier != schema_identifier
            or artifact.imported_at_utc != imported_at_utc
        ):
            raise IfcInteropIntegrityError(
                'ifc import artifact row disagrees with payload'
            )
        return artifact

    # ------------------------------------------------------------------
    # Entity mappings

    def save_entity_mappings(
        self, mappings: Iterable[IfcEntityMapping]
    ) -> None:
        with closing(self._connect()) as connection, connection:
            for mapping in mappings:
                self._save_entity_mapping_in_connection(
                    connection, mapping
                )

    def _save_entity_mapping_in_connection(
        self,
        connection: sqlite3.Connection,
        mapping: IfcEntityMapping,
    ) -> None:
        """Append-only mapping insert on an open connection (#981).

        Used by the diff-apply transaction so a partial apply can never
        leave half-saved reconciliation marks.
        """
        _assert_sealed(mapping, 'mapping_sha256', 'mapping_id')
        artifact = self._import_artifact_in_connection(
            connection, mapping.import_artifact_id
        )
        if artifact is None or (
            artifact.artifact_sha256 != mapping.import_artifact_sha256
        ):
            raise IfcInteropIntegrityError(
                'entity mapping refers to an unimported artifact'
            )
        existing_row = connection.execute(
            """
            SELECT mapping_sha256 FROM cad_ifc_entity_mappings
            WHERE mapping_id=?
            """,
            (mapping.mapping_id,),
        ).fetchone()
        if existing_row is not None:
            if existing_row[0] == mapping.mapping_sha256:
                return
            raise IfcInteropConflictError(
                'ifc entity mappings are append-only'
            )
        connection.execute(
            """
            INSERT INTO cad_ifc_entity_mappings (
                mapping_id, mapping_sha256, document_id,
                import_artifact_id, ifc_global_id, ifc_type,
                htdt_role, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mapping.mapping_id,
                mapping.mapping_sha256,
                mapping.document_id,
                mapping.import_artifact_id,
                mapping.ifc_global_id,
                mapping.ifc_type,
                mapping.htdt_role,
                mapping.model_dump_json(),
            ),
        )

    def _import_artifact_in_connection(
        self,
        connection: sqlite3.Connection,
        artifact_id: str,
    ) -> IfcImportArtifact | None:
        row = connection.execute(
            """
            SELECT artifact_id, artifact_sha256, document_id,
                   file_name, schema_identifier, imported_at_utc,
                   payload_json
            FROM cad_ifc_import_artifacts
            WHERE artifact_id=?
            """,
            (artifact_id,),
        ).fetchone()
        if row is None:
            return None
        return self._artifact_from_row(row)

    def get_entity_mapping(
        self, mapping_id: str
    ) -> IfcEntityMapping | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT mapping_id, mapping_sha256, document_id,
                       import_artifact_id, ifc_global_id, ifc_type,
                       htdt_role, payload_json
                FROM cad_ifc_entity_mappings
                WHERE mapping_id=?
                """,
                (mapping_id,),
            ).fetchone()
        if row is None:
            return None
        return self._mapping_from_row(row)

    def list_entity_mappings(
        self, document_id: str, import_artifact_id: str | None = None
    ) -> tuple[IfcEntityMapping, ...]:
        if import_artifact_id is None:
            sql = (
                """
                SELECT mapping_id, mapping_sha256, document_id,
                       import_artifact_id, ifc_global_id, ifc_type,
                       htdt_role, payload_json
                FROM cad_ifc_entity_mappings
                WHERE document_id=?
                ORDER BY seq ASC
                """
            )
            params: tuple = (document_id,)
        else:
            sql = (
                """
                SELECT mapping_id, mapping_sha256, document_id,
                       import_artifact_id, ifc_global_id, ifc_type,
                       htdt_role, payload_json
                FROM cad_ifc_entity_mappings
                WHERE document_id=? AND import_artifact_id=?
                ORDER BY seq ASC
                """
            )
            params = (document_id, import_artifact_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, params).fetchall()
        return tuple(self._mapping_from_row(row) for row in rows)

    def _mapping_from_row(self, row: tuple) -> IfcEntityMapping:
        (
            mapping_id,
            mapping_sha256,
            document_id,
            import_artifact_id,
            ifc_global_id,
            ifc_type,
            htdt_role,
            payload_json,
        ) = row
        mapping = IfcEntityMapping.model_validate_json(payload_json)
        if (
            mapping.mapping_id != mapping_id
            or mapping.mapping_sha256 != mapping_sha256
            or mapping.document_id != document_id
            or mapping.import_artifact_id != import_artifact_id
            or mapping.ifc_global_id != ifc_global_id
            or mapping.ifc_type != ifc_type
            or mapping.htdt_role != htdt_role
        ):
            raise IfcInteropIntegrityError(
                'ifc entity mapping row disagrees with payload'
            )
        return mapping

    # ------------------------------------------------------------------
    # Revision deltas

    def save_revision_delta(self, delta: IfcRevisionDelta) -> None:
        _assert_sealed(delta, 'delta_sha256', 'delta_id')
        prior = self.get_import_artifact(delta.prior_artifact_id)
        new = self.get_import_artifact(delta.new_artifact_id)
        if prior is None or prior.artifact_sha256 != delta.prior_artifact_sha256:
            raise IfcInteropIntegrityError(
                'delta refers to an unstored prior artifact'
            )
        if new is None or new.artifact_sha256 != delta.new_artifact_sha256:
            raise IfcInteropIntegrityError(
                'delta refers to an unstored new artifact'
            )
        existing = self.get_revision_delta(delta.delta_id)
        if existing is not None:
            if existing.delta_sha256 == delta.delta_sha256:
                return
            raise IfcInteropConflictError(
                'ifc revision deltas are append-only'
            )
        with closing(self._connect()) as connection, connection:
            self._save_revision_delta_in_connection(connection, delta)

    def _save_revision_delta_in_connection(
        self,
        connection: sqlite3.Connection,
        delta: IfcRevisionDelta,
    ) -> None:
        """Append-only delta insert on an open connection (#981).

        Both artifacts are verified inside the caller's transaction so a
        diff apply can never persist a delta bound to unimported state.
        """
        prior = self._import_artifact_in_connection(
            connection, delta.prior_artifact_id
        )
        if prior is None or (
            prior.artifact_sha256 != delta.prior_artifact_sha256
        ):
            raise IfcInteropIntegrityError(
                'delta refers to an unstored prior artifact'
            )
        new = self._import_artifact_in_connection(
            connection, delta.new_artifact_id
        )
        if new is None or new.artifact_sha256 != delta.new_artifact_sha256:
            raise IfcInteropIntegrityError(
                'delta refers to an unstored new artifact'
            )
        existing_row = connection.execute(
            """
            SELECT delta_sha256 FROM cad_ifc_revision_deltas
            WHERE delta_id=?
            """,
            (delta.delta_id,),
        ).fetchone()
        if existing_row is not None:
            if existing_row[0] == delta.delta_sha256:
                return
            raise IfcInteropConflictError(
                'ifc revision deltas are append-only'
            )
        connection.execute(
            """
            INSERT INTO cad_ifc_revision_deltas (
                delta_id, delta_sha256, document_id,
                prior_artifact_id, new_artifact_id,
                reconciliation_state, evaluated_at_utc, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                delta.delta_id,
                delta.delta_sha256,
                delta.document_id,
                delta.prior_artifact_id,
                delta.new_artifact_id,
                delta.reconciliation_state,
                delta.evaluated_at_utc,
                delta.model_dump_json(),
            ),
        )

    def get_revision_delta(
        self, delta_id: str
    ) -> IfcRevisionDelta | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT delta_id, delta_sha256, document_id,
                       prior_artifact_id, new_artifact_id,
                       reconciliation_state, evaluated_at_utc, payload_json
                FROM cad_ifc_revision_deltas
                WHERE delta_id=?
                """,
                (delta_id,),
            ).fetchone()
        if row is None:
            return None
        return self._delta_from_row(row)

    def list_revision_deltas(
        self, document_id: str
    ) -> tuple[IfcRevisionDelta, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT delta_id, delta_sha256, document_id,
                       prior_artifact_id, new_artifact_id,
                       reconciliation_state, evaluated_at_utc, payload_json
                FROM cad_ifc_revision_deltas
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._delta_from_row(row) for row in rows)

    def _delta_from_row(self, row: tuple) -> IfcRevisionDelta:
        (
            delta_id,
            delta_sha256,
            document_id,
            prior_artifact_id,
            new_artifact_id,
            reconciliation_state,
            evaluated_at_utc,
            payload_json,
        ) = row
        delta = IfcRevisionDelta.model_validate_json(payload_json)
        if (
            delta.delta_id != delta_id
            or delta.delta_sha256 != delta_sha256
            or delta.document_id != document_id
            or delta.prior_artifact_id != prior_artifact_id
            or delta.new_artifact_id != new_artifact_id
            or delta.reconciliation_state != reconciliation_state
            or delta.evaluated_at_utc != evaluated_at_utc
        ):
            raise IfcInteropIntegrityError(
                'ifc revision delta row disagrees with payload'
            )
        return delta

    # ------------------------------------------------------------------
    # Intake profiles

    def save_intake_profile(self, profile: IfcIntakeProfile) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_intake_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise IfcInteropConflictError(
                'ifc intake profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_ifc_intake_profiles (
                    profile_id, profile_sha256, document_id,
                    name, profile_version, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.name,
                    profile.profile_version,
                    profile.model_dump_json(),
                ),
            )

    def get_intake_profile(
        self, profile_id: str
    ) -> IfcIntakeProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT profile_id, profile_sha256, document_id,
                       name, profile_version, payload_json
                FROM cad_ifc_intake_profiles
                WHERE profile_id=?
                """,
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        return self._profile_from_row(row)

    def list_intake_profiles(
        self, document_id: str
    ) -> tuple[IfcIntakeProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT profile_id, profile_sha256, document_id,
                       name, profile_version, payload_json
                FROM cad_ifc_intake_profiles
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._profile_from_row(row) for row in rows)

    def _profile_from_row(self, row: tuple) -> IfcIntakeProfile:
        (
            profile_id,
            profile_sha256,
            document_id,
            name,
            profile_version,
            payload_json,
        ) = row
        profile = IfcIntakeProfile.model_validate_json(payload_json)
        if (
            profile.profile_id != profile_id
            or profile.profile_sha256 != profile_sha256
            or profile.document_id != document_id
            or profile.name != name
            or profile.profile_version != profile_version
        ):
            raise IfcInteropIntegrityError(
                'ifc intake profile row disagrees with payload'
            )
        return profile

    # ------------------------------------------------------------------
    # Intake evaluations

    def save_intake_evaluation(
        self, evaluation: IfcIntakeEvaluation
    ) -> None:
        _assert_sealed(evaluation, 'evaluation_sha256', 'evaluation_id')
        profile = self.get_intake_profile(evaluation.profile_id)
        if profile is None or (
            profile.profile_sha256 != evaluation.profile_sha256
        ):
            raise IfcInteropIntegrityError(
                'intake evaluation refers to an unstored profile'
            )
        existing = self.get_intake_evaluation(evaluation.evaluation_id)
        if existing is not None:
            if existing.evaluation_sha256 == evaluation.evaluation_sha256:
                return
            raise IfcInteropConflictError(
                'ifc intake evaluations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_ifc_intake_evaluations (
                    evaluation_id, evaluation_sha256, document_id,
                    profile_id, file_sha256, overall_state,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.evaluation_sha256,
                    evaluation.document_id,
                    evaluation.profile_id,
                    evaluation.file_sha256,
                    evaluation.overall_state,
                    evaluation.evaluated_at_utc,
                    evaluation.model_dump_json(),
                ),
            )

    def get_intake_evaluation(
        self, evaluation_id: str
    ) -> IfcIntakeEvaluation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT evaluation_id, evaluation_sha256, document_id,
                       profile_id, file_sha256, overall_state,
                       evaluated_at_utc, payload_json
                FROM cad_ifc_intake_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        return self._evaluation_from_row(row)

    def list_intake_evaluations(
        self, document_id: str
    ) -> tuple[IfcIntakeEvaluation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT evaluation_id, evaluation_sha256, document_id,
                       profile_id, file_sha256, overall_state,
                       evaluated_at_utc, payload_json
                FROM cad_ifc_intake_evaluations
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._evaluation_from_row(row) for row in rows)

    def _evaluation_from_row(self, row: tuple) -> IfcIntakeEvaluation:
        (
            evaluation_id,
            evaluation_sha256,
            document_id,
            profile_id,
            file_sha256,
            overall_state,
            evaluated_at_utc,
            payload_json,
        ) = row
        evaluation = IfcIntakeEvaluation.model_validate_json(payload_json)
        if (
            evaluation.evaluation_id != evaluation_id
            or evaluation.evaluation_sha256 != evaluation_sha256
            or evaluation.document_id != document_id
            or evaluation.profile_id != profile_id
            or evaluation.file_sha256 != file_sha256
            or evaluation.overall_state != overall_state
            or evaluation.evaluated_at_utc != evaluated_at_utc
        ):
            raise IfcInteropIntegrityError(
                'ifc intake evaluation row disagrees with payload'
            )
        return evaluation

    # ------------------------------------------------------------------
    # Export packages

    def save_export_package(self, package: IfcExportPackage) -> None:
        _assert_sealed(package, 'export_sha256', 'export_id')
        if package.source_artifact_id is not None:
            artifact = self.get_import_artifact(
                package.source_artifact_id
            )
            if artifact is None or (
                artifact.artifact_sha256 != package.source_artifact_sha256
            ):
                raise IfcInteropIntegrityError(
                    'export package refers to an unstored artifact'
                )
        if package.step_sha256 != canonical_sha256(
            package.step_text.encode('utf-8')
        ):
            raise IfcInteropIntegrityError(
                'export step_sha256 does not match emitted text'
            )
        existing = self.get_export_package(package.export_id)
        if existing is not None:
            if existing.export_sha256 == package.export_sha256:
                return
            raise IfcInteropConflictError(
                'ifc export packages are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_ifc_exports (
                    export_id, export_sha256, document_id,
                    mode, source_artifact_id, step_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    package.export_id,
                    package.export_sha256,
                    package.document_id,
                    package.mode,
                    package.source_artifact_id,
                    package.step_sha256,
                    package.created_at_utc,
                    package.model_dump_json(),
                ),
            )

    def get_export_package(
        self, export_id: str
    ) -> IfcExportPackage | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT export_id, export_sha256, document_id,
                       mode, source_artifact_id, step_sha256,
                       created_at_utc, payload_json
                FROM cad_ifc_exports
                WHERE export_id=?
                """,
                (export_id,),
            ).fetchone()
        if row is None:
            return None
        return self._export_from_row(row)

    def list_export_packages(
        self, document_id: str
    ) -> tuple[IfcExportPackage, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT export_id, export_sha256, document_id,
                       mode, source_artifact_id, step_sha256,
                       created_at_utc, payload_json
                FROM cad_ifc_exports
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._export_from_row(row) for row in rows)

    def _export_from_row(self, row: tuple) -> IfcExportPackage:
        (
            export_id,
            export_sha256,
            document_id,
            mode,
            source_artifact_id,
            step_sha256,
            created_at_utc,
            payload_json,
        ) = row
        package = IfcExportPackage.model_validate_json(payload_json)
        if (
            package.export_id != export_id
            or package.export_sha256 != export_sha256
            or package.document_id != document_id
            or package.mode != mode
            or package.source_artifact_id != source_artifact_id
            or package.step_sha256 != step_sha256
            or package.created_at_utc != created_at_utc
        ):
            raise IfcInteropIntegrityError(
                'ifc export package row disagrees with payload'
            )
        return package

    # ------------------------------------------------------------------
    # Diff-review applies (#981)

    def save_diff_apply(self, record: IfcDiffApply) -> None:
        """Persist a sealed diff-review apply record (append-only).

        The referenced delta and both import artifacts must already be
        stored — an apply can never float free of the revisions it
        reconciled.
        """
        _assert_sealed(record, 'apply_sha256', 'apply_id')
        delta = self.get_revision_delta(record.delta_ref.ref_id)
        if delta is None or (
            delta.delta_sha256 != record.delta_ref.ref_sha256
        ):
            raise IfcInteropIntegrityError(
                'diff apply refers to an unstored revision delta'
            )
        for ref in (
            record.prior_artifact_ref,
            record.new_artifact_ref,
        ):
            artifact = self.get_import_artifact(ref.ref_id)
            if artifact is None or (
                artifact.artifact_sha256 != ref.ref_sha256
            ):
                raise IfcInteropIntegrityError(
                    'diff apply refers to an unstored import artifact'
                )
        existing = self.get_diff_apply(record.apply_id)
        if existing is not None:
            if existing.apply_sha256 == record.apply_sha256:
                return
            raise IfcInteropConflictError(
                'ifc diff applies are append-only'
            )
        with closing(self._connect()) as connection, connection:
            self._save_diff_apply_in_connection(connection, record)

    def _save_diff_apply_in_connection(
        self,
        connection: sqlite3.Connection,
        record: IfcDiffApply,
    ) -> None:
        """Append-only apply insert on an open connection (#981)."""
        _assert_sealed(record, 'apply_sha256', 'apply_id')
        delta_row = connection.execute(
            """
            SELECT delta_sha256 FROM cad_ifc_revision_deltas
            WHERE delta_id=?
            """,
            (record.delta_ref.ref_id,),
        ).fetchone()
        if delta_row is None or (
            delta_row[0] != record.delta_ref.ref_sha256
        ):
            raise IfcInteropIntegrityError(
                'diff apply refers to an unstored revision delta'
            )
        for ref in (
            record.prior_artifact_ref,
            record.new_artifact_ref,
        ):
            artifact = self._import_artifact_in_connection(
                connection, ref.ref_id
            )
            if artifact is None or (
                artifact.artifact_sha256 != ref.ref_sha256
            ):
                raise IfcInteropIntegrityError(
                    'diff apply refers to an unstored import artifact'
                )
        existing_row = connection.execute(
            """
            SELECT apply_sha256 FROM cad_ifc_diff_applies
            WHERE apply_id=?
            """,
            (record.apply_id,),
        ).fetchone()
        if existing_row is not None:
            if existing_row[0] == record.apply_sha256:
                return
            raise IfcInteropConflictError(
                'ifc diff applies are append-only'
            )
        connection.execute(
            """
            INSERT INTO cad_ifc_diff_applies (
                apply_id, apply_sha256, document_id, delta_id,
                prior_artifact_id, new_artifact_id, merged_subject_id,
                applied_at_utc, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.apply_id,
                record.apply_sha256,
                record.document_id,
                record.delta_ref.ref_id,
                record.prior_artifact_ref.ref_id,
                record.new_artifact_ref.ref_id,
                record.merged_subject_id,
                record.applied_at_utc,
                record.model_dump_json(),
            ),
        )

    def get_diff_apply(self, apply_id: str) -> IfcDiffApply | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT apply_id, apply_sha256, document_id, delta_id,
                       prior_artifact_id, new_artifact_id,
                       merged_subject_id, applied_at_utc, payload_json
                FROM cad_ifc_diff_applies
                WHERE apply_id=?
                """,
                (apply_id,),
            ).fetchone()
        if row is None:
            return None
        return self._apply_from_row(row)

    def list_diff_applies(
        self, document_id: str
    ) -> tuple[IfcDiffApply, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT apply_id, apply_sha256, document_id, delta_id,
                       prior_artifact_id, new_artifact_id,
                       merged_subject_id, applied_at_utc, payload_json
                FROM cad_ifc_diff_applies
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._apply_from_row(row) for row in rows)

    def latest_diff_apply(
        self, document_id: str, delta_id: str
    ) -> IfcDiffApply | None:
        """Newest apply recorded against (document, delta) — re-applies
        append, so the latest row carries the current operator state."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT apply_id, apply_sha256, document_id, delta_id,
                       prior_artifact_id, new_artifact_id,
                       merged_subject_id, applied_at_utc, payload_json
                FROM cad_ifc_diff_applies
                WHERE document_id=? AND delta_id=?
                ORDER BY seq DESC
                LIMIT 1
                """,
                (document_id, delta_id),
            ).fetchone()
        if row is None:
            return None
        return self._apply_from_row(row)

    def _apply_from_row(self, row: tuple) -> IfcDiffApply:
        (
            apply_id,
            apply_sha256,
            document_id,
            delta_id,
            prior_artifact_id,
            new_artifact_id,
            merged_subject_id,
            applied_at_utc,
            payload_json,
        ) = row
        record = IfcDiffApply.model_validate_json(payload_json)
        if (
            record.apply_id != apply_id
            or record.apply_sha256 != apply_sha256
            or record.document_id != document_id
            or record.delta_ref.ref_id != delta_id
            or record.prior_artifact_ref.ref_id != prior_artifact_id
            or record.new_artifact_ref.ref_id != new_artifact_id
            or record.merged_subject_id != merged_subject_id
            or record.applied_at_utc != applied_at_utc
        ):
            raise IfcInteropIntegrityError(
                'ifc diff apply row disagrees with payload'
            )
        return record


__all__ = [
    'CadIfcInteropRepository',
    'IfcInteropConflictError',
    'IfcInteropIntegrityError',
]
