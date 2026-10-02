"""Backend coverage for the #450 project/document library.

The contract is metadata authority over projects (UUIDv4 identity bound to a
document_id, display names that are presentation only, last-opened recents,
archiving) plus the migration that registers every document already carrying
scene content — including the labelled legacy default-document cases.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    F1_DOCUMENT_ID,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    make_f1_scene,
)
from htdt.default_document import LEGACY_PROJECT_LABEL, SYNTHETIC_FIXTURE_LABEL
from htdt.project_library import (
    ProjectArchivedError,
    ProjectLibraryError,
    ProjectNotFoundError,
)
from htdt.project_library_repository import (
    DEFAULT_PROJECT_NAME,
    ProjectLibraryRepository,
)


def _repository(tmp_path: Path) -> SceneRepository:
    return SceneRepository(tmp_path / 'cad-scenes.sqlite3')


def _scene(document_id: str, *, speaker_x: float = 1.0) -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                position=Position3(x_m=speaker_x, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.22, y_m=0.28, z_m=0.42),
                speaker_role='FL',
            ),
        ),
    )


def test_startup_resolution_creates_fresh_default_project(tmp_path):
    """An empty library gets a brand-new project — startup never binds to
    the legacy default document id implicitly (#781)."""

    library = ProjectLibraryRepository(_repository(tmp_path))

    entry = library.resolve_startup_document(None)

    assert entry.document_id != F1_DOCUMENT_ID
    assert entry.display_name == DEFAULT_PROJECT_NAME
    assert entry.last_opened_at_utc is not None
    assert library.get_by_document_id(F1_DOCUMENT_ID) is None
    # The created project is durable and is what the next launch reopens.
    assert (
        library.resolve_startup_document(None).project_id
        == entry.project_id
    )


def test_startup_resolution_registers_explicit_document(tmp_path):
    library = ProjectLibraryRepository(_repository(tmp_path))

    entry = library.resolve_startup_document('document-x')

    assert entry.document_id == 'document-x'
    assert entry.display_name == 'document-x'
    # And it outranks the untouched default on the next open.
    assert library.resolve_startup_document(None).project_id == entry.project_id


def test_lifecycle_registered_project_reads_as_library_entry(tmp_path):
    """The lifecycle registry and the library repository share one table:
    a row written by ``ProjectLibrary.register_project`` (template
    instantiation, document adoption) must satisfy the repository layer's
    UUIDv4 identity contract — a non-hyphenated id would otherwise poison
    every listing."""
    from htdt.project_lifecycle import ProjectLibrary

    lifecycle = ProjectLibrary(tmp_path / 'cad-scenes.sqlite3')
    record = lifecycle.register_project('doc-template-1', display_name='From Template')

    library = ProjectLibraryRepository(_repository(tmp_path))
    entry = library.get_by_document_id('doc-template-1')
    assert entry is not None
    assert entry.project_id == record.project_id
    assert library.list_projects()


def test_migration_registers_existing_documents_as_projects(tmp_path):
    repository = _repository(tmp_path)
    repository.save(_scene('document-alpha'), parent_revision_id=None)

    library = ProjectLibraryRepository(repository)

    entry = library.get_by_document_id('document-alpha')
    assert entry is not None
    assert entry.display_name == 'document-alpha'
    # Idempotent: re-opening the library never re-registers.
    library2 = ProjectLibraryRepository(repository)
    assert len(library2.list_projects()) == 1


def test_migration_labels_synthetic_fixture(tmp_path):
    repository = _repository(tmp_path)
    repository.save(make_f1_scene(), parent_revision_id=None)

    library = ProjectLibraryRepository(repository)

    assert (
        library.get_by_document_id(F1_DOCUMENT_ID).display_name
        == SYNTHETIC_FIXTURE_LABEL
    )


def test_migration_labels_legacy_project(tmp_path):
    repository = _repository(tmp_path)
    repository.save(
        _scene(F1_DOCUMENT_ID), parent_revision_id=None
    )

    library = ProjectLibraryRepository(repository)

    assert (
        library.get_by_document_id(F1_DOCUMENT_ID).display_name
        == LEGACY_PROJECT_LABEL
    )


def test_create_open_recent_and_rename(tmp_path):
    library = ProjectLibraryRepository(_repository(tmp_path))
    first = library.create_project('Alpha')
    second = library.create_project('Beta')

    assert len({first.project_id, second.project_id}) == 2
    assert len({first.document_id, second.document_id}) == 2

    opened = library.open_project(first.project_id)
    assert opened.last_opened_at_utc is not None
    assert library.most_recent_project().project_id == first.project_id
    assert [e.project_id for e in library.recent_projects()] == [
        first.project_id,
        second.project_id,
    ]

    renamed = library.rename_project(first.project_id, '  Alpha One  ')
    assert renamed.display_name == 'Alpha One'
    assert renamed.document_id == first.document_id
    assert library.get_by_document_id(first.document_id).display_name == (
        'Alpha One'
    )


def test_create_rejects_empty_name_and_document_collision(tmp_path):
    library = ProjectLibraryRepository(_repository(tmp_path))
    with pytest.raises(ProjectLibraryError):
        library.create_project('   ')

    entry = library.create_project('Alpha')
    with pytest.raises(ProjectLibraryError):
        library.create_project('Other', document_id=entry.document_id)


def test_archive_hides_and_blocks_open(tmp_path):
    library = ProjectLibraryRepository(_repository(tmp_path))
    entry = library.create_project('Alpha')

    archived = library.set_archived(entry.project_id, True)
    assert archived.archived is True
    assert library.list_projects() == ()
    assert library.list_projects(include_archived=True)[0].archived
    assert library.most_recent_project() is None
    with pytest.raises(ProjectArchivedError):
        library.open_project(entry.project_id)

    restored = library.set_archived(entry.project_id, False)
    assert restored.archived is False
    assert library.most_recent_project().project_id == entry.project_id


def test_unknown_project_raises(tmp_path):
    library = ProjectLibraryRepository(_repository(tmp_path))
    with pytest.raises(ProjectNotFoundError):
        library.open_project('00000000-0000-4000-8000-000000000000')
    with pytest.raises(ProjectNotFoundError):
        library.rename_project(
            '00000000-0000-4000-8000-000000000000', 'x'
        )


def test_duplicate_project_clones_head_with_new_identity(tmp_path):
    repository = _repository(tmp_path)
    library = ProjectLibraryRepository(repository)
    source = library.create_project('Alpha')
    head = repository.save(
        _scene(source.document_id, speaker_x=2.5), parent_revision_id=None
    ).revision

    clone = library.duplicate_project(source.project_id, 'Alpha copy')

    assert clone.project_id != source.project_id
    assert clone.document_id != source.document_id
    assert clone.cloned_from_project_id == source.project_id
    assert clone.source_revision_id == head.revision_id

    cloned_head = repository.current_head(clone.document_id)
    assert cloned_head is not None
    # The clone carries the source scene's content under a new document
    # identity (document_id is part of the canonical payload, so the hash
    # differs by design).
    assert cloned_head.document.document_id == clone.document_id
    assert cloned_head.document.entities == head.document.entities
    assert cloned_head.document.room == head.document.room
    # The clone is a separate lineage, not a continuation of the source graph.
    assert cloned_head.parent_revision_id is None
    # Source remains untouched and both are listed.
    assert repository.current_head(source.document_id).revision_id == (
        head.revision_id
    )
    assert len(library.list_projects()) == 2


def test_duplicate_empty_project(tmp_path):
    library = ProjectLibraryRepository(_repository(tmp_path))
    source = library.create_project('Empty')

    clone = library.duplicate_project(source.project_id, 'Empty copy')

    assert clone.source_revision_id is None
    assert clone.cloned_from_project_id == source.project_id


def test_ensure_document_registered_is_idempotent(tmp_path):
    library = ProjectLibraryRepository(_repository(tmp_path))

    entry = library.ensure_document_registered('doc-9')
    again = library.ensure_document_registered('doc-9', 'renamed-ignored')

    assert again.project_id == entry.project_id
    assert again.display_name == 'doc-9'


def test_resolve_startup_prefers_most_recent(tmp_path):
    library = ProjectLibraryRepository(_repository(tmp_path))
    older = library.create_project('Older')
    newer = library.create_project('Newer')
    library.open_project(older.project_id)
    library.open_project(newer.project_id)

    assert library.resolve_startup_document(None).project_id == (
        newer.project_id
    )
    # Explicit override wins over recency.
    assert library.resolve_startup_document(
        'other-doc'
    ).document_id == 'other-doc'


def test_library_entry_model_validates_uuid_identity(tmp_path):
    library = ProjectLibraryRepository(_repository(tmp_path))
    entry = library.create_project('Alpha')

    assert isinstance(entry.project_id, str)
    assert entry.archived is False
    assert entry.last_opened_at_utc is None
