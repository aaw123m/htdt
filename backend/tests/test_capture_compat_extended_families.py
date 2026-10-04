"""Extended-family compatibility gate.

The ``swift-extended`` fixture derives from ``swift-maximal`` (real
Swift emit-path output) and adds the two newest payload families the
app emits — ``derived/segmentation-observations.json`` and
``evidence/reference-object-observations.json`` — plus an
``advisory/operator-advisories.json`` note using the
``derived_candidate_accepted`` kind. It exists because the vendored
contract once drifted behind the emitter: the production validator
then rejected real app bundles with "JSON payload owned by no
published schema". This gate keeps every currently-emitted family
under test on the receiver side.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile

import pytest

from htdt.capture_bundle import FrozenBundle
from htdt.capture_import import import_capture_artifact
from htdt.capture_ingestion_transaction import CaptureIngestionRepository
from htdt.capture_reference import (
    build_ingestion_plan,
    canonical_plan_bytes,
)

FIXTURE_ROOT = (
    Path(__file__).resolve().parent / 'fixtures' / 'capture' / 'swift-extended'
)

BUNDLE_DIGEST = (
    '077c247f60d427dacaf2dd8332a8575cd73e1405a80887862ae674c9c8f3a135'
)
PLAN_CANONICAL_SHA256 = (
    '3dcb953c2cc3c0722dcd554bd44d139295586912363e29816a57ab8b9ac3bdb6'
)


def _frozen() -> FrozenBundle:
    return FrozenBundle(FIXTURE_ROOT)


def test_extended_bundle_is_contract_valid() -> None:
    report = _frozen().report
    assert report['valid'] is True
    assert report['bundle_digest'] == BUNDLE_DIGEST
    assert report['payload_count'] == 30
    versions = report['payload_versions']
    assert versions['segmentation-observations'] == '1.0.0'
    assert versions['reference-object-observations'] == '1.0.0'


def test_extended_plan_is_stable() -> None:
    plan = build_ingestion_plan(_frozen())
    assert (
        hashlib.sha256(canonical_plan_bytes(plan)).hexdigest()
        == PLAN_CANONICAL_SHA256
    )
    paths = {entry['path'] for entry in plan['source_evidence']}
    assert 'derived/segmentation-observations.json' in paths
    assert 'evidence/reference-object-observations.json' in paths


def test_extended_import_commits_and_reimports_clean() -> None:
    from htdt.cad_repository import SceneRepository

    with tempfile.TemporaryDirectory() as tmp:
        repository = CaptureIngestionRepository(
            SceneRepository(Path(tmp) / 'cad.sqlite3')
        )
        result = import_capture_artifact(FIXTURE_ROOT, repository)
        assert result.created is True
        assert result.source_evidence_count == 30
        assert result.quality_state == 'validated'

        reimport = import_capture_artifact(FIXTURE_ROOT, repository)
        assert reimport.created is False
        assert reimport.bundle_digest == result.bundle_digest


CONTRACT_DIR = (
    Path(__file__).resolve().parents[1] / 'src' / 'htdt' / 'capture_contract'
)


def test_vendored_contract_is_internally_consistent() -> None:
    """Every family document resolves to a vendored schema file and
    every vendored schema file is reachable from the matrix — the
    file-level half of the emitter drift class."""
    import json

    matrix = json.loads(
        (CONTRACT_DIR / 'support-matrix.json').read_text(encoding='utf-8')
    )
    assert matrix['schema'] == 'htdt.capture.bundle-support-matrix'
    # the vendored ingestion-plan schema is an extra emitter file (sync
    # tool EXTRA_EMITTER_FILES), not a bundle-family schema — exclude it
    schemas = {
        p.name
        for p in CONTRACT_DIR.glob('*.schema.json')
        if p.name != 'htdt-ingestion-plan-v1.schema.json'
    }
    referenced: set[str] = set()
    for name, contract in matrix['families'].items():
        if contract.get('external'):
            continue
        assert contract.get('paths'), f'family {name}: no paths'
        for doc in contract.get('documents', {}).values():
            referenced.add(f'{doc}.schema.json')
    assert referenced <= schemas, (
        f'matrix documents missing files: {sorted(referenced - schemas)}'
    )
    assert schemas <= referenced, (
        f'vendored schemas unreferenced: {sorted(schemas - referenced)}'
    )
