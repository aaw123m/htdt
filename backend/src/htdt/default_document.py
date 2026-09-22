"""Conservative classification of the legacy default document identity (#627).

Pre-#627 builds silently persisted the synthetic F1 fixture scene as a
canonical SceneRevision the first time the default document was opened. Data
directories created by those builds may therefore contain an F1 revision graph
whose true provenance is unknown: untouched synthetic fixture, or a real user
project that started from it and accumulated edits and evidence.

This module classifies what is actually persisted so migration and
presentation layers can act conservatively:

- ``absent``: no revision history exists. Nothing to migrate; normal startup
  keeps the document empty.
- ``synthetic_fixture``: the revision graph is exactly the single known
  historical seed revision and no document-scoped evidence (measurements,
  predictions, search artifacts, recovery drafts, ...) is attached. Such data
  may be archived or offered as an *Example/Synthetic* project without being
  confused with user work.
- ``legacy_project``: anything else. The content differs from the fixture or
  user evidence exists, so it is preserved and surfaced as a legacy project —
  it is never overwritten with a new empty scene.

Classification never writes; it is a read-only provenance report.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import sqlite3
from typing import Literal

from .cad_repository import SceneRepository
from .cad_scene import F1_DOCUMENT_ID, make_f1_scene, scene_content_hash


DefaultDocumentKind = Literal['absent', 'synthetic_fixture', 'legacy_project']

SYNTHETIC_FIXTURE_LABEL = 'Example project (synthetic fixture)'
LEGACY_PROJECT_LABEL = 'Legacy project (previous default document)'

# Tables that never count as attached user evidence for classification:
# ``scene_revisions`` is the revision graph being classified itself,
# ``scene_document_heads`` is its derived head pointer (#626), and
# ``editor_view_states`` is disposable UI convenience state (selection,
# hidden/locked ids) that says nothing about physical project work. Every
# other document-scoped row — measurements, predictions, search artifacts,
# recovery drafts — is treated conservatively as user work.
_NON_EVIDENCE_TABLES = frozenset(
    {'scene_revisions', 'scene_document_heads', 'editor_view_states'}
)


@dataclass(frozen=True)
class DefaultDocumentReport:
    """Read-only provenance report for one document identity."""

    document_id: str
    kind: DefaultDocumentKind
    label: str | None
    revision_count: int
    matches_known_seed: bool
    has_attached_evidence: bool


def _revision_graph(connection: sqlite3.Connection, document_id: str) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            'SELECT revision_id, parent_revision_id, content_hash '
            'FROM scene_revisions WHERE document_id=? ORDER BY seq',
            (document_id,),
        ).fetchall()
    )


def _document_scoped_tables(connection: sqlite3.Connection) -> tuple[str, ...]:
    names = {
        str(row['name'])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    tables: list[str] = []
    for name in sorted(names - _NON_EVIDENCE_TABLES):
        columns = {
            str(row['name'])
            for row in connection.execute(f'PRAGMA table_info("{name}")').fetchall()
        }
        if 'document_id' in columns:
            tables.append(name)
    return tuple(tables)


def _has_attached_evidence(
    connection: sqlite3.Connection,
    document_id: str,
) -> bool:
    for table in _document_scoped_tables(connection):
        row = connection.execute(
            f'SELECT EXISTS(SELECT 1 FROM "{table}" WHERE document_id=?)',
            (document_id,),
        ).fetchone()
        if row is not None and row[0]:
            return True
    return False


def classify_default_document(
    repository: SceneRepository,
    document_id: str = F1_DOCUMENT_ID,
) -> DefaultDocumentReport:
    """Classify what a data directory actually contains for ``document_id``.

    The fixture match is deliberately exact — one root revision whose content
    hash equals the canonical ``make_f1_scene()`` payload — so a single edit,
    extra revision, or attached evidence row already classifies the document
    as a legacy user project. Doubt always resolves toward preservation.
    """

    connection = sqlite3.connect(repository.path)
    connection.row_factory = sqlite3.Row
    try:
        has_revision_table = bool(
            connection.execute(
                "SELECT EXISTS(SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='scene_revisions')"
            ).fetchone()[0]
        )
        revisions = (
            _revision_graph(connection, document_id) if has_revision_table else []
        )
        if not revisions:
            return DefaultDocumentReport(
                document_id=document_id,
                kind='absent',
                label=None,
                revision_count=0,
                matches_known_seed=False,
                has_attached_evidence=False,
            )
        seed_hash = scene_content_hash(make_f1_scene())
        matches_known_seed = (
            len(revisions) == 1
            and revisions[0]['parent_revision_id'] is None
            and revisions[0]['content_hash'] == seed_hash
        )
        has_evidence = _has_attached_evidence(connection, document_id)
    finally:
        connection.close()

    if matches_known_seed and not has_evidence:
        return DefaultDocumentReport(
            document_id=document_id,
            kind='synthetic_fixture',
            label=SYNTHETIC_FIXTURE_LABEL,
            revision_count=len(revisions),
            matches_known_seed=True,
            has_attached_evidence=False,
        )
    return DefaultDocumentReport(
        document_id=document_id,
        kind='legacy_project',
        label=LEGACY_PROJECT_LABEL,
        revision_count=len(revisions),
        matches_known_seed=matches_known_seed,
        has_attached_evidence=has_evidence,
    )


def log_default_document_classification(
    repository: SceneRepository,
    logger: logging.Logger,
    document_id: str = F1_DOCUMENT_ID,
) -> DefaultDocumentReport | None:
    """Classify the legacy default document and record the outcome.

    Transitional surfacing for #627 section 3 until the #450 project library
    owns presentation: a preserved legacy project or an untouched synthetic
    fixture is written to the diagnostics log at startup so the distinction is
    observable without altering any persisted data. An absent default document
    is the normal fresh-install case and stays silent.

    Best-effort like every diagnostics sink: a classification failure is
    logged as a warning and never propagates into startup.
    """

    try:
        report = classify_default_document(repository, document_id)
    except Exception as exc:
        logger.warning(
            'default document classification failed: document=%s error=%s',
            document_id,
            exc,
        )
        return None
    if report.kind != 'absent':
        logger.info(
            'default document classification: document=%s kind=%s '
            'revisions=%d matches_known_seed=%s attached_evidence=%s label=%s',
            report.document_id,
            report.kind,
            report.revision_count,
            report.matches_known_seed,
            report.has_attached_evidence,
            report.label,
        )
    return report


__all__ = [
    'DefaultDocumentKind',
    'DefaultDocumentReport',
    'LEGACY_PROJECT_LABEL',
    'SYNTHETIC_FIXTURE_LABEL',
    'classify_default_document',
    'log_default_document_classification',
]
