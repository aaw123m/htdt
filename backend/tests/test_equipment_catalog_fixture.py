from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from htdt.cad_equipment_catalog import EquipmentCatalogSnapshot


FIXTURE_PATH = (
    Path(__file__).parent / 'fixtures' / 'equipment_catalog_snapshot_v1.json'
)


def _fixture_bytes() -> bytes:
    return FIXTURE_PATH.read_bytes()


def test_pinned_snapshot_fixture_validates_and_reproduces_bytes() -> None:
    raw = _fixture_bytes()
    document = json.loads(raw.decode('utf-8'))
    snapshot = EquipmentCatalogSnapshot.model_validate(document)

    assert snapshot.authority_version == 'o100c-equipment-definition-1'
    assert len(snapshot.definitions) == 3
    # The pinned bytes are the contract HTDT-Capture consumes: the real model
    # must round-trip them exactly (canonical serialization stability).
    assert snapshot.canonical_bytes() == raw
    # Stable pinned digest — any protocol drift in serialization changes this.
    assert sha256(raw).hexdigest() == (
        '8d776e86a5365f20de27d06e2502cc84d155357a8507a5a3b'
        '39a2ce62ba5891d'
    )


def test_fixture_entry_ordering_and_identities() -> None:
    snapshot = EquipmentCatalogSnapshot.model_validate_json(
        _fixture_bytes().decode('utf-8')
    )
    ids = [entry.definition_id for entry in snapshot.definitions]
    # Ordering is the catalog contract: sorted by (manufacturer, model,
    # user_label, ...) with empty/None identity fields first.
    assert ids == ['fixture-av-receiver', 'fixture-bookshelf', 'fixture-sub']
    kinds = {entry.definition_id: entry.identity_kind for entry in snapshot.definitions}
    assert kinds['fixture-av-receiver'] == 'user_defined'
    assert kinds['fixture-bookshelf'] == 'manufacturer'


def test_fixture_rejects_wrong_schema() -> None:
    document = json.loads(_fixture_bytes().decode('utf-8'))
    document['schema'] = 'htdt.equipment.catalog-snapshot.v2'
    with pytest.raises(ValueError):
        EquipmentCatalogSnapshot.model_validate(document)


def test_fixture_rejects_wrong_schema_version() -> None:
    document = json.loads(_fixture_bytes().decode('utf-8'))
    document['schema_version'] = 2
    with pytest.raises(ValueError):
        EquipmentCatalogSnapshot.model_validate(document)


def test_fixture_rejects_wrong_authority_version() -> None:
    document = json.loads(_fixture_bytes().decode('utf-8'))
    document['authority_version'] = 'o100c-equipment-definition-2'
    with pytest.raises(ValueError):
        EquipmentCatalogSnapshot.model_validate(document)


def test_fixture_rejects_duplicate_id_version() -> None:
    document = json.loads(_fixture_bytes().decode('utf-8'))
    entry = dict(document['definitions'][0])
    entry['semantic_sha256'] = 'f' * 64
    document['definitions'].append(entry)
    with pytest.raises(ValueError):
        EquipmentCatalogSnapshot.model_validate(document)
