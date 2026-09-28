"""Round 12: adversarial / out-of-order user journeys.

- The 800 ms launch-intent pump must defer dispatch while a modal dialog is
  open: a forwarded intent that switches the project would dispose mounted
  workspaces underneath the dialog (and its pending accept).
- ``WorkspaceRouter.navigate`` must refuse reentrant navigation while its own
  dirty-state resolution modal is still running.
- The measurement workspace's busy block reason must be operation-generic —
  the shared job pool covers more than REW import (batch commit, analysis).
"""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QLabel

import htdt.native_cad as native_cad
from htdt.workflow_navigation import WorkspaceId
from htdt.workflow_shell import (
    WorkspaceMount,
    WorkspaceRouter,
    build_canonical_workspace_registrations,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _QueuedIntent:
    def __init__(self, kind: str = "activate") -> None:
        self.intent = SimpleNamespace(kind=kind, source="forwarded", path=None)


def test_launch_intent_drain_dispatches_and_completes_each_queued_intent(
    tmp_path, monkeypatch
) -> None:
    _app()
    queued = [_QueuedIntent(), _QueuedIntent()]
    completed: list[tuple[object, bool]] = []
    dispatched: list[object] = []

    monkeypatch.setattr(
        native_cad, "drain_launch_intents", lambda _dir: list(queued)
    )
    monkeypatch.setattr(
        native_cad,
        "complete_queued_intent",
        lambda item, *, succeeded: completed.append((item, succeeded)),
    )

    def dispatch(intent):
        dispatched.append(intent)
        return SimpleNamespace(outcome="activated")

    count = native_cad._drain_queued_launch_intents(tmp_path, dispatch)

    assert count == 2
    assert dispatched == [item.intent for item in queued]
    assert completed == [(queued[0], True), (queued[1], True)]


def test_launch_intent_drain_defers_while_a_modal_dialog_is_open(
    tmp_path, monkeypatch
) -> None:
    app = _app()
    calls = {"drain": 0, "dispatch": 0}

    def drain(_dir):
        calls["drain"] += 1
        return [_QueuedIntent()]

    def dispatch(intent):
        calls["dispatch"] += 1
        return SimpleNamespace(outcome="activated")

    monkeypatch.setattr(native_cad, "drain_launch_intents", drain)
    monkeypatch.setattr(native_cad, "complete_queued_intent", lambda *a, **k: None)

    dialog = QDialog()
    dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
    dialog.open()
    app.processEvents()
    try:
        assert QApplication.activeModalWidget() is dialog

        count = native_cad._drain_queued_launch_intents(tmp_path, dispatch)

        # Nothing was dispatched and the queue file was never consumed —
        # the same intent can be routed on the next drain tick.
        assert count == 0
        assert calls == {"drain": 0, "dispatch": 0}
    finally:
        dialog.close()
        app.processEvents()

    count = native_cad._drain_queued_launch_intents(tmp_path, dispatch)
    assert count == 1
    assert calls == {"drain": 1, "dispatch": 1}


def test_router_navigate_refuses_reentrant_navigation() -> None:
    _app()
    inner: dict[str, object] = {}

    def factory(workspace_id):
        def build() -> WorkspaceMount:
            guard = None
            if workspace_id is WorkspaceId.ROOM:

                def guard():
                    inner["result"] = router.navigate(WorkspaceId.MEASUREMENT)
                    inner["reason"] = router.last_block_reason
                    return True, None

            return WorkspaceMount.from_widget(
                QLabel(workspace_id.value), before_deactivate=guard
            )

        return build

    router = WorkspaceRouter(
        build_canonical_workspace_registrations(
            {workspace_id: factory(workspace_id) for workspace_id in WorkspaceId}
        )
    )
    assert router.navigate(WorkspaceId.OVERVIEW) is not None
    assert router.navigate(WorkspaceId.ROOM) is not None

    # ROOM's deactivation guard reenters navigate — e.g. a queued signal or
    # a forwarded intent delivered inside a nested event loop.
    mount = router.navigate(WorkspaceId.OPTIMIZATION)

    assert inner["result"] is None
    assert inner["reason"]
    assert mount is not None
    assert router.current_workspace_id is WorkspaceId.OPTIMIZATION
    router.deleteLater()
    _app().processEvents()


def test_measurement_busy_reason_is_operation_generic(tmp_path) -> None:
    from htdt.cad_measurement_repository import CadMeasurementRepository
    from htdt.cad_repository import SceneRepository
    from htdt.cad_scene import make_f1_scene
    from htdt.measurement_page_workspace import MeasurementPageWorkspace
    from htdt.measurement_workflow import MeasurementWorkflowController

    _app()
    repository = SceneRepository(tmp_path / "cad.sqlite3")
    revision = repository.save(make_f1_scene(), parent_revision_id=None).revision
    controller = MeasurementWorkflowController(
        repository,
        revision.document_id,
        measurement_repository=CadMeasurementRepository(repository),
    )
    workspace = MeasurementPageWorkspace(controller)

    class _BusyPool:
        active_count = 1

    workspace._job_pool = _BusyPool()

    allowed, reason = workspace.before_deactivate()
    assert allowed is False
    assert reason is not None
    # The pool also runs batch commits and analyses — the reason must not
    # blame REW import for a commit that is still finishing.
    assert "REW" not in reason
    workspace.deleteLater()
