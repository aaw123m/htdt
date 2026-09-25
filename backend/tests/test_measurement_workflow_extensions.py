from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality import (
    CadMeasurementQualityEvidence,
    build_measurement_observation,
    build_measurement_quality_report,
    observation_binding,
)
from htdt.cad_measurement_quality_repository import CadMeasurementQualityRepository
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, make_f1_scene
from htdt.cad_document import WorkingDocument
from htdt.measurement_analysis import (
    phase_trace,
    processing_summary,
    smoothed_level_trace,
    trace_label,
)
from htdt.measurement_workflow import (
    AssignmentCorrection,
    MeasurementAssignment,
    MeasurementWorkflowController,
)


def _saved_f1(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / "cad.sqlite3")
    revision = scene_repository.save(make_f1_scene(), parent_revision_id=None).revision
    return scene_repository, revision


def _controller(scene_repository, document_id) -> tuple[MeasurementWorkflowController, CadMeasurementRepository, CadMeasurementQualityRepository]:
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(measurement_repository)
    controller = MeasurementWorkflowController(
        scene_repository,
        document_id,
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
    )
    return controller, measurement_repository, quality_repository


def _assignment(**overrides) -> MeasurementAssignment:
    values = dict(
        measurement_entity_id="point-mlp",
        evidence_type="measured",
        channel_role="front_left",
        source_speaker_ids=("speaker-fl",),
        radiation_scope="single",
        # #858: 'verified' now requires a resolvable verified profile;
        # commits without one carry honest 'manual' evidence.
        routing_evidence="manual",
    )
    values.update(overrides)
    return MeasurementAssignment(**values)


# ---------------------------------------------------------------------
# #446 batch import + idempotent duplicates + source attachments


