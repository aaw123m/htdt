"""REV50-PERFUX regression tests.

Locks in the perf/UX sweep's contracts:

- ``count_*`` repository queries stay equal to the authoritative list
  lengths while skipping payload decode/re-attestation (display counters).
- ``assess_spec_staleness`` accepts a pre-resolved baseline so callers
  fanning out over N specs never re-resolve N times.
- ``latest_*_for_document`` batch reads return the same rows the
  per-entity readers returned, in one query instead of N.
- ``progress_for_run`` returns plan + cell states in a single read path
  that keeps the fail-closed runner checks.
- Runner plan listings stay scoped to their owning document.
- Disabled buttons keep a visible border on the dark theme.
- The video panel's metre fields follow the #496 length display policy
  and tall dialogs scroll instead of overflowing a 768px screen.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_equipment_binding import build_equipment_binding_semantics
from htdt.cad_installation_context import build_installation_context
from htdt.cad_installation_context_repository import CadInstallationContextRepository
from htdt.cad_objectives import build_pareto_set
from htdt.cad_measurement_runner import runner_progress
from htdt.joint_optimization_context import JointOptimizationContext

from test_cad_current_equipment import (  # noqa: E402  (shared fixtures)
    DOCUMENT_ID as EQUIP_DOCUMENT_ID,
    _fixture as _equipment_fixture,
    _provenance,
)
from test_cad_joint_optimization import (  # noqa: E402  (shared fixtures)
    DOCUMENT_ID as JOINT_DOCUMENT_ID,
    _fixture as _joint_fixture,
)
from test_cad_measurement_runner import (  # noqa: E402  (shared fixtures)
    _plan,
    _setup as _runner_setup,
)
from test_cad_objective_repository import (  # noqa: E402  (shared fixtures)
    _evaluation,
    _fixture as _objective_fixture,
)
from test_rev44_install_surfaces import (  # noqa: E402  (shared fixtures)
    _equipment as _install_equipment,
    _provenance as _install_provenance,
    _repositories as _install_repositories,
    _save_equipment as _install_save_equipment,
    _speaker as _install_speaker,
    DOCUMENT_ID as INSTALL_DOCUMENT_ID,
)


# -- count_* metadata reads ---------------------------------------------------


def test_count_evaluations_matches_list_length(tmp_path: Path) -> None:
    _scene_repo, revision, spec, candidates, repository, responses = (
        _objective_fixture(tmp_path)
    )
    assert repository.count_evaluations(spec.search_spec_id) == 0

    evaluations = (
        _evaluation(revision, spec, responses, candidates[0].candidate_id, 1.0, 3.0),
        _evaluation(revision, spec, responses, candidates[1].candidate_id, 2.0, 2.0),
        _evaluation(revision, spec, responses, candidates[2].candidate_id, 3.0, 3.0),
    )
    for evaluation in evaluations:
        repository.save_evaluation(evaluation)

    assert repository.count_evaluations(spec.search_spec_id) == len(
        repository.list_evaluations(spec.search_spec_id)
    )
    assert repository.count_evaluations(spec.search_spec_id) == 3
    # Other specs stay at zero — the counter never scans foreign rows.
    assert repository.count_evaluations('search-spec-other') == 0


def test_count_pareto_sets_matches_list_length(tmp_path: Path) -> None:
    _scene_repo, revision, spec, candidates, repository, responses = (
        _objective_fixture(tmp_path)
    )
    evaluations = (
        _evaluation(revision, spec, responses, candidates[0].candidate_id, 1.0, 3.0),
        _evaluation(revision, spec, responses, candidates[1].candidate_id, 2.0, 2.0),
    )
    for evaluation in evaluations:
        repository.save_evaluation(evaluation)
    pareto_set = build_pareto_set(
        evaluations,
        ('response.rms_difference_db', 'response.shape_rms_db'),
    )
    repository.save_pareto_set(pareto_set)

    assert repository.count_pareto_sets(spec.search_spec_id) == len(
        repository.list_pareto_sets(spec.search_spec_id)
    )
    assert repository.count_pareto_sets(spec.search_spec_id) == 1


def test_campaign_and_validation_counts_on_empty_spec(tmp_path: Path) -> None:
    """Metadata counters agree with the list readers on an empty spec."""
    from htdt.cad_measurement_repository import CadMeasurementRepository
    from htdt.cad_model_validation_repository import CadModelValidationRepository
    from htdt.cad_roomsim_repository import CadRoomSimRepository
    from htdt.cad_validation_campaign_repository import CadValidationCampaignRepository

    scene_repo, _revision, spec, _candidates, _repository, _responses = (
        _objective_fixture(tmp_path)
    )

    # Rebuild the three repositories the workspace's journey counters use.
    from htdt.cad_search_repository import CadSearchRepository

    search_repo = CadSearchRepository(scene_repo)
    measurement_repo = CadMeasurementRepository(scene_repo)
    campaign_repo = CadValidationCampaignRepository(search_repo, measurement_repo)
    validation_repo = CadModelValidationRepository(
        search_repo,
        CadRoomSimRepository(scene_repo, search_repo),
        measurement_repo,
    )

    assert campaign_repo.count_for_search_spec(spec.search_spec_id) == len(
        campaign_repo.list_for_search_spec(spec.search_spec_id)
    )
    assert campaign_repo.count_for_search_spec(spec.search_spec_id) == 0
    assert validation_repo.count_for_search_spec(spec.search_spec_id) == len(
        validation_repo.inspect_for_search_spec(spec.search_spec_id)
    )
    assert validation_repo.count_for_search_spec(spec.search_spec_id) == 0


# -- shared-baseline staleness ------------------------------------------------


def test_assess_spec_staleness_reuses_supplied_baseline(tmp_path: Path) -> None:
    """N specs cost ONE baseline resolve, not N (the panel's saved-specs loop)."""
    fixture = _joint_fixture(tmp_path)
    context = JointOptimizationContext(
        fixture.scene_repository,
        JOINT_DOCUMENT_ID,
        objective_repository=fixture.objective_repository,
    )

    calls = 0
    original = context.resolve_baseline

    def counting() -> object:
        nonlocal calls
        calls += 1
        return original()

    context.resolve_baseline = counting  # type: ignore[method-assign]

    baseline = context.resolve_baseline()
    spec = context.create_spec(
        baseline=baseline,
        mode='placement_only',
        dsp_variables=(),
        candidate_budget=8,
    )
    calls_after_setup = calls

    # Supplied baseline: no further resolves however often the caller
    # loops (the panel's saved-specs refresh).
    assert context.assess_spec_staleness(spec.spec_id, baseline=baseline) == ()
    assert context.assess_spec_staleness(spec.spec_id, baseline=baseline) == ()
    assert calls == calls_after_setup

    # Callers that omit the baseline still get lazy single-spec resolution.
    assert context.assess_spec_staleness(spec.spec_id) == ()
    assert calls == calls_after_setup + 1


# -- batch latest-reads --------------------------------------------------------


def test_latest_bindings_for_document_match_per_entity_reads(
    tmp_path: Path,
) -> None:
    (
        _scene_repository,
        _baseline,
        _variant_repository,
        _equipment_repository,
        binding_repository,
        model_a,
        model_b,
    ) = _equipment_fixture(tmp_path)

    for entity_id, definition, binding_id in (
        ('fl', model_a, 'binding-fl-1'),
        ('fr', model_b, 'binding-fr-1'),
    ):
        binding_repository.save_binding(
            build_equipment_binding_semantics(
                binding_id=binding_id,
                document_id=EQUIP_DOCUMENT_ID,
                entity_id=entity_id,
                equipment_definition=definition,
                body_geometry_authority='equipment_nominal',
                acoustic_reference_authority='equipment_derived',
                provenance=(_provenance('f' * 64),),
                created_at_utc='2026-09-21T00:00:00+00:00',
            )
        )

    batch = binding_repository.latest_bindings_for_document(EQUIP_DOCUMENT_ID)
    assert set(batch) == {'fl', 'fr'}
    for entity_id, binding in batch.items():
        per_entity = binding_repository.latest_binding_for_current_entity(
            EQUIP_DOCUMENT_ID, entity_id
        )
        assert per_entity is not None
        assert binding.binding_id == per_entity.binding_id
        assert binding == per_entity
    assert 'mlp' not in batch
    assert binding_repository.latest_bindings_for_document('doc-other') == {}


def test_latest_contexts_for_document_match_per_entity_reads(
    tmp_path: Path,
) -> None:
    scene_repository, _revision, _variants, equipment_repository = (
        _install_repositories(
            tmp_path,
            entities=(
                _install_speaker('speaker-fl'),
                _install_speaker('speaker-fr'),
            ),
        )
    )
    definition = _install_equipment()
    _install_save_equipment(equipment_repository, definition)
    context_repository = CadInstallationContextRepository(
        scene_repository, equipment_repository
    )

    for entity_id, context_id in (
        ('speaker-fl', 'ctx-fl-1'),
        ('speaker-fr', 'ctx-fr-1'),
    ):
        context_repository.save_context(
            build_installation_context(
                context_id=context_id,
                document_id=INSTALL_DOCUMENT_ID,
                entity_id=entity_id,
                equipment_definition=definition,
                selected_mounting_mode='free_standing',
                provenance=(_install_provenance('install', '6'),),
                created_at_utc='2026-10-04T00:00:00+00:00',
            )
        )
    # A second, newer record for one entity must win the latest-slot.
    context_repository.save_context(
        build_installation_context(
            context_id='ctx-fl-2',
            document_id=INSTALL_DOCUMENT_ID,
            entity_id='speaker-fl',
            equipment_definition=definition,
            selected_mounting_mode='free_standing',
            provenance=(_install_provenance('install', '7'),),
            created_at_utc='2026-10-04T00:01:00+00:00',
        )
    )

    batch = context_repository.latest_contexts_for_document(INSTALL_DOCUMENT_ID)
    assert set(batch) == {'speaker-fl', 'speaker-fr'}
    assert batch['speaker-fl'].context_id == 'ctx-fl-2'
    for entity_id, context in batch.items():
        chain = context_repository.list_contexts_for_entity(
            INSTALL_DOCUMENT_ID, entity_id
        )
        assert context.context_id == chain[-1].context_id
        assert context == context_repository.get_context_for_entity(
            INSTALL_DOCUMENT_ID, entity_id
        )
    assert context_repository.latest_contexts_for_document('doc-other') == {}


# -- runner single-round-trip progress ----------------------------------------


def test_progress_for_run_matches_separate_reads(tmp_path: Path) -> None:
    _scene_repo, revision, _measurements, _quality, runner = _runner_setup(tmp_path)
    plan = _plan(revision)
    runner.save_plan(plan)
    run = runner.start_run(plan.plan_id, started_at='2026-09-23T00:00:00+00:00')

    loaded_plan, states = runner.progress_for_run(run.run_id)
    assert loaded_plan == runner.get_plan(plan.plan_id)
    assert states == runner.cell_states(run.run_id)

    # The aggregate the page renders is identical to the classic path.
    progress = runner_progress(loaded_plan, states)
    assert progress.total == len(plan.cells)
    assert progress.not_started == len(plan.cells)


def test_runner_plan_lists_stay_document_scoped(tmp_path: Path) -> None:
    scene_repository, revision, _measurements, _quality, runner = _runner_setup(
        tmp_path
    )
    plan = _plan(revision)
    runner.save_plan(plan)
    other_revision = scene_repository.save(
        scene_repository.get(revision.revision_id)
        .document.model_copy(update={'document_id': 'doc-runner-other'}),
        parent_revision_id=None,
    ).revision
    other_plan = _plan(other_revision)
    runner.save_plan(other_plan)

    assert runner.list_plans('doc-runner') == (plan,)
    assert runner.list_plans('doc-runner-other') == (other_plan,)
    assert set(runner.list_plan_created_at_utc('doc-runner')) == {plan.plan_id}
    assert set(runner.list_plan_created_at_utc('doc-runner-other')) == {
        other_plan.plan_id
    }


# -- dark-theme disabled border ------------------------------------------------


def test_disabled_buttons_keep_visible_border() -> None:
    from htdt.ui_theme import build_dark_stylesheet

    stylesheet = build_dark_stylesheet()
    assert 'QPushButton:disabled' in stylesheet
    disabled = stylesheet.split('QPushButton:disabled', 1)[1]
    block = disabled.split('}', 1)[0]
    assert 'border-color' in block
    assert 'border-color: transparent' not in block


# -- video panel #496 policy + dialog scrolling --------------------------------


def _app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_video_panel_length_policy_propagates(tmp_path: Path) -> None:
    _app()
    from htdt.cad_display_units import display_length_policy
    from htdt.room_video_panel import RoomVideoPanel

    panel = RoomVideoPanel()
    panel.set_length_policy(display_length_policy('mm', decimals=0))
    assert panel.screen_width.suffix().strip() == 'mm'
    assert panel.sightline_clearance.suffix().strip() == 'mm'

    # SI metres stay authoritative: round-trip through the display unit.
    panel.screen_width.set_value_m(2.4)
    assert panel.screen_width.value() == pytest.approx(2400.0)
    assert panel.current_screen_values()['visible_width_m'] == pytest.approx(2.4)

    # A second policy change re-renders every metre field.
    panel.set_length_policy(display_length_policy('cm', decimals=1))
    assert panel.display_width.suffix().strip() == 'cm'


def test_display_spec_dialog_inherits_length_policy() -> None:
    _app()
    from htdt.cad_display_units import display_length_policy
    from htdt.room_video_panel import DisplaySpecDialog

    dialog = DisplaySpecDialog(
        None, length_policy=display_length_policy('mm', decimals=0)
    )
    assert dialog.chassis_width.suffix().strip() == 'mm'
    dialog.chassis_width.set_value_m(1.23)
    assert dialog.values()['chassis_width_m'] == pytest.approx(1.23)


def test_tall_dialogs_scroll_instead_of_overflowing() -> None:
    _app()
    from PySide6.QtWidgets import QScrollArea
    from htdt.room_video_panel import ProjectorSpecDialog

    dialog = ProjectorSpecDialog()
    assert dialog.findChild(QScrollArea) is not None
    # 738px of form content must fit a 768px screen including window chrome.
    assert dialog.sizeHint().height() < 700
