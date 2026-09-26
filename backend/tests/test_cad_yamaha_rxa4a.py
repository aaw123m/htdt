"""#1057 Yamaha RX-A4A qualification packet tests — docs/fixtures only."""

from __future__ import annotations

import re

import pytest

from htdt.cad_device_compatibility import resolve_firmware_capability
from htdt.cad_yamaha_rxa4a import (
    RXA4A_BACKUP_SEMANTICS,
    RXA4A_CURRENT_FIRMWARE,
    RXA4A_FIRMWARE_HISTORY,
    RXA4A_FIXTURES,
    RXA4A_HARDWARE_GATE_CHECKLIST,
    RXA4A_PLANNED_ADAPTER_ID,
    RXA4A_SOURCE_INVENTORY,
    YamahaSourceEntry,
    build_rxa4a_capability_matrix,
)


NOW = '2026-09-25T00:00:00+00:00'


def test_matrix_is_firmware_scoped_and_never_hardware_claimed():
    matrix = build_rxa4a_capability_matrix(
        matrix_id='rxa4a-packet-1', created_at_utc=NOW
    )
    assert matrix.rows
    for row in matrix.rows:
        assert row.firmware_version == '2.26'
        assert row.model == 'RX-A4A'
        assert row.tier in (
            'DOCUMENTED_ONLY', 'UNKNOWN', 'UNSUPPORTED'
        )
        # Hardware tiers would require evidence hashes — none exist yet.
        assert row.tier not in (
            'HARDWARE_VERIFIED', 'HARDWARE_READBACK_VERIFIED'
        )


def test_stale_firmware_never_silently_inherits():
    matrix = build_rxa4a_capability_matrix(
        matrix_id='rxa4a-packet-1', created_at_utc=NOW
    )
    resolution = resolve_firmware_capability(
        matrix,
        adapter_id=RXA4A_PLANNED_ADAPTER_ID,
        model='RX-A4A',
        observed_firmware='1.73',
        capability='power',
        direction='read',
    )
    assert resolution.state == 'NEEDS_REQUALIFICATION'


def test_firmware_history_marks_superseded_rows():
    stale = {e.version for e in RXA4A_FIRMWARE_HISTORY if e.stale_after}
    assert {'1.65', '1.73', '2.02', '2.12', '2.24'} <= stale
    current = [e for e in RXA4A_FIRMWARE_HISTORY if e.stale_after is None]
    assert [e.version for e in current] == [RXA4A_CURRENT_FIRMWARE]


def test_source_inventory_is_primary_and_sanitized():
    urls = [entry.source_url for entry in RXA4A_SOURCE_INVENTORY]
    assert any('jp.yamaha.com/support/updates' in url for url in urls)
    assert any('manual.yamaha.com/av/20/rxa4a' in url for url in urls)
    sibling = [
        e for e in RXA4A_SOURCE_INVENTORY if 'rxv4a' in e.source_url
    ]
    assert sibling and all(e.currency == 'research_only' for e in sibling)
    with pytest.raises(Exception):
        YamahaSourceEntry(
            source_url='https://example.com/?token=abc',
            source_date=NOW,
            claim_family='x',
        )


def test_fixtures_are_sanitized():
    mac_or_ip = re.compile(
        r'([0-9a-fA-F]{2}:){5}|192\.168|10\.0\.0|172\.16\.'
    )
    for name, payload in RXA4A_FIXTURES.items():
        text = str(payload)
        assert not mac_or_ip.search(text), name
        for marker in ('password', 'api_key', 'secret'):
            assert marker not in text.lower(), name
    ok = RXA4A_FIXTURES['get_system_info_ok']
    assert ok['model_name'] == 'RX-A4A'
    assert ok['system_version'] == 2.26


def test_backup_semantics_stay_opaque_until_review():
    assert RXA4A_BACKUP_SEMANTICS['format_status'] == 'unreviewed_opaque'
    assert 'content-addressed' in RXA4A_BACKUP_SEMANTICS['storage_policy']
    assert not RXA4A_BACKUP_SEMANTICS['overwrite_in_place']
    assert RXA4A_HARDWARE_GATE_CHECKLIST
