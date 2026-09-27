"""Wire-format pin for the pydantic ``schema`` field — round 2 finding.

Round 1 flagged that every authority model declares a field named ``schema``
which shadows ``BaseModel.schema()``. Renaming it was deferred pending a
wire-compat check. The check is settled here: the field is serialized under
its own name by ``model_dump()`` (no alias is configured) and those payloads
persist verbatim into SQLite JSON columns and capture bundles, so the Python
name IS the wire key. Renaming it would silently rewrite every stored
payload. These tests pin the current contract so a future rename must be a
deliberate, migration-aware change rather than an accidental one.
"""

from __future__ import annotations

import json

from htdt.cad_review_note import ReviewNote, add_review_note


def _note() -> ReviewNote:
    return add_review_note(
        document_id='doc-1',
        subject_kind='project_document',
        subject_ref='dataset:abc',
        author_label='reviewer',
        body='確認済み',
    )


def test_schema_field_serializes_under_literal_key() -> None:
    note = _note()
    dumped = note.model_dump(mode='json')
    assert dumped['schema'] == 'htdt.review-note'
    # The serialized key is the Python field name — not an alias.
    assert 'schema' in dumped
    assert ReviewNote.model_fields['schema'].alias is None
    assert ReviewNote.model_fields['schema'].serialization_alias is None


def test_schema_key_survives_json_round_trip() -> None:
    note = _note()
    payload = json.loads(note.model_dump_json())
    assert payload['schema'] == 'htdt.review-note'
    restored = ReviewNote.model_validate(payload)
    assert restored == note


def test_schema_shadowing_does_not_break_model_schema() -> None:
    # ``Model.schema()`` is the classmethod (JSON schema), while ``model.schema``
    # is the instance field — the shadowing is a wart but harmless because
    # pydantic resolves both correctly. Pin both behaviors.
    note = _note()
    assert note.schema == 'htdt.review-note'
    json_schema = ReviewNote.model_json_schema()
    assert json_schema['properties']['schema']['const'] == 'htdt.review-note'
