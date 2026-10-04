"""REV42-FULLREVIEW regression tests.

Defects found in the second-pass adversarial review:

- ``scan_rew_watch_dir`` never evicted ``seen`` keys — unbounded growth and
  an identical-signature re-drop after deletion was silently lost.
- ``stage_rew_text_files``/``stage_rew_snapshots`` leaked appended entries
  when a mid-stage repository read failed; the auto-ingest retry then
  staged the same files again and doubled the queue rows.
- A measurement whose fetch kept failing was re-requested every poll tick
  forever; a watch file whose staging kept failing cycled through
  scan→stage→fail every other tick; both failure notices re-shouted each
  tick.
- Uncommitted batch rows sat outside the dirty-state contract — document
  switch, project switch, or exit silently dropped staged queue rows —
  and there was no way to remove a failed/unwanted row at all.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from htdt.application_preferences import ApplicationPreferenceStore
from htdt.measurement_workflow import MeasurementAssignment
from htdt.rew_auto import scan_rew_watch_dir

from test_measurement_rew_auto import (  # noqa: E402
    _FakeRewClient,
    _app,
    _close,
    _drain,
    _snapshot,
    _workspace,
)


REW_TEXT = b"* REW export\n20.0 70.0\n30.0 71.0\n40.0 72.0\n"


def test_watch_seen_map_stays_bounded_and_redelivers(tmp_path: Path) -> None:
    """Vanished files lose their seen marker; an identical-signature
    re-drop after deletion is delivered again."""
    watch = tmp_path / "watch"
    watch.mkdir()
    seen: dict[str, tuple[int, int]] = {}
    pending: dict[str, tuple[int, int]] = {}

    scan_rew_watch_dir(watch, seen, pending)  # baseline
    target = watch / "a.txt"
    target.write_bytes(REW_TEXT)
    scan_rew_watch_dir(watch, seen, pending)
    files, _ = scan_rew_watch_dir(watch, seen, pending)
    assert len(files) == 1
    old_signature = seen[str(target)]

    # Deliver + delete several transient files — the map must not retain
    # their markers.
    for index in range(5):
        transient = watch / f"t{index}.frd"
        transient.write_bytes(REW_TEXT + str(index).encode())
        scan_rew_watch_dir(watch, seen, pending)
        files, _ = scan_rew_watch_dir(watch, seen, pending)
        assert len(files) == 1
        transient.unlink()
    target.unlink()
    scan_rew_watch_dir(watch, seen, pending)
    assert set(seen) == {'\x00scanned'}, seen

    # Re-drop with the identical signature — the stale marker must not
    # suppress re-delivery.
    target.write_bytes(REW_TEXT)
    os.utime(target, ns=(old_signature[0], old_signature[0]))
    scan_rew_watch_dir(watch, seen, pending)
    files, _ = scan_rew_watch_dir(watch, seen, pending)
    assert len(files) == 1
    assert files[0][1] == "a.txt"


def _commit_first_measurement(controller) -> None:
    items = controller.stage_rew_text_files([(REW_TEXT, "first.txt")])
    controller.set_batch_item_assignment(
        items[0].item_id,
        MeasurementAssignment(measurement_entity_id="point-mlp"),
    )
    outcomes = controller.commit_batch([items[0].item_id])
    assert outcomes[0].outcome == "committed"


def test_failed_stage_rolls_back_and_retry_stages_once(tmp_path: Path) -> None:
    """A mid-stage repository failure must not leak entries that a retry
    would double."""
    app = _app()
    controller, workspace = _workspace(tmp_path)
    try:
        _commit_first_measurement(controller)
        # The identical bytes classify 'exact_duplicate', so the tail of
        # stage_rew_text_files runs _duplicate_names_for — fail it.
        controller._duplicate_names_for = lambda entries: (_ for _ in ()).throw(
            RuntimeError("simulated repo read failure")
        )
        with pytest.raises(RuntimeError):
            controller.stage_rew_text_files([(REW_TEXT, "second.txt")])
        assert controller.uncommitted_batch_item_ids() == frozenset()
        del controller._duplicate_names_for

        controller.stage_rew_text_files([(REW_TEXT, "second.txt")])
        rows = [
            item for item in controller.batch_items()
            if item.filename == "second.txt"
        ]
        assert len(rows) == 1
        assert rows[0].status == "staged"
        assert rows[0].duplicate_kind == "exact_duplicate"
    finally:
        _close(app, workspace)


def test_failed_snapshot_stage_rolls_back(tmp_path: Path) -> None:
    app = _app()
    controller, workspace = _workspace(tmp_path)
    try:
        controller._duplicate_names_for = lambda entries: (_ for _ in ()).throw(
            RuntimeError("simulated repo read failure")
        )
        with pytest.raises(RuntimeError):
            controller.stage_rew_snapshots(
                [_snapshot("u1", "MLP", "m-1")]
            )
        assert controller.uncommitted_batch_item_ids() == frozenset()
    finally:
        _close(app, workspace)


def test_uncommitted_ids_and_discard_batch_items(tmp_path: Path) -> None:
    app = _app()
    controller, workspace = _workspace(tmp_path)
    try:
        _commit_first_measurement(controller)
        committed_id = next(
            item.item_id for item in controller.batch_items()
            if item.status == "committed"
        )
        staged = controller.stage_rew_text_files(
            [(b"20 70\n40 71\n80 69\n", "new.txt")]
        )
        staged_id = staged[0].item_id
        assert controller.uncommitted_batch_item_ids() == frozenset(
            {staged_id}
        )
        # Committed rows are never touched by the discard path.
        removed = controller.discard_batch_items(
            [staged_id, committed_id, "nonexistent"]
        )
        assert removed == 1
        remaining = {item.item_id for item in controller.batch_items()}
        assert remaining == {committed_id}
    finally:
        _close(app, workspace)


def test_fetch_failure_capped_marked_seen_and_reported(
    tmp_path: Path,
) -> None:
    app = _app()
    client = _FakeRewClient(rows=[{"uuid": "u1", "title": "ok"}])
    controller, workspace = _workspace(tmp_path, client)
    try:
        workspace.mount_activated()
        _drain(app, workspace)  # baseline
        # 'u-bad' is listed but never fetchable.
        client.rows.append({"uuid": "u-bad", "title": "broken"})
        for _ in range(4):
            workspace._rew_auto_tick()
            _drain(app, workspace)
        assert client.fetch_calls.count("u-bad") == 3
        assert "u-bad" in workspace._rew_seen_uuids
        assert "スキップ" in workspace.notice.text()
        assert "broken" in workspace.notice.text()
        # It does not come back: further ticks fetch nothing.
        workspace._rew_auto_tick()
        _drain(app, workspace)
        assert client.fetch_calls.count("u-bad") == 3
    finally:
        _close(app, workspace)


def test_stage_failure_notice_dedupes(tmp_path: Path) -> None:
    app = _app()
    client = _FakeRewClient(rows=[{"uuid": "u1", "title": "ok"}])
    controller, workspace = _workspace(tmp_path, client)
    try:
        workspace.mount_activated()
        _drain(app, workspace)
        client.rows.append({"uuid": "u2", "title": "new"})
        client.snapshots["u2"] = _snapshot("u2", "new", "m-2")

        def boom(files):
            raise RuntimeError("simulated stage failure")

        controller.stage_rew_snapshots = boom
        workspace._rew_auto_tick()
        _drain(app, workspace)
        first_notice = workspace.notice.text()
        assert first_notice
        workspace._rew_auto_tick()
        _drain(app, workspace)
        # The second failing tick must not re-shout over the notice.
        assert workspace.notice.text() == first_notice
        assert "stage_failed" in workspace._rew_auto_notice_keys
    finally:
        _close(app, workspace)


def test_watch_file_stage_failure_stops_requeueing(
    tmp_path: Path,
) -> None:
    """A deterministically failing watch file must stop cycling through
    scan→stage→fail — after the cap it keeps its seen marker."""
    app = _app()
    watch = tmp_path / "watch"
    watch.mkdir()
    prefs = ApplicationPreferenceStore(tmp_path / "prefs.json")
    prefs.set("integrations.rew_watch_dir", str(watch))
    client = _FakeRewClient(rows=[])
    controller, workspace = _workspace(tmp_path, client, preferences=prefs)
    try:
        workspace.mount_activated()
        _drain(app, workspace)  # baseline scan
        dropped = watch / "bad.txt"
        dropped.write_bytes(REW_TEXT)

        def boom(files):
            raise RuntimeError("simulated stage failure")

        controller.stage_rew_text_files = boom
        for _ in range(6):
            workspace._rew_auto_tick()
            _drain(app, workspace)
        # After _REW_AUTO_MAX_ATTEMPTS failures the file keeps its seen
        # marker and is never delivered again.
        key = str(dropped)
        assert key in workspace._rew_watch_seen
        assert "watch_wedged:bad.txt" in workspace._rew_auto_notice_keys
        assert "スキップ" in workspace.notice.text()
    finally:
        _close(app, workspace)


def test_uncommitted_batch_rows_join_the_dirty_contract(
    tmp_path: Path,
) -> None:
    app = _app()
    controller, workspace = _workspace(tmp_path)
    try:
        controller.stage_rew_text_files(
            [(b"20 70\n40 71\n80 69\n", "q.txt")]
        )
        allowed, reason = workspace.before_deactivate()
        assert not allowed
        assert "読み込みキュー" in reason
        assert workspace.dirty_state() == "pending_import"

        # keep_draft releases the current rows only.
        resolved, _ = workspace.resolve_dirty_state("keep_draft")
        assert resolved
        assert workspace.dirty_state() == "clean"
        allowed, _ = workspace.before_deactivate()
        assert allowed

        # A newly staged row re-arms the gate.
        controller.stage_rew_text_files(
            [(b"20 70\n40 71\n80 69\n", "q2.txt")]
        )
        assert workspace.dirty_state() == "pending_import"

        # discard_pending drops uncommitted rows and clears the gate.
        resolved, _ = workspace.resolve_dirty_state("discard_pending")
        assert resolved
        assert controller.batch_items() == ()
        assert workspace.dirty_state() == "clean"
    finally:
        _close(app, workspace)


def test_keep_draft_with_only_batch_rows_releases_them(
    tmp_path: Path,
) -> None:
    app = _app()
    controller, workspace = _workspace(tmp_path)
    try:
        resolved, _ = workspace.resolve_dirty_state("keep_draft")
        assert not resolved  # nothing staged — honest refusal
        controller.stage_rew_text_files(
            [(b"20 70\n40 71\n80 69\n", "q.txt")]
        )
        resolved, _ = workspace.resolve_dirty_state("keep_draft")
        assert resolved
        assert workspace.dirty_state() == "clean"
        # Released rows stay in the queue — the operator kept them.
        assert len(controller.batch_items()) == 1
    finally:
        _close(app, workspace)


def test_committed_rows_do_not_reblock_after_partial_commit(
    tmp_path: Path,
) -> None:
    """A keep_draft-released row that later commits must not re-arm the
    gate for the remaining released rows."""
    app = _app()
    controller, workspace = _workspace(tmp_path)
    try:
        staged = controller.stage_rew_text_files(
            [(b"20 70\n40 71\n80 69\n", "q1.txt"),
             (b"20 70\n40 71\n80 69\n", "q2.txt")]
        )
        workspace.resolve_dirty_state("keep_draft")
        # Commit only the first released row — the second stays released.
        controller.set_batch_item_assignment(
            staged[0].item_id,
            MeasurementAssignment(measurement_entity_id="point-mlp"),
        )
        controller.commit_batch([staged[0].item_id])
        assert workspace.dirty_state() == "clean"
    finally:
        _close(app, workspace)


def test_discard_selected_batch_items_button(tmp_path: Path) -> None:
    app = _app()
    controller, workspace = _workspace(tmp_path)
    try:
        controller.stage_rew_text_files(
            [(b"20 70\n40 71\n80 69\n", "q.txt")]
        )
        workspace.refresh()
        assert workspace.batch_table.rowCount() == 1
        workspace.batch_table.selectRow(0)
        workspace._discard_selected_batch_items()
        assert controller.batch_items() == ()
        assert "取り除き" in workspace.notice.text()
        # Nothing selected -> honest warning, no removal.
        controller.stage_rew_text_files(
            [(b"20 70\n40 71\n80 69\n", "q2.txt")]
        )
        workspace.refresh()
        workspace.batch_table.clearSelection()
        workspace._discard_selected_batch_items()
        assert len(controller.batch_items()) == 1
    finally:
        _close(app, workspace)
