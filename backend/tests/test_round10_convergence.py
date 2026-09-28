"""Round 10 convergence regressions (#4 robustness authoring UI, #5 worker
lanes, #8 search-spec re-author affordance).

Qt panel tests run offscreen per the repo norm.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from htdt.cad_objective_models import CadObjectiveInputRef
from htdt.cad_objectives import build_objective_evaluation
from htdt.cad_search import build_cad_search_spec
from htdt.cad_search_models import CadSearchAxis
from htdt.cad_topology_comparison_execution import TopologyComparisonCancelled
from htdt.joint_optimization_context import JointOptimizationContext
from htdt.joint_optimization_panel import JointOptimizationPanel
from htdt.optimization_objectives import movement_objectives
from htdt.optimization_robustness import RobustnessEvaluationCancelled
from htdt.robustness_authoring_context import RobustnessAuthoringContext
from htdt.robustness_authoring_panel import RobustnessAuthoringPanel
from htdt.search_space import SearchGenerationCancelled

from test_cad_joint_optimization import (  # noqa: E402
    DOCUMENT_ID,
    _fixture,
)


# ----------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------


def _context(fx) -> RobustnessAuthoringContext:
    return RobustnessAuthoringContext(
        scene_repository=fx.scene_repository,
        search_repository=fx.search_repository,
        extended_search_repository=fx.extended_search_repository,
        objective_repository=fx.objective_repository,
        robustness_repository=fx.robustness_repository,
        document_id=DOCUMENT_ID,
    )


def _movement_evaluation(fx):
    """Replace the fixture O30 authority with a ``candidate_movement`` eval."""
    candidate = fx.base_candidate
    baseline = {}
    for entity_id in candidate.positions:
        entity = fx.revision.document.entity(entity_id)
        baseline[entity_id] = {
            "x_m": float(entity.position.x_m),
            "y_m": float(entity.position.y_m),
            "z_m": float(entity.position.z_m),
        }
    evaluation = build_objective_evaluation(
        fx.revision,
        fx.search_spec,
        candidate.candidate_id,
        movement_objectives(
            candidate.candidate_id, baseline, candidate.positions
        ),
        evaluation_spec={
            "algorithm_version": "objective-vector-1",
            "objective_method": "candidate_movement",
            "objectives": ["movement.total_m", "movement.max_m"],
        },
        input_refs=(
            CadObjectiveInputRef(
                evidence_class="derived",
                source_kind="candidate_geometry",
                source_id=candidate.candidate_id,
            ),
        ),
    )
    fx.objective_repository.save_evaluation(evaluation)
    return evaluation


def _axes(context, fx, candidate_id):
    choices = context.axis_choices(fx.search_spec, candidate_id)
    return tuple(choice.to_axis(choice.default_delta) for choice in choices)


# ----------------------------------------------------------------------
# #4 — authoring context
# ----------------------------------------------------------------------


def test_candidate_choices_list_latest_evaluations(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    context = _context(fx)

    choices = context.candidate_choices(fx.search_spec)

    assert [choice.candidate_id for choice in choices] == [
        fx.base_candidate.candidate_id
    ]
    assert "fixture.response_error_db" in choices[0].label


def test_axis_choices_offer_only_perturbable_axes(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    context = _context(fx)

    choices = context.axis_choices(
        fx.search_spec, fx.base_candidate.candidate_id
    )

    # The fixture search spec searches only speaker-fl x: that is the single
    # O90-perturbable axis the authority offers.
    assert [choice.parameter for choice in choices] == ["speaker_x_m"]
    choice = choices[0]
    assert choice.entity_id == "speaker-fl"
    assert choice.unit == "m"
    assert choice.default_delta == pytest.approx(0.05)
    assert choice.nominal_value == pytest.approx(1.0)


def test_create_spec_persists_bound_spec(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    context = _context(fx)
    axes = _axes(context, fx, fx.base_candidate.candidate_id)

    spec = context.create_spec(
        search_spec=fx.search_spec,
        candidate_id=fx.base_candidate.candidate_id,
        axes=axes,
    )

    persisted = fx.robustness_repository.get_spec(spec.robustness_spec_id)
    assert persisted.robustness_spec_sha256 == spec.robustness_spec_sha256
    assert persisted.candidate_id == fx.base_candidate.candidate_id
    assert (
        persisted.nominal_objective_evaluation_id
        == spec.nominal_objective_evaluation_id
    )
    # Honest provenance: fixture O30 spec carries no model_id, so the label
    # names the authority it was derived from — never a fabricated model.
    assert spec.model_id == "o30:fixture-metrics-1"


def test_create_spec_rejects_candidate_outside_search_set(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    context = _context(fx)

    with pytest.raises(ValueError, match="実行可能候補集合"):
        context.create_spec(
            search_spec=fx.search_spec,
            candidate_id="not-a-candidate",
            axes=(),
        )


def test_evaluate_spec_movement_recomputes_perturbed_objectives(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    _movement_evaluation(fx)
    context = _context(fx)
    axes = _axes(context, fx, fx.base_candidate.candidate_id)
    spec = context.create_spec(
        search_spec=fx.search_spec,
        candidate_id=fx.base_candidate.candidate_id,
        axes=axes,
    )

    samples, evaluations = context.evaluate_spec(spec)

    assert len(samples) == 3  # nominal + minus + plus for the single axis
    persisted_samples = fx.robustness_repository.list_samples(
        spec.robustness_spec_id
    )
    persisted_evaluations = fx.robustness_repository.list_evaluations(
        spec.robustness_spec_id
    )
    assert len(persisted_samples) == 3
    # movement.total_m and movement.max_m each aggregate a row.
    assert len(persisted_evaluations) == len(evaluations) == 2
    plus = next(sample for sample in persisted_samples if sample.step == "plus")
    assert plus.feasible
    assert plus.objective_vector is not None
    assert plus.objective_vector.metric("movement.total_m").value == pytest.approx(
        0.05
    )


def test_evaluate_spec_unsupported_authority_fails_closed(
    tmp_path: Path,
) -> None:
    """fixture-metrics-1 cannot recompute on a perturbed doc: every perturbed
    sample must record an objective_evaluation_failed, not a fake value."""
    fx = _fixture(tmp_path)
    context = _context(fx)
    axes = _axes(context, fx, fx.base_candidate.candidate_id)
    spec = context.create_spec(
        search_spec=fx.search_spec,
        candidate_id=fx.base_candidate.candidate_id,
        axes=axes,
    )

    samples, _evaluations = context.evaluate_spec(spec)

    perturbed = [s for s in samples if s.step != "nominal"]
    assert perturbed
    assert all(
        sample.failure_reason is not None
        and sample.failure_reason.startswith(
            "objective_evaluation_failed:unsupported"
        )
        and sample.objective_vector is None
        for sample in perturbed
    )


def test_evaluate_spec_cancel_aborts_without_persisting(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    context = _context(fx)
    axes = _axes(context, fx, fx.base_candidate.candidate_id)
    spec = context.create_spec(
        search_spec=fx.search_spec,
        candidate_id=fx.base_candidate.candidate_id,
        axes=axes,
    )

    with pytest.raises(RobustnessEvaluationCancelled):
        context.evaluate_spec(spec, is_cancelled=lambda: True)

    assert (
        fx.robustness_repository.list_samples(spec.robustness_spec_id) == ()
    )
    assert (
        fx.robustness_repository.list_evaluations(spec.robustness_spec_id)
        == ()
    )
    # The spec itself was saved earlier — a cancelled stencil leaves it
    # re-evaluable rather than half-written.
    assert (
        fx.robustness_repository.get_spec(spec.robustness_spec_id)
        .robustness_spec_sha256
        == spec.robustness_spec_sha256
    )


# ----------------------------------------------------------------------
# #4 — authoring panel (Qt, offscreen)
# ----------------------------------------------------------------------


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_idle(panel, timeout_s: float = 15.0) -> None:
    app = _app()
    deadline = time.monotonic() + timeout_s
    while panel.is_running() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    for _ in range(3):
        app.processEvents()


def test_authoring_panel_run_creates_spec_and_evaluates(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    _movement_evaluation(fx)
    _app()
    statuses: list[str] = []
    panel = RobustnessAuthoringPanel(
        _context(fx),
        selected_spec_id=lambda: fx.search_spec.search_spec_id,
        on_status=statuses.append,
    )

    assert panel.run_button.isEnabled()
    panel.run_button.click()
    _wait_idle(panel)

    assert not panel.is_running()
    specs = fx.robustness_repository.list_specs_for_search(
        document_id=DOCUMENT_ID,
        scene_revision_id=fx.revision.revision_id,
        search_spec_id=fx.search_spec.search_spec_id,
    )
    authored = [s for s in specs if s.robustness_spec_id != fx.robustness_spec.robustness_spec_id]
    assert len(authored) == 1
    assert (
        fx.robustness_repository.list_samples(authored[0].robustness_spec_id)
    )
    assert statuses
    panel.dispose()


def test_joint_panel_gates_create_button_on_missing_o90(
    tmp_path: Path,
) -> None:
    import json

    from htdt.cad_constraint_models import CadConstraintSet

    fx = _fixture(tmp_path)
    constraint_set = CadConstraintSet.model_validate(
        json.loads(fx.search_spec.constraint_snapshot_json)
    )
    spec_without_o90, _estimate = build_cad_search_spec(
        fx.revision,
        constraint_set,
        fx.search_spec.axes,
        candidate_limit=8,
        name="no-o90 spec",
    )
    fx.search_repository.save(spec_without_o90)

    _app()
    context = JointOptimizationContext(
        fx.scene_repository,
        DOCUMENT_ID,
        objective_repository=fx.objective_repository,
    )
    panel = JointOptimizationPanel(context, on_status=lambda _m: None)
    panel.refresh()

    baseline = context.resolve_baseline()
    assert baseline is not None
    assert baseline.robustness_spec is None
    assert not panel.create_button.isEnabled()
    assert "O90" in panel.preflight_label.text()
    panel.dispose()


# ----------------------------------------------------------------------
# #5 — worker-lane cancel plumbing
# ----------------------------------------------------------------------


def test_topology_comparison_cancel_aborts_mid_lane(tmp_path: Path) -> None:
    from test_cad_topology_comparison_execution import (
        _fixture as _topology_fixture,
        _full_authority,
        _persist_authority,
        _policy,
        _proposal,
        _run,
    )

    fx = _topology_fixture(tmp_path)
    source_bytes, definition, dataset = _full_authority()
    _persist_authority(
        fx["equipment_repository"],
        fx["directivity_repository"],
        definition,
        dataset,
        source_bytes,
    )
    proposal = _proposal(fx, definition)

    with pytest.raises(TopologyComparisonCancelled):
        _run(fx, [proposal], is_cancelled=lambda: True)


def test_adaptive_build_cancel_aborts_enumeration(tmp_path: Path) -> None:
    from test_cad_adaptive_planner import _fixture as _adaptive_fixture

    (
        record,
        _page,
        _repository,
        service,
        _candidate_ids,
        _objectives,
        _spec,
        _revision,
    ) = _adaptive_fixture(tmp_path)

    with pytest.raises(SearchGenerationCancelled):
        service.build_and_save(
            validation_id=record.validation_id,
            execution_scope="development_synthetic",
            length_scale_m=0.35,
            proposal_limit=10,
            is_cancelled=lambda: True,
        )


def test_adaptive_build_cancel_aborts_inner_loop(
    tmp_path: Path, monkeypatch
) -> None:
    from test_cad_adaptive_planner import _fixture as _adaptive_fixture
    import htdt.cad_adaptive_service as adaptive_service

    (
        record,
        _page,
        _repository,
        service,
        _candidate_ids,
        _objectives,
        _spec,
        _revision,
    ) = _adaptive_fixture(tmp_path)

    cancel = {"on": False}
    real_build = adaptive_service.build_adaptive_plan

    def build_then_cancel(*args, **kwargs):
        cancel["on"] = True
        return real_build(*args, **kwargs)

    monkeypatch.setattr(
        adaptive_service, "build_adaptive_plan", build_then_cancel
    )
    with pytest.raises(RuntimeError, match="adaptive plan build cancelled"):
        service.build_and_save(
            validation_id=record.validation_id,
            execution_scope="development_synthetic",
            length_scale_m=0.35,
            proposal_limit=10,
            is_cancelled=lambda: cancel["on"],
        )


def test_adaptive_extended_build_cancel_aborts_inner_loop(
    tmp_path: Path, monkeypatch
) -> None:
    from test_cad_adaptive_extended import _seeded
    import htdt.cad_adaptive_extended_service as extended_service
    from htdt.cad_adaptive_extended_service import (
        CadAdaptiveExtendedPlannerService,
    )

    (
        _scene_repository,
        result,
        _search,
        validation,
        extended,
        adaptive_extended,
    ) = _seeded(tmp_path)
    service = CadAdaptiveExtendedPlannerService(
        extended,
        validation,
        adaptive_extended,
    )

    cancel = {"on": False}
    real_build = extended_service.build_adaptive_extended_plan

    def build_then_cancel(*args, **kwargs):
        cancel["on"] = True
        return real_build(*args, **kwargs)

    monkeypatch.setattr(
        extended_service,
        "build_adaptive_extended_plan",
        build_then_cancel,
    )
    with pytest.raises(
        RuntimeError, match="adaptive extended plan build cancelled"
    ):
        service.build_and_save(
            extended_search_id=result.extended_search_id,
            validation_id=result.validation_id,
            execution_scope="development_synthetic",
            is_cancelled=lambda: cancel["on"],
        )


def test_topology_proposal_cancel_aborts_page_loop(tmp_path: Path) -> None:
    from test_system_expansion_workflow import (
        _fixture as _expansion_fixture,
        _save_fixture_equipment,
        _surround_inputs,
        _mirror_pair_rules,
    )

    _scene, _baseline, _repository, _existing, service = _expansion_fixture(
        tmp_path
    )
    equipment = _save_fixture_equipment(service)

    with pytest.raises(RuntimeError, match="cancel"):
        service.create_topology_proposal(
            proposal_name="proposed 5.0.2 cancelled",
            speakers=_surround_inputs(equipment.semantic_sha256),
            linked_rules=_mirror_pair_rules(),
            max_returned_candidates=24,
            is_cancelled=lambda: True,
        )


# ----------------------------------------------------------------------
# #8 — re-author affordance
# ----------------------------------------------------------------------


def test_reauthor_search_spec_recreates_on_current_revision(
    tmp_path: Path,
) -> None:
    from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
    from htdt.optimization_workflow_workspace import (
        OptimizationWorkflowWorkspace,
    )
    from test_optimization_workflow_workspace import FakeOptimizationViewport

    from htdt.cad_repository import SceneRepository

    _app()
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    first = repository.save(make_f1_scene(), parent_revision_id=None)
    workspace = OptimizationWorkflowWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: FakeOptimizationViewport(parent),
    )
    controller = workspace.controller

    spec, _estimate = build_cad_search_spec(
        controller.repository.get(first.revision.revision_id),
        controller.constraint_set,
        (
            CadSearchAxis(
                entity_id="speaker-fl",
                axis="x",
                min_m=1.0,
                max_m=2.0,
                step_m=0.5,
            ),
        ),
        candidate_limit=16,
        name="初期探索",
    )
    controller.search_repository.save(spec)
    controller.search_selected_spec_id = spec.search_spec_id

    # The workspace commits a second revision: the stored spec goes stale.
    moved = make_f1_scene().model_copy(
        update={
            "entities": make_f1_scene().entities[:-1],
        }
    )
    second = repository.save(
        moved, parent_revision_id=first.revision.revision_id
    )
    assert controller.scene.reload_if_clean()
    controller.refresh_from_authorities()

    assert controller.working.source_revision_id == second.revision.revision_id
    controller._refresh_search_specs()
    assert controller.search_reauthor_button.isEnabled()

    controller.reauthor_selected_search_spec()

    specs = controller.search_repository.list_specs(F1_DOCUMENT_ID)
    assert len(specs) == 2
    authored = specs[-1]
    assert authored.search_spec_id != spec.search_spec_id
    assert authored.scene_revision_id == second.revision.revision_id
    assert authored.axes == spec.axes
    assert authored.candidate_limit == spec.candidate_limit
    assert authored.name == spec.name
    assert authored.linked_variables == spec.linked_variables
    assert controller.search_selected_spec_id == authored.search_spec_id

    # Now current: re-author is a no-op, button disabled.
    controller.reauthor_selected_search_spec()
    assert len(controller.search_repository.list_specs(F1_DOCUMENT_ID)) == 2
    controller._refresh_search_specs()
    assert not controller.search_reauthor_button.isEnabled()

    workspace.close()
    workspace.deleteLater()
    _app().processEvents()
