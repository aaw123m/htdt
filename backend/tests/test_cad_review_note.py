"""#730: bounded review notes pinned to exact authority."""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_review_note import (
    ReviewNoteRepository,
    add_review_note,
    resolve_review_note,
)


def test_note_pins_exact_authority() -> None:
    note = add_review_note(
        document_id='doc-1',
        subject_kind='scene_revision',
        subject_ref='rev-7',
        subject_sha256='a' * 64,
        author_label='installer-kai',
        body='Subwoofer toe-in deviates from approved layout.',
        created_at_utc='2026-09-24T00:00:00+00:00',
    )
    assert note.subject_ref == 'rev-7'
    assert note.resolution == 'open'
    ref = note.authority_ref()
    assert ref.semantic_hash_sha256 == note.semantic_sha256


def test_resolution_transition_mints_new_note_state() -> None:
    note = add_review_note(
        document_id='doc-1',
        subject_kind='commissioning_deviation',
        subject_ref='dev-3',
        author_label='owner',
        body='Accept the rack door swing as built.',
        created_at_utc='2026-09-24T00:00:00+00:00',
    )
    accepted = resolve_review_note(note, 'accepted')
    assert accepted.note_id == note.note_id
    assert accepted.resolution == 'accepted'
    assert accepted.semantic_sha256 != note.semantic_sha256
    # The reviewed authority is never mutated by annotation.
    assert accepted.subject_ref == 'dev-3'


def test_author_is_label_not_account() -> None:
    note = add_review_note(
        document_id='doc-1',
        subject_kind='design_alternative',
        subject_ref='alt-2',
        author_label='Kai (no login required)',
        body='Prefer alternative B for sightline reasons.',
        created_at_utc='2026-09-24T00:00:00+00:00',
    )
    assert note.author_label == 'Kai (no login required)'


def test_subject_kind_is_closed() -> None:
    for kind in (
        'scene_revision',
        'system_variant',
        'commissioning_deviation',
        'design_alternative',
        'installation_evidence',
        'project_document',
    ):
        note = add_review_note(
            document_id='doc-1',
            subject_kind=kind,
            subject_ref='x',
            author_label='a',
            body='b',
            created_at_utc='2026-09-24T00:00:00+00:00',
        )
        assert note.subject_kind == kind
    with pytest.raises(ValueError):
        add_review_note(
            document_id='doc-1',
            subject_kind='slack_thread',
            subject_ref='x',
            author_label='a',
            body='b',
        )


def test_repository_round_trip(tmp_path: Path) -> None:
    repo = ReviewNoteRepository(tmp_path / 'cad.sqlite3')
    note = add_review_note(
        document_id='doc-1',
        subject_kind='scene_revision',
        subject_ref='rev-7',
        author_label='installer',
        body='Note one',
        created_at_utc='2026-09-24T00:00:00+00:00',
    )
    repo.save_note(note)
    assert repo.get_note(note.note_id) == note

    accepted = resolve_review_note(note, 'accepted')
    repo.save_note(accepted)
    assert repo.get_note(note.note_id).resolution == 'accepted'

    other = add_review_note(
        document_id='doc-1',
        subject_kind='scene_revision',
        subject_ref='rev-7',
        author_label='owner',
        body='Note two',
        created_at_utc='2026-09-24T01:00:00+00:00',
    )
    repo.save_note(other)
    assert [
        n.note_id
        for n in repo.list_notes_for_subject('doc-1', 'rev-7')
    ] == [note.note_id, other.note_id]
