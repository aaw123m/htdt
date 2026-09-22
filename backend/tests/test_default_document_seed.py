"""Regression coverage for #627.

A fresh project/default document must never silently gain the synthetic F1
fixture as canonical SceneRevision authority. The synthetic development demo
remains available only through the explicit ``--seed-synthetic-demo`` path
under its own document identity, and pre-existing F1 revision graphs are
preserved and classified conservatively instead of overwritten.
"""

from __future__ import annotations

import logging
import os
import sqlite3

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from htdt import native_cad
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_model_validation_repository import CadModelValidationRepository
from htdt.cad_prediction_repository import CadPredictionRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_roomsim_repository import CadRoomSimRepository
from htdt.cad_scene import (
    F1_DOCUMENT_ID,
    Position3,
    SceneEntity,
    Size3,
    make_empty_scene,
    make_f1_scene,
    scene_content_hash,
)
from htdt.cad_search_repository import CadSearchRepository
from htdt.cad_synthetic_demo import (
    SYNTHETIC_DEMO_DOCUMENT_ID,
    seed_synthetic_optimization_demo,
)
from htdt.default_document import (
    LEGACY_PROJECT_LABEL,
    SYNTHETIC_FIXTURE_LABEL,
    classify_default_document,
    log_default_document_classification,
)
from htdt.native_editor import NativeEditorWindow
from htdt.overview_readiness import OverviewReadinessService
from htdt.room_workspace import RoomWorkspaceController
from htdt.theater_workflow import TheaterWorkflowWindow


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _overview_service(repository: SceneRepository) -> OverviewReadinessService:
    measurement = CadMeasurementRepository(repository)
    search = CadSearchRepository(repository)
    roomsim = CadRoomSimRepository(repository, search)
    validation = CadModelValidationRepository(search, roomsim, measurement)
    prediction = CadPredictionRepository(repository)
    return OverviewReadinessService(
        repository,
        measurement,
        prediction,
        search,
        validation,
    )


def _edited_f1_document():
    """A real user edit on top of the fixture: one extra seat entity."""

    document = make_f1_scene()
    return document.model_copy(
        update={
            "entities": document.entities
            + (
                SceneEntity(
                    entity_id="seat-user",
                    kind="seat",
                    name="User seat",
                    position=Position3(x_m=3.0, y_m=3.2, z_m=0.45),
                    size_m=Size3(x_m=0.70, y_m=0.80, z_m=0.90),
                ),
            )
        }
    )


# --- Fresh startup never persists synthetic content ---------------------------


def test_fresh_default_document_opens_empty_scene(tmp_path) -> None:
    """Opening the default F1 identity on a new data dir must not seed demo data."""

    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)

    revision = repository.latest(F1_DOCUMENT_ID)
    assert revision is not None
    assert revision.document.room is None
    assert revision.document.entities == ()
    assert revision.content_hash == scene_content_hash(
        make_empty_scene(F1_DOCUMENT_ID)
    )
    assert revision.content_hash != scene_content_hash(make_f1_scene())
    assert controller.committed_document == revision.document
    # No room means the workspace honestly refuses object editing.
    assert not controller.can_edit


def test_unknown_document_opens_empty_scene_without_alias(tmp_path) -> None:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    controller = RoomWorkspaceController(repository, "project-new")

    revision = repository.latest("project-new")
    assert revision is not None
    assert revision.document == make_empty_scene("project-new")
    assert controller.committed_document.room is None
    # Opening another document never creates or claims the F1 identity.
    assert repository.latest(F1_DOCUMENT_ID) is None


def test_legacy_editor_windows_open_fresh_default_document_empty(tmp_path) -> None:
    """Both windowed startup paths seed empty content, not the fixture."""

    app = _app()
    for window_type in (NativeEditorWindow, TheaterWorkflowWindow):
        repository = SceneRepository(tmp_path / f"{window_type.__name__}.sqlite3")
        window = window_type(repository, F1_DOCUMENT_ID)
        try:
            revision = repository.latest(F1_DOCUMENT_ID)
            assert revision is not None
            assert revision.document.room is None
            assert revision.document.entities == ()
            assert window.working.committed_document == revision.document
        finally:
            window.close()
            window.deleteLater()
            app.processEvents()


