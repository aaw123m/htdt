"""Append-only persistence for saved analysis studies (#594).

Every ``bound_refs`` entry is resolved through the shared exact-authority
resolver before the study commits: every kind resolves through the
canonical registry (#902) — scene/variant refs plus the measurement
authorities (``measurement``, ``measurement_dataset``, ``comparison_set``)
wired by ``measurement_repository``, and any additional kind via injected
kind resolvers. An unresolvable or hash-mismatched authority fails the
save, so a study can never claim binding to evidence that does not exist
in this document.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Mapping

from .cad_analysis_study import AnalysisStudy, StudyAuthorityRef
from .cad_authority_resolver import (
    AuthorityRef,
    ExactAuthorityResolver,
    KindResolver,
)
from .cad_measurement_repository import CadMeasurementRepository
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite
from .cad_system_variant_repository import CadSystemVariantRepository


class AnalysisStudyConflictError(ValueError):
    """A study save violated append-only identity rules."""


class CadAnalysisStudyRepository:
    """Native storage for AnalysisStudy records.

    Studies are immutable: the only "update" path is duplicating a study into
    a new record. ``duplicated_from_study_id`` / ``supersedes_study_id`` are
    explicit lineage metadata — the repository treats every persisted study
    as an independent artifact, does not derive a single current head, and
    allows parallel branches in the chain.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        system_variant_repository: CadSystemVariantRepository | None = None,
        measurement_repository: CadMeasurementRepository | None = None,
        kind_resolvers: Mapping[str, KindResolver] | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.system_variant_repository = system_variant_repository
        self.measurement_repository = measurement_repository
        # #902: measurement authorities resolve through the canonical
        # registry — a study's 'measurement', 'measurement_dataset' and
        # 'comparison_set' refs share the same adapter and semantic hash
        # every other consumer uses; a callsite ``kind_resolvers`` entry
        # may still override for a kind the registry does not own.
        self.resolver = ExactAuthorityResolver(
            scene_repository,
            system_variant_repository=system_variant_repository,
            measurement_repository=measurement_repository,
            kind_resolvers=kind_resolvers,
        )
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_analysis_studies')

    def _resolve_study_authority(self, study: AnalysisStudy) -> None:
        """Resolve every authority the study claims, inside its document."""

        for ref in study.bound_refs:
            self.resolver.resolve(
                AuthorityRef(
                    kind=ref.kind,
                    ref_id=ref.ref_id,
                    ref_sha256=ref.ref_sha256,
                ),
                document_id=study.document_id,
            )
        if study.scene_revision_id is not None:
            self.resolver.resolve(
                AuthorityRef(
                    kind='scene_revision',
                    ref_id=study.scene_revision_id,
                    ref_sha256=study.scene_content_hash,
                ),
                document_id=study.document_id,
            )
        if study.system_variant_id is not None:
            self.resolver.resolve(
                AuthorityRef(
                    kind='system_variant',
                    ref_id=study.system_variant_id,
                    ref_sha256=study.system_variant_sha256,
                ),
                document_id=study.document_id,
            )

    def save_study(self, study: AnalysisStudy) -> None:
        if self.get_study(study.study_id) is not None:
            raise AnalysisStudyConflictError(
                'AnalysisStudy ids are append-only'
            )
        self._resolve_study_authority(study)
        for prior_id in (
            study.supersedes_study_id,
            study.duplicated_from_study_id,
        ):
            if prior_id is not None:
                prior = self.get_study(prior_id)
                if prior is None:
                    raise ValueError('chained study is not persisted')
                if prior.document_id != study.document_id:
                    raise ValueError('chained study belongs to another document')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_analysis_studies (
                    study_id, document_id, study_kind, study_sha256,
                    supersedes_study_id, duplicated_from_study_id,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    study.study_id,
                    study.document_id,
                    study.study_kind,
                    study.study_sha256,
                    study.supersedes_study_id,
                    study.duplicated_from_study_id,
                    study.created_at_utc,
                    study.model_dump_json(),
                ),
            )

    def get_study(self, study_id: str) -> AnalysisStudy | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_analysis_studies WHERE study_id=?',
                (study_id,),
            ).fetchone()
        if row is None:
            return None
        return AnalysisStudy.model_validate_json(row['payload_json'])

    def list_studies(
        self,
        document_id: str,
    ) -> tuple[AnalysisStudy, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_analysis_studies
                WHERE document_id=?
                ORDER BY created_at_utc, study_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            AnalysisStudy.model_validate_json(row['payload_json'])
            for row in rows
        )


__all__ = ['AnalysisStudyConflictError', 'CadAnalysisStudyRepository']
