"""Round-11 scale regression: listing + batch paths keep repository
round-trips bounded instead of O(N) per measurement row.

The fail-closed contract (every authoritative read re-verifies persisted
seals) is unchanged — verification is memoized per operation, never cached
across calls. Repro numbers at N=60 live in docs/reviews/round11-scale.md;
these tests assert the complexity contract at modest volume.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from htdt.cad_measurement_quality_repository import (
    CadMeasurementQualityRepository,
)
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.measurement_workflow import (
    AssignmentCorrection,
    MeasurementAssignment,
    MeasurementWorkflowController,
)


def _saved_f1(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / "cad.sqlite3")
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    return scene_repository, revision


def _controller(
    scene_repository, document_id
) -> tuple[
    MeasurementWorkflowController,
    CadMeasurementRepository,
    CadMeasurementQualityRepository,
]:
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(
        measurement_repository
    )
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
        routing_evidence="manual",
    )
    values.update(overrides)
    return MeasurementAssignment(**values)


def _seed(controller, count: int, *, base: int = 0) -> list[str]:
    ids: list[str] = []
    for index in range(count):
        level = 60 + base + index
        controller.stage_rew_text(
            f"20 {level}\n40 {level + 1}\n80 {level - 1}\n".encode(),
            f"m{base + index}.txt",
        )
        ids.append(
            controller.commit_pending(_assignment()).measurement_id
        )
    return ids


class _ConnectionCounter:
    """Counts ``sqlite3.connect`` calls — every repository connection
    funnels through it via ``htdt.cad_schema.connect_sqlite``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        real_connect = sqlite3.connect

        def counting(*args, **kwargs):
            self.calls += 1
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", counting)


def test_measurement_views_open_bounded_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One listing must not pay per-row connection churn.

    Before the fix each row re-opened connections for its dataset verify,
    correction, disposition, report and attachments (~10 connections per
    measurement — 604 connections / 7.7s at N=60). After batching, a listing
    is a handful of document-scoped queries regardless of row count.
    """
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository, _ = _controller(
        scene_repository, revision.document_id
    )
    ids = _seed(controller, 30)
    # Rows with overlays (correction) and extras (attachment) exercise the
    # joins that used to fan out per row.
    controller.correct_assignment(
        ids[0],
        AssignmentCorrection(channel_role="center"),
        reason="mislabeled at import",
    )
    controller.save_source_attachment(
        ids[1], kind="notes", filename="n.txt", raw_bytes=b"x"
    )

    counter = _ConnectionCounter(monkeypatch)
    views = controller.measurement_views()
    calls_first = counter.calls
    assert calls_first <= 30, (
        f"measurement_views opened {calls_first} connections for "
        f"{len(views)} rows — per-row fan-out returned"
    )

    counter.calls = 0
    views_again = controller.measurement_views()
    calls_second = counter.calls
    # Dataset memoization is per-call (a second listing re-verifies every
    # seal — fail closed); only the file-signature schema gate may skip its
    # own ro-connection when the database file is unchanged.
    assert calls_second <= calls_first
    assert len(views_again) == len(views) == 30


def test_measurement_views_overlay_fields_preserved(
    tmp_path: Path,
) -> None:
    """Batched reads must return the same overlay fields the per-row path
    produced (effective bindings, disposition, attachment count)."""
    scene_repository, revision = _saved_f1(tmp_path)
    controller, _, _ = _controller(scene_repository, revision.document_id)
    ids = _seed(controller, 12)
    controller.correct_assignment(
        ids[2],
        AssignmentCorrection(
            channel_role="center",
            source_speaker_ids=("speaker-fr",),
        ),
        reason="mislabeled at import",
    )
    controller.save_source_attachment(
        ids[3], kind="notes", filename="n.txt", raw_bytes=b"x"
    )

    views = {v.measurement_id: v for v in controller.measurement_views()}
    corrected = views[ids[2]]
    assert corrected.is_corrected is True
    assert corrected.disposition == "corrected"
    assert corrected.channel_role == "front_left"
    assert corrected.effective_channel_role == "center"
    assert corrected.effective_source_speaker_ids == ("speaker-fr",)
    assert corrected.is_normally_eligible is True

    attached = views[ids[3]]
    assert attached.attachment_count == 1

    plain = views[ids[5]]
    assert plain.is_corrected is False
    assert plain.attachment_count == 0


def test_staging_files_does_not_rescan_authoritative_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``stage_rew_text_files`` must not call ``measurement_views`` at all —
    duplicate names/detection used to run the full evidence verification
    per staged item (501s to stage 60 files at N=60)."""
    scene_repository, revision = _saved_f1(tmp_path)
    controller, _, _ = _controller(scene_repository, revision.document_id)
    _seed(controller, 15)

    calls = 0
    real_views = MeasurementWorkflowController.measurement_views

    def counting(self):
        nonlocal calls
        calls += 1
        return real_views(self)

    monkeypatch.setattr(
        MeasurementWorkflowController, "measurement_views", counting
    )
    counter = _ConnectionCounter(monkeypatch)
    items = controller.stage_rew_text_files(
        [
            (f"20 {100 + i}\n40 {101 + i}\n80 {99 + i}\n".encode(), f"n{i}.txt")
            for i in range(10)
        ]
    )
    assert len(items) == 10
    assert {item.status for item in items} == {"staged"}
    assert calls == 0, (
        f"staging re-ran the authoritative listing {calls} times"
    )
    assert counter.calls <= 12, (
        f"staging opened {counter.calls} connections for 10 files "
        f"(duplicate detection must reuse one map per batch)"
    )


