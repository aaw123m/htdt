from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_repository import SceneRepository
from .cad_system_variant import SystemVariant, materialize_system_variant
from .cad_scene import scene_content_hash


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


class SystemVariantApplication(BaseModel):
    """Append-only lineage from a selected proposal to the new SceneRevision."""

    model_config = ConfigDict(frozen=True)

    application_id: str = Field(min_length=1)
    variant_id: str = Field(min_length=1)
    variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str = Field(min_length=1)
    baseline_revision_id: str = Field(min_length=1)
    baseline_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    applied_revision_id: str = Field(min_length=1)
    applied_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    selected_by: str = Field(min_length=1)
    selected_at_utc: str = Field(min_length=1)
    application_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_identity(self) -> 'SystemVariantApplication':
        if self.application_sha256 != _digest(self.identity_payload()):
            raise ValueError('SystemVariantApplication identity hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'variant_id': self.variant_id,
            'variant_sha256': self.variant_sha256,
            'document_id': self.document_id,
            'baseline_revision_id': self.baseline_revision_id,
            'baseline_content_hash': self.baseline_content_hash,
            'applied_revision_id': self.applied_revision_id,
            'applied_content_hash': self.applied_content_hash,
            'selected_by': self.selected_by,
            'selected_at_utc': self.selected_at_utc,
        }


class SystemVariantComparisonRef(BaseModel):
    """Stable comparison handle; comparison math remains owned by existing authorities."""

    model_config = ConfigDict(frozen=True)

    variant_id: str = Field(min_length=1)
    variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    baseline_revision_id: str = Field(min_length=1)
    baseline_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    proposed_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    applied_revision_id: str | None = Field(default=None, min_length=1)
    applied_content_hash: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )

    @model_validator(mode='after')
    def applied_pair(self) -> 'SystemVariantComparisonRef':
        if (self.applied_revision_id is None) != (self.applied_content_hash is None):
            raise ValueError('applied revision/hash must be supplied together')
        return self


