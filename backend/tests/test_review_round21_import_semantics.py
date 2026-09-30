"""Round-21 import-into-existing semantics: duplicate/merge coverage.

The batch importer's duplicate classification used to see only persisted
evidence — two identical sources staged before either committed both
passed as 'new' and registered byte-identical measurements the UI cannot
distinguish. Source attachments and impulse-response datasets had the same
silent-duplicate hole on direct re-import.
"""

from __future__ import annotations

import base64
import os
import struct
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from htdt.cad_measurement_quality_repository import CadMeasurementQualityRepository
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.measurement_workflow import (
    MeasurementAssignment,
    MeasurementWorkflowController,
)
from htdt.rew_api import (
    RewFrequencyResponseSnapshot,
    decode_frequency_response,
)


def _saved_f1(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / "cad.sqlite3")
    revision = scene_repository.save(make_f1_scene(), parent_revision_id=None).revision
    return scene_repository, revision


def _controller(scene_repository, document_id):
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(measurement_repository)
    controller = MeasurementWorkflowController(
        scene_repository,
        document_id,
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
    )
    return controller, measurement_repository


def _assignment(**overrides) -> MeasurementAssignment:
    values = dict(
        measurement_entity_id="point-mlp",
        evidence_type="measured",
        channel_role="front_left",
        source_speaker_ids=("speaker-fl",),
        radiation_scope="single",
        routing_evidence="manual",
    )
    values.update(overrides)
    return MeasurementAssignment(**values)


def _enc_f32(values: tuple[float, ...]) -> str:
    return base64.b64encode(struct.pack(f">{len(values)}f", *values)).decode()


def _snapshot(
    measurement_uuid: str,
    magnitude: tuple[float, ...] = (70.0, 71.0, 69.0),
) -> RewFrequencyResponseSnapshot:
    raw_fr = {
        "magnitude": _enc_f32(magnitude),
        "startFreq": 20.0,
        "freqStep": 20.0,
        "unit": "SPL",
    }
    decoded = decode_frequency_response(
        measurement_uuid, raw_fr, requested_unit="SPL"
    )
    return RewFrequencyResponseSnapshot(
        {"title": f"REW {measurement_uuid}"},
        {"unit": "SPL"},
        raw_fr,
        decoded,
    )


# ---------------------------------------------------------------------------
# Intra-batch duplicates: identical sources staged before either commits


