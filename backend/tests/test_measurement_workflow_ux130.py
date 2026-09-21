from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDockWidget

from hashlib import sha256

from htdt.cad_document import WorkingDocument
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    declared_fr_raw,
    measurement_record_for_revision,
    normalize_rew_text,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, make_f1_scene
from htdt.measurement_page_workspace import (
    MeasurementPageWorkspace,
    build_measurement_workspace_mount,
    create_measurement_workspace_factory,
)
from htdt.measurement_workflow import MeasurementAssignment, MeasurementWorkflowController


def _saved_f1(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / "cad.sqlite3")
    revision = scene_repository.save(make_f1_scene(), parent_revision_id=None).revision
    return scene_repository, revision


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_stage_assignment_and_commit_use_existing_measurement_authorities(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    measurement_repository = CadMeasurementRepository(scene_repository)
    controller = MeasurementWorkflowController(
        scene_repository,
        revision.document_id,
        measurement_repository=measurement_repository,
    )

    raw = b"Frequency SPL\n20 70.0\n40 71.5\n80 69.0\n"
    pending = controller.stage_rew_text(raw, "mlp-fl.txt")

    assert measurement_repository.list_measurements(revision.document_id) == ()
    assert pending.sample_count == 3
    assert pending.frequency_band_hz == (20.0, 80.0)
    assert {target.entity_id for target in controller.assignment_targets()} >= {"point-mlp"}
    assert {speaker.entity_id for speaker in controller.source_speakers()} >= {"speaker-fl"}

    record = controller.commit_pending(
        MeasurementAssignment(
            measurement_entity_id="point-mlp",
            evidence_type="measured",
            channel_role="front_left",
            source_speaker_ids=("speaker-fl",),
            radiation_scope="single",
            routing_evidence="verified",
        )
    )

    reopened = measurement_repository.get_measurement(record.measurement_id)
    dataset = measurement_repository.dataset_for_measurement(record.measurement_id)
    assert reopened == record
    assert dataset is not None
    assert record.scene_revision_id == revision.revision_id
    assert record.scene_content_hash == revision.content_hash
    assert record.measurement_entity_id == "point-mlp"
    assert record.evidence_type == "measured"
    assert record.channel_role == "front_left"
    assert record.source_speaker_ids == ("speaker-fl",)
    assert record.routing_evidence == "verified"
    assert controller.pending_import is None


def test_pending_assignment_fails_closed_if_saved_scene_changes(tmp_path: Path) -> None:
    scene_repository, revision_a = _saved_f1(tmp_path)
    controller = MeasurementWorkflowController(scene_repository, revision_a.document_id)
    controller.stage_rew_text(b"20 70\n40 71\n", "before-change.txt")

    working = WorkingDocument(
        revision_a.document,
        source_revision_id=revision_a.revision_id,
        saved_content_hash=revision_a.content_hash,
    )
    working.move_entity("point-mlp", Position3(x_m=3.1, y_m=3.0, z_m=1.1))
    scene_repository.save(
        working.committed_document,
        parent_revision_id=revision_a.revision_id,
    )

    try:
        controller.commit_pending(
            MeasurementAssignment(measurement_entity_id="point-mlp")
        )
    except ValueError as exc:
        assert "読み込み直" in str(exc)
    else:
        raise AssertionError("staged assignment must not silently move to a newer SceneRevision")


def test_quality_and_phase_capability_are_read_from_saved_record_and_dataset(tmp_path: Path) -> None:
    scene_repository, revision_a = _saved_f1(tmp_path)
    measurement_repository = CadMeasurementRepository(scene_repository)
    # Declared importer: the raw asset honestly declares valid phase data, so
    # the persisted dataset's phase_status is authoritative rather than a
    # post-normalization mutation that cannot rederive from the raw bytes.
    source = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
        phase_deg=(10.0, 20.0, 30.0),
        phase_status="valid",
    )
    record = measurement_record_for_revision(
        revision_a,
        "point-mlp",
        evidence_type="measured",
        channel_role="center",
        imported_at="2026-09-17T09:30:00+00:00",
        source_kind="unknown",
        quality_status="verified",
        quality_reasons=("fixture-authority-reason",),
        quality_source="fixture-authority",
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f"dataset-{record.measurement_id}",
        measurement_id=record.measurement_id,
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
        phase_deg=(10.0, 20.0, 30.0),
        phase_status="valid",
        source_sha256=sha256(source).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    measurement_repository.save(
        record,
        dataset,
        raw_filename="phase.json",
        raw_bytes=source,
    )

    controller = MeasurementWorkflowController(
        scene_repository,
        revision_a.document_id,
        measurement_repository=measurement_repository,
    )
    view = controller.measurement_views()[0]

    assert view.quality_status == "verified"
    assert view.quality_reasons == ("fixture-authority-reason",)
    assert view.quality_source == "fixture-authority"
    assert view.phase_status == "valid"
    assert view.phase_timing_available is True
    assert view.scene_matches_current is True

    working = WorkingDocument(
        revision_a.document,
        source_revision_id=revision_a.revision_id,
        saved_content_hash=revision_a.content_hash,
    )
    working.move_entity("speaker-fl", Position3(x_m=1.45, y_m=0.75, z_m=1.05))
    scene_repository.save(
        working.committed_document,
        parent_revision_id=revision_a.revision_id,
    )
    assert controller.measurement_views()[0].scene_matches_current is False


def test_predicted_vs_measured_comparison_delegates_and_persists(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    measurement_repository = CadMeasurementRepository(scene_repository)
    controller = MeasurementWorkflowController(
        scene_repository,
        revision.document_id,
        measurement_repository=measurement_repository,
    )

    controller.stage_rew_text(b"20 70\n40 71\n80 69\n", "measured.txt")
    measured = controller.commit_pending(
        MeasurementAssignment(
            measurement_entity_id="point-mlp",
            evidence_type="measured",
            channel_role="front_left",
        )
    )
    controller.stage_rew_text(b"20 69\n40 70\n80 68\n", "predicted.txt")
    predicted = controller.commit_pending(
        MeasurementAssignment(
            measurement_entity_id="point-mlp",
            evidence_type="predicted",
            channel_role="front_left",
        )
    )

    measured_dataset = measurement_repository.dataset_for_measurement(measured.measurement_id)
    predicted_dataset = measurement_repository.dataset_for_measurement(predicted.measurement_id)
    assert measured_dataset is not None
    assert predicted_dataset is not None
    assert [row.measurement_id for row in controller.comparison_candidates("measured")] == [
        measured.measurement_id
    ]
    assert [row.measurement_id for row in controller.comparison_candidates("predicted")] == [
        predicted.measurement_id
    ]

    saved = controller.compare_datasets(
        measured_dataset.dataset_id,
        predicted_dataset.dataset_id,
        low_hz=20.0,
        high_hz=80.0,
    )

    assert saved.dataset_a_id == measured_dataset.dataset_id
    assert saved.dataset_b_id == predicted_dataset.dataset_id
    assert saved.scene_revision_a_id == revision.revision_id
    assert saved.scene_revision_b_id == revision.revision_id
    assert measurement_repository.get_comparison(saved.comparison_id) == saved


def test_measurement_workspace_is_page_based_and_shell_mountable(tmp_path: Path) -> None:
    app = _app()
    scene_repository, revision = _saved_f1(tmp_path)
    controller = MeasurementWorkflowController(scene_repository, revision.document_id)
    controller.stage_rew_text(b"20 70\n40 71\n80 69\n", "ui.txt")
    record = controller.commit_pending(
        MeasurementAssignment(
            measurement_entity_id="point-mlp",
            evidence_type="measured",
        )
    )

    mount = build_measurement_workspace_mount(controller)
    workspace = mount.widget
    assert isinstance(workspace, MeasurementPageWorkspace)
    assert workspace.parent() is None
    assert workspace.pages.count() == 4
    assert workspace.findChildren(QDockWidget) == []

    assert mount.on_context_changed is not None
    mount.on_context_changed("quality")
    assert workspace.current_context_id == "quality"
    assert workspace.pages.currentWidget().objectName() == "measurementQualityPage"

    assert mount.on_entity_requested is not None
    mount.on_entity_requested(record.measurement_id)
    assert workspace.quality_table.currentRow() >= 0

    factory = create_measurement_workspace_factory(
        scene_repository,
        revision.document_id,
    )
    second_mount = factory()
    assert isinstance(second_mount.widget, MeasurementPageWorkspace)
    assert second_mount.widget.parent() is None

    second_mount.widget.close()
    workspace.close()
    second_mount.widget.deleteLater()
    workspace.deleteLater()
    app.processEvents()
