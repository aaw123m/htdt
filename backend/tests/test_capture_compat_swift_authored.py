"""Swift-authored bundle compatibility gate.

The ``swift-maximal`` fixture was produced by HTDT-Capture's production
Swift emit path (working-set store → seal → ``BundleRevisionFinalizer``)
on the ka0923s-a11y/HTDT-Capture emit branch — not by a Python fixture
generator — so it exercises every payload family the app actually
finalizes, including ``capture-advisory`` 1.1.0, ``entities`` 1.3.0,
``derived-geometry-candidates``, and the coordinate-space policy
document.

Unlike ``test_capture_compat_pinned`` (which pins the historical
phase-6 fixture via ``docs/CAPTURE_COMPATIBILITY.json``), this gate
pins its identity inline: the bytes are generated inside HTDT-Capture's
own test harness, so a digest change means the emit side changed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import pytest

from htdt.capture_bundle import FrozenBundle
from htdt.capture_reference import (
    CaptureIngestionContractError,
    build_ingestion_plan,
    canonical_plan_bytes,
)

FIXTURE_ROOT = (
    Path(__file__).resolve().parent / 'fixtures' / 'capture' / 'swift-maximal'
)

BUNDLE_DIGEST = (
    'caf013fc373221b2e96c40f3f60a885fca520dbf19370129126d94d9795094d2'
)
PLAN_CANONICAL_SHA256 = (
    '9d8e6f7d4aaab3d4fdda6871f42b8d081ca62007e51cd7c2a5d0403f3be609e2'
)


def _frozen() -> FrozenBundle:
    return FrozenBundle(FIXTURE_ROOT)


def test_swift_authored_bundle_is_contract_valid() -> None:
    report = _frozen().report
    assert report['valid'] is True
    assert report['bundle_digest'] == BUNDLE_DIGEST
    assert report['payload_count'] == 28
    # The emit side's current contract surface — every family the app
    # actually writes, at its emitted schema version.
    assert report['payload_versions'] == {
        'advisory-notes': '1.0.0',
        'entities': '1.3.0',
        'measurements': '1.1.0',
        'authority-dependencies': '1.0.0',
        'derived-geometry-candidates': '1.0.0',
        'mesh-anchors': '1.0.0',
        'capture-advisory': '1.1.0',
        'quality': '1.0.0',
        'capabilities': '1.0.0',
        'capture-configuration': '1.0.0',
        'session': '1.0.0',
        'capture-strategy': '1.0.0',
        'coordinate-space-policy': '1.0.0',
        'device': '1.0.0',
        'field-notes': '1.0.0',
        'working-revision-state': '1.0.0',
        'revisit-flags': '1.0.0',
        'timing': '1.0.0',
    }


def test_swift_authored_manifest_digest_is_bundle_digest() -> None:
    frozen = _frozen()
    assert (
        frozen.report['bundle_digest']
        == hashlib.sha256(frozen.manifest_bytes).hexdigest()
    )


def test_swift_authored_plan_is_stable() -> None:
    plan = build_ingestion_plan(_frozen())
    assert (
        hashlib.sha256(canonical_plan_bytes(plan)).hexdigest()
        == PLAN_CANONICAL_SHA256
    )
    assert len(plan['source_evidence']) == 28
    assert len(plan['roomplan_records']) == 2


def test_swift_authored_derived_entries_carry_source_refs() -> None:
    """Derived-role manifest entries must declare provenance refs —
    the receiver rejects dangling derived provenance."""
    plan = build_ingestion_plan(_frozen())
    derived = [
        e for e in plan['source_evidence'] if e['role'] == 'derived'
    ]
    assert derived, 'fixture must contain derived-role evidence'
    for entry in derived:
        assert entry['source_refs'], (
            f"{entry['path']}: derived entry without source_refs"
        )


def _mutated_metadata_bundle(
    tmp_path: Path,
    mutate,
) -> FrozenBundle:
    """Swift fixture copy whose captured-room-metadata document is
    mutated while the manifest entry is honestly re-emitted — the outer
    bundle stays valid so only the lineage check can reject it."""
    bundle_dir = tmp_path / 'bundle'
    shutil.copytree(FIXTURE_ROOT, bundle_dir)
    meta_path = bundle_dir / 'roomplan' / 'captured-room-metadata.json'
    document = json.loads(meta_path.read_bytes())
    mutate(document)
    meta_bytes = json.dumps(
        document, separators=(',', ':'), sort_keys=True
    ).encode()
    meta_path.write_bytes(meta_bytes)
    manifest_path = bundle_dir / 'manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    for entry in manifest['files']:
        if entry['path'] == 'roomplan/captured-room-metadata.json':
            entry['bytes'] = len(meta_bytes)
            entry['sha256'] = hashlib.sha256(meta_bytes).hexdigest()
    manifest_path.write_bytes(
        json.dumps(
            manifest, separators=(',', ':'), sort_keys=True
        ).encode()
    )
    return FrozenBundle(bundle_dir)


def test_swift_authored_raw_byte_count_must_match_manifest(
    tmp_path: Path,
) -> None:
    """#475/#476: a supplied raw_byte_count that disagrees with the
    selected manifest payload's declared length rejects the plan."""
    frozen = _mutated_metadata_bundle(
        tmp_path,
        lambda document: document.update({'raw_byte_count': 13}),
    )
    assert frozen.report['valid'] is True
    with pytest.raises(
        CaptureIngestionContractError, match='raw_byte_count'
    ):
        build_ingestion_plan(frozen)


def test_swift_authored_processed_byte_count_must_match_manifest(
    tmp_path: Path,
) -> None:
    """#475/#476: same check for the selected processed payload."""
    frozen = _mutated_metadata_bundle(
        tmp_path,
        lambda document: document.update({'processed_byte_count': 19}),
    )
    assert frozen.report['valid'] is True
    with pytest.raises(
        CaptureIngestionContractError, match='processed_byte_count'
    ):
        build_ingestion_plan(frozen)


def test_swift_authored_absent_byte_counts_stay_accepted(
    tmp_path: Path,
) -> None:
    """Backward compatibility: emitters that omit the optional lineage
    counts still produce a plan."""
    frozen = _mutated_metadata_bundle(
        tmp_path,
        lambda document: (
            document.pop('raw_byte_count', None),
            document.pop('processed_byte_count', None),
        ),
    )
    plan = build_ingestion_plan(frozen)
    assert len(plan['roomplan_records']) == 2


def test_swift_authored_processed_byte_count_needs_processed_payload(
    tmp_path: Path,
) -> None:
    """A supplied processed count without a selected processed payload
    is inconsistent lineage and rejects."""
    frozen = _mutated_metadata_bundle(
        tmp_path,
        lambda document: (
            document.pop('processed_payload_path', None),
            document.pop('processed_sha256', None),
        ),
    )
    assert frozen.report['valid'] is True
    with pytest.raises(
        CaptureIngestionContractError, match='processed_byte_count'
    ):
        build_ingestion_plan(frozen)
