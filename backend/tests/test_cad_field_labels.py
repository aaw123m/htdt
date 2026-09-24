from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError

from htdt.cad_field_labels import (
    LABEL_PAYLOAD_VERSION,
    FieldLabelPayload,
    LabelRegistry,
    LabelTargetRecord,
    generate_label_sheet,
    mint_label,
    mint_replacement_label,
    parse_label_payload,
    resolve_label_scan,
)


PROJECT_ID = str(uuid.uuid4())
NOW = '2026-09-24T00:00:00+00:00'


def _registry(*targets: LabelTargetRecord) -> LabelRegistry:
    return LabelRegistry(project_id=PROJECT_ID, targets=targets)


def test_payload_is_versioned_identity_only():
    label = mint_label(
        project_id=PROJECT_ID, target_kind='installed_equipment',
        target_id='inst-fl-01', created_at_utc=NOW,
    )
    payload = label.payload
    assert payload.v == LABEL_PAYLOAD_VERSION
    qr = json.loads(payload.qr_text())
    assert set(qr) == {'v', 'p', 'k', 't', 'g', 'c'}
    # no filesystem paths or project data leak into the payload
    assert not any(
        '/' in str(value) and key not in {'v'}
        for key, value in qr.items()
    )


def test_short_code_is_deterministic_and_typed():
    a = mint_label(project_id=PROJECT_ID, target_kind='speaker_instance',
                   target_id='spk-fl', created_at_utc=NOW)
    b = mint_label(project_id=PROJECT_ID, target_kind='speaker_instance',
                   target_id='spk-fl', created_at_utc='2027-01-01T00:00:00+00:00')
    assert a.short_code == b.short_code
    assert a.short_code.startswith('SP-')
    assert mint_label(
        project_id=PROJECT_ID, target_kind='cable_run', target_id='run-3',
        created_at_utc=NOW,
    ).short_code.startswith('CB-')


def test_scan_resolution_states():
    label = mint_label(project_id=PROJECT_ID, target_kind='installed_equipment',
                       target_id='inst-1', created_at_utc=NOW)
    registry = _registry(LabelTargetRecord(
        target_id='inst-1', target_kind='installed_equipment',
        latest_generation=1,
    ))
    assert resolve_label_scan(label.payload.qr_text(), registry).status == 'ok'

    wrong = LabelRegistry(project_id=str(uuid.uuid4()), targets=())
    assert resolve_label_scan(
        label.payload.qr_text(), wrong
    ).status == 'wrong_project'

    assert resolve_label_scan('{"v":"htdt-label-v9"}', registry).status == 'malformed'
    assert resolve_label_scan('not json', registry).status == 'malformed'

    text = label.payload.qr_text()
    tampered = json.loads(text)
    tampered['t'] = 'inst-2'
    assert resolve_label_scan(
        json.dumps(tampered), registry
    ).status == 'bad_checksum'

    registry_missing_kind = LabelRegistry(
        project_id=PROJECT_ID,
        targets=(LabelTargetRecord(
            target_id='inst-1', target_kind='rack', latest_generation=1,
        ),),
    )
    assert resolve_label_scan(
        label.payload.qr_text(), registry_missing_kind
    ).status == 'unknown_target'

    # stale generation after replacement
    stale_registry = _registry(LabelTargetRecord(
        target_id='inst-1', target_kind='installed_equipment',
        latest_generation=2,
    ))
    assert resolve_label_scan(
        label.payload.qr_text(), stale_registry
    ).status == 'stale_generation'

    retired_registry = _registry(LabelTargetRecord(
        target_id='inst-1', target_kind='installed_equipment',
        latest_generation=1, retired=True,
    ))
    assert resolve_label_scan(
        label.payload.qr_text(), retired_registry
    ).status == 'retired_target'

    registry_missing = LabelRegistry(
        project_id=PROJECT_ID,
        targets=(LabelTargetRecord(
            target_id='inst-9', target_kind='rack', latest_generation=1,
        ),),
    )
    assert resolve_label_scan(
        label.payload.qr_text(), registry_missing
    ).status == 'unknown_target'


def test_replacement_mints_new_generation():
    old = mint_label(project_id=PROJECT_ID, target_kind='rack',
                     target_id='rack-1', created_at_utc=NOW)
    new = mint_replacement_label(old, created_at_utc=NOW)
    assert new.payload.g == 2
    assert new.payload.t == old.payload.t
    assert new.payload.c != old.payload.c
    assert new.label_id != old.label_id


def test_payload_checksum_detection():
    forged = FieldLabelPayload(
        p=PROJECT_ID, k='rack', t='r-1', g=1, c='deadbeef',
    )
    assert not forged.checksum_ok()
    label = mint_label(project_id=PROJECT_ID, target_kind='rack',
                       target_id='r-1', created_at_utc=NOW)
    assert label.payload.checksum_ok()


def test_parse_label_payload_rejects_garbage():
    assert parse_label_payload('') is None
    assert parse_label_payload('[]') is None
    assert parse_label_payload('{"v":"htdt-label-v1"}') is None


def test_label_sheet_svg_is_deterministic_and_lays_out():
    labels = tuple(
        mint_label(project_id=PROJECT_ID, target_kind='installed_equipment',
                   target_id=f'inst-{i}', created_at_utc=NOW)
        for i in range(6)
    )
    sheet = generate_label_sheet(labels, label_kind='device', sheet_id='s-1')
    svg = sheet.to_svg()
    assert svg.startswith('<svg')
    assert svg.count('<rect') > 6
    sheet2 = generate_label_sheet(labels, label_kind='device', sheet_id='s-1')
    assert svg == sheet2.to_svg()
    assert sheet.sheet_content_sha256 == sheet2.sheet_content_sha256


def test_label_sheet_overflow_rejected():
    labels = tuple(
        mint_label(project_id=PROJECT_ID, target_kind='installed_equipment',
                   target_id=f'inst-{i}', created_at_utc=NOW)
        for i in range(200)
    )
    with pytest.raises(ValueError):
        generate_label_sheet(labels, label_kind='compact_termination',
                             sheet_id='s-big', page=(100.0, 100.0))
