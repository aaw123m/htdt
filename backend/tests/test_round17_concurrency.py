"""Round 17 (REV17-CONCUR): threading/concurrency regressions.

Each test encodes an interleaving verified during the round-17 audit:

- worker *launch* paths reached after ``dispose()`` must return quietly —
  ``NativeWorkerPool.start`` on a shut-down pool raises ``RuntimeError``,
  which would escape the slot into the uncaught-exception surface;
- worker *completion* slots invoked post-dispose must be swallowed by the
  ``_disposed`` guard — pool disconnect does not retract an already-queued
  emission (native_worker.py documents this contract);
- the batch commit worker mutates shared ``_BatchEntry`` state, so the
  per-row resolution combo and item selection — GUI write paths onto the
  same entries — must be inert while a commit runs;
- ``AutomaticBackupRunner`` must not surface a queued completion after
  ``shutdown()`` — the closing shell is no longer a live composition.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import htdt.automatic_backup_runner as runner_module
from htdt.automatic_backup_runner import AutomaticBackupRunner
from htdt.joint_optimization_context import JointOptimizationContext
from htdt.joint_optimization_panel import JointOptimizationPanel
from htdt.measurement_page_workspace import MeasurementPageWorkspace
from htdt.optimization_adaptive_controller import AdaptiveControllerMixin
from htdt.optimization_adaptive_extended_controller import (
    AdaptiveExtendedControllerMixin,
)
from htdt.robustness_authoring_panel import RobustnessAuthoringPanel
from htdt.system_expansion_widgets import (
    SystemExpansionOptimizePanel,
    SystemExpansionRoomPanel,
)

from test_cad_joint_optimization import (  # noqa: E402  (shared fixtures)
    DOCUMENT_ID,
    _fixture,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _RecordingControl:
    """setEnabled/setVisible recorder standing in for a real widget."""

    def __init__(self) -> None:
        self.enabled = True
        self.visible = True

    def setEnabled(self, value: bool) -> None:
        self.enabled = value

    def setVisible(self, value: bool) -> None:
        self.visible = value


def test_disposed_start_paths_are_no_ops() -> None:
    """Every pool.start launch path must honour the _disposed contract.

    Reached post-dispose (a queued UI signal or a late click on a widget that
    outlived its owner), ``pool.start`` raises ``RuntimeError('native worker
    pool is shut down')``. Siblings gate inside ``_start_*`` helpers; these
    paths lacked the check entirely.
    """
    stub = SimpleNamespace(_disposed=True)
    JointOptimizationPanel._execute_selected(stub)
    RobustnessAuthoringPanel._run(stub)
    SystemExpansionRoomPanel._create_proposal(stub)
    SystemExpansionOptimizePanel._evaluate(stub)
    AdaptiveControllerMixin.build_selected_adaptive_plan(stub)
    AdaptiveExtendedControllerMixin.build_selected_adaptive_extended_plan(stub)


def test_disposed_adaptive_completions_are_no_ops() -> None:
    """Queued completions delivered after dispose() must not touch UI/repos.

    ``_adaptive_*_build_completed`` were the only completion slots missing
    the ``_disposed`` guard every sibling handler enforces.
    """
    stub = SimpleNamespace(_disposed=True)
    AdaptiveControllerMixin._adaptive_build_completed(
        stub, 'adaptive:build', object(), None
    )
    AdaptiveExtendedControllerMixin._adaptive_extended_build_completed(
        stub, 'adaptive_extended:build', object(), None
    )


def test_execute_after_dispose_does_not_raise(tmp_path: Path) -> None:
    """End-to-end: _execute_selected post-dispose must not reach pool.start."""
    _app()
    fixture = _fixture(tmp_path)
    context = JointOptimizationContext(
        fixture.scene_repository,
        DOCUMENT_ID,
        objective_repository=fixture.objective_repository,
    )
    panel = JointOptimizationPanel(context)
    baseline = panel.context.resolve_baseline()
    panel.context.create_spec(
        baseline=baseline,
        mode='placement_only',
        dsp_variables=(),
        candidate_budget=8,
    )
    panel.refresh()
    panel.spec_tree.topLevelItem(0).setSelected(True)

    panel.dispose()
    assert panel._disposed

    # A late execution request must be swallowed, not raise RuntimeError
    # out of the slot into the uncaught-exception surface.
    panel._execute_selected()
    assert not panel.is_running()


def test_batch_table_inert_while_committing() -> None:
    """commit_batch mutates shared _BatchEntry fields on the worker thread.

    The per-row resolution combo (``_batch_resolution_changed`` →
    ``set_batch_resolution`` → ``entry.resolution = ...``) and row selection
    are GUI-side writes onto the same entries with no synchronization — the
    table must stay disabled for the commit's duration so a mid-flight flip
    cannot re-key an entry the worker is already processing.
    """
    workspace = SimpleNamespace(
        batch_add_button=_RecordingControl(),
        batch_attach_button=_RecordingControl(),
        batch_clear_button=_RecordingControl(),
        batch_table=_RecordingControl(),
        batch_cancel_button=_RecordingControl(),
        assignment_save_button=_RecordingControl(),
    )
    MeasurementPageWorkspace._set_batch_committing(workspace, True)
    assert workspace.batch_table.enabled is False
    assert workspace.batch_add_button.enabled is False
    assert workspace.batch_attach_button.enabled is False
    assert workspace.batch_clear_button.enabled is False
    assert workspace.batch_cancel_button.enabled is True
    assert workspace.assignment_save_button.enabled is False

    MeasurementPageWorkspace._set_batch_committing(workspace, False)
    assert workspace.batch_table.enabled is True
    assert workspace.batch_add_button.enabled is True
    assert workspace.batch_attach_button.enabled is True
    assert workspace.batch_clear_button.enabled is True


def test_runner_drops_queued_completion_after_shutdown(tmp_path: Path) -> None:
    """A completion queued before shutdown() must not surface on the shell.

    ``shutdown()`` disconnects the pool's completion path, but Qt still
    delivers an emission that was already queued — the runner then emitted
    ``backup_completed`` into a closing composition (Activity Center entry +
    statusbar write on a dying shell).
    """
    _app()
    runner = AutomaticBackupRunner(tmp_path)
    delivered: list[tuple[object, object]] = []
    runner.backup_completed.connect(lambda r, e: delivered.append((r, e)))

    runner.shutdown()
    # Simulate the pool's queued completion landing post-shutdown.
    runner._on_completed('automatic-backup.periodic', ('p', object()), None)
    assert delivered == []
