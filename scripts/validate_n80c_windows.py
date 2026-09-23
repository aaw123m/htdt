from __future__ import annotations

import argparse
import gc
from hashlib import sha256
from pathlib import Path
import sys
import tempfile

from PySide6.QtWidgets import QApplication, QListWidget, QTreeWidget

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend' / 'src'))

from htdt.cad_measurement_models import CadFrequencyResponseDataset, CadMeasurementRecord
from htdt.cad_objective_models import CadObjectiveInputRef
from htdt.cad_objectives import build_objective_evaluation
from htdt.cad_repository import SceneRepository
from htdt.native_cad import TheaterEditorWindow
from htdt.native_editor import ROLE
from htdt.optimization_objectives import ObjectiveMetric, ObjectiveVector
from htdt.optimization_workspace import OptimizationWorkspaceWindow

from validate_n40_windows import click_action, click_global, foreground, pump, wait_until
from validate_n80_windows import (
    activate_optimization_tab,
    click_optimization_button,
    configure_and_save_search,
    fixture_scene,
    select_last_candidate,
    start_search,
    wait_search_empty,
)

if sys.platform != 'win32':
    raise SystemExit('This acceptance harness requires Windows.')

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(errors='backslashreplace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(errors='backslashreplace')

FIXTURE_ID = 'fixture-n80c-o50'


def _select_tree_item(widget: QTreeWidget, index: int, app: QApplication) -> bool:
    if index < 0 or index >= widget.topLevelItemCount():
        return False
    item = widget.topLevelItem(index)
    widget.scrollToItem(item)
    pump(app, 0.05)
    rect = widget.visualItemRect(item)
    if rect.isEmpty():
        return False
    click_global(widget.viewport().mapToGlobal(rect.center()), app)
    return wait_until(app, lambda: widget.currentItem() is item, 0.8)


def _select_list_item(widget: QListWidget, index: int, app: QApplication) -> bool:
    item = widget.item(index)
    if item is None:
        return False
    widget.scrollToItem(item)
    pump(app, 0.05)
    rect = widget.visualItemRect(item)
    if rect.isEmpty():
        return False
    click_global(widget.viewport().mapToGlobal(rect.center()), app)
    return wait_until(app, lambda: item.isSelected(), 0.8)


def _save_objective_fixtures(window: OptimizationWorkspaceWindow) -> tuple[str, ...]:
    spec = window._selected_search_spec()
    page = window.search_candidate_page
    if spec is None or page is None or len(page.candidates) < 2:
        raise AssertionError('N80c requires at least two generated candidates')
    revision = window.repository.get(spec.scene_revision_id)
    if revision is None:
        raise AssertionError('SearchSpec source revision disappeared')

    candidate_ids: list[str] = []
    response_values = (1.0, 2.0, 3.0)
    movement_values = (3.0, 2.0, 4.0)
    for index, candidate in enumerate(page.candidates[:3]):
        candidate_ids.append(candidate.candidate_id)
        vector = ObjectiveVector(
            candidate_id=candidate.candidate_id,
            metrics=(
                ObjectiveMetric(
                    objective_id='response.shape_rms_db',
                    value=response_values[index],
                    unit='dB',
                ),
                ObjectiveMetric(
                    objective_id='movement.total_m',
                    value=movement_values[index],
                    unit='m',
                ),
            ),
        )
        evaluation = build_objective_evaluation(
            revision,
            spec,
            candidate.candidate_id,
            vector,
            evaluation_spec={
                'algorithm_version': 'objective-vector-1',
                'objectives': ['response.shape_rms_db', 'movement.total_m'],
                'acceptance_fixture': 'n80c-o50',
            },
            input_refs=(
                CadObjectiveInputRef(
                    evidence_class='derived',
                    source_kind='candidate_geometry',
                    source_id=candidate.candidate_id,
                ),
                CadObjectiveInputRef(
                    evidence_class='predicted',
                    source_kind='fixture_prediction',
                    source_id=f'prediction:{candidate.candidate_id}',
                ),
            ),
        )
        window.objective_repository.save_evaluation(evaluation)
    return tuple(candidate_ids)


def _save_measured_fixture(window: OptimizationWorkspaceWindow, revision_id: str) -> str:
    revision = window.repository.get(revision_id)
    if revision is None:
        raise AssertionError('applied revision not found')
    measurement_id = 'n80c-measured-fl'
    raw = b'n80c-o50-measured-frequency-response'
    record = CadMeasurementRecord(
        measurement_id=measurement_id,
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        measurement_entity_id='point-mlp',
        measurement_position=revision.document.entity('point-mlp').position,
        evidence_type='measured',
        channel_role='FL',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
        routing_evidence='manual',
        imported_at='2026-09-18T00:00:00+00:00',
        source_kind='rew_text',
        provenance_json='{"acceptance":"n80c-o50"}',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id='n80c-dataset-fl',
        measurement_id=measurement_id,
        frequency_hz=(20.0, 40.0, 80.0, 160.0),
        level_db=(80.0, 81.0, 79.0, 80.5),
        phase_deg=None,
        phase_status='absent',
        level_reference='fixture',
        smoothing=None,
        processing_json='{}',
        source_sha256=sha256(raw).hexdigest(),
        importer_version='n80c-acceptance-1',
    )
    window.measurement_repository.save(
        record,
        dataset,
        raw_filename='n80c-fixture.txt',
        raw_bytes=raw,
    )
    return measurement_id


def run_gate(app: QApplication, root: Path) -> bool:
    root.mkdir(parents=True, exist_ok=True)
    repository = SceneRepository(root / 'scene.sqlite3')
    repository.save(fixture_scene(FIXTURE_ID), parent_revision_id=None)
    window = TheaterEditorWindow(repository, FIXTURE_ID)
    foreground(window, app)
    try:
        product_ok = isinstance(window, OptimizationWorkspaceWindow)
        print('N80C_PRODUCT_COMPOSITION', product_ok, flush=True)
        if not product_ok:
            return False

        spec_id = configure_and_save_search(window, app, suffix='n80c-o50')
        print('N80C_SEARCHSPEC_CREATED', spec_id is not None, flush=True)
        if spec_id is None:
            return False

        if start_search(window, app) is None:
            print('N80C_CANDIDATE_GENERATION_STARTED', False, flush=True)
            return False
        generated = wait_search_empty(window, app, 2.5) and window.search_candidate_page is not None
        print('N80C_CANDIDATES_GENERATED', generated, flush=True)
        if not generated:
            return False

        candidate_ids = _save_objective_fixtures(window)
        first_refresh = click_optimization_button(window, 'Pareto集合を再計算・保存', app)
        pareto_visible = (
            first_refresh
            and window.pareto_tree is not None
            and wait_until(app, lambda: window.pareto_tree.topLevelItemCount() == len(candidate_ids), 1.0)
        )
        print('N80C_PARETO_VISIBLE', pareto_visible, flush=True)
        if not pareto_visible:
            return False
        provenance_ok = all(
            'predicted:fixture_prediction:' in window.pareto_tree.topLevelItem(index).text(2)
            for index in range(window.pareto_tree.topLevelItemCount())
        )
        print('N80C_PARETO_PROVENANCE_VISIBLE', provenance_ok, flush=True)
        if not provenance_ok:
            return False

        snapshots_before = len(window.objective_repository.list_pareto_sets(spec_id))
        second_refresh = click_optimization_button(window, 'Pareto集合を再計算・保存', app)
        snapshots_after = len(window.objective_repository.list_pareto_sets(spec_id))
        dedup_ok = second_refresh and snapshots_before == 1 and snapshots_after == snapshots_before
        print('N80C_PARETO_SEMANTIC_DEDUP', dedup_ok, snapshots_before, snapshots_after, flush=True)
        if not dedup_ok:
            return False

        selected = select_last_candidate(window, app)
        candidate = window._selected_search_candidate()
        print('N80C_CANDIDATE_SELECTED', selected and candidate is not None, flush=True)
        if not selected or candidate is None:
            return False

        applied = click_optimization_button(window, '候補を適用', app)
        print('N80C_CANDIDATE_APPLIED', applied, flush=True)
        if not applied:
            return False
        click_action(window, window.save_action, app)
        saved_revision = repository.current_head(FIXTURE_ID)
        save_ok = (
            saved_revision is not None
            and not window.working.is_dirty
            and saved_revision.parent_revision_id == window.search_repository.get(spec_id).scene_revision_id
        )
        print('N80C_CANDIDATE_SAVED_REVISION', save_ok, flush=True)
        if not save_ok or saved_revision is None:
            return False

        planned_click = click_optimization_button(
            window,
            '現在の保存版を実測候補として記録',
            app,
        )
        plans = window.measurement_repository.latest_measurement_plans(spec_id)
        planned_ok = planned_click and len(plans) == 1 and plans[0].status == 'planned'
        print('O50_MEASUREMENT_PLAN_CREATED', planned_ok, flush=True)
        if not planned_ok:
            return False

        measurement_id = _save_measured_fixture(window, saved_revision.revision_id)
        window.refresh_measurement_plans()
        if not activate_optimization_tab(window, app):
            return False
        plan_tree = window.measurement_plan_tree
        match_list = window.measurement_match_list
        if plan_tree is None or match_list is None:
            return False
        plan_selected = _select_tree_item(plan_tree, 0, app)
        measurement_visible = plan_selected and wait_until(app, lambda: match_list.count() == 1, 1.0)
        print('O50_EXACT_REVISION_MEASUREMENT_VISIBLE', measurement_visible, flush=True)
        if not measurement_visible:
            return False
        measurement_selected = _select_list_item(match_list, 0, app)
        completed_click = click_optimization_button(
            window,
            '選択したN60実測を候補へ関連付け',
            app,
        )
        completed_plans = window.measurement_repository.latest_measurement_plans(spec_id)
        completed_ok = (
            measurement_selected
            and completed_click
            and len(completed_plans) == 1
            and completed_plans[0].status == 'measured'
            and completed_plans[0].measurement_ids == (measurement_id,)
            and len(window.measurement_repository.list_measurement_plans(spec_id)) == 2
        )
        print('O50_PLANNED_TO_MEASURED_APPEND_ONLY', completed_ok, flush=True)
        if not completed_ok:
            return False

        pareto_count_before_stale = len(window.objective_repository.list_pareto_sets(spec_id))
        stale_clicked = click_optimization_button(window, 'Pareto集合を再計算・保存', app)
        stale_rejected = (
            stale_clicked
            and window.pareto_summary_label is not None
            and 'stale' in window.pareto_summary_label.text()
            and len(window.objective_repository.list_pareto_sets(spec_id)) == pareto_count_before_stale
        )
        print('N80C_STALE_PARETO_REJECTED', stale_rejected, flush=True)
        return stale_rejected
    finally:
        window.close()
        pump(app, 0.70)
        window.deleteLater()
        gc.collect()
        pump(app, 0.08)


def main() -> int:
    parser = argparse.ArgumentParser(description='Run N80c/O50 Windows acceptance.')
    parser.add_argument('--keep-data', type=Path, default=None)
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([sys.argv[0]])

    if args.keep_data is not None:
        passed = run_gate(app, args.keep_data)
    else:
        with tempfile.TemporaryDirectory(prefix='htdt-n80c-', ignore_cleanup_errors=True) as temp:
            passed = run_gate(app, Path(temp))

    print('N80C_O50_WINDOWS_RESULT', 'PASS' if passed else 'FAIL', flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
