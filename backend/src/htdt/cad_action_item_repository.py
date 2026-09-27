"""SQLite store for :class:`ProjectActionItem` (#667, hardened #866).

Shares the SceneRepository database so action items and spatial notes stay
inside the project file and travel with backup/export. Items are workflow
metadata, not authority: ``save`` upserts by ``action_id`` (status changes
are expected), while the content hash keeps each record self-verifying.

Referential integrity (per the #866 contract):

- every ``subject_refs``/``resolution_ref`` whose kind has a canonical
  owner is resolved through :class:`CanonicalAuthorityRefResolver` before
  save: the authority must exist, belong to the item's document, and
  carry the exact semantic hash when the owner exposes one. Kinds with
  no canonical owner (``other`` and derived subjects such as
  ``room_surface``) pass through — they are addresses, not claims;
- a ``spatial_anchor`` pins an exact ``scene_revision_id`` in the item's
  document, and ``entity_ref``/``surface_ref`` must resolve inside that
  pinned revision — never reinterpreted against current head;
- reads compare indexed row columns to the self-hashed payload and fail
  closed on drift, and never re-resolve refs — a subject that later
  disappears stays a valid historical item, not a save-time failure;
- ``subject_ref_state`` classifies a ref as ``current``/``historical``/
  ``unresolved`` for presentation without ever rebinding by name.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal

from .cad_schema import connect_sqlite, require_native_tables
from .cad_action_item import (
    ActionSpatialAnchor,
    ActionSubjectRef,
    ProjectActionItem,
)
from .cad_authority_refs import (
    AuthorityRefResolver,
    CanonicalAuthorityRefResolver,
)


class ActionItemConflictError(ValueError):
    """Persisted item exists for another document — never silently rebound."""


class ActionItemRefError(ValueError):
    """A subject/resolution ref or spatial anchor failed validation."""


class ActionItemIntegrityError(ValueError):
    """Row columns disagree with the self-hashed payload — fail closed."""


#: Read-time classification of a subject ref against canonical state.
SubjectRefState = Literal['current', 'historical', 'unresolved']


class CadActionItemRepository:
    """Per-document action items, ordered by creation."""

    def __init__(
        self,
        scene_repository,
        ref_resolver: AuthorityRefResolver | None = None,
    ) -> None:
        self.path = Path(scene_repository.path)
        self._scene_repository = scene_repository
        if ref_resolver is None:
            ref_resolver = CanonicalAuthorityRefResolver(scene_repository)
        self.ref_resolver = ref_resolver
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection,
                'project_action_items',
            )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # -- canonical ref validation (#866) -----------------------------------

    def _resolve_ref(self, ref: ActionSubjectRef, document_id: str) -> None:
        resolver = self.ref_resolver
        if resolver is None or not resolver.knows(ref.kind):
            # No canonical owner for the kind (explicit ``other`` and
            # derived subjects) — the ref stays an address, not a claim.
            return
        resolved = resolver.resolve(ref.kind, ref.ref_id, document_id)
        if resolved is None:
            raise ActionItemRefError(
                f'unknown {ref.kind} authority: {ref.ref_id}'
            )
        if (
            resolved.document_id is not None
            and resolved.document_id != document_id
        ):
            raise ActionItemRefError(
                f'{ref.kind} {ref.ref_id} belongs to another project'
            )
        if resolved.semantic_sha256 is not None:
            if ref.ref_sha256 is None:
                raise ActionItemRefError(
                    f'{ref.kind} {ref.ref_id} is hash-bearing: '
                    'ref_sha256 is required'
                )
            if ref.ref_sha256 != resolved.semantic_sha256:
                raise ActionItemRefError(
                    f'{ref.kind} {ref.ref_id} semantic hash mismatch'
                )

    def _validate_anchor(
        self, anchor: ActionSpatialAnchor, document_id: str
    ) -> None:
        if anchor.scene_revision_id is None:
            raise ActionItemRefError(
                f'{anchor.kind} anchor requires scene_revision_id'
            )
        revision = self._scene_repository.get(anchor.scene_revision_id)
        if revision is None:
            raise ActionItemRefError(
                f'unknown scene_revision: {anchor.scene_revision_id}'
            )
        if revision.document_id != document_id:
            raise ActionItemRefError(
                'anchor scene_revision belongs to another project'
            )
        if anchor.kind == 'entity_ref' and not any(
            entity.entity_id == anchor.ref_id
            for entity in revision.document.entities
        ):
            raise ActionItemRefError(
                f'entity {anchor.ref_id} absent from pinned revision'
            )
        if anchor.kind == 'surface_ref':
            document = revision.document
            surface_ids = {
                assembly.assembly_id
                for assembly in document.construction_assemblies or ()
            }
            topology = document.wall_topology
            if topology is not None:
                surface_ids.update(wall.wall_id for wall in topology.walls)
                surface_ids.update(
                    opening.opening_id for opening in topology.openings
                )
            if anchor.ref_id not in surface_ids:
                raise ActionItemRefError(
                    f'surface {anchor.ref_id} absent from pinned revision'
                )

    def _validate_item(self, item: ProjectActionItem) -> None:
        for ref in item.subject_refs:
            self._resolve_ref(ref, item.document_id)
        if item.resolution_ref is not None:
            self._resolve_ref(item.resolution_ref, item.document_id)
        if item.spatial_anchor is not None:
            self._validate_anchor(item.spatial_anchor, item.document_id)

    def save(self, item: ProjectActionItem) -> ProjectActionItem:
        self._validate_item(item)
        with closing(self._connect()) as connection:
            existing = connection.execute(
                'SELECT document_id FROM project_action_items WHERE action_id = ?',
                (item.action_id,),
            ).fetchone()
            if existing is not None and existing['document_id'] != item.document_id:
                raise ActionItemConflictError(
                    f'action item {item.action_id} belongs to another document'
                )
            with connection:
                connection.execute(
                    """
                    INSERT INTO project_action_items (
                        action_id, document_id, status, priority,
                        created_at_utc, updated_at_utc, archived,
                        action_sha256, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(action_id) DO UPDATE SET
                        status = excluded.status,
                        priority = excluded.priority,
                        updated_at_utc = excluded.updated_at_utc,
                        archived = excluded.archived,
                        action_sha256 = excluded.action_sha256,
                        payload_json = excluded.payload_json
                    """,
                    (
                        item.action_id,
                        item.document_id,
                        item.status,
                        item.priority,
                        item.created_at_utc,
                        item.updated_at_utc,
                        1 if item.archived else 0,
                        item.action_sha256,
                        json.dumps(
                            item.model_dump(mode='json'), ensure_ascii=False,
                            allow_nan=False,
                        ),
                    ),
                )
        return item

    def _row_to_item(self, row: sqlite3.Row) -> ProjectActionItem:
        item = ProjectActionItem.model_validate_json(row['payload_json'])
        if (
            row['document_id'] != item.document_id
            or row['status'] != item.status
            or row['priority'] != item.priority
            or row['created_at_utc'] != item.created_at_utc
            or row['updated_at_utc'] != item.updated_at_utc
            or bool(row['archived']) != item.archived
            or row['action_sha256'] != item.action_sha256
        ):
            raise ActionItemIntegrityError(
                f'action item {row["action_id"]} row columns drift from payload'
            )
        return item

    def get(self, action_id: str) -> ProjectActionItem | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM project_action_items WHERE action_id = ?',
                (action_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_item(row)

    def list_actions(
        self,
        document_id: str,
        *,
        status: str | None = None,
        include_archived: bool = False,
    ) -> tuple[ProjectActionItem, ...]:
        clauses = ['document_id = ?']
        params: list[object] = [document_id]
        if status is not None:
            clauses.append('status = ?')
            params.append(status)
        if not include_archived:
            clauses.append('archived = 0')
        query = (
            'SELECT * FROM project_action_items WHERE '
            + ' AND '.join(clauses)
            + ' ORDER BY created_at_utc ASC, action_id ASC'
        )
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(self._row_to_item(row) for row in rows)

    # -- read-time subject classification (#866F) ---------------------------

    def subject_ref_state(
        self, ref: ActionSubjectRef, document_id: str
    ) -> SubjectRefState:
        """Classify a stored subject ref without ever rebinding it.

        ``current`` when the canonical owner still resolves the exact ref;
        ``historical`` when the subject disappeared from the current head
        but exists in an earlier revision of the same document;
        ``unresolved`` when no canonical proof is possible. Kinds with no
        canonical owner are always ``unresolved`` — never a same-name or
        fuzzy fallback.
        """

        resolver = self.ref_resolver
        if resolver is None or not resolver.knows(ref.kind):
            return 'unresolved'
        if resolver.resolve(ref.kind, ref.ref_id, document_id) is not None:
            return 'current'
        if ref.kind == 'scene_entity':
            head = self._scene_repository.current_head(document_id)
            for revision in self._scene_repository.list_revisions(
                document_id
            ):
                if head is not None and (
                    revision.revision_id == head.revision_id
                ):
                    continue
                if any(
                    entity.entity_id == ref.ref_id
                    for entity in revision.document.entities
                ):
                    return 'historical'
        return 'unresolved'


__all__ = [
    'ActionItemConflictError',
    'ActionItemIntegrityError',
    'ActionItemRefError',
    'CadActionItemRepository',
    'SubjectRefState',
]