# --- Explicit synthetic demo stays available under its own identity -----------


def test_explicit_synthetic_demo_seed_uses_dedicated_identity(tmp_path) -> None:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")

    result = seed_synthetic_optimization_demo(repository)

    assert result.document_id == SYNTHETIC_DEMO_DOCUMENT_ID
    revision = repository.latest(SYNTHETIC_DEMO_DOCUMENT_ID)
    assert revision is not None
    assert revision.document.room is not None
    assert revision.document.entities
    # The explicit demo never creates or claims the default user document.
    assert repository.latest(F1_DOCUMENT_ID) is None


def test_seed_synthetic_demo_cli_stays_explicit_and_separate(tmp_path, capsys) -> None:
    data_dir = tmp_path / "data"

    assert (
        native_cad.main(["--data-dir", str(data_dir), "--seed-synthetic-demo"])
        == 0
    )

    repository = SceneRepository(data_dir / "cad-scenes.sqlite3")
    assert repository.latest(SYNTHETIC_DEMO_DOCUMENT_ID) is not None
    assert repository.latest(F1_DOCUMENT_ID) is None
    captured = capsys.readouterr()
    assert "synthetic" in captured.out.lower()


# --- Existing default-document history is preserved, never overwritten --------


def test_untouched_fixture_revision_is_preserved_not_replaced(tmp_path) -> None:
    """A pre-#627 auto-seeded F1 revision still opens as-is (classified, not
    silently deleted or overwritten by an empty root)."""

    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    seeded = repository.save(make_f1_scene(), parent_revision_id=None).revision

    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)

    assert controller.working.source_revision_id == seeded.revision_id
    latest = repository.latest(F1_DOCUMENT_ID)
    assert latest is not None
    assert latest.revision_id == seeded.revision_id
    assert latest.content_hash == seeded.content_hash
    assert controller.committed_document.entity("speaker-fl") is not None


def test_edited_default_document_history_is_preserved(tmp_path) -> None:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    seeded = repository.save(make_f1_scene(), parent_revision_id=None).revision
    edited = repository.save(
        _edited_f1_document(), parent_revision_id=seeded.revision_id
    ).revision

    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)

    # The user's head revision loads unchanged; no new root is written.
    assert controller.working.source_revision_id == edited.revision_id
    latest = repository.latest(F1_DOCUMENT_ID)
    assert latest is not None
    assert latest.revision_id == edited.revision_id
    assert latest.document.entity("seat-user").name == "User seat"


# --- Conservative classification for migration (#627 section 3) ---------------


def test_classify_absent_default_document(tmp_path) -> None:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")

    report = classify_default_document(repository)

    assert report.kind == "absent"
    assert report.label is None
    assert report.revision_count == 0
    assert report.matches_known_seed is False
    assert report.has_attached_evidence is False


def test_classify_untouched_fixture_as_synthetic(tmp_path) -> None:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)

    report = classify_default_document(repository)

    assert report.kind == "synthetic_fixture"
    assert report.label == SYNTHETIC_FIXTURE_LABEL
    assert report.revision_count == 1
    assert report.matches_known_seed is True
    assert report.has_attached_evidence is False


def test_view_state_alone_keeps_fixture_classification(tmp_path) -> None:
    """Disposable UI state is not user work: selection rows do not make an
    untouched fixture a legacy project."""

    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    repository.save_view_state(
        F1_DOCUMENT_ID,
        selected_id="speaker-fl",
        hidden_ids=set(),
        locked_ids=set(),
    )

    report = classify_default_document(repository)

    assert report.kind == "synthetic_fixture"
    assert report.has_attached_evidence is False


def test_classify_edited_default_document_as_legacy_project(tmp_path) -> None:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    seeded = repository.save(make_f1_scene(), parent_revision_id=None).revision
    repository.save(
        _edited_f1_document(), parent_revision_id=seeded.revision_id
    )

    report = classify_default_document(repository)

    assert report.kind == "legacy_project"
    assert report.label == LEGACY_PROJECT_LABEL
    assert report.revision_count == 2
    assert report.matches_known_seed is False


