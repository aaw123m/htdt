"""Scene revision value records — Qt-free domain types (#807 boundary refactor).

``SceneRevision`` / ``SceneRevisionSummary`` / ``SaveResult`` /
``RecoverySnapshot`` are the persisted-document value types handed back by
``SceneRepository``. They depend only on ``cad_scene``'s document model, so
they live at domain rank; ``cad_repository`` re-exports them, which keeps
every existing ``cad_repository import SceneRevision`` path working while
packaged domain modules (measurement) can take the records without pulling
in the persistence layer.
"""

from __future__ import annotations

from dataclasses import dataclass

from .cad_scene import SceneDocument


@dataclass(frozen=True)
class SceneRevision:
    revision_id: str
    document_id: str
    parent_revision_id: str | None
    created_at_utc: str
    content_hash: str
    document: SceneDocument
    #: True when the revision is deliberate non-head lineage: it was written
    #: by ``save_detached_revision`` (or reconstructed as off-mainline during
    #: head migration) and never became the document's current head.
    detached: bool = False
    #: Optional provenance note for detached lineage (fixture, analytical
    #: candidate materialization, historical comparison, ...).
    detached_reason: str | None = None


@dataclass(frozen=True)
class SceneRevisionSummary:
    """Compact revision metadata for history browsing (#663).

    Carries lineage and identity only — the payload column is never read,
    so listing several hundred revisions stays O(metadata) rather than
    O(total project bytes). ``payload_bytes`` reports the stored payload
    size so surfaces can show relative revision weight without decoding.
    """

    revision_id: str
    document_id: str
    parent_revision_id: str | None
    created_at_utc: str
    content_hash: str
    detached: bool
    detached_reason: str | None
    payload_bytes: int


@dataclass(frozen=True)
class SaveResult:
    revision: SceneRevision
    created: bool


@dataclass(frozen=True)
class RecoverySnapshot:
    document_id: str
    source_revision_id: str | None
    updated_at_utc: str
    content_hash: str
    document: SceneDocument

__all__ = [
    'RecoverySnapshot',
    'SaveResult',
    'SceneRevision',
    'SceneRevisionSummary',
]
