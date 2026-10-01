from __future__ import annotations

from collections import OrderedDict
from contextlib import closing
from pathlib import Path
import threading
import sqlite3
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_repository import SceneRepository, SceneRevision, _SharedReadConnection
from .cad_system_variant import SystemVariant, materialize_system_variant
from .cad_scene import SceneDocument, scene_content_hash
from .cad_schema import require_native_tables, connect_sqlite
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest
from .clock import utc_now_iso as _utc_now


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
        # Bounded memo for the deterministic authority replay: keyed by the
        # variant's content address and the baseline revision it binds, so
        # every read still re-fetches the variant row and re-runs every
        # liveness/consistency probe — only the pure
        # materialize/hash computation is reused. LRU past the cap.
        self._proposed_cache: OrderedDict[
            tuple[str, str, str],
            tuple[SceneDocument, str],
        ] = OrderedDict()
        self._thread_reads = threading.local()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _read(self) -> sqlite3.Connection:
        connection = getattr(self._thread_reads, 'connection', None)
        if connection is None:
            connection = _SharedReadConnection(connect_sqlite(self.path))
            self._thread_reads.connection = connection
        return connection

    def close(self) -> None:
        """Release the calling thread's shared read connection, if any."""
        connection = getattr(self._thread_reads, 'connection', None)
        if connection is not None:
            connection._inner.close()
            self._thread_reads.connection = None

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_system_variants', 'cad_system_variant_applications')

    def _validate_equipment_bindings_persisted(
        self,
        variant: SystemVariant,
    ) -> None:
        if not variant.equipment_bindings:
            return
        with closing(self._read()) as connection, connection:
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

    def _require_variant_authority(
        self,
        variant: SystemVariant,
        lineage: frozenset[str] = frozenset(),
    ) -> tuple[SceneRevision, SceneDocument]:
        """Replay the exact external authority one SystemVariant is bound to.

        Returns the resolved baseline SceneRevision and the materialized
        proposed scene. Missing, mismatched, or cyclical authority fails
        closed; used by both save-time validation and authoritative reads.
        """

        self._validate_equipment_bindings_persisted(variant)
        baseline = self.scene_repository.get(variant.baseline_revision_id)
        if baseline is None:
            raise ValueError('SystemVariant baseline SceneRevision does not exist')
        if (
            baseline.document_id != variant.document_id
            or baseline.content_hash != variant.baseline_content_hash
        ):
            raise ValueError('SystemVariant baseline authority mismatch')
        proposed, _proposed_hash = self._materialized_proposed(baseline, variant)

        if variant.parent_variant_id is not None:
            if (
                variant.parent_variant_id == variant.variant_id
                or variant.parent_variant_id in lineage
            ):
                raise ValueError('SystemVariant parent lineage contains a cycle')
            parent = self._get_variant(
                variant.parent_variant_id,
                lineage | {variant.variant_id},
            )
            if parent is None:
                raise ValueError('SystemVariant parent variant does not exist')
            if parent.document_id != variant.document_id:
                raise ValueError('SystemVariant parent belongs to another document')
        return baseline, proposed

    def _validated_variant(
        self,
        row: sqlite3.Row,
        lineage: frozenset[str],
    ) -> SystemVariant:
        """Deserialize one persisted variant row and replay its exact authority."""

        variant = SystemVariant.model_validate_json(row['payload_json'])
        if (
            row['variant_id'] != variant.variant_id
            or row['document_id'] != variant.document_id
            or row['baseline_revision_id'] != variant.baseline_revision_id
            or row['baseline_content_hash'] != variant.baseline_content_hash
            or row['parent_variant_id'] != variant.parent_variant_id
            or row['variant_sha256'] != variant.variant_sha256
            or row['created_at_utc'] != variant.created_at_utc
        ):
            raise ValueError(
                'persisted SystemVariant row disagrees with its payload'
            )
        self._require_variant_authority(variant, lineage)
        return variant

    #: Upper bound on the per-instance materialization memo.
    _PROPOSED_CACHE_LIMIT = 512

    def _materialized_proposed(
        self,
        baseline: SceneRevision,
        variant: SystemVariant,
    ) -> tuple[SceneDocument, str]:
        """Replay ``variant`` over ``baseline``, memoized by content address.

        Both inputs are content-addressed and immutable, so the proposed
        scene and its content hash are a pure function of them; callers
        still perform every liveness/consistency check before reaching here.
        """
        key = (
            variant.variant_sha256,
            baseline.revision_id,
            baseline.content_hash,
        )
        cached = self._proposed_cache.pop(key, None)
        if cached is not None:
            self._proposed_cache[key] = cached
            return cached
        proposed = materialize_system_variant(baseline, variant)
        entry = (proposed, scene_content_hash(proposed))
        self._proposed_cache[key] = entry
        while len(self._proposed_cache) > self._PROPOSED_CACHE_LIMIT:
            self._proposed_cache.popitem(last=False)
        return entry

    def _get_variant(
        self,
        variant_id: str,
        lineage: frozenset[str],
    ) -> SystemVariant | None:
        with closing(self._read()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_system_variants WHERE variant_id=?',
                (variant_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validated_variant(row, lineage)

    def _validated_application(
        self,
        row: sqlite3.Row,
    ) -> SystemVariantApplication:
        """Deserialize one application row and replay its exact authority."""

        application = SystemVariantApplication.model_validate_json(
            row['payload_json']
        )
        if (
            row['application_id'] != application.application_id
            or row['variant_id'] != application.variant_id
            or row['document_id'] != application.document_id
            or row['baseline_revision_id'] != application.baseline_revision_id
            or row['applied_revision_id'] != application.applied_revision_id
            or row['application_sha256'] != application.application_sha256
            or row['selected_at_utc'] != application.selected_at_utc
        ):
            raise ValueError(
                'persisted SystemVariantApplication row disagrees with its payload'
            )
        variant = self.get_variant(application.variant_id)
        if variant is None:
            raise ValueError(
                'SystemVariant application references missing variant'
            )
        if (
            variant.variant_sha256 != application.variant_sha256
            or variant.document_id != application.document_id
            or variant.baseline_revision_id != application.baseline_revision_id
            or variant.baseline_content_hash != application.baseline_content_hash
        ):
            raise ValueError(
                'SystemVariant application variant authority mismatch'
            )
        baseline = self.scene_repository.get(application.baseline_revision_id)
        if baseline is None:
            raise ValueError(
                'SystemVariant application baseline SceneRevision does not exist'
            )
        if (
            baseline.document_id != application.document_id
            or baseline.content_hash != application.baseline_content_hash
        ):
            raise ValueError(
                'SystemVariant application baseline authority mismatch'
            )
        applied = self.scene_repository.get(application.applied_revision_id)
        if applied is None:
            raise ValueError(
                'SystemVariant application applied SceneRevision does not exist'
            )
        if (
            applied.document_id != application.document_id
            or applied.parent_revision_id != baseline.revision_id
            or applied.content_hash != application.applied_content_hash
        ):
            raise ValueError(
                'SystemVariant application applied SceneRevision mismatch'
            )
        proposed, proposed_hash = self._materialized_proposed(baseline, variant)
        if proposed_hash == baseline.content_hash:
            raise ValueError(
                'SystemVariant application must not reproduce its baseline'
            )
        if (
            proposed_hash != application.applied_content_hash
            or proposed != applied.document
        ):
            raise ValueError(
                'SystemVariant application does not reproduce the applied '
                'SceneRevision'
            )
        return application

    def save_variant(self, variant: SystemVariant) -> None:
        variant = SystemVariant.model_validate(variant.model_dump(mode='python'))
        self._require_variant_authority(variant)

        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            self._save_variant_in_transaction(connection, variant)

    def _save_variant_in_transaction(
        self,
        connection: sqlite3.Connection,
        variant: SystemVariant,
    ) -> None:
        """Insert one validated SystemVariant inside the caller's transaction.

        The caller owns BEGIN/COMMIT/ROLLBACK and must have replayed
        ``_require_variant_authority`` first: that validation resolves rows on
        second connections, which must not run while BEGIN IMMEDIATE is held.
        Higher-level repositories sharing this database use this helper to
        commit a variant together with their own dependent rows atomically.
        """
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
        return self._get_variant(variant_id, frozenset())

    def variant_for_sha256(
        self,
        document_id: str,
        variant_sha256: str,
    ) -> SystemVariant | None:
        """Indexed single-row lookup by content identity.

        ``variant_sha256`` is UNIQUE, so matching callers no longer have to
        run ``list_variants`` — which replays full authority validation on
        every row — just to resolve one known identity. The returned row
        still goes through ``_validated_variant``.
        """

        with closing(self._read()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_system_variants '
                'WHERE document_id=? AND variant_sha256=?',
                (document_id, variant_sha256),
            ).fetchone()
        if row is None:
            return None
        return self._validated_variant(row, frozenset())

    def list_variants(self, document_id: str) -> tuple[SystemVariant, ...]:
        with closing(self._read()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM cad_system_variants '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            self._validated_variant(row, frozenset())
            for row in rows
        )

    def get_application(self, application_id: str) -> SystemVariantApplication | None:
        with closing(self._read()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_system_variant_applications '
                'WHERE application_id=?',
                (application_id,),
            ).fetchone()
        return None if row is None else self._validated_application(row)

    def application_for_variant(self, variant_id: str) -> SystemVariantApplication | None:
        with closing(self._read()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_system_variant_applications '
                'WHERE variant_id=?',
                (variant_id,),
            ).fetchone()
        return None if row is None else self._validated_application(row)

    def application_for_revision(self, revision_id: str) -> SystemVariantApplication | None:
        with closing(self._read()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_system_variant_applications '
                'WHERE applied_revision_id=?',
                (revision_id,),
            ).fetchone()
        return None if row is None else self._validated_application(row)

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
        proposed, proposed_hash = self._materialized_proposed(baseline, variant)
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
                'SELECT * FROM cad_system_variant_applications '
                'WHERE variant_id=?',
                (variant.variant_id,),
            ).fetchone()
            if existing_row is not None:
                return self._validated_application(existing_row)

            # The apply baseline must be the document's explicit current head,
            # not merely the newest inserted row: detached lineage must not
            # satisfy or break this check (#626).
            head_row = self.scene_repository._head_revision_row(
                connection,
                variant.document_id,
            )
            if (
                head_row is None
                or head_row['revision_id'] != baseline.revision_id
                or head_row['content_hash'] != baseline.content_hash
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