def test_classify_fixture_with_attached_evidence_as_legacy(tmp_path) -> None:
    """A recovery draft is in-flight user work: even an untouched fixture
    revision classifies conservatively as a legacy project."""

    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    seeded = repository.save(make_f1_scene(), parent_revision_id=None).revision
    repository.save_recovery(
        _edited_f1_document(), source_revision_id=seeded.revision_id
    )

    report = classify_default_document(repository)

    assert report.kind == "legacy_project"
    assert report.matches_known_seed is True
    assert report.has_attached_evidence is True


def test_classify_other_document_ids(tmp_path) -> None:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_empty_scene("project-a"), parent_revision_id=None)

    report = classify_default_document(repository, "project-a")

    assert report.kind == "legacy_project"
    assert report.revision_count == 1
    assert report.matches_known_seed is False


# --- Startup surfacing of the classification (#627 section 3) -----------------


def test_classification_logging_surfaces_legacy_project(tmp_path, caplog) -> None:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    seeded = repository.save(make_f1_scene(), parent_revision_id=None).revision
    repository.save(
        _edited_f1_document(), parent_revision_id=seeded.revision_id
    )

    logger = logging.getLogger("htdt.test.default_document")
    with caplog.at_level(logging.INFO, logger=logger.name):
        report = log_default_document_classification(repository, logger)

    assert report.kind == "legacy_project"
    assert "kind=legacy_project" in caplog.text
    assert LEGACY_PROJECT_LABEL in caplog.text


def test_classification_logging_marks_untouched_fixture(tmp_path, caplog) -> None:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)

    logger = logging.getLogger("htdt.test.default_document")
    with caplog.at_level(logging.INFO, logger=logger.name):
        report = log_default_document_classification(repository, logger)

    assert report.kind == "synthetic_fixture"
    assert "kind=synthetic_fixture" in caplog.text
    assert SYNTHETIC_FIXTURE_LABEL in caplog.text


def test_classification_logging_stays_silent_when_absent(tmp_path, caplog) -> None:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")

    logger = logging.getLogger("htdt.test.default_document")
    with caplog.at_level(logging.INFO, logger=logger.name):
        report = log_default_document_classification(repository, logger)

    assert report.kind == "absent"
    assert "default document classification" not in caplog.text


def test_classify_absent_when_revision_schema_missing(tmp_path) -> None:
    """A store without the revisions table simply has no history."""

    database = tmp_path / "empty.sqlite3"
    sqlite3.connect(database).close()

    class _BareRepository:
        def __init__(self, path):
            self.path = path

    report = classify_default_document(_BareRepository(database))

    assert report.kind == "absent"
    assert report.revision_count == 0


def test_classification_logging_fails_closed_without_breaking(tmp_path, caplog) -> None:
    """Diagnostics must never break startup: an unreadable store warns only."""

    class _BrokenRepository:
        @property
        def path(self):
            raise OSError("store unavailable")

    logger = logging.getLogger("htdt.test.default_document")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        report = log_default_document_classification(_BrokenRepository(), logger)

    assert report is None
    assert "default document classification failed" in caplog.text


# --- Honest empty-project readiness (#627 section 6) ---------------------------


def test_unopened_document_reports_room_missing(tmp_path) -> None:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")

    view = _overview_service(repository).read(F1_DOCUMENT_ID)

    assert view.blockers[0].code == "room.missing"
    assert view.optimization_ready is False


def test_fresh_default_document_reports_honest_not_defined_readiness(tmp_path) -> None:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    RoomWorkspaceController(repository, F1_DOCUMENT_ID)

    view = _overview_service(repository).read(F1_DOCUMENT_ID)

    blocker_codes = {notice.code for notice in view.blockers}
    assert "room.geometry_incomplete" in blocker_codes
    assert "speaker.missing" in blocker_codes
    warning_codes = {notice.code for notice in view.warnings}
    assert "measurement.missing" in warning_codes
    assert "prediction.missing" in warning_codes
    assert view.optimization_ready is False
    assert view.next_action is not None
    assert view.next_action.action_id == "room.complete_geometry"