class CadSystemVariantRepository:
    """O100A persistence layered on immutable SceneRevision authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = Path(scene_repository.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cad_system_variants (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    variant_id TEXT NOT NULL UNIQUE,
                    document_id TEXT NOT NULL,
                    baseline_revision_id TEXT NOT NULL,
                    baseline_content_hash TEXT NOT NULL,
                    parent_variant_id TEXT,
                    variant_sha256 TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    FOREIGN KEY(baseline_revision_id)
                        REFERENCES scene_revisions(revision_id),
                    FOREIGN KEY(parent_variant_id)
                        REFERENCES cad_system_variants(variant_id)
                );
                CREATE INDEX IF NOT EXISTS idx_system_variant_document_seq
                    ON cad_system_variants(document_id, seq ASC);

                CREATE TABLE IF NOT EXISTS cad_system_variant_applications (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_id TEXT NOT NULL UNIQUE,
                    variant_id TEXT NOT NULL UNIQUE,
                    document_id TEXT NOT NULL,
                    baseline_revision_id TEXT NOT NULL,
                    applied_revision_id TEXT NOT NULL UNIQUE,
                    application_sha256 TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    selected_at_utc TEXT NOT NULL,
                    FOREIGN KEY(variant_id)
                        REFERENCES cad_system_variants(variant_id),
                    FOREIGN KEY(baseline_revision_id)
                        REFERENCES scene_revisions(revision_id),
                    FOREIGN KEY(applied_revision_id)
                        REFERENCES scene_revisions(revision_id)
                );
                CREATE INDEX IF NOT EXISTS idx_system_variant_application_document_seq
                    ON cad_system_variant_applications(document_id, seq ASC);
                """
            )

    def _validate_equipment_bindings_persisted(
        self,
        variant: SystemVariant,
    ) -> None:
        if not variant.equipment_bindings:
            return
        with closing(self._connect()) as connection, connection:
            table = connection.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type='table' AND name='cad_equipment_definitions'
                """
            ).fetchone()
            if table is None:
                raise ValueError(
                    'SystemVariant equipment binding requires persisted EquipmentDefinition authority'
                )
            for binding in variant.equipment_bindings:
                row = connection.execute(
                    """
                    SELECT definition_id, version
                    FROM cad_equipment_definitions
                    WHERE semantic_sha256=?
                    """,
                    (binding.equipment_definition_sha256,),
                ).fetchone()
                if row is None:
                    raise ValueError(
                        'SystemVariant equipment binding references an unpersisted definition'
                    )
                if (
                    row['definition_id'] != binding.equipment_definition_id
                    or row['version'] != binding.equipment_definition_version
                ):
                    raise ValueError(
                        'SystemVariant equipment binding definition identity mismatch'
                    )

    def save_variant(self, variant: SystemVariant) -> None:
        variant = SystemVariant.model_validate(variant.model_dump(mode='python'))
        self._validate_equipment_bindings_persisted(variant)
        baseline = self.scene_repository.get(variant.baseline_revision_id)
        if baseline is None:
            raise ValueError('SystemVariant baseline SceneRevision does not exist')
        if (
            baseline.document_id != variant.document_id
            or baseline.content_hash != variant.baseline_content_hash
        ):
            raise ValueError('SystemVariant baseline authority mismatch')
        materialize_system_variant(baseline, variant)

        if variant.parent_variant_id is not None:
            parent = self.get_variant(variant.parent_variant_id)
            if parent is None:
                raise ValueError('SystemVariant parent variant does not exist')
            if parent.document_id != variant.document_id:
                raise ValueError('SystemVariant parent belongs to another document')

        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_system_variants(
                    variant_id, document_id, baseline_revision_id,
                    baseline_content_hash, parent_variant_id, variant_sha256,
                    payload_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    variant.variant_id,
                    variant.document_id,
                    variant.baseline_revision_id,
                    variant.baseline_content_hash,
                    variant.parent_variant_id,
                    variant.variant_sha256,
                    variant.model_dump_json(),
                    variant.created_at_utc,
                ),
            )

    def get_variant(self, variant_id: str) -> SystemVariant | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_system_variants WHERE variant_id=?',
                (variant_id,),
            ).fetchone()
        return None if row is None else SystemVariant.model_validate_json(row['payload_json'])

    def list_variants(self, document_id: str) -> tuple[SystemVariant, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_system_variants '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(SystemVariant.model_validate_json(row['payload_json']) for row in rows)

    def get_application(self, application_id: str) -> SystemVariantApplication | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_system_variant_applications '
                'WHERE application_id=?',
                (application_id,),
            ).fetchone()
        return (
            None
            if row is None
            else SystemVariantApplication.model_validate_json(row['payload_json'])
        )

    def application_for_variant(self, variant_id: str) -> SystemVariantApplication | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_system_variant_applications '
                'WHERE variant_id=?',
                (variant_id,),
            ).fetchone()
        return (
            None
            if row is None
            else SystemVariantApplication.model_validate_json(row['payload_json'])
        )

    def application_for_revision(self, revision_id: str) -> SystemVariantApplication | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_system_variant_applications '
                'WHERE applied_revision_id=?',
                (revision_id,),
            ).fetchone()
        return (
            None
            if row is None
            else SystemVariantApplication.model_validate_json(row['payload_json'])
        )

    def apply_variant(
        self,
        variant_id: str,
        *,
        selected_by: str,
        selected_at_utc: str | None = None,
    ) -> SystemVariantApplication:
        """Explicitly materialize a selected proposal as one new SceneRevision."""

        existing = self.application_for_variant(variant_id)
        if existing is not None:
            return existing

        variant = self.get_variant(variant_id)
        if variant is None:
            raise ValueError('SystemVariant does not exist')
        baseline = self.scene_repository.get(variant.baseline_revision_id)
        if baseline is None:
            raise ValueError('SystemVariant baseline SceneRevision does not exist')
        proposed = materialize_system_variant(baseline, variant)
        proposed_hash = scene_content_hash(proposed)
        if proposed_hash == baseline.content_hash:
            raise ValueError('cannot apply a no-op SystemVariant as a new SceneRevision')

        selected_at = _utc_now() if selected_at_utc is None else selected_at_utc
        applied_revision_id = str(uuid4())
        applied_created_at = _utc_now()
        identity = {
            'variant_id': variant.variant_id,
            'variant_sha256': variant.variant_sha256,
            'document_id': variant.document_id,
            'baseline_revision_id': baseline.revision_id,
            'baseline_content_hash': baseline.content_hash,
            'applied_revision_id': applied_revision_id,
            'applied_content_hash': proposed_hash,
            'selected_by': selected_by,
            'selected_at_utc': selected_at,
        }
        application = SystemVariantApplication(
            application_id=str(uuid4()),
            variant_id=variant.variant_id,
            variant_sha256=variant.variant_sha256,
            document_id=variant.document_id,
            baseline_revision_id=baseline.revision_id,
            baseline_content_hash=baseline.content_hash,
            applied_revision_id=applied_revision_id,
            applied_content_hash=proposed_hash,
            selected_by=selected_by,
            selected_at_utc=selected_at,
            application_sha256=_digest(identity),
        )

        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')

            existing_row = connection.execute(
                'SELECT payload_json FROM cad_system_variant_applications '
                'WHERE variant_id=?',
                (variant.variant_id,),
            ).fetchone()
            if existing_row is not None:
                return SystemVariantApplication.model_validate_json(
                    existing_row['payload_json']
                )

            latest_row = connection.execute(
                'SELECT revision_id, content_hash FROM scene_revisions '
                'WHERE document_id=? ORDER BY seq DESC LIMIT 1',
                (variant.document_id,),
            ).fetchone()
            if (
                latest_row is None
                or latest_row['revision_id'] != baseline.revision_id
                or latest_row['content_hash'] != baseline.content_hash
            ):
                raise ValueError(
                    'cannot apply SystemVariant from a stale baseline SceneRevision'
                )

            saved = self.scene_repository._save_in_transaction(
                connection,
                proposed,
                parent_revision_id=baseline.revision_id,
                revision_id=applied_revision_id,
                created_at_utc=applied_created_at,
            )
            if not saved.created:
                raise ValueError('SystemVariant apply did not create a new SceneRevision')
            if (
                saved.revision.revision_id != application.applied_revision_id
                or saved.revision.content_hash != application.applied_content_hash
            ):
                raise ValueError('SystemVariant application SceneRevision mismatch')

            connection.execute(
                """
                INSERT INTO cad_system_variant_applications(
                    application_id, variant_id, document_id,
                    baseline_revision_id, applied_revision_id,
                    application_sha256, payload_json, selected_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    application.application_id,
                    application.variant_id,
                    application.document_id,
                    application.baseline_revision_id,
                    application.applied_revision_id,
                    application.application_sha256,
                    application.model_dump_json(),
                    application.selected_at_utc,
                ),
            )
        return application

    def comparison_ref(self, variant_id: str) -> SystemVariantComparisonRef:
        variant = self.get_variant(variant_id)
        if variant is None:
            raise ValueError('SystemVariant does not exist')
        baseline = self.scene_repository.get(variant.baseline_revision_id)
        if baseline is None:
            raise ValueError('SystemVariant baseline SceneRevision does not exist')
        proposed_hash = scene_content_hash(materialize_system_variant(baseline, variant))
        application = self.application_for_variant(variant_id)
        return SystemVariantComparisonRef(
            variant_id=variant.variant_id,
            variant_sha256=variant.variant_sha256,
            baseline_revision_id=baseline.revision_id,
            baseline_content_hash=baseline.content_hash,
            proposed_content_hash=proposed_hash,
            applied_revision_id=None if application is None else application.applied_revision_id,
            applied_content_hash=None if application is None else application.applied_content_hash,
        )

    def proposal_lineage_for_revision(
        self,
        revision_id: str,
    ) -> tuple[SystemVariantApplication, SystemVariant] | None:
        """Resolve the nearest applied SystemVariant ancestor for a revision.

        The exact applied revision remains authoritative, but subsequent
        immutable SceneRevision edits do not erase proposal origin. If a later
        SystemVariant is explicitly applied on a descendant branch, the nearest
        application in the ancestry wins.
        """

        revision = self.scene_repository.get(revision_id)
        if revision is None:
            return None

        seen: set[str] = set()
        current = revision
        while True:
            if current.revision_id in seen:
                raise ValueError('SceneRevision ancestry contains a cycle')
            seen.add(current.revision_id)

            application = self.application_for_revision(current.revision_id)
            if application is not None:
                variant = self.get_variant(application.variant_id)
                if variant is None:
                    raise ValueError(
                        'SystemVariant application references missing variant'
                    )
                if (
                    application.document_id != revision.document_id
                    or variant.document_id != revision.document_id
                ):
                    raise ValueError(
                        'SystemVariant application ancestry crosses document boundary'
                    )
                return application, variant

            parent_id = current.parent_revision_id
            if parent_id is None:
                return None
            parent = self.scene_repository.get(parent_id)
            if parent is None:
                raise ValueError(
                    'SceneRevision ancestry references missing parent revision'
                )
            if parent.document_id != revision.document_id:
                raise ValueError(
                    'SceneRevision ancestry crosses document boundary'
                )
            current = parent