def test_batch_import_stages_commits_and_retries_idempotently(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository, _ = _controller(
        scene_repository, revision.document_id
    )

    items = controller.stage_rew_text_files(
        [
            (b"20 70\n40 71\n80 69\n", "seat-a.txt"),
            (b"20 60\n40 62\n80 61\n", "seat-b.txt"),
        ]
    )
    assert len(items) == 2
    assert {item.status for item in items} == {"staged"}
    assert {item.duplicate_kind for item in items} == {"new"}
    assert measurement_repository.list_measurements(revision.document_id) == ()

    applied = controller.apply_batch_assignment(_assignment())
    assert applied == 2
    outcomes = controller.commit_batch()
    assert {o.outcome for o in outcomes} == {"committed"}
    assert len(measurement_repository.list_measurements(revision.document_id)) == 2

    # Retry must not re-register committed items.
    retry = controller.commit_batch()
    assert {o.outcome for o in retry} == {"already_committed"}
    assert len(measurement_repository.list_measurements(revision.document_id)) == 2


def test_batch_duplicate_classification_and_reuse(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository, _ = _controller(
        scene_repository, revision.document_id
    )
    raw = b"20 70\n40 71\n80 69\n"
    (first,) = controller.stage_rew_text_files([(raw, "first.txt")])
    controller.apply_batch_assignment(_assignment(), item_ids=[first.item_id])
    (outcome,) = controller.commit_batch([first.item_id])
    assert outcome.outcome == "committed"
    original_id = outcome.measurement_id

    # Byte-identical re-staging classifies as exact duplicate; the item is
    # never auto-registered — the user must choose a resolution.
    (second,) = controller.stage_rew_text_files([(raw, "reexport.txt")])
    assert second.duplicate_kind == "exact_duplicate"
    assert second.duplicate_of_measurement_id == original_id
    assert second.resolution == "reuse_existing"
    controller.set_batch_item_assignment(second.item_id, _assignment())
    (reuse,) = controller.commit_batch([second.item_id])
    assert reuse.outcome == "reused"
    assert reuse.measurement_id == original_id
    assert len(measurement_repository.list_measurements(revision.document_id)) == 1

    # The same bytes explicitly imported as new stay a distinct record.
    (third,) = controller.stage_rew_text_files([(raw, "again.txt")])
    controller.set_batch_resolution(third.item_id, "import_as_new")
    controller.set_batch_item_assignment(third.item_id, _assignment())
    (committed,) = controller.commit_batch([third.item_id])
    assert committed.outcome == "committed"
    assert committed.measurement_id != original_id


def test_batch_item_failure_is_isolated_and_reported(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, _, _ = _controller(scene_repository, revision.document_id)

    items = controller.stage_rew_text_files(
        [
            (b"20 70\n40 71\n", "good.txt"),
            (b"not a frequency response", "broken.txt"),
        ]
    )
    by_name = {item.filename: item for item in controller.batch_items()}
    assert by_name["good.txt"].status == "staged"
    assert by_name["broken.txt"].status == "failed"
    assert by_name["broken.txt"].error is not None

    controller.set_batch_item_assignment(by_name["good.txt"].item_id, _assignment())
    outcomes = controller.commit_batch()
    assert {o.outcome for o in outcomes} == {"committed", "failed"}
    by_name = {item.filename: item for item in controller.batch_items()}
    assert by_name["broken.txt"].status == "failed"
    # Failed items stay in the queue for review; clearing removes them too.
    controller.discard_batch_committed()
    remaining = {item.filename for item in controller.batch_items()}
    assert "good.txt" not in remaining


def test_source_attachments_roundtrip_through_backup_authority(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository, _ = _controller(
        scene_repository, revision.document_id
    )
    controller.stage_rew_text(b"20 70\n40 71\n80 69\n", "mlp.txt")
    record = controller.commit_pending(_assignment())

    attachment = controller.save_source_attachment(
        record.measurement_id,
        kind="mdat",
        filename="session.mdat",
        raw_bytes=b"MDAT-BYTES",
        note="capture session",
    )
    listed = controller.list_source_attachments(record.measurement_id)
    assert [a.attachment_id for a in listed] == [attachment.attachment_id]
    assert listed[0].kind == "mdat"
    read_back = controller.read_source_attachment(attachment)
    assert read_back == b"MDAT-BYTES"
    # Attachments bind to a real measurement only.
    with pytest.raises(ValueError):
        controller.save_source_attachment(
            "missing-measurement",
            kind="notes",
            filename="n.txt",
            raw_bytes=b"x",
        )


# ---------------------------------------------------------------------
# #509 disposition + non-destructive correction


def test_correction_overlays_binding_without_mutating_record(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository, quality_repository = _controller(
        scene_repository, revision.document_id
    )
    controller.stage_rew_text(b"20 70\n40 71\n", "misassigned.txt")
    record = controller.commit_pending(_assignment())
    dataset = measurement_repository.dataset_for_measurement(record.measurement_id)

    controller.correct_assignment(
        record.measurement_id,
        AssignmentCorrection(
            channel_role="center",
            source_speaker_ids=("speaker-fr",),
        ),
        reason="channel was mislabeled at import",
    )

    reopened = measurement_repository.get_measurement(record.measurement_id)
    assert reopened == record  # immutable evidence is untouched
    corrections = controller.corrections_for(record.measurement_id)
    assert len(corrections) == 1
    assert corrections[0].dataset_id == dataset.dataset_id
    assert corrections[0].dataset_sha256  # pins the exact bound dataset

    view = next(
        v for v in controller.measurement_views()
        if v.measurement_id == record.measurement_id
    )
    assert view.is_corrected is True
    assert view.disposition == "corrected"
    assert view.channel_role == "front_left"  # original stays visible
    assert view.effective_channel_role == "center"
    assert view.effective_source_speaker_ids == ("speaker-fr",)
    assert view.is_normally_eligible is True

    # Retake lineage is distinct from metadata correction: no lineage rows.
    assert view.supersedes_measurement_id is None

    with pytest.raises(ValueError):
        controller.correct_assignment(
            record.measurement_id,
            AssignmentCorrection(),  # nothing changed
            reason="no-op",
        )
    with pytest.raises(ValueError):
        controller.correct_assignment(
            record.measurement_id,
            AssignmentCorrection(channel_role="center"),
            reason="",  # reason required
        )


def test_disposition_gates_comparison_eligibility(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository, _ = _controller(
        scene_repository, revision.document_id
    )
    controller.stage_rew_text(b"20 70\n40 71\n", "a.txt")
    a = controller.commit_pending(_assignment())
    controller.stage_rew_text(b"20 60\n40 62\n", "b.txt")
    b = controller.commit_pending(_assignment(channel_role="center"))
    ds_a = measurement_repository.dataset_for_measurement(a.measurement_id)
    ds_b = measurement_repository.dataset_for_measurement(b.measurement_id)

    eligible = controller.comparison_candidates()
    assert {v.measurement_id for v in eligible} == {
        a.measurement_id,
        b.measurement_id,
    }

    controller.set_disposition(
        a.measurement_id, "excluded_from_normal_use", "noisy capture"
    )
    eligible = controller.comparison_candidates()
    assert {v.measurement_id for v in eligible} == {b.measurement_id}
    with pytest.raises(ValueError):
        controller.compare_datasets(
            ds_a.dataset_id, ds_b.dataset_id, low_hz=20.0, high_hz=40.0
        )

    dispositions = controller.dispositions_for(a.measurement_id)
    assert [d.disposition for d in dispositions] == ["excluded_from_normal_use"]

    controller.set_disposition(a.measurement_id, "active", "verified capture")
    assert {
        v.measurement_id for v in controller.comparison_candidates()
    } == {a.measurement_id, b.measurement_id}

    with pytest.raises(ValueError):
        controller.set_disposition(b.measurement_id, "test_only", "")


# ---------------------------------------------------------------------
# #483 generic A/B + mismatch advisory


def test_measured_pair_comparison_and_mismatch_advisory(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository, _ = _controller(
        scene_repository, revision.document_id
    )
    controller.stage_rew_text(b"20 70\n40 71\n80 69\n", "before.txt")
    a = controller.commit_pending(_assignment())
    controller.stage_rew_text(b"20 60\n40 62\n80 61\n", "after.txt")
    b = controller.commit_pending(
        _assignment(channel_role="front_right", source_speaker_ids=("speaker-fr",))
    )
    ds_a = measurement_repository.dataset_for_measurement(a.measurement_id)
    ds_b = measurement_repository.dataset_for_measurement(b.measurement_id)

    # No evidence-type filter: measured-vs-measured seats are candidates.
    ids = {v.measurement_id for v in controller.comparison_candidates()}
    assert {a.measurement_id, b.measurement_id} <= ids

    mismatches = controller.comparison_mismatches(ds_a.dataset_id, ds_b.dataset_id)
    assert "channel_role" in mismatches
    assert "source_speakers" in mismatches

    saved = controller.compare_datasets(
        ds_a.dataset_id, ds_b.dataset_id, low_hz=20.0, high_hz=80.0
    )
    assert saved.dataset_a_id == ds_a.dataset_id
    assert saved.valid_points > 0


# ---------------------------------------------------------------------
# #484 comparison spec: reference band + excluded bands


def test_comparison_spec_records_reference_band_and_exclusions(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository, _ = _controller(
        scene_repository, revision.document_id
    )
    controller.stage_rew_text(b"20 70\n40 71\n80 69\n", "a.txt")
    a = controller.commit_pending(_assignment())
    controller.stage_rew_text(b"20 60\n40 62\n80 61\n", "b.txt")
    b = controller.commit_pending(_assignment())
    ds_a = measurement_repository.dataset_for_measurement(a.measurement_id)
    ds_b = measurement_repository.dataset_for_measurement(b.measurement_id)
    sha_a, sha_b = ds_a.source_sha256, ds_b.source_sha256

    saved = controller.compare_datasets(
        ds_a.dataset_id,
        ds_b.dataset_id,
        low_hz=20.0,
        high_hz=80.0,
        reference_band_hz=(20.0, 60.0),
        excluded_bands=((45.0, 65.0),),
    )
    assert saved.reference_band_hz == (20.0, 60.0)
    assert saved.excluded_bands == ((45.0, 65.0),)
    assert saved.level_offset_db is not None or saved.valid_points == 0
    # Spec never mutates the datasets it compares.
    assert ds_a.source_sha256 == sha_a and ds_b.source_sha256 == sha_b

    # A reference band outside every shared grid reports as unavailable.
    saved2 = controller.compare_datasets(
        ds_a.dataset_id,
        ds_b.dataset_id,
        low_hz=20.0,
        high_hz=80.0,
        reference_band_hz=(5000.0, 8000.0),
    )
    assert saved2.level_offset_db is None
    assert saved2.shape_rms_db is None


# ---------------------------------------------------------------------
# #489 phase visualization authority


def test_phase_trace_returns_stored_samples_and_deterministic_unwrap(
    tmp_path: Path,
) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    measurement_repository = CadMeasurementRepository(scene_repository)
    source = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
        phase_deg=(170.0, -175.0, -160.0),
        phase_status="valid",
    )
    record = measurement_record_for_revision(
        revision, "point-mlp", evidence_type="measured", channel_role="front_left",
        source_kind="unknown",
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id="dataset-phase",
        measurement_id=record.measurement_id,
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
        phase_deg=(170.0, -175.0, -160.0),
        phase_status="valid",
        source_sha256=sha256(source).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    measurement_repository.save(
        record, dataset, raw_filename="phase.json", raw_bytes=source
    )

    wrapped = phase_trace(dataset)
    assert wrapped is not None
    assert wrapped.phase_deg == (170.0, -175.0, -160.0)  # stored, untouched
    assert wrapped.unwrapped is False

    unwrapped = phase_trace(dataset, unwrap=True)
    assert unwrapped is not None
    # 170 → -175 is a -345 jump; unwrap renders it as +15 continuation.
    assert unwrapped.unwrapped is True
    deltas = [
        b - a
        for a, b in zip(unwrapped.phase_deg, unwrapped.phase_deg[1:])
    ]
    assert all(abs(d) <= 180.0 for d in deltas)
    # Stored samples are never modified by display processing.
    assert dataset.phase_deg == (170.0, -175.0, -160.0)

    absent = CadFrequencyResponseDataset(
        dataset_id="dataset-noph",
        measurement_id=record.measurement_id,
        frequency_hz=(20.0, 40.0),
        level_db=(70.0, 71.0),
        phase_status="absent",
        source_sha256=sha256(b"x").hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    assert phase_trace(absent) is None  # no synthetic phase


# ---------------------------------------------------------------------
# #503 display smoothing + provenance


def test_display_smoothing_is_deterministic_and_non_destructive(
    tmp_path: Path,
) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    measurement_repository = CadMeasurementRepository(scene_repository)
    source = declared_fr_raw(
        frequency_hz=(20.0, 25.0, 32.0, 40.0, 50.0),
        level_db=(60.0, 66.0, 63.0, 70.0, 68.0),
        smoothing="1/24 oct (imported)",
        processing={"fixture_raw": "x"},
    )
    record = measurement_record_for_revision(
        revision, "point-mlp", evidence_type="measured", channel_role="front_left",
        source_kind="unknown",
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id="dataset-smooth",
        measurement_id=record.measurement_id,
        frequency_hz=(20.0, 25.0, 32.0, 40.0, 50.0),
        level_db=(60.0, 66.0, 63.0, 70.0, 68.0),
        phase_status="absent",
        smoothing="1/24 oct (imported)",
        processing_json=canonical_json({"fixture_raw": "x"}),
        source_sha256=sha256(source).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    measurement_repository.save(
        record, dataset, raw_filename="s.json", raw_bytes=source
    )

    raw = smoothed_level_trace(dataset, 0)
    assert raw.level_db == dataset.level_db
    smoothed = smoothed_level_trace(dataset, 6)
    assert smoothed.frequency_hz == dataset.frequency_hz
    assert smoothed.algorithm is not None
    again = smoothed_level_trace(dataset, 6)
    assert again == smoothed  # deterministic
    assert dataset.level_db == (60.0, 66.0, 63.0, 70.0, 68.0)

    label = trace_label(dataset, 6)
    assert "1/6" in label and "imported" in label
    assert "imported" in processing_summary(dataset)


# ---------------------------------------------------------------------
# #487 spatial context


def test_spatial_context_reports_bound_and_current_revisions(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, _, _ = _controller(scene_repository, revision.document_id)
    controller.stage_rew_text(b"20 70\n40 71\n", "spatial.txt")
    record = controller.commit_pending(_assignment())

    context = controller.spatial_context(record.measurement_id)
    assert context.bound_revision is not None
    assert context.bound_revision.revision_id == revision.revision_id
    assert context.bound_is_current is True
    assert context.room_changed is False
    assert context.effective_entity_id == "point-mlp"
    assert context.effective_source_speaker_ids == ("speaker-fl",)

    # Move a speaker: the bound revision stays pinned while a diff appears.
    working = WorkingDocument(
        revision.document,
        source_revision_id=revision.revision_id,
        saved_content_hash=revision.content_hash,
    )
    working.move_entity("speaker-fl", Position3(x_m=9.9, y_m=9.9, z_m=1.0))
    scene_repository.save(
        working.committed_document, parent_revision_id=revision.revision_id
    )
    context = controller.spatial_context(record.measurement_id)
    assert context.bound_revision.revision_id == revision.revision_id
    assert context.bound_is_current is False
    assert context.room_changed is True
    moved = [c for c in context.changes if c.kind == "moved"]
    assert {c.entity_id for c in moved} == {"speaker-fl"}
    assert moved[0].distance_m > 1.0
