from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDockWidget, QLabel, QWidget

from htdt.cad_measurement_quality import (
    CadMicrophoneCapture,
    build_acquisition_context,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Direction3, F1_DOCUMENT_ID, make_f1_scene
from htdt.cad_search_models import CadCandidate, CadCandidateSetPage
from htdt.measurement_instrument_onboarding import evaluate_instrument_onboarding
from htdt.measurement_page_workspace import MeasurementPageWorkspace
from htdt.native_editor import ROLE
from htdt.measurement_workflow import MeasurementWorkflowController
from htdt.optimization_workflow_workspace import (
    OptimizationWorkflowWorkspace,
)
from htdt.overview_workspace import OverviewWorkspace
from htdt.workflow_navigation import WorkspaceDeepLink, WorkspaceId
from htdt.workflow_shell import WorkspaceMount, WorkflowShellWindow, build_canonical_workspace_registrations


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _measurement_workspace(tmp_path) -> MeasurementPageWorkspace:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    controller = MeasurementWorkflowController(repository, F1_DOCUMENT_ID)
    return MeasurementPageWorkspace(controller)


class _FakePlotter:
    def add_mesh(self, *_args, **_kwargs):
        return object()

    def remove_actor(self, *_args, **_kwargs) -> None:
        pass

    def add_text(self, *_args, **_kwargs) -> None:
        pass

    def render(self) -> None:
        pass


class FakeOptimizationViewport(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.plotter = _FakePlotter()

    def render_document(self, *_args, **_kwargs) -> None:
        pass


def _optimization_workspace(tmp_path) -> OptimizationWorkflowWorkspace:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    return OptimizationWorkflowWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: FakeOptimizationViewport(parent),
    )


def _umik_context(
    *,
    profile: str = "90deg",
    direction: Direction3 | None = Direction3(x=0.0, y=0.0, z=1.0),
    serial: str | None = "SN-001",
    cal_filename: str | None = "umik1_90deg.txt",
    sample_rate: int | None = 48000,
):
    mic = CadMicrophoneCapture(
        manufacturer="miniDSP",
        model="UMIK-1",
        serial=serial,
        sample_rate_hz=sample_rate,
        calibration_profile=profile,
        calibration_filename=cal_filename,
        direction=direction,
    )
    return build_acquisition_context(
        source_kind="native",
        subject_measurement_ids=("meas-1",),
        acquisition_context_id="acq-1",
        created_at_utc="2026-09-26T00:00:00+00:00",
        microphone=mic,
    )


# ---------------------------------------------------------------------------
# #1061 — measurement instrument onboarding / calibration page


def _steps_by_key(steps):
    return {step.key: step for step in steps}


def test_onboarding_no_context_requests_registration() -> None:
    steps = _steps_by_key(
        evaluate_instrument_onboarding(
            context=None, level_calibrations=(), plan_count=0
        )
    )
    assert steps["microphone"].status == "action"
    assert steps["microphone"].link == "assignment"
    assert steps["calibration_file"].status == "action"
    assert steps["orientation"].status == "action"
    assert steps["spl_readiness"].status == "manual"
    assert steps["rew_campaign"].status == "action"
    assert steps["rew_campaign"].link == "campaign"


def test_onboarding_ready_umik90_context() -> None:
    steps = _steps_by_key(
        evaluate_instrument_onboarding(
            context=_umik_context(), level_calibrations=(), plan_count=1
        )
    )
    assert steps["microphone"].status == "ready"
    assert steps["calibration_file"].status == "ready"
    assert steps["orientation"].status == "ready"
    assert steps["sample_rate"].status == "ready"
    assert steps["spl_readiness"].status == "manual"  # UMIK-1 is relative-only
    assert steps["rew_campaign"].status == "ready"


def test_onboarding_flags_profile_file_mismatch_and_missing_serial() -> None:
    context = _umik_context(
        serial=None, cal_filename="umik1.txt"  # missing _90deg suffix
    )
    steps = _steps_by_key(
        evaluate_instrument_onboarding(
            context=context, level_calibrations=(), plan_count=0
        )
    )
    assert steps["microphone"].status == "manual"
    assert steps["calibration_file"].status == "action"
    assert "_90deg" in steps["calibration_file"].detail


def test_onboarding_flags_orientation_mismatch_and_wrong_rate() -> None:
    context = _umik_context(
        direction=Direction3(x=0.0, y=-1.0, z=0.0),  # 0deg aim on a 90deg profile
        sample_rate=44100,
    )
    steps = _steps_by_key(
        evaluate_instrument_onboarding(
            context=context, level_calibrations=(), plan_count=0
        )
    )
    assert steps["orientation"].status == "action"
    assert steps["sample_rate"].status == "action"
    assert "48" in steps["sample_rate"].detail


def test_onboarding_zero_deg_requires_non_90deg_file() -> None:
    context = _umik_context(
        profile="0deg",
        direction=Direction3(x=0.0, y=-1.0, z=0.0),
        cal_filename="umik1_90deg.txt",
    )
    steps = _steps_by_key(
        evaluate_instrument_onboarding(
            context=context, level_calibrations=(), plan_count=0
        )
    )
    assert steps["calibration_file"].status == "action"
    assert steps["orientation"].status == "ready"


def test_calibration_context_routes_to_onboarding_page(tmp_path) -> None:
    _app()
    workspace = _measurement_workspace(tmp_path)

    workspace.set_context("calibration")
    assert workspace.current_context_id == "calibration"
    assert workspace.onboarding_table.rowCount() == len(workspace._onboarding_steps)
    assert workspace.context_label.text()

    # Double-clicked action rows jump to their linked page.
    item = workspace.onboarding_table.item(0, 0)
    workspace._onboarding_step_activated(item)
    assert workspace.current_context_id == "assignment"

    workspace.set_context("calibration")
    workspace.set_context("comparison")
    workspace.close()
    workspace.deleteLater()


def test_measurement_canonical_contexts_all_route(tmp_path) -> None:
    _app()
    workspace = _measurement_workspace(tmp_path)
    from htdt.workflow_navigation import CANONICAL_WORKSPACE_CONTEXTS

    for context in CANONICAL_WORKSPACE_CONTEXTS[WorkspaceId.MEASUREMENT]:
        workspace.set_context(context.context_id)
        assert workspace.current_context_id == context.context_id

    workspace.close()
    workspace.deleteLater()


# ---------------------------------------------------------------------------
# #1086 — overview vertical overflow


def _fake_overview_service(notice_count: int):
    notices = tuple(
        SimpleNamespace(
            area="measurement",
            severity="blocker" if index % 2 == 0 else "warning",
            state_label="要対応",
            message=f"notice-{index} " + "x" * 200,
            action=None,
        )
        for index in range(notice_count)
    )
    view = SimpleNamespace(
        summary="テスト概要",
        blockers=notices[: notice_count // 2],
        warnings=notices[notice_count // 2 :],
        next_action=SimpleNamespace(
            label="次へ", action_id="next", target=WorkspaceDeepLink(WorkspaceId.ROOM)
        ),
        variant_states=(),
        optimization_ready=False,
        recent_activity=(),
    )
    return SimpleNamespace(read=lambda _document_id: view)


def test_overview_keeps_next_action_reachable_when_short(tmp_path) -> None:
    app = _app()
    workspace = OverviewWorkspace(
        _fake_overview_service(12), "doc-1", navigate=lambda _link: True
    )
    workspace.resize(640, 200)
    workspace.show()
    app.processEvents()

    assert workspace.cards_scroll is not None
    assert workspace.next_button.isVisible()
    # Button must sit inside the widget's own rect — not pushed off-screen by
    # the card column.
    assert workspace.next_button.y() < workspace.height()
    # The scroll area must be able to overflow vertically.
    assert workspace.cards_scroll.widget().sizeHint().height() > (
        workspace.cards_scroll.viewport().height()
    )

    workspace.close()
    workspace.deleteLater()


# ---------------------------------------------------------------------------
# #1087 — top context bar overflow


def test_context_bar_overflows_into_scroll_area() -> None:
    app = _app()
    registrations = build_canonical_workspace_registrations(
        {
            workspace_id: (lambda wid=workspace_id: WorkspaceMount.from_widget(QLabel(wid.value)))
            for workspace_id in WorkspaceId
        }
    )
    window = WorkflowShellWindow(registrations)
    assert window.navigate(WorkspaceId.OPTIMIZATION)
    window.show()
    app.processEvents()

    bar = window.context_bar
    buttons = tuple(bar._context_buttons.values())
    assert len(buttons) == 6
    # The container keeps its natural (uncompressed) width.
    assert bar._context_container.minimumWidth() > 0

    # Narrow the window until the 6-button context row can no longer fit.
    window.resize(500, 600)
    app.processEvents()
    natural = bar._context_layout.sizeHint().width()
    assert bar._context_scroll.viewport().width() < natural
    assert bar._context_container.width() >= natural
    for index, left in enumerate(buttons):
        for right in buttons[index + 1 :]:
            assert not left.geometry().intersects(right.geometry())

    window.close()
    window.deleteLater()
    app.processEvents()


# ---------------------------------------------------------------------------
# #1088 / #1090 — optimize workspace: candidate triage + setup presets


def _candidate_page(count: int, *, offset: int = 0) -> CadCandidateSetPage:
    candidates = tuple(
        CadCandidate(
            candidate_id=f"cand-{offset + index}",
            raw_index=offset + index,
            feasible_index=offset + index,
            positions={
                "speaker-1": {"x_m": 1.0 + index, "y_m": 2.0, "z_m": 0.5}
            },
        )
        for index in range(count)
    )
    return CadCandidateSetPage(
        search_spec_id="spec-1",
        search_spec_sha256="0" * 64,
        candidate_set_sha256="1" * 64,
        raw_candidate_count=count,
        feasible_candidate_count=offset + count,
        rejected_candidate_count=0,
        duplicate_candidate_count=0,
        rejection_counts={},
        offset=offset,
        limit=250,
        candidates=candidates,
    )


def test_candidate_tree_filters_and_sorts(tmp_path) -> None:
    app = _app()
    workspace = _optimization_workspace(tmp_path)
    controller = workspace.controller

    controller.search_candidate_page = _candidate_page(15)
    controller._refresh_search_candidate_tree()
    tree = controller.search_candidate_tree
    assert tree.topLevelItemCount() == 15

    # Sorting by the 番号 column is numeric, not lexical.
    tree.sortItems(1, Qt.SortOrder.DescendingOrder)
    app.processEvents()
    assert tree.topLevelItem(0).text(1) == "15"
    tree.sortItems(1, Qt.SortOrder.AscendingOrder)
    app.processEvents()
    assert tree.topLevelItem(0).text(1) == "1"

    # Filtering hides rows without changing the stored selection.
    hidden_id = tree.topLevelItem(0).data(0, ROLE)
    controller.search_selected_candidate_id = str(hidden_id)
    controller.search_candidate_filter_field.setText("候補 2")
    app.processEvents()
    visible = [
        tree.topLevelItem(index)
        for index in range(tree.topLevelItemCount())
        if not tree.topLevelItem(index).isHidden()
    ]
    assert visible and all("候補 2" in item.text(0) for item in visible)
    assert controller.search_selected_candidate_id == str(hidden_id)

    controller.search_candidate_filter_field.clear()
    app.processEvents()
    assert all(
        not tree.topLevelItem(index).isHidden()
        for index in range(tree.topLevelItemCount())
    )

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_search_task_completed_preserves_selection_on_page(
    tmp_path, monkeypatch
) -> None:
    _app()
    workspace = _optimization_workspace(tmp_path)
    controller = workspace.controller

    import htdt.optimization_search_controller as search_mod

    monkeypatch.setattr(
        search_mod, "search_spec_current_working", lambda *a, **k: True
    )
    controller._search_task_spec_ids["task-1"] = "spec-1"
    spec_stub = SimpleNamespace(
        search_spec_id="spec-1",
        axes=(SimpleNamespace(entity_id="speaker-1"),),
    )
    controller.search_repository = SimpleNamespace(
        get=lambda _spec_id: spec_stub
    )
    controller.search_selected_spec_id = "spec-1"
    controller.search_selected_candidate_id = "cand-7"

    controller._search_task_completed("task-1", _candidate_page(10), None)

    # The previously selected candidate is still on the page -> kept.
    assert controller.search_selected_candidate_id == "cand-7"
    current = controller.search_candidate_tree.currentItem()
    assert current is not None and current.data(0, ROLE) == "cand-7"

    # When it is not on the new page the selection falls back to the first row.
    controller._search_task_spec_ids["task-2"] = "spec-1"
    controller._search_task_completed(
        "task-2", _candidate_page(3, offset=100), None
    )
    assert controller.search_selected_candidate_id == "cand-100"

    workspace.close()
    workspace.deleteLater()


def test_setup_page_exposes_presets_and_collapsed_authoring(tmp_path) -> None:
    app = _app()
    workspace = _optimization_workspace(tmp_path)
    controller = workspace.controller

    workspace.select_section("setup")
    app.processEvents()

    assert controller.search_preset_combo.count() == len(
        controller.SEARCH_RANGE_PRESETS
    )
    # Low-level authoring widgets exist but live inside collapsed blocks —
    # they are not visible until the user expands the detail toggle.
    assert not controller.search_min_field.isVisible()
    assert not controller.linked_relation_combo.isVisible()

    # Applying a preset authors the preset's axes for the selected entity.
    controller._refresh_search_entities()
    controller.search_preset_combo.setCurrentIndex(0)  # nudge ±0.5 x/y
    controller.apply_search_range_preset()
    tree = controller.search_axis_tree
    assert tree.topLevelItemCount() == 2
    payload = tree.topLevelItem(0).data(0, ROLE)
    assert payload["axis"] in ("x", "y")
    span = payload["max_m"] - payload["min_m"]
    assert 0.0 < span <= 1.0 + 1e-9

    workspace.close()
    workspace.deleteLater()
    app.processEvents()
