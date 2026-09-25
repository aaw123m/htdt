"""Append-only persistence for saved analysis studies (#594).

Every ``bound_refs`` entry is resolved through the shared exact-authority
resolver before the study commits: scene/variant refs use the built-in
resolvers and remaining kinds (``measurement_dataset``,
``prediction_result``, ``standards_profile``, …) resolve through injected
kind resolvers — an unresolvable or hash-mismatched authority fails the
save, so a study can never claim binding to evidence that does not exist
in this document.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Mapping

from hashlib import sha256
import json

from .cad_analysis_study import AnalysisStudy, StudyAuthorityRef
from .cad_authority_resolver import (
    AuthorityRef,
    ExactAuthorityResolver,
    KindResolver,
    ResolvedAuthority,
)
from .cad_measurement_repository import CadMeasurementRepository
from .cad_repository import SceneRepository
from .cad_system_variant_repository import CadSystemVariantRepository


class AnalysisStudyConflictError(ValueError):
    """A study save violated append-only identity rules."""


def _record_sha256(record: object) -> str:
    """Deterministic semantic identity for a measurement record."""

    payload = record.model_dump(mode='json')  # type: ignore[attr-defined]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )
    return sha256(canonical.encode('utf-8')).hexdigest()


def _measurement_dataset_resolver(
    repository: CadMeasurementRepository,
) -> KindResolver:
    def resolve(ref_id: str) -> ResolvedAuthority | None:
        dataset = repository.get_dataset(ref_id)
        if dataset is None:
            return None
        # A dataset's project scope comes through its owning measurement.
        record = repository.get_measurement(dataset.measurement_id)
        if record is None:
            return None
        return ResolvedAuthority(
            kind='measurement_dataset',
            ref_id=ref_id,
            document_id=record.document_id,
            semantic_sha256=dataset.dataset_sha256,
        )

    return resolve


def _measurement_record_resolver(
    repository: CadMeasurementRepository,
) -> KindResolver:
    def resolve(ref_id: str) -> ResolvedAuthority | None:
        record = repository.get_measurement(ref_id)
        if record is None:
            return None
        return ResolvedAuthority(
            kind='measurement',
            ref_id=ref_id,
            document_id=record.document_id,
            semantic_sha256=_record_sha256(record),
        )

    return resolve


def _comparison_set_resolver(
    repository: CadMeasurementRepository,
) -> KindResolver:
    def resolve(ref_id: str) -> ResolvedAuthority | None:
        comparison = repository.get_comparison(ref_id)
        if comparison is None:
            return None
        return ResolvedAuthority(
            kind='comparison_set',
            ref_id=ref_id,
            document_id=comparison.document_id,
            semantic_sha256=comparison.comparison_sha256,
        )

    return resolve


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
        resolvers: dict[str, KindResolver] = dict(kind_resolvers or {})
        if measurement_repository is not None:
            resolvers.setdefault(
                'measurement_dataset',
                _measurement_dataset_resolver(measurement_repository),
            )
            resolvers.setdefault(
                'measurement',
                _measurement_record_resolver(measurement_repository),
            )
            resolvers.setdefault(
                'comparison_set',
                _comparison_set_resolver(measurement_repository),
            )
        self.resolver = ExactAuthorityResolver(
            scene_repository,
            system_variant_repository=system_variant_repository,
            kind_resolvers=resolvers,
        )
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_analysis_studies (
                    study_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    study_kind TEXT NOT NULL,
                    study_sha256 TEXT NOT NULL,
                    supersedes_study_id TEXT,
                    duplicated_from_study_id TEXT,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cad_analysis_studies_document
                ON cad_analysis_studies (document_id, created_at_utc)
                """
            )

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
