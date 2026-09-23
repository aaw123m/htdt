"""Representative project performance budget (#663).

Structural gates only: no fragile wall-clock assertions. The contract under
test is that history browsing uses compact metadata (never deserializing
every payload), the fixture generator produces valid projects through
production save paths, and the benchmark reports measurements against the
frozen interaction-class budget table.
"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from htdt.cad_repository import SceneRepository
from htdt.native_backup import DATABASE_NAME
from htdt.project_performance import (
    OPERATION_BUDGETS,
    GENERATOR_VERSION,
    InteractionClass,
    ProjectClass,
    build_representative_project,
    measure_project_operations,
    perf_timer,
    record_perf_event,
    report_to_dict,
)


def _sabotage_payloads(db_path: Path, document_id: str, keep_newest: int = 2) -> None:
    """Corrupt every revision payload except the newest few.

    A compact metadata index must not even read these columns; a full-decode
    listing must fail loudly on them.
    """
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            "UPDATE scene_revisions SET payload_json='not-json' "
            'WHERE document_id=? AND seq <= ('
            'SELECT MAX(seq) - ? FROM scene_revisions WHERE document_id=?)',
            (document_id, keep_newest, document_id),
        )


def test_fixture_builds_through_production_paths(tmp_path: Path) -> None:
    data_dir = tmp_path / 'data'
    manifest = build_representative_project(data_dir, ProjectClass.P_SMALL)

    assert manifest.generator_version == GENERATOR_VERSION
    assert manifest.revision_count == 10
    assert manifest.measurement_count == 3
    assert manifest.checkpoint_count >= 2
    assert manifest.asset_count == 3
    assert manifest.database_bytes > 0

    repository = SceneRepository(data_dir / DATABASE_NAME)
    head = repository.current_head(manifest.document_id)
    assert head is not None
    # The mature project has real entities and real revision depth.
    assert len(head.document.entities) > 10
    revisions = repository.list_revision_summaries(manifest.document_id)
    assert len(revisions) == 10
    # Checkpoints recorded as labels, not new history.
    assert len(repository.revision_labels(manifest.document_id)) == (
        manifest.checkpoint_count
    )


def test_summaries_never_decode_payloads(tmp_path: Path) -> None:
    data_dir = tmp_path / 'data'
    manifest = build_representative_project(data_dir, ProjectClass.P_SMALL)
    db_path = data_dir / DATABASE_NAME
    document_id = manifest.document_id

    _sabotage_payloads(db_path, document_id)

    repository = SceneRepository(db_path)
    # Compact listing is unaffected: payloads are not selected at all.
    summaries = repository.list_revision_summaries(document_id)
    assert len(summaries) == manifest.revision_count
    assert summaries[0].payload_bytes == len('not-json')
    assert all(s.document_id == document_id for s in summaries)
    # Lineage metadata intact.
    assert summaries[1].parent_revision_id == summaries[0].revision_id

    # The full-decode path fails loudly on the same rows.
    with pytest.raises(ValueError):
        repository.list_revisions(document_id)

    # Opening the project reads only the head payload — still valid.
    assert repository.current_head(document_id) is not None


def test_summaries_match_full_lineage(tmp_path: Path) -> None:
    data_dir = tmp_path / 'data'
    manifest = build_representative_project(data_dir, ProjectClass.P_SMALL)
    repository = SceneRepository(data_dir / DATABASE_NAME)
    document_id = manifest.document_id

    full = repository.list_revisions(document_id)
    compact = repository.list_revision_summaries(document_id)
    assert [s.revision_id for s in compact] == [r.revision_id for r in full]
    assert [s.parent_revision_id for s in compact] == [
        r.parent_revision_id for r in full
    ]
    assert [s.content_hash for s in compact] == [r.content_hash for r in full]


def test_benchmark_measures_declared_operations(tmp_path: Path) -> None:
    data_dir = tmp_path / 'data'
    manifest = build_representative_project(data_dir, ProjectClass.P_SMALL)
    report = measure_project_operations(
        data_dir, manifest.document_id, report_dir=data_dir
    )

    measured = {m.operation for m in report.measurements}
    assert {
        'open_project',
        'history_list',
        'revision_compare',
        'save_revision',
        'create_checkpoint',
        'measurement_enumerate',
        'backup_create',
    } <= measured
    # Every measured operation resolves a frozen budget entry.
    for measurement in report.measurements:
        assert measurement.operation in OPERATION_BUDGETS
        assert isinstance(measurement.interaction_class, InteractionClass)

    payload = report_to_dict(report)
    assert payload['project_class'] == 'p-small'
    assert 'machine' in payload
    json.dumps(payload)  # serializable artifact

    # Benchmark results journaled through the single instrumentation layer.
    journal = data_dir / 'diagnostics' / 'perf-events.jsonl'
    assert journal.is_file()
    lines = journal.read_text(encoding='utf-8').strip().splitlines()
    assert len(lines) == len(report.measurements)
    first = json.loads(lines[0])
    assert first['operation'] == 'open_project'
    assert first['duration_ms'] >= 0


def test_perf_journal_is_bounded_and_optional(tmp_path: Path) -> None:
    data_dir = tmp_path / 'data'
    record_perf_event(
        data_dir, operation='probe', duration_ms=1.5, document_id='d1'
    )
    journal = data_dir / 'diagnostics' / 'perf-events.jsonl'
    events = [
        json.loads(line)
        for line in journal.read_text(encoding='utf-8').splitlines()
    ]
    assert events[0]['operation'] == 'probe'
    with perf_timer(data_dir, 'probe2', document_id='d1'):
        pass
    events = [
        json.loads(line)
        for line in journal.read_text(encoding='utf-8').splitlines()
    ]
    assert [e['operation'] for e in events] == ['probe', 'probe2']


def test_budget_table_covers_measured_surface() -> None:
    # The measured data-path surface stays inside the declared budget table;
    # adding a benchmark operation without a budget fails here.
    for operation in (
        'open_project',
        'history_list',
        'revision_compare',
        'save_revision',
        'create_checkpoint',
        'measurement_enumerate',
        'inbox_summary',
        'backup_create',
    ):
        assert operation in OPERATION_BUDGETS
    # Long operations carry the threading contract instead of a ms cap.
    long_ops = [
        name
        for name, budget in OPERATION_BUDGETS.items()
        if budget.interaction_class is InteractionClass.LONG_OPERATION
    ]
    for name in long_ops:
        assert OPERATION_BUDGETS[name].budget_ms is None
        assert OPERATION_BUDGETS[name].requires_background_progress
