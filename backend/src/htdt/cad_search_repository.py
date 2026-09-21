from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .cad_repository import SceneRepository
from .cad_search import require_search_spec_authority
from .cad_search_models import CAD_SEARCH_SCHEMA_VERSION, CadSearchSpec


class CadSearchRepository:
    """Immutable native SearchSpec storage beside SceneRevision authority."""

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
            connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS cad_search_specs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    search_spec_id TEXT NOT NULL UNIQUE,
                    document_id TEXT NOT NULL,
                    scene_revision_id TEXT NOT NULL,
                    scene_content_hash TEXT NOT NULL,
                    constraint_workspace_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    search_spec_sha256 TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id)
                )
                '''
            )
            connection.execute(
                'CREATE INDEX IF NOT EXISTS idx_cad_search_document_seq '
                'ON cad_search_specs(document_id, seq DESC)'
            )

    def _require_spec_authority(self, spec: CadSearchSpec) -> None:
        """Re-resolve the exact SceneRevision and replay the pinned compiler.

        Save and every authoritative read share this full contract: the
        source SceneRevision must exist with the declared document/content
        hash, and the persisted engine/O10 payloads must equal the canonical
        compilation of the spec's declared scene/constraint authority. A
        coherently rehashed payload that is not the canonical compilation
        fails closed.
        """

        source = self.scene_repository.get(spec.scene_revision_id)
        if source is None:
            raise ValueError('SearchSpec source revision does not exist')
        require_search_spec_authority(source, spec)

    def save(self, spec: CadSearchSpec) -> None:
        spec = CadSearchSpec.model_validate(spec.model_dump(mode='python'))
        self._require_spec_authority(spec)

        payload_json = spec.model_dump_json()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                '''
                INSERT INTO cad_search_specs(
                    search_spec_id, document_id, scene_revision_id, scene_content_hash,
                    constraint_workspace_hash, payload_json, search_spec_sha256, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    spec.search_spec_id,
                    spec.document_id,
                    spec.scene_revision_id,
                    spec.scene_content_hash,
                    spec.constraint_workspace_hash,
                    payload_json,
                    spec.search_spec_sha256,
                    spec.created_at_utc,
                ),
            )

    def _validated_spec(self, row: sqlite3.Row) -> CadSearchSpec:
        """Deserialize one persisted row and replay its exact authority."""

        raw = json.loads(row['payload_json'])
        version = raw.get('schema_version') if isinstance(raw, dict) else None
        if isinstance(version, int) and version < CAD_SEARCH_SCHEMA_VERSION:
            raise ValueError(
                'stored SearchSpec uses a prior schema_version; recompile '
                'and persist a new SearchSpec instead of reusing old hashes'
            )
        spec = CadSearchSpec.model_validate(raw)
        if (
            row['search_spec_id'] != spec.search_spec_id
            or row['document_id'] != spec.document_id
            or row['scene_revision_id'] != spec.scene_revision_id
            or row['scene_content_hash'] != spec.scene_content_hash
            or row['constraint_workspace_hash'] != spec.constraint_workspace_hash
            or row['search_spec_sha256'] != spec.search_spec_sha256
            or row['created_at_utc'] != spec.created_at_utc
        ):
            raise ValueError(
                'persisted SearchSpec row disagrees with its payload'
            )
        self._require_spec_authority(spec)
        return spec

    def get(self, search_spec_id: str) -> CadSearchSpec | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_search_specs WHERE search_spec_id=?',
                (search_spec_id,),
            ).fetchone()
        return None if row is None else self._validated_spec(row)

    def list_specs(self, document_id: str) -> tuple[CadSearchSpec, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM cad_search_specs WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(self._validated_spec(row) for row in rows)
