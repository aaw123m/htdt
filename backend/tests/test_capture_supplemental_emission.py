"""Supplemental-handoff emission at the ingestion boundary (#564).

``build_ingestion_plan`` used to leave every declared supplemental
document as raw source evidence — the typed
``CaptureSupplementalDocument`` layer was reachable only by tests
injecting plans by hand. These tests pin the production emission path:
supported kinds become typed handoffs, the ingest contract comparison
rederives them from exact bytes, and promotion authority kinds surface
them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent))
import capture_fixture_support as support  # noqa: E402

from htdt.cad_repository import SceneRepository
from htdt.capture_ingestion_transaction import (
    CaptureIngestionPlan,
    CaptureIngestionRepository,
    CaptureIngestionTransactionError,
    CapturePayloadContractError,
)
from htdt.capture_inbox import plan_authority_kinds


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')


def _supplemental_spec(path: str, document: dict) -> dict:
    return {
        'bytes': _canonical(document),
        'media_type': 'application/json',
        'producer': 'htdt_mission',
        'provenance_class': 'capture_app_derived',
        'role': 'canonical',
    }


def _candidates_doc() -> dict:
    return {
        'schema': 'htdt.capture.derived-geometry-candidates',
        'schema_version': '1.0.0',
        'capture_revision_id': support.REVISION_ID,
        'capture_session_id': support.SESSION_ID,
        'candidates': [],
    }


def _reference_targets_doc() -> dict:
    return {
        'schema': 'htdt.capture.reference-targets',
        'schema_version': '1.0.0',
        'capture_revision_id': support.REVISION_ID,
        'capture_session_id': support.SESSION_ID,
        'targets': [],
        'observations': [],
        'diagnostics': [],
    }


def _files_with(path: str, document: dict) -> dict:
    files = support.default_file_specs()
    files[path] = _supplemental_spec(path, document)
    return files


def _ingested(tmp_path: Path, files: dict):
    plan, payloads, _ = support.plan_and_payloads(
        tmp_path / 'bundle', files=files
    )
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    ingestion = CaptureIngestionRepository(scene)
    ingestion.ingest(plan, payloads)
    return CaptureIngestionPlan.model_validate(plan)


class TestSupplementalEmission:
    def test_plan_emits_typed_supplemental_document(
        self, tmp_path: Path
    ) -> None:
        files = _files_with(
            'derived/geometry-candidates.json', _candidates_doc()
        )
        plan, _, _ = support.plan_and_payloads(
            tmp_path / 'bundle', files=files
        )
        supplemental = plan['supplemental_documents']
        assert len(supplemental) == 1
        entry = supplemental[0]
        assert entry['document_kind'] == 'derived_geometry_candidates'
        assert entry['validation_state'] == 'supported'
        assert entry['path'] == 'derived/geometry-candidates.json'
        assert entry['schema'] == (
            'htdt.capture.derived-geometry-candidates'
        )
        assert entry['schema_version'] == '1.0.0'
        # Dependency ids are collected from the document body.
        assert entry['capture_session_ids'] == [support.SESSION_ID]
        assert entry['coordinate_space_ids'] == []
        assert len(entry['supplemental_document_handoff_id']) == 64

    def test_emitted_supplemental_ingests(
        self, tmp_path: Path
    ) -> None:
        """The emitted rows must survive the ingest-time rederivation —
        the exact contract comparison in
        ``_validate_source_payload_contract``."""
        files = _files_with(
            'derived/geometry-candidates.json', _candidates_doc()
        )
        plan = _ingested(tmp_path, files)
        assert len(plan.supplemental_documents) == 1

    def test_supplemental_in_authority_kinds(
        self, tmp_path: Path
    ) -> None:
        files = _files_with(
            'derived/geometry-candidates.json', _candidates_doc()
        )
        plan = _ingested(tmp_path, files)
        assert 'supplemental_authority' in plan_authority_kinds(plan)

    def test_reference_targets_report_their_own_authority_kind(
        self, tmp_path: Path
    ) -> None:
        files = _files_with(
            'evidence/reference-targets.json', _reference_targets_doc()
        )
        plan = _ingested(tmp_path, files)
        kinds = plan_authority_kinds(plan)
        # The supplemental kind must map to its own promotion authority
        # kind — the umbrella 'supplemental_authority' alone can never
        # satisfy a reference_targets promotion gate.
        assert 'reference_targets' in kinds
        assert 'supplemental_authority' in kinds

    def test_tampered_supplemental_payload_fails_closed(
        self, tmp_path: Path
    ) -> None:
        files = _files_with(
            'derived/geometry-candidates.json', _candidates_doc()
        )
        plan, payloads, _ = support.plan_and_payloads(
            tmp_path / 'bundle', files=files
        )
        payloads['derived/geometry-candidates.json'] = _canonical(
            {
                'schema': 'htdt.capture.derived-geometry-candidates',
                'schema_version': '1.0.0',
                'capture_revision_id': support.REVISION_ID,
                'capture_session_id': support.SESSION_ID,
                'candidates': [{'unexpected': 'member'}],
            }
        )
        scene = SceneRepository(tmp_path / 'cad.sqlite3')
        ingestion = CaptureIngestionRepository(scene)
        # Tampered bytes must be rejected — either as a byte/SHA source
        # mismatch or as the rederived contract comparison. Both error
        # types are closed failure modes.
        with pytest.raises(
            (
                CaptureIngestionTransactionError,
                CapturePayloadContractError,
            )
        ):
            ingestion.ingest(plan, payloads)

    def test_plan_without_supplemental_omits_key(
        self, tmp_path: Path
    ) -> None:
        """Bundles without supplemental documents keep the historical
        plan shape — the key stays absent so plan bytes are identical."""
        plan, _, _ = support.plan_and_payloads(tmp_path / 'bundle')
        assert 'supplemental_documents' not in plan