def test_batch_items_single_map_per_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``batch_items`` resolves duplicate labels from one map, not a
    ``measurement_views`` call per row."""
    scene_repository, revision = _saved_f1(tmp_path)
    controller, _, _ = _controller(scene_repository, revision.document_id)
    committed = _seed(controller, 10)

    # Stage an exact duplicate of an existing measurement so
    # duplicate_of_name resolution is exercised.
    dup = f"20 {60}\n40 {61}\n80 {59}\n".encode()
    controller.stage_rew_text_files([(dup, "dup.txt")])
    controller.stage_rew_text_files(
        [(f"20 {120 + i}\n40 {121 + i}\n80 {119 + i}\n".encode(), f"p{i}.txt")
         for i in range(8)]
    )

    calls = 0
    real_views = MeasurementWorkflowController.measurement_views

    def counting(self):
        nonlocal calls
        calls += 1
        return real_views(self)

    monkeypatch.setattr(
        MeasurementWorkflowController, "measurement_views", counting
    )
    items = controller.batch_items()
    assert calls == 0
    dup_item = next(i for i in items if i.duplicate_of_measurement_id)
    assert dup_item.duplicate_of_measurement_id == committed[0]
    assert dup_item.duplicate_of_name


def test_commit_batch_bounded_and_reports_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository, _ = _controller(
        scene_repository, revision.document_id
    )
    _seed(controller, 10)
    items = controller.stage_rew_text_files(
        [
            (f"20 {100 + i}\n40 {101 + i}\n80 {99 + i}\n".encode(), f"c{i}.txt")
            for i in range(8)
        ]
    )
    applied = controller.apply_batch_assignment(_assignment())
    assert applied == 8

    progress: list[tuple[int, int]] = []
    counter = _ConnectionCounter(monkeypatch)
    outcomes = controller.commit_batch(
        progress=lambda done, total: progress.append((done, total))
    )
    assert {o.outcome for o in outcomes} == {"committed"}
    assert len(measurement_repository.list_measurements(
        revision.document_id
    )) == 18
    assert progress
    # Progress runs once per item before + after the commit attempt.
    assert progress[-1][0] == 8 and progress[-1][1] == 8
    assert counter.calls <= 60, (
        f"commit_batch opened {counter.calls} connections for 8 items — "
        f"per-item map rebuilds returned"
    )


def test_commit_batch_cancel_event_stops_early(tmp_path: Path) -> None:
    """Cancel is cooperative: the loop stops between items, already-saved
    items stay committed (the last commit is atomic per item)."""
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository, _ = _controller(
        scene_repository, revision.document_id
    )
    items = controller.stage_rew_text_files(
        [
            (f"20 {100 + i}\n40 {101 + i}\n80 {99 + i}\n".encode(), f"x{i}.txt")
            for i in range(6)
        ]
    )
    controller.apply_batch_assignment(_assignment())

    cancel = Event()
    committed_before_cancel = 0

    def cancel_after_two(done: int, total: int) -> None:
        nonlocal committed_before_cancel
        committed_before_cancel = done
        if done >= 2:
            cancel.set()

    outcomes = controller.commit_batch(
        cancel_event=cancel, progress=cancel_after_two
    )
    committed = [o for o in outcomes if o.outcome == "committed"]
    assert 1 <= len(committed) <= 3  # stopped at the item boundary
    remaining = [
        i for i in controller.batch_items()
        if i.status in ("staged", "failed")
    ]
    assert len(remaining) == 6 - len(committed)
    assert len(measurement_repository.list_measurements(
        revision.document_id
    )) == len(committed)


def test_refresh_uses_single_authoritative_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``refresh()`` must not pay ``measurement_views`` per section — the
    batch table, campaign combo, quality table and comparison pickers share
    one verified listing (previously up to 9 calls ≈ 69s at N=60)."""
    from PySide6.QtWidgets import QApplication

    from htdt.measurement_page_workspace import MeasurementPageWorkspace

    QApplication.instance() or QApplication([])
    scene_repository, revision = _saved_f1(tmp_path)
    controller, _, _ = _controller(scene_repository, revision.document_id)
    _seed(controller, 8)

    calls = 0
    real_views = MeasurementWorkflowController.measurement_views

    def counting(self):
        nonlocal calls
        calls += 1
        return real_views(self)

    workspace = MeasurementPageWorkspace(controller)
    try:
        monkeypatch.setattr(
            MeasurementWorkflowController, "measurement_views", counting
        )
        workspace.refresh()
        assert calls == 1, (
            f"refresh() ran measurement_views {calls} times — "
            f"sections must share one listing"
        )
    finally:
        workspace.close()
        workspace.deleteLater()