def test_intra_batch_text_duplicate_reuses_sibling(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository = _controller(
        scene_repository, revision.document_id
    )
    raw = b"20 70\n40 71\n80 69\n"

    first, second = controller.stage_rew_text_files(
        [(raw, "seat-a.txt"), (raw, "copy-of-seat-a.txt")]
    )
    assert first.duplicate_kind == "new"
    assert second.duplicate_kind == "exact_duplicate"
    assert second.duplicate_of_measurement_id is None
    assert second.duplicate_of_item_id == first.item_id
    assert second.duplicate_of_name == "seat-a.txt"
    assert second.resolution == "reuse_existing"

    controller.apply_batch_assignment(_assignment())
    outcomes = {o.item.filename: o for o in controller.commit_batch()}
    assert outcomes["seat-a.txt"].outcome == "committed"
    assert outcomes["copy-of-seat-a.txt"].outcome == "reused"
    assert (
        outcomes["copy-of-seat-a.txt"].measurement_id
        == outcomes["seat-a.txt"].measurement_id
    )
    # Exactly one measurement landed — no silent second record.
    records = measurement_repository.list_measurements(revision.document_id)
    assert len(records) == 1


def test_intra_batch_duplicate_across_stage_calls(tmp_path: Path) -> None:
    """Sibling detection covers the whole uncommitted queue, not just one
    stage call — a second stage call before commit must still flag it."""
    scene_repository, revision = _saved_f1(tmp_path)
    controller, _ = _controller(scene_repository, revision.document_id)
    raw = b"20 70\n40 71\n80 69\n"

    (first,) = controller.stage_rew_text_files([(raw, "a.txt")])
    (second,) = controller.stage_rew_text_files([(raw, "b.txt")])
    assert second.duplicate_kind == "exact_duplicate"
    assert second.duplicate_of_item_id == first.item_id


def test_intra_batch_explicit_import_as_new_still_possible(tmp_path: Path) -> None:
    """The escape hatch stays: an explicit import_as_new resolution on an
    intra-batch duplicate registers a distinct record by choice."""
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository = _controller(
        scene_repository, revision.document_id
    )
    raw = b"20 70\n40 71\n80 69\n"

    first, second = controller.stage_rew_text_files(
        [(raw, "a.txt"), (raw, "b.txt")]
    )
    controller.set_batch_resolution(second.item_id, "import_as_new")
    controller.apply_batch_assignment(_assignment())
    outcomes = {o.item.filename: o for o in controller.commit_batch()}
    assert outcomes["b.txt"].outcome == "committed"
    assert (
        outcomes["b.txt"].measurement_id
        != outcomes["a.txt"].measurement_id
    )
    assert len(measurement_repository.list_measurements(revision.document_id)) == 2


def test_intra_batch_snapshot_duplicate_flags_same_acquisition(tmp_path: Path) -> None:
    """Two copies of one REW API measurement staged in a batch flag the
    second as same_acquisition of the uncommitted sibling."""
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository = _controller(
        scene_repository, revision.document_id
    )
    uuid = "11111111-2222-3333-4444-555555555555"
    snap = _snapshot(uuid)

    first, second = controller.stage_rew_snapshots([snap, snap])
    assert first.duplicate_kind == "new"
    assert second.duplicate_kind == "same_acquisition"
    assert second.duplicate_of_item_id == first.item_id

    controller.set_batch_resolution(second.item_id, "reuse_existing")
    controller.apply_batch_assignment(_assignment())
    outcomes = controller.commit_batch()
    # Sibling snapshots share one source_label — key by item order.
    assert outcomes[0].outcome == "committed"
    assert outcomes[1].outcome == "reused"
    assert outcomes[1].measurement_id == outcomes[0].measurement_id
    assert len(measurement_repository.list_measurements(revision.document_id)) == 1


def test_reuse_existing_never_silently_inserts(tmp_path: Path) -> None:
    """A reuse_existing item whose duplicate target never committed is an
    honest skip — never a silent import-as-new."""
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository = _controller(
        scene_repository, revision.document_id
    )
    raw = b"20 70\n40 71\n80 69\n"

    first, second = controller.stage_rew_text_files(
        [(raw, "a.txt"), (raw, "b.txt")]
    )
    # Commit only the duplicate; its sibling never produced a measurement.
    (outcome,) = controller.commit_batch([second.item_id])
    assert outcome.outcome == "skipped"
    assert outcome.measurement_id is None
    assert outcome.error
    assert measurement_repository.list_measurements(revision.document_id) == ()

    # Retry after the sibling commits: the same item resolves honestly.
    controller.set_batch_item_assignment(first.item_id, _assignment())
    (first_outcome,) = controller.commit_batch([first.item_id])
    assert first_outcome.outcome == "committed"
    (retry,) = controller.commit_batch([second.item_id])
    assert retry.outcome == "reused"
    assert retry.measurement_id == first_outcome.measurement_id
    assert len(measurement_repository.list_measurements(revision.document_id)) == 1


def test_duplicate_sibling_chain_resolves_to_first_commit(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository = _controller(
        scene_repository, revision.document_id
    )
    raw = b"20 70\n40 71\n80 69\n"

    a, b, c = controller.stage_rew_text_files(
        [(raw, "a.txt"), (raw, "b.txt"), (raw, "c.txt")]
    )
    assert b.duplicate_of_item_id == a.item_id
    assert c.duplicate_of_item_id == a.item_id
    controller.apply_batch_assignment(_assignment())
    outcomes = controller.commit_batch()
    assert [o.outcome for o in outcomes] == ["committed", "reused", "reused"]
    ids = {o.measurement_id for o in outcomes}
    assert len(ids) == 1
    assert len(measurement_repository.list_measurements(revision.document_id)) == 1


# ---------------------------------------------------------------------------
# Source attachments: exact re-attach is an idempotent reuse


def test_source_attachment_exact_reattach_dedupes(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository = _controller(
        scene_repository, revision.document_id
    )
    controller.stage_rew_text(b"20 70\n40 71\n80 69\n", "mlp.txt")
    record = controller.commit_pending(_assignment())
    mid = record.measurement_id

    first = controller.save_source_attachment(
        mid, filename="notes.txt", raw_bytes=b"payload", kind="note", note="n1"
    )
    second = controller.save_source_attachment(
        mid, filename="notes.txt", raw_bytes=b"payload", kind="note", note="n1"
    )
    assert second.attachment_id == first.attachment_id
    assert len(measurement_repository.list_attachments(mid)) == 1

    # A different annotation on the same artifact is a distinct row.
    third = controller.save_source_attachment(
        mid, filename="notes.txt", raw_bytes=b"payload", kind="note", note="n2"
    )
    assert third.attachment_id != first.attachment_id
    assert len(measurement_repository.list_attachments(mid)) == 2


# ---------------------------------------------------------------------------
# Impulse-response datasets: identical re-import is an idempotent reuse


def test_ir_reimport_semantic_duplicate_dedupes(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository = _controller(
        scene_repository, revision.document_id
    )
    controller.stage_rew_text(b"20 70\n40 71\n80 69\n", "mlp.txt")
    record = controller.commit_pending(_assignment())
    mid = record.measurement_id

    ir_raw = b"* IR\n0.0 0.1\n0.001 0.5\n0.002 0.2\n"
    first = controller.import_ir_for_measurement(
        mid, ir_raw, filename="ir1.txt"
    )
    second = controller.import_ir_for_measurement(
        mid, ir_raw, filename="ir1-again.txt"
    )
    assert second.dataset_id == first.dataset_id
    assert len(measurement_repository.ir_datasets_for_measurement(mid)) == 1

    # Same raw under a different declared interpretation is a real new
    # dataset — never deduplicated away.
    third = controller.import_ir_for_measurement(
        mid, ir_raw, filename="ir1-hann.txt", window_kind="hann"
    )
    assert third.dataset_id != first.dataset_id
    assert len(measurement_repository.ir_datasets_for_measurement(mid)) == 2
