"""Append-only persistence for field / as-built evidence (#507).

Persistence resolves every declared target before commit: scene revision
and entity targets must exist inside the record's own project document and
pin the revision's exact content hash, variant targets pin
``variant_sha256``, and ``other`` targets resolve through a registered kind
resolver carrying an explicit sha256 pin — a target that does not resolve
fails before any row is written, so foreign or nonexistent authority can
never appear in evidence lookups.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Mapping

from .cad_authority_resolver import (
    AuthorityRef,
    ExactAuthorityResolver,
    KindResolver,
)
from .cad_field_evidence import (
    EvidenceTarget,
    FieldEvidenceRecord,
)
from .cad_repository import SceneRepository
from .cad_system_variant_repository import CadSystemVariantRepository


class FieldEvidenceConflictError(ValueError):
    """An evidence save violated append-only identity rules."""


class CadFieldEvidenceRepository:
    """Native storage for FieldEvidenceRecord records.

    Binary payloads are stored through the scene repository's
    content-addressed blob store (``store_blob``/``read_blob``) and referenced
    by ``FieldEvidenceAsset.asset_sha256``; this table only stores the binding
    records plus a document-scoped target index for look-ups by exact
    revision/entity.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        system_variant_repository: CadSystemVariantRepository | None = None,
        kind_resolvers: Mapping[str, KindResolver] | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.resolver = ExactAuthorityResolver(
            scene_repository,
            system_variant_repository=system_variant_repository,
            field_evidence_repository=self,
            kind_resolvers=kind_resolvers,
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
                CREATE TABLE IF NOT EXISTS cad_field_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    asset_sha256 TEXT,
                    evidence_sha256 TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_field_evidence_targets (
                    evidence_id TEXT NOT NULL,
                    target_kind TEXT NOT NULL,
                    revision_id TEXT,
                    entity_id TEXT,
                    ref_id TEXT,
                    FOREIGN KEY (evidence_id)
                        REFERENCES cad_field_evidence (evidence_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cad_field_evidence_targets
                ON cad_field_evidence_targets (target_kind, revision_id, entity_id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cad_field_evidence_document
                ON cad_field_evidence (document_id, created_at_utc)
                """
            )

    def store_asset(self, content: bytes) -> str:
        """Store a binary payload in the content-addressed blob store."""

        return self.scene_repository.store_blob(content)

    def read_asset(self, asset_sha256: str) -> bytes:
        payload = self.scene_repository.read_blob(asset_sha256)
        if payload is None:
            raise ValueError(
                'evidence asset is not stored in the blob store'
            )
        return payload

    def save_evidence(self, record: FieldEvidenceRecord) -> None:
        """Append an evidence record. No target is mutated as a side effect.

        Every declared target is resolved against the pinned authority
        first: a revision/entity must exist inside ``record.document_id``
        and match the pinned content hash, a variant must carry its exact
        ``variant_sha256``, and ``other`` targets must resolve through a
        registered kind resolver. Unresolvable targets fail the whole save
        before any row or index entry is committed. Validation always runs
        against the *pinned* revision — evidence bound to a historical
        revision stays valid after the document head advances.
        """

        if self.get_evidence(record.evidence_id) is not None:
            raise FieldEvidenceConflictError(
                'FieldEvidenceRecord ids are append-only'
            )
        for target in record.targets:
            self._resolve_target(record.document_id, target)
        if record.asset is not None:
            payload = self.read_asset(record.asset.asset_sha256)
            if len(payload) != record.asset.byte_length:
                raise ValueError(
                    'evidence asset byte_length does not match the stored '
                    'blob'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_field_evidence (
                    evidence_id, document_id, kind, asset_sha256,
                    evidence_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.evidence_id,
                    record.document_id,
                    record.kind,
                    record.asset.asset_sha256 if record.asset else None,
                    record.evidence_sha256,
                    record.created_at_utc,
                    record.model_dump_json(),
                ),
            )
            for target in record.targets:
                connection.execute(
                    """
                    INSERT INTO cad_field_evidence_targets (
                        evidence_id, target_kind, revision_id, entity_id, ref_id
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        record.evidence_id,
                        target.kind,
                        target.revision_id,
                        target.entity_id,
                        target.ref_id,
                    ),
                )

    def get_evidence(self, evidence_id: str) -> FieldEvidenceRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_field_evidence WHERE evidence_id=?',
                (evidence_id,),
            ).fetchone()
        if row is None:
            return None
        return FieldEvidenceRecord.model_validate_json(row['payload_json'])

    def _load_many(
        self, evidence_ids: list[str]
    ) -> tuple[FieldEvidenceRecord, ...]:
        if not evidence_ids:
            return ()
        placeholders = ','.join('?' for _ in evidence_ids)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT payload_json FROM cad_field_evidence
                WHERE evidence_id IN ({placeholders})
                ORDER BY created_at_utc, evidence_id
                """,
                tuple(evidence_ids),
            ).fetchall()
        return tuple(
            FieldEvidenceRecord.model_validate_json(row['payload_json'])
            for row in rows
        )

    def list_evidence(
        self,
        document_id: str,
    ) -> tuple[FieldEvidenceRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_field_evidence
                WHERE document_id=?
                ORDER BY created_at_utc, evidence_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            FieldEvidenceRecord.model_validate_json(row['payload_json'])
            for row in rows
        )

    def _resolve_target(
        self,
        document_id: str,
        target: EvidenceTarget,
    ) -> None:
        """Resolve one evidence target against exact stored authority.

        ``scene_revision`` and ``scene_entity`` resolve the pinned revision
        (existence, same document, exact content hash) plus — for entity
        targets — membership inside that pinned revision's entity set.
        ``system_variant`` and ``other`` resolve through the shared
        resolver, which enforces the semantic hash pin.
        """

        if target.kind in {'scene_revision', 'scene_entity'}:
            assert target.revision_id is not None
            self.resolver.resolve(
                AuthorityRef(
                    kind='scene_revision',
                    ref_id=target.revision_id,
                    ref_sha256=target.ref_sha256,
                ),
                document_id=document_id,
            )
            if target.kind == 'scene_entity':
                assert target.entity_id is not None
                self.resolver.resolve_scene_entity(
                    target.revision_id,
                    target.entity_id,
                    document_id=document_id,
                )
            return
        if target.kind == 'installation_section':
            # A stable typed vocabulary — validated structurally on the
            # EvidenceTarget model; nothing further to resolve.
            return
        assert target.ref_id is not None
        self.resolver.resolve(
            AuthorityRef(
                kind=target.kind,
                ref_id=target.ref_id,
                ref_sha256=target.ref_sha256,
            ),
            document_id=document_id,
        )

    def evidence_for_revision(
        self,
        revision_id: str,
        *,
        document_id: str | None = None,
    ) -> tuple[FieldEvidenceRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT t.evidence_id FROM cad_field_evidence_targets t
                JOIN cad_field_evidence e ON e.evidence_id = t.evidence_id
                WHERE t.revision_id=?
                  AND (? IS NULL OR e.document_id=?)
                """,
                (revision_id, document_id, document_id),
            ).fetchall()
        return self._load_many([row['evidence_id'] for row in rows])

    def evidence_for_entity(
        self,
        revision_id: str,
        entity_id: str,
        *,
        document_id: str | None = None,
    ) -> tuple[FieldEvidenceRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT t.evidence_id FROM cad_field_evidence_targets t
                JOIN cad_field_evidence e ON e.evidence_id = t.evidence_id
                WHERE t.revision_id=? AND t.entity_id=?
                  AND (? IS NULL OR e.document_id=?)
                """,
                (revision_id, entity_id, document_id, document_id),
            ).fetchall()
        return self._load_many([row['evidence_id'] for row in rows])

    def evidence_for_ref(
        self,
        target_kind: str,
        ref_id: str,
        *,
        document_id: str | None = None,
    ) -> tuple[FieldEvidenceRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT t.evidence_id FROM cad_field_evidence_targets t
                JOIN cad_field_evidence e ON e.evidence_id = t.evidence_id
                WHERE t.target_kind=? AND t.ref_id=?
                  AND (? IS NULL OR e.document_id=?)
                """,
                (target_kind, ref_id, document_id, document_id),
            ).fetchall()
        return self._load_many([row['evidence_id'] for row in rows])


__all__ = ['CadFieldEvidenceRepository', 'FieldEvidenceConflictError']
