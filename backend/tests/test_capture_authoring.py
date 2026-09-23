"""Capture semantic handoff — authoring-input tests (#336)."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_fixture_support as support  # noqa: E402

from htdt.cad_repository import SceneRepository
from htdt.capture_ingestion_transaction import CaptureIngestionRepository
from htdt.capture_authoring import (
    CaptureAuthoringError,
    CaptureAuthoringService,
)


ANNOTATION_ID = support.ANNOTATION_ID
MEASUREMENT_ID = '10000000-0000-4000-8000-000000000007'
SPACE_ID = support.SPACE_ID


def _service(tmp_path: Path) -> tuple[CaptureAuthoringService, str]:
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads, manifest = support.plan_and_payloads(tmp_path)
    result = repository.ingest(plan, payloads, manifest=manifest)
    return CaptureAuthoringService(repository), result.lineage_digest


def test_batch_groups_typed_inputs_by_lineage(tmp_path: Path) -> None:
    service, lineage = _service(tmp_path)
    batch = service.authoring_inputs(lineage)
    assert batch.lineage_digest == lineage
    assert batch.bundle_digest == support.BUNDLE_DIGEST
    assert batch.capture_revision_id == support.REVISION_ID
    assert [a.record_id for a in batch.annotations] == [ANNOTATION_ID]
    assert [m.record_id for m in batch.measurements] == [MEASUREMENT_ID]
    assert {r.kind for r in batch.roomplan_suggestions} == {
        'raw_scan',
        'postprocessed_inference',
    }
    assert all(r.suggestion_only for r in batch.roomplan_suggestions)


def test_annotation_resolves_evidence_and_coordinate_space(
    tmp_path: Path,
) -> None:
    service, lineage = _service(tmp_path)
    annotation = service.authoring_inputs(lineage).annotations[0]
    assert annotation.entity_type == 'speaker'
    assert annotation.label == 'Left speaker'
    assert annotation.coordinate_space_id == SPACE_ID
    # Locators resolved by the transaction layer arrive verbatim.
    assert annotation.evidence_refs == (
        f'mesh_anchor:{support.ANCHOR_ID}',
    )
    assert annotation.resolved_evidence
    assert not annotation.conflicts


def test_measurement_reconciles_instead_of_overwriting(
    tmp_path: Path,
) -> None:
    service, lineage = _service(tmp_path)
    measurement = service.authoring_inputs(lineage).measurements[0]
    assert measurement.quantity_type == 'room_width'
    assert measurement.value == pytest.approx(3.4)
    assert service.reconcile_measurement(measurement, None)[0] == 'new'
    assert (
        service.reconcile_measurement(measurement, 3.4)[0] == 'consistent'
    )
    outcome, detail = service.reconcile_measurement(measurement, 3.0)
    assert outcome == 'conflict'
    assert '3.4' in detail and '3.0' in detail


def test_display_entity_type_stays_typed_unsupported(
    tmp_path: Path,
) -> None:
    entities = json.loads(
        support.fixture_payloads()['annotations/entities.json']
    )
    entities['entities'][0]['type'] = 'display'
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads, manifest = support.plan_and_payloads(
        tmp_path,
        json_overrides={'annotations/entities.json': entities},
    )
    result = repository.ingest(plan, payloads, manifest=manifest)
    service = CaptureAuthoringService(repository)
    batch = service.authoring_inputs(result.lineage_digest)
    annotation = batch.annotations[0]
    assert annotation.entity_type == 'display'
    assert annotation.suggestion_only is True
    assert any(
        c.kind == 'unsupported_entity_type' for c in annotation.conflicts
    )
    with pytest.raises(CaptureAuthoringError, match='scene-enterable'):
        service.apply(
            batch,
            annotation,
            scene_revision_id='rev-x',
            operator_action='place_display',
        )


def test_apply_preserves_capture_provenance(tmp_path: Path) -> None:
    service, lineage = _service(tmp_path)
    batch = service.authoring_inputs(lineage)
    annotation = batch.annotations[0]
    provenance = service.apply(
        batch,
        annotation,
        scene_revision_id='scene-rev-1',
        operator_action='place_speaker',
    )
    assert provenance.record_kind == 'annotation'
    assert provenance.record_id == ANNOTATION_ID
    assert provenance.bundle_digest == support.BUNDLE_DIGEST
    assert provenance.capture_revision_id == support.REVISION_ID
    assert provenance.coordinate_space_id == SPACE_ID
    assert provenance.authority_record_handoff_id == (
        annotation.authority_record_handoff_id
    )
    assert provenance.source_payload_sha256 == (
        annotation.source_payload_sha256
    )
    assert provenance.resolved_refs


def test_apply_rejects_cross_lineage_records(tmp_path: Path) -> None:
    service, lineage = _service(tmp_path)
    batch = service.authoring_inputs(lineage)
    foreign = json.loads(
        support.fixture_payloads()['annotations/entities.json']
    )
    foreign['entities'][0]['entity_id'] = (
        '10000000-0000-4000-8000-0000000000ff'
    )
    repository2 = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad2.sqlite3')
    )
    plan2, payloads2, manifest2 = support.plan_and_payloads(
        tmp_path / 'other',
        json_overrides={'annotations/entities.json': foreign},
        manifest_overrides={
            'capture_revision_id': '20000000-0000-4000-8000-000000000002',
            'created_at': '2026-09-21T00:00:00Z',
            'finalized_at': '2026-09-21T00:00:01Z',
        },
    )
    result2 = repository2.ingest(plan2, payloads2, manifest=manifest2)
    service2 = CaptureAuthoringService(repository2)
    batch2 = service2.authoring_inputs(result2.lineage_digest)
    # Applying batch-2's record against batch-1 must fail: records are
    # bound to their ingestion lineage.
    with pytest.raises(CaptureAuthoringError, match='lineage'):
        service.apply(
            batch,
            batch2.annotations[0],
            scene_revision_id='scene-rev-1',
            operator_action='place_speaker',
        )
