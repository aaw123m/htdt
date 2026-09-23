from __future__ import annotations

import pytest

from htdt.project_identity import (
    HTDTLegacyProjectRef,
    HTDTProjectDestination,
    HTDTProjectReference,
    ProjectIdentityConflictError,
    ProjectIdentityError,
    assert_destination_matches,
    assert_project_identity_compatible,
    classify_project_reference,
    duplicate_project_reference,
    migrate_project_reference,
    new_project_reference,
    resolve_project_reference,
)


def test_new_reference_mints_uuid4_identity() -> None:
    ref = new_project_reference(
        project_name='Theater',
        document_id='doc-1',
        room_id='room-1',
        room_name='Den',
    )
    assert len(ref.project_id) == 36
    assert ref.document_id == 'doc-1'
    assert ref.cloned_from_project_id is None
    # Display names are free to differ; they are not identity.
    assert ref.project_name == 'Theater'


def test_duplicate_is_distinct_identity_with_lineage() -> None:
    source = new_project_reference(
        project_name='Theater', document_id='doc-1', room_id='room-1'
    )
    clone = duplicate_project_reference(source)
    assert clone.project_id != source.project_id
    assert clone.cloned_from_project_id == source.project_id
    assert clone.document_id == source.document_id
    assert clone.room_id == source.room_id


def test_migration_retains_identity_verbatim() -> None:
    source = new_project_reference(
        project_name='Theater', document_id='doc-1', room_id='room-1'
    )
    migrated = migrate_project_reference(source)
    assert migrated == source
    assert migrated.project_id == source.project_id


def test_resolve_structured_and_legacy_references() -> None:
    ref = new_project_reference(document_id='doc-1')
    structured = resolve_project_reference(ref.model_dump(mode='json'))
    assert isinstance(structured, HTDTProjectReference)
    assert structured.project_id == ref.project_id

    legacy = resolve_project_reference('opaque-projectRef-string')
    assert isinstance(legacy, HTDTLegacyProjectRef)
    assert legacy.legacy_project_ref == 'opaque-projectRef-string'

    legacy_typed = resolve_project_reference(
        {
            'schema': 'htdt.project-reference.legacy',
            'schema_version': 1,
            'legacy_project_ref': 'opaque-projectRef-string',
        }
    )
    assert isinstance(legacy_typed, HTDTLegacyProjectRef)

    with pytest.raises(ProjectIdentityError):
        resolve_project_reference(42)


def test_compatibility_conflict_on_identity_fields() -> None:
    issuing = new_project_reference(document_id='doc-1', room_id='room-1')
    candidate = issuing.model_copy(update={'project_name': 'Renamed'})
    # Name drift never fails — renames must not break routing.
    assert_project_identity_compatible(issuing, candidate)

    other_id = (
        'f' + issuing.project_id[1:]
        if issuing.project_id[0] != 'f'
        else 'e' + issuing.project_id[1:]
    )
    wrong_project = candidate.model_copy(update={'project_id': other_id})
    assert wrong_project.project_id != issuing.project_id
    with pytest.raises(ProjectIdentityConflictError):
        assert_project_identity_compatible(issuing, wrong_project)

    wrong_doc = candidate.model_copy(update={'document_id': 'doc-2'})
    with pytest.raises(ProjectIdentityConflictError):
        assert_project_identity_compatible(issuing, wrong_doc)

    wrong_room = candidate.model_copy(update={'room_id': 'room-2'})
    with pytest.raises(ProjectIdentityConflictError):
        assert_project_identity_compatible(issuing, wrong_room)


def test_destination_routing_requires_exact_project() -> None:
    pinned = new_project_reference(document_id='doc-1', room_id='room-1')
    destination = HTDTProjectDestination(
        project=pinned,
        receiver_instance_id='50000000-0000-4000-8000-000000000050',
        destination_id='60000000-0000-4000-8000-000000000060',
        routing_generation=1,
    )
    # Exact pinned project routes.
    assert_destination_matches(pinned, destination)
    other = new_project_reference(document_id='doc-1', room_id='room-1')
    other_destination = HTDTProjectDestination(
        project=other,
        receiver_instance_id='50000000-0000-4000-8000-000000000050',
        destination_id='60000000-0000-4000-8000-000000000060',
        routing_generation=1,
    )
    with pytest.raises(ProjectIdentityConflictError):
        assert_destination_matches(pinned, other_destination)


def test_classification_by_identity_not_name() -> None:
    known = new_project_reference(
        project_name='Shared Name', document_id='doc-1'
    )
    clone = duplicate_project_reference(known)
    stranger = new_project_reference(
        project_name='Shared Name', document_id='doc-1'
    )
    legacy = resolve_project_reference('opaque')

    assert (
        classify_project_reference(known, [known]) == 'exact_project_match'
    )
    assert (
        classify_project_reference(clone, [known]) == 'known_project_lineage'
    )
    # Same display name but different identity is still unknown.
    assert (
        classify_project_reference(stranger, [known])
        == 'unknown_project_reference'
    )
    assert classify_project_reference(legacy, [known]) == 'legacy_project_ref'
