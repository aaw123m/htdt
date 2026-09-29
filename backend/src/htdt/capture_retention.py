"""Reference-safe retention, inventory, and purge for Capture data (#352).

Scope and guarantees:

- Purge operates only on HTDT's own persisted database rows. The original
  external ``.htdtcapture`` bundle is an external producer artifact and is
  never opened, mutated, or deleted by this module.
- A dry-run plan is required first: it reports the exact row sets that
  would be removed, the dependents that block removal (promotions and
  compositions), the shared records retained because other runs still
  reference them, and the reclaimable byte estimate.
- Execution is a single ``BEGIN IMMEDIATE`` transaction that deletes only
  when the freshly recomputed plan is reference-safe, then runs
  ``PRAGMA foreign_key_check`` — any violation aborts the transaction and
  restores the prior state.
- Content blobs are garbage-collected only when no remaining record
  anywhere in the database still references their SHA-256.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Literal

from .cad_schema import connect_sqlite


class CaptureRetentionError(ValueError):
    pass


CapturePurgeStatus = Literal['ready', 'blocked', 'absent']


@dataclass(frozen=True)
class CapturePurgeDependent:
    """One record that keeps a purge candidate alive."""

    kind: str
    identifier: str
    detail: str


@dataclass(frozen=True)
class CaptureStorageInventory:
    """Catalog-wide capture storage inventory (#352)."""

    ingestion_run_count: int
    capture_revision_count: int
    source_evidence_count: int
    source_payload_bytes: int
    mesh_binding_count: int
    authority_record_count: int
    roomplan_record_count: int
    coordinate_authority_count: int
    mesh_composition_count: int
    promotion_count: int
    content_blob_count: int
    content_blob_bytes: int


@dataclass(frozen=True)
class CaptureRevisionListing:
    """One persisted capture revision, for retention pickers."""

    capture_revision_id: str
    capture_series_id: str
    ingestion_run_count: int
    latest_recorded_at_utc: str


@dataclass(frozen=True)
class CapturePurgePlan:
    """Dry-run deletion plan for one imported Capture revision.

    ``blocking_dependents`` is the exact dependency report — every
    promotion/composition that still consumes this revision's records and
    every shared record retained on behalf of other runs — so an operator
    sees precisely why a purge is refused before anything is deleted.
    """

    capture_revision_id: str
    status: CapturePurgeStatus
    ingestion_run_ids: tuple[str, ...]
    lineage_digests: tuple[str, ...]
    deletable_source_evidence_ids: tuple[str, ...]
    retained_source_evidence_ids: tuple[str, ...]
    deletable_mesh_binding_ids: tuple[str, ...]
    retained_mesh_binding_ids: tuple[str, ...]
    deletable_authority_record_ids: tuple[str, ...]
    retained_authority_record_ids: tuple[str, ...]
    deletable_coordinate_authority_ids: tuple[str, ...]
    retained_coordinate_authority_ids: tuple[str, ...]
    roomplan_record_count: int
    blocking_dependents: tuple[CapturePurgeDependent, ...]
    reclaimable_bytes: int
    reclaimed_blob_sha256: tuple[str, ...]


class CaptureRetentionService:
    """Retention operations over the shared native database."""

    def __init__(self, scene_repository) -> None:
        self.scene_repository = scene_repository
        self.path = Path(scene_repository.path)

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # ---- inventory --------------------------------------------------------

    def inventory(self) -> CaptureStorageInventory:
        """Bounded storage inventory across the capture schema."""

        def count(connection, query: str) -> int:
            return int(connection.execute(query).fetchone()[0])

        with closing(self._connect()) as connection:
            tables = {
                str(row['name'])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }

            def has(table: str) -> bool:
                return table in tables

            blob_count = blob_bytes = 0
            if has('htdt_content_blobs'):
                row = connection.execute(
                    'SELECT COUNT(*), COALESCE(SUM(byte_count), 0) '
                    'FROM htdt_content_blobs'
                ).fetchone()
                blob_count, blob_bytes = int(row[0]), int(row[1])
            evidence_bytes = (
                count(
                    connection,
                    'SELECT COALESCE(SUM(byte_count), 0) '
                    'FROM capture_source_evidence',
                )
                if has('capture_source_evidence')
                else 0
            )
            return CaptureStorageInventory(
                ingestion_run_count=(
                    count(connection, 'SELECT COUNT(*) FROM capture_ingestion_runs')
                    if has('capture_ingestion_runs')
                    else 0
                ),
                capture_revision_count=(
                    count(
                        connection,
                        'SELECT COUNT(DISTINCT capture_revision_id) '
                        'FROM capture_ingestion_runs',
                    )
                    if has('capture_ingestion_runs')
                    else 0
                ),
                source_evidence_count=(
                    count(connection, 'SELECT COUNT(*) FROM capture_source_evidence')
                    if has('capture_source_evidence')
                    else 0
                ),
                source_payload_bytes=evidence_bytes,
                mesh_binding_count=(
                    count(
                        connection,
                        'SELECT COUNT(*) FROM capture_raw_visual_mesh_bindings',
                    )
                    if has('capture_raw_visual_mesh_bindings')
                    else 0
                ),
                authority_record_count=(
                    count(
                        connection,
                        'SELECT COUNT(*) FROM capture_authority_records',
                    )
                    if has('capture_authority_records')
                    else 0
                ),
                roomplan_record_count=(
                    count(
                        connection,
                        'SELECT COUNT(*) FROM capture_roomplan_records',
                    )
                    if has('capture_roomplan_records')
                    else 0
                ),
                coordinate_authority_count=(
                    count(
                        connection,
                        'SELECT COUNT(*) FROM capture_coordinate_authorities',
                    )
                    if has('capture_coordinate_authorities')
                    else 0
                ),
                mesh_composition_count=(
                    count(
                        connection,
                        'SELECT COUNT(*) FROM capture_mesh_compositions',
                    )
                    if has('capture_mesh_compositions')
                    else 0
                ),
                promotion_count=(
                    count(
                        connection,
                        'SELECT COUNT(*) FROM capture_semantic_promotions',
                    )
                    if has('capture_semantic_promotions')
                    else 0
                ),
                content_blob_count=blob_count,
                content_blob_bytes=blob_bytes,
            )

    def list_capture_revisions(self) -> tuple[CaptureRevisionListing, ...]:
        """Persisted capture revisions, most recent activity first."""

        with closing(self._connect()) as connection:
            if not self._has_table(connection, 'capture_ingestion_runs'):
                return ()
            rows = connection.execute(
                '''
                SELECT capture_revision_id, capture_series_id,
                       COUNT(*) AS run_count,
                       MAX(recorded_at_utc) AS latest
                FROM capture_ingestion_runs
                GROUP BY capture_revision_id
                ORDER BY latest DESC, capture_revision_id ASC
                '''
            ).fetchall()
        return tuple(
            CaptureRevisionListing(
                capture_revision_id=str(row['capture_revision_id']),
                capture_series_id=str(row['capture_series_id']),
                ingestion_run_count=int(row['run_count']),
                latest_recorded_at_utc=str(row['latest']),
            )
            for row in rows
        )

    # ---- dry-run plan -----------------------------------------------------

    def plan_capture_revision_purge(
        self, capture_revision_id: str
    ) -> CapturePurgePlan:
        """Compute the exact deletion plan for one revision without deleting.

        A revision is removable only when no persisted promotion or
        composition still consumes one of its runs' records, and no other
        run still references the same evidence — everything else is
        reported as a dependent or a shared retained record.
        """

        with closing(self._connect()) as connection:
            return self._plan(connection, capture_revision_id)

    def _plan(
        self, connection: sqlite3.Connection, capture_revision_id: str
    ) -> CapturePurgePlan:
            runs = connection.execute(
                '''
                SELECT ingestion_run_id, lineage_digest, bundle_digest
                FROM capture_ingestion_runs
                WHERE capture_revision_id=?
                ORDER BY ingestion_run_id ASC
                ''',
                (capture_revision_id,),
            ).fetchall()
            if not runs:
                return CapturePurgePlan(
                    capture_revision_id=capture_revision_id,
                    status='absent',
                    ingestion_run_ids=(),
                    lineage_digests=(),
                    deletable_source_evidence_ids=(),
                    retained_source_evidence_ids=(),
                    deletable_mesh_binding_ids=(),
                    retained_mesh_binding_ids=(),
                    deletable_authority_record_ids=(),
                    retained_authority_record_ids=(),
                    deletable_coordinate_authority_ids=(),
                    retained_coordinate_authority_ids=(),
                    roomplan_record_count=0,
                    blocking_dependents=(),
                    reclaimable_bytes=0,
                    reclaimed_blob_sha256=(),
                )

            run_ids = tuple(str(row['ingestion_run_id']) for row in runs)
            bundle_digest = str(runs[0]['bundle_digest'])
            placeholders = ','.join('?' for _ in run_ids)
            other_runs = {
                str(row['ingestion_run_id'])
                for row in connection.execute(
                    f'''
                    SELECT ingestion_run_id FROM capture_ingestion_runs
                    WHERE ingestion_run_id NOT IN ({placeholders})
                    ''',
                    run_ids,
                ).fetchall()
            }

            dependents: list[CapturePurgeDependent] = []

            promotions = (
                connection.execute(
                    f'''
                    SELECT promotion_id, ingestion_run_id,
                           raw_mesh_binding_id, mesh_composition_id,
                           scene_revision_id
                    FROM capture_semantic_promotions
                    WHERE ingestion_run_id IN ({placeholders})
                    ORDER BY promotion_id ASC
                    ''',
                    run_ids,
                ).fetchall()
                if self._has_table(connection, 'capture_semantic_promotions')
                else []
            )
            dependent_bindings: set[str] = set()
            for row in promotions:
                dependents.append(
                    CapturePurgeDependent(
                        kind='semantic_promotion',
                        identifier=str(row['promotion_id']),
                        detail=(
                            'scene_revision='
                            + str(row['scene_revision_id'])
                        ),
                    )
                )
                if row['raw_mesh_binding_id'] is not None:
                    dependent_bindings.add(str(row['raw_mesh_binding_id']))

            compositions = (
                connection.execute(
                    f'''
                    SELECT composition_id, ingestion_run_id,
                           binding_ids_json
                    FROM capture_mesh_compositions
                    WHERE ingestion_run_id IN ({placeholders})
                    ORDER BY composition_id ASC
                    ''',
                    run_ids,
                ).fetchall()
                if self._has_table(connection, 'capture_mesh_compositions')
                else []
            )
            for row in compositions:
                dependents.append(
                    CapturePurgeDependent(
                        kind='mesh_composition',
                        identifier=str(row['composition_id']),
                        detail=(
                            'ingestion_run='
                            + str(row['ingestion_run_id'])
                        ),
                    )
                )
                for binding_id in json.loads(row['binding_ids_json']):
                    dependent_bindings.add(str(binding_id))

            mesh_rows = connection.execute(
                f'''
                SELECT binding_id, ingestion_run_id
                FROM capture_ingestion_mesh_links
                WHERE ingestion_run_id IN ({placeholders})
                ORDER BY binding_id ASC
                ''',
                run_ids,
            ).fetchall()
            deletable_bindings: list[str] = []
            retained_bindings: list[str] = []
            for row in mesh_rows:
                binding_id = str(row['binding_id'])
                shared = connection.execute(
                    '''
                    SELECT 1 FROM capture_ingestion_mesh_links
                    WHERE binding_id=? AND ingestion_run_id NOT IN (
                '''
                    + placeholders
                    + ') LIMIT 1',
                    (binding_id, *run_ids),
                ).fetchone()
                if binding_id in dependent_bindings or shared is not None:
                    retained_bindings.append(binding_id)
                else:
                    deletable_bindings.append(binding_id)

            authority_rows = connection.execute(
                f'''
                SELECT authority_record_handoff_id, ingestion_run_id
                FROM capture_ingestion_authority_links
                WHERE ingestion_run_id IN ({placeholders})
                ORDER BY authority_record_handoff_id ASC
                ''',
                run_ids,
            ).fetchall()
            deletable_authorities: list[str] = []
            retained_authorities: list[str] = []
            for row in authority_rows:
                authority_id = str(row['authority_record_handoff_id'])
                shared = connection.execute(
                    '''
                    SELECT 1 FROM capture_ingestion_authority_links
                    WHERE authority_record_handoff_id=?
                      AND ingestion_run_id NOT IN (
                '''
                    + placeholders
                    + ') LIMIT 1',
                    (authority_id, *run_ids),
                ).fetchone()
                if shared is None:
                    deletable_authorities.append(authority_id)
                else:
                    retained_authorities.append(authority_id)

            source_rows = connection.execute(
                f'''
                SELECT source_evidence_id, ingestion_run_id
                FROM capture_ingestion_source_links
                WHERE ingestion_run_id IN ({placeholders})
                ORDER BY source_evidence_id ASC
                ''',
                run_ids,
            ).fetchall()
            deletable_evidence: list[str] = []
            retained_evidence: list[str] = []
            # A record this same purge deletes cannot pin the evidence it
            # consumes — only bindings/authorities outside the deletable
            # set keep an evidence row alive.
            deletable_binding_params = tuple(deletable_bindings)
            binding_clause = (
                'AND binding_id NOT IN ('
                + ','.join('?' for _ in deletable_binding_params)
                + ')'
                if deletable_binding_params
                else ''
            )
            deletable_authority_params = tuple(deletable_authorities)
            authority_clause = (
                'AND authority_record_handoff_id NOT IN ('
                + ','.join('?' for _ in deletable_authority_params)
                + ')'
                if deletable_authority_params
                else ''
            )
            for row in source_rows:
                evidence_id = str(row['source_evidence_id'])
                shared_link = connection.execute(
                    '''
                    SELECT 1 FROM capture_ingestion_source_links
                    WHERE source_evidence_id=? AND ingestion_run_id NOT IN (
                    '''
                    + placeholders
                    + ') LIMIT 1',
                    (evidence_id, *run_ids),
                ).fetchone()
                if shared_link is not None:
                    retained_evidence.append(evidence_id)
                    continue
                binding_ref = connection.execute(
                    '''
                    SELECT 1 FROM capture_raw_visual_mesh_bindings
                    WHERE (
                        anchor_index_source_evidence_id=?
                        OR geometry_source_evidence_id=?
                    )
                    '''
                    + binding_clause
                    + ' LIMIT 1',
                    (
                        evidence_id,
                        evidence_id,
                        *deletable_binding_params,
                    ),
                ).fetchone()
                authority_ref = connection.execute(
                    '''
                    SELECT 1 FROM capture_authority_records
                    WHERE source_evidence_id=?
                    '''
                    + authority_clause
                    + ' LIMIT 1',
                    (evidence_id, *deletable_authority_params),
                ).fetchone()
                if binding_ref is not None or authority_ref is not None:
                    retained_evidence.append(evidence_id)
                else:
                    deletable_evidence.append(evidence_id)

            coordinate_rows = connection.execute(
                f'''
                SELECT coordinate_authority_id, registered_by_run_id
                FROM capture_coordinate_authorities
                WHERE bundle_digest=?
                ORDER BY coordinate_authority_id ASC
                ''',
                (bundle_digest,),
            ).fetchall()
            deletable_coordinate: list[str] = []
            retained_coordinate: list[str] = []
            composition_authorities = {
                str(row['coordinate_authority_id'])
                for row in connection.execute(
                    '''
                    SELECT coordinate_authority_id
                    FROM capture_mesh_compositions
                    '''
                ).fetchall()
            } if self._has_table(
                connection, 'capture_mesh_compositions'
            ) else set()
            for row in coordinate_rows:
                authority_id = str(row['coordinate_authority_id'])
                registered_elsewhere = (
                    str(row['registered_by_run_id']) in other_runs
                )
                if (
                    registered_elsewhere
                    or authority_id in composition_authorities
                ):
                    retained_coordinate.append(authority_id)
                else:
                    deletable_coordinate.append(authority_id)

            roomplan_count = int(
                connection.execute(
                    f'''
                    SELECT COUNT(*) FROM capture_roomplan_records
                    WHERE ingestion_run_id IN ({placeholders})
                    ''',
                    run_ids,
                ).fetchone()[0]
            )

            # Reclaimable bytes: payloads whose blob would be garbage
            # collected because no record anywhere references the digest
            # after this plan, plus row metadata sizes. Computed exactly —
            # the GC set is enumerated, not estimated.
            blob_candidates = {
                str(row['payload_sha256'])
                for row in connection.execute(
                    'SELECT payload_sha256 FROM capture_source_evidence '
                    'WHERE source_evidence_id IN ('
                    + ','.join('?' for _ in deletable_evidence)
                    + ')',
                    tuple(deletable_evidence),
                ).fetchall()
            } if deletable_evidence else set()
            exclude_rows: dict[str, tuple[str, set[str]]] = {
                'capture_ingestion_runs': (
                    'ingestion_run_id',
                    set(run_ids),
                ),
                'capture_roomplan_records': (
                    'ingestion_run_id',
                    set(run_ids),
                ),
                'capture_ingestion_source_links': (
                    'ingestion_run_id',
                    set(run_ids),
                ),
                'capture_ingestion_mesh_links': (
                    'ingestion_run_id',
                    set(run_ids),
                ),
                'capture_ingestion_authority_links': (
                    'ingestion_run_id',
                    set(run_ids),
                ),
                'capture_raw_visual_mesh_bindings': (
                    'binding_id',
                    set(deletable_bindings),
                ),
                'capture_authority_records': (
                    'authority_record_handoff_id',
                    set(deletable_authorities),
                ),
                'capture_coordinate_authorities': (
                    'coordinate_authority_id',
                    set(deletable_coordinate),
                ),
            }
            reclaimable_blobs = tuple(
                sorted(
                    sha
                    for sha in blob_candidates
                    if not self._blob_referenced_anywhere(
                        connection, sha,
                        exclude_evidence=set(deletable_evidence),
                        exclude_rows=exclude_rows,
                    )
                )
            )
            reclaimable_bytes = int(
                connection.execute(
                    'SELECT COALESCE(SUM(byte_count), 0) '
                    'FROM htdt_content_blobs WHERE payload_sha256 IN ('
                    + ','.join('?' for _ in reclaimable_blobs)
                    + ')',
                    tuple(reclaimable_blobs),
                ).fetchone()[0]
            ) if reclaimable_blobs else 0
            reclaimable_bytes += int(
                connection.execute(
                    'SELECT COALESCE(SUM(byte_count), 0) '
                    'FROM capture_source_evidence '
                    'WHERE source_evidence_id IN ('
                    + ','.join('?' for _ in deletable_evidence)
                    + ')',
                    tuple(deletable_evidence),
                ).fetchone()[0]
            ) if deletable_evidence else 0

            status: CapturePurgeStatus = (
                'blocked' if dependents else 'ready'
            )
            return CapturePurgePlan(
                capture_revision_id=capture_revision_id,
                status=status,
                ingestion_run_ids=run_ids,
                lineage_digests=tuple(
                    sorted({str(row['lineage_digest']) for row in runs})
                ),
                deletable_source_evidence_ids=tuple(deletable_evidence),
                retained_source_evidence_ids=tuple(retained_evidence),
                deletable_mesh_binding_ids=tuple(deletable_bindings),
                retained_mesh_binding_ids=tuple(retained_bindings),
                deletable_authority_record_ids=tuple(deletable_authorities),
                retained_authority_record_ids=tuple(retained_authorities),
                deletable_coordinate_authority_ids=tuple(
                    deletable_coordinate
                ),
                retained_coordinate_authority_ids=tuple(
                    retained_coordinate
                ),
                roomplan_record_count=roomplan_count,
                blocking_dependents=tuple(dependents),
                reclaimable_bytes=reclaimable_bytes,
                reclaimed_blob_sha256=reclaimable_blobs,
            )

    # ---- purge ------------------------------------------------------------

    def purge_capture_revision(
        self, capture_revision_id: str
    ) -> CapturePurgePlan:
        """Execute the revision purge only when reference-safe.

        The plan is recomputed inside the write transaction: if any
        dependent appeared between planning and purging, or the revision
        is no longer present, the transaction aborts with the prior state
        intact. ``PRAGMA foreign_key_check`` must pass before commit.
        """

        with closing(self._connect()) as connection:
            try:
                connection.execute('BEGIN IMMEDIATE')
                plan = self._plan(connection, capture_revision_id)
                if plan.status == 'absent':
                    raise CaptureRetentionError(
                        'capture revision is not persisted: '
                        f'{capture_revision_id}'
                    )
                if plan.status == 'blocked':
                    ids = '; '.join(
                        f'{dep.kind}:{dep.identifier}'
                        for dep in plan.blocking_dependents
                    )
                    raise CaptureRetentionError(
                        'capture revision still has dependents; refusing '
                        f'purge: {ids}'
                    )
                self._execute_plan(connection, plan)
                violations = connection.execute(
                    'PRAGMA foreign_key_check'
                ).fetchall()
                if violations:
                    raise CaptureRetentionError(
                        'post-purge foreign-key check failed; rolling back'
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return plan

    def _execute_plan(
        self, connection: sqlite3.Connection, plan: CapturePurgePlan
    ) -> None:
        run_ids = plan.ingestion_run_ids
        placeholders = ','.join('?' for _ in run_ids)

        connection.execute(
            f'DELETE FROM capture_ingestion_source_links '
            f'WHERE ingestion_run_id IN ({placeholders})',
            run_ids,
        )
        connection.execute(
            f'DELETE FROM capture_ingestion_mesh_links '
            f'WHERE ingestion_run_id IN ({placeholders})',
            run_ids,
        )
        connection.execute(
            f'DELETE FROM capture_ingestion_authority_links '
            f'WHERE ingestion_run_id IN ({placeholders})',
            run_ids,
        )
        connection.execute(
            f'DELETE FROM capture_roomplan_records '
            f'WHERE ingestion_run_id IN ({placeholders})',
            run_ids,
        )
        if plan.deletable_coordinate_authority_ids:
            connection.execute(
                'DELETE FROM capture_coordinate_authorities '
                'WHERE coordinate_authority_id IN ('
                + ','.join('?' for _ in plan.deletable_coordinate_authority_ids)
                + ')',
                plan.deletable_coordinate_authority_ids,
            )
        if plan.deletable_mesh_binding_ids:
            connection.execute(
                'DELETE FROM capture_raw_visual_mesh_bindings '
                'WHERE binding_id IN ('
                + ','.join('?' for _ in plan.deletable_mesh_binding_ids)
                + ')',
                plan.deletable_mesh_binding_ids,
            )
        if plan.deletable_authority_record_ids:
            connection.execute(
                'DELETE FROM capture_authority_records '
                'WHERE authority_record_handoff_id IN ('
                + ','.join('?' for _ in plan.deletable_authority_record_ids)
                + ')',
                plan.deletable_authority_record_ids,
            )
        if plan.deletable_source_evidence_ids:
            connection.execute(
                'DELETE FROM capture_source_evidence '
                'WHERE source_evidence_id IN ('
                + ','.join('?' for _ in plan.deletable_source_evidence_ids)
                + ')',
                plan.deletable_source_evidence_ids,
            )
        connection.execute(
            f'DELETE FROM capture_ingestion_runs '
            f'WHERE ingestion_run_id IN ({placeholders})',
            run_ids,
        )
        self._purge_inbox_bookkeeping(connection, plan)
        # Content blobs are GC'd only when nothing left in the catalog
        # still references the digest — conservative by construction.
        for sha in plan.reclaimed_blob_sha256:
            connection.execute(
                'DELETE FROM htdt_content_blobs WHERE payload_sha256=?',
                (sha,),
            )

    def _purge_inbox_bookkeeping(
        self, connection: sqlite3.Connection, plan: CapturePurgePlan
    ) -> None:
        """Remove Capture Inbox review entries for purged lineages.

        Inbox rows are a review queue over retained payloads; once the
        payload is gone the queue entry can only strand (``inspect`` fails
        as store corruption), so it is deleted with the run rows.
        Supersessions/registrations naming a purged lineage dangle on
        either side and go with it. An item flipped ``superseded`` by a
        lineage that was just purged reverts to its promoted-derived
        disposition: the replacement claim no longer exists.
        """

        digests = plan.lineage_digests
        if not digests or not self._has_table(
            connection, 'capture_inbox_items'
        ):
            return
        placeholders = ','.join('?' for _ in digests)
        if self._has_table(connection, 'capture_inbox_promotions'):
            connection.execute(
                f'DELETE FROM capture_inbox_promotions '
                f'WHERE lineage_digest IN ({placeholders})',
                digests,
            )
        if self._has_table(connection, 'capture_inbox_supersessions'):
            connection.execute(
                f'DELETE FROM capture_inbox_supersessions '
                f'WHERE superseded_lineage_digest IN ({placeholders}) '
                f'OR superseding_lineage_digest IN ({placeholders})',
                digests + digests,
            )
        if self._has_table(connection, 'capture_inbox_registrations'):
            connection.execute(
                f'DELETE FROM capture_inbox_registrations '
                f'WHERE older_lineage_digest IN ({placeholders}) '
                f'OR newer_lineage_digest IN ({placeholders})',
                digests + digests,
            )
        connection.execute(
            f'DELETE FROM capture_inbox_items '
            f'WHERE lineage_digest IN ({placeholders})',
            digests,
        )

        if not self._has_table(connection, 'capture_inbox_supersessions'):
            return
        stuck = connection.execute(
            "SELECT lineage_digest FROM capture_inbox_items "
            "WHERE disposition='superseded'"
        ).fetchall()
        for row in stuck:
            digest = str(row['lineage_digest'])
            covered = {
                str(entry['authority_kind'])
                for entry in connection.execute(
                    'SELECT authority_kind FROM capture_inbox_supersessions '
                    'WHERE superseded_lineage_digest=?',
                    (digest,),
                ).fetchall()
            }
            promoted = set()
            if self._has_table(connection, 'capture_inbox_promotions'):
                promoted = {
                    str(entry['authority_kind'])
                    for entry in connection.execute(
                        'SELECT authority_kind FROM capture_inbox_promotions '
                        "WHERE lineage_digest=? AND outcome='promoted'",
                        (digest,),
                    ).fetchall()
                }
            if promoted and promoted <= covered:
                continue  # still fully superseded by surviving records
            disposition = (
                'partially_promoted'
                if promoted and promoted < set(
                    self._available_authority_kinds(connection, digest)
                )
                else 'promoted'
                if promoted
                else 'pending'
            )
            connection.execute(
                'UPDATE capture_inbox_items SET disposition=?, '
                'disposition_reason=?, disposition_at_utc=? '
                'WHERE lineage_digest=?',
                (disposition, '', None, digest),
            )

    def _available_authority_kinds(
        self, connection: sqlite3.Connection, lineage_digest: str
    ) -> set[str]:
        """Authority kinds the surviving plan offers, for re-deriving a
        reverted disposition. Returns an empty set when the item's own
        plan can no longer be read. Reads inside the purge transaction —
        no second connection, no schema bootstrap."""
        row = connection.execute(
            'SELECT plan_json FROM capture_ingestion_runs '
            'WHERE lineage_digest=? '
            'ORDER BY recorded_at_utc DESC, ingestion_run_id DESC LIMIT 1',
            (lineage_digest,),
        ).fetchone()
        if row is None:
            return set()
        from .capture_inbox import plan_authority_kinds
        from .capture_ingestion_transaction import CaptureIngestionPlan

        plan = CaptureIngestionPlan.model_validate_json(
            str(row['plan_json'])
        )
        return set(plan_authority_kinds(plan))

    # ---- shared referrers -------------------------------------------------

    @staticmethod
    def _has_table(connection: sqlite3.Connection, table: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            is not None
        )

    def _blob_referenced_anywhere(
        self,
        connection: sqlite3.Connection,
        payload_sha256: str,
        *,
        exclude_evidence: set[str],
        exclude_rows: dict[str, tuple[str, set[str]]] | None = None,
    ) -> bool:
        """True if any surviving record can still resolve this digest.

        The check is catalog-wide, not just capture-local: any TEXT column
        in any table that embeds the digest (JSON payloads, references,
        repair bundles, scene revisions) keeps the blob. ``exclude_evidence``
        skips the evidence rows this same plan is deleting; ``exclude_rows``
        maps a table to (key column, ids the plan deletes) so rows going
        away with this purge do not keep their own blobs alive.
        """

        rows = connection.execute(
            'SELECT source_evidence_id FROM capture_source_evidence '
            'WHERE payload_sha256=?'
            ,
            (payload_sha256,),
        ).fetchall()
        if any(
            str(row['source_evidence_id']) not in exclude_evidence
            for row in rows
        ):
            return True
        for table, in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT IN ('htdt_content_blobs', "
            "'capture_source_evidence', 'sqlite_sequence')"
        ).fetchall():
            table = str(table)
            excluded = (exclude_rows or {}).get(table)
            for col in connection.execute(
                f'PRAGMA table_info("{table}")'
            ).fetchall():
                if str(col['type']).upper() not in ('TEXT', ''):
                    continue
                query = f'SELECT 1 FROM "{table}" WHERE "{col["name"]}" LIKE ?'
                params: list[str] = ['%' + payload_sha256 + '%']
                if excluded is not None:
                    key_column, excluded_ids = excluded
                    if excluded_ids:
                        query += (
                            f' AND "{key_column}" NOT IN ('
                            + ','.join('?' for _ in excluded_ids)
                            + ')'
                        )
                        params.extend(sorted(excluded_ids))
                try:
                    found = connection.execute(
                        query + ' LIMIT 1', params
                    ).fetchone()
                except sqlite3.DatabaseError:
                    continue
                if found is not None:
                    return True
        return False



