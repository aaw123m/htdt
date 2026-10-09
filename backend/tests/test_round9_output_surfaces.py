"""Round-9 output & report surface regressions.

Covers the fixes from docs/reviews/round9-report.md:
- atomic export writes (export_io) — no partial or clobbered artifacts;
- the web comparison report chart drops malformed points jointly instead
  of mis-pairing x/y or aborting the page;
- the analysis HTML plot renders frequency series on a log axis;
- honest deliverables catalog rows (no phantom formats/deliverables);
- per-comparison CSV/PNG export on the comparison page.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import csv
import io
import zipfile
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from htdt.analysis_export import (
    AnalysisSeries,
    AnalysisSeriesPoint,
    _plot_svg,
    build_analysis_export,
    comparison_side_series,
    render_analysis_html,
)
from htdt.cad_measurement_models import (
    CadMeasurementComparison,
    build_measurement_comparison,
)
from htdt.comparison import ComparisonResult
from htdt.cad_measurement_quality_repository import CadMeasurementQualityRepository
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
from htdt.deliverables_catalog import DeliverablesCatalogService
from htdt.export_io import (
    write_bytes_atomic,
    write_export_generation,
    write_text_atomic,
)
from htdt.measurement_page_workspace import MeasurementPageWorkspace
from htdt.measurement_workflow import (
    MeasurementAssignment,
    MeasurementWorkflowController,
)
from htdt.report import _svg_chart
from htdt.support_diagnostics import DiagnosticPackageBuilder

PIN = 'ab' * 32


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _controller(scene_repository, document_id):
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(measurement_repository)
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


def _commit(controller, raw: bytes, evidence_type: str = "measured"):
    controller.stage_rew_text(raw, "import.txt")
    return controller.commit_pending(_assignment(evidence_type=evidence_type))


def _fake_comparison() -> CadMeasurementComparison:
    return build_measurement_comparison(
        comparison_id='cmp-1',
        document_id='doc-1',
        dataset_a_id='ds-a',
        dataset_b_id='ds-b',
        dataset_a_sha256=PIN,
        dataset_b_sha256=PIN,
        scene_revision_a_id='rev-a',
        scene_revision_b_id='rev-b',
        created_at='2026-09-27T00:00:00+00:00',
        result=ComparisonResult(
            requested_band_hz=(20.0, 20000.0),
            actual_band_hz=(20.0, 20000.0),
            grid_hz=(20.0, 40.0, 80.0),
            a_db=(70.0, 71.0, 69.0),
            b_db=(60.0, 62.0, 61.0),
            difference_db=(10.0, 9.0, 8.0),
            mean_difference_db=9.0,
            rms_difference_db=9.0,
            level_offset_db=None,
            shape_rms_db=None,
            valid_points=3,
            total_grid_points=3,
        ),
        label_a='実測 A',
        label_b='予測 B',
        level_compatibility='absolute_level_comparable',
    )


def _series(series_id: str, xs, ys, *, x_label='Frequency') -> AnalysisSeries:
    return AnalysisSeries(
        series_id=series_id,
        label=series_id,
        value_class='raw',
        x_label=x_label,
        y_label='Level',
        unit='db',
        points=tuple(
            AnalysisSeriesPoint(x=x, y=y) for x, y in zip(xs, ys)
        ),
        source_kind='measurement',
        source_id=series_id,
        source_sha256=PIN,
    )


def _bundle(series) -> object:
    return build_analysis_export(
        document_id='doc-1',
        title='t',
        generated_at_utc='2026-09-27T00:00:00+00:00',
        series=tuple(series),
    )


# ---------------------------------------------------------------------
# export_io: atomic writes + collision-safe stems


def test_atomic_writers_leave_no_tmp_and_preserve_bytes(tmp_path) -> None:
    text_path = write_text_atomic(tmp_path / 'out.csv', 'héllo\n')
    assert text_path.read_text(encoding='utf-8') == 'héllo\n'
    blob_path = write_bytes_atomic(tmp_path / 'out.bin', b'\x00\xff' * 9)
    assert blob_path.read_bytes() == b'\x00\xff' * 9
    leftovers = [p.name for p in tmp_path.iterdir() if '.tmp' in p.name]
    assert leftovers == []


def test_export_generation_skips_occupied_stems(tmp_path) -> None:
    first = write_export_generation(
        tmp_path, 'analysis', {'export.csv': 'a'}
    )
    assert first.stem == 'analysis'
    # A legacy flat member still bumps the stem — no overwrites.
    second = write_export_generation(
        tmp_path, 'analysis', {'export.csv': 'b'}
    )
    assert second.stem == 'analysis-2'
    assert (tmp_path / 'analysis' / 'export.csv').read_text(
        encoding='utf-8'
    ) == 'a'
    assert (tmp_path / 'analysis-2' / 'export.csv').read_text(
        encoding='utf-8'
    ) == 'b'


def test_export_generation_member_bom_and_bytes(tmp_path) -> None:
    generation = write_export_generation(
        tmp_path / 'sub',
        'calibration',
        {'settings.csv': 'x,y\n1,2\n', 'blob.bin': b'\x00\xff'},
        bom_suffixes=('.csv',),
    )
    assert generation.stem == 'calibration'
    assert (tmp_path / 'sub' / 'calibration' / 'settings.csv').read_bytes().startswith(
        b'\xef\xbb\xbf'
    )
    assert (tmp_path / 'sub' / 'calibration' / 'blob.bin').read_bytes() == b'\x00\xff'


# ---------------------------------------------------------------------
# Web comparison report chart: joint filtering of malformed points


def test_svg_chart_drops_nonpositive_frequency_jointly() -> None:
    result = {
        'grid_hz': [20.0, 0.0, 80.0],
        'a_db': [70.0, 999.0, 69.0],
        'b_db': [60.0, 999.0, 61.0],
    }
    svg = _svg_chart(result)
    assert svg.startswith('<svg')
    polyline = svg.split('<polyline class="a" points="')[1].split('"')[0]
    # Two surviving points — the zero-frequency row was removed with its
    # levels, not silently re-paired into a wrong position.
    assert len(polyline.split()) == 2


def test_svg_chart_drops_nan_jointly_and_insufficient_rows() -> None:
    result = {
        'grid_hz': [20.0, 40.0, 80.0, 160.0],
        'a_db': [70.0, float('nan'), 69.0, 72.0],
        'b_db': [60.0, 61.0, float('inf'), 63.0],
    }
    svg = _svg_chart(result)
    polyline = svg.split('<polyline class="a" points="')[1].split('"')[0]
    assert len(polyline.split()) == 2  # the 20 Hz and 160 Hz rows survive
    assert 'nan' not in svg.lower()

    too_few = _svg_chart(
        {
            'grid_hz': [20.0, -5.0],
            'a_db': [70.0, 71.0],
            'b_db': [60.0, 61.0],
        }
    )
    assert too_few.startswith('<p')


# ---------------------------------------------------------------------
# Analysis HTML plot: log-frequency axis, per-point finite filtering


def test_analysis_html_plots_frequency_on_log_axis() -> None:
    xs = [20.0, 100.0, 1000.0, 10000.0, 20000.0]
    ys = [70.0, 72.0, 71.0, 73.0, 72.5]
    html_text = render_analysis_html(_bundle((_series('s1', xs, ys),)))
    assert 'Frequency (log)' in html_text
    # Octave tick labels land in the chart, e.g. 20000 Hz.
    assert '>20000<' in html_text


def test_analysis_html_keeps_nonfrequency_axes_linear() -> None:
    series = _series(
        's-time', [0.0, 1.0, 2.0], [1.0, 2.0, 1.5], x_label='Time'
    )
    html_text = render_analysis_html(_bundle((series,)))
    assert '(log)' not in html_text


def test_plot_svg_filters_nonfinite_points_per_point() -> None:
    # The bundle hash rejects NaN before render, but _plot_svg also
    # filters per point — a trace must never emit a 'nan' coordinate.
    xs = [20.0, 40.0, float('nan'), 80.0]
    ys = [70.0, 71.0, 69.0, float('nan')]
    svg = _plot_svg((_series('s1', xs, ys),), 'db')
    assert 'nan' not in svg.lower()
    polyline = svg.split('<polyline points="')[1].split('"')[0]
    assert len(polyline.split()) == 2


# ---------------------------------------------------------------------
# comparison_side_series + per-comparison export


def test_comparison_side_series_pins_authority_and_label() -> None:
    comparison = _fake_comparison()
    side = comparison_side_series(
        comparison, 'a', current_scene_revision_id='rev-a'
    )
    assert side.series_id == 'comparison:cmp-1:a'
    assert side.label == 'A: 実測 A'
    assert side.unit == 'db'
    assert side.value_class == 'derived'
    assert side.operation == 'measurement_comparison'
    assert side.operation_version == comparison.algorithm_version
    assert [p.y for p in side.points] == [70.0, 71.0, 69.0]
    assert side.historical is False

    stale = comparison_side_series(
        comparison, 'b', current_scene_revision_id='rev-later'
    )
    assert stale.historical is True
    assert stale.label == 'B: 予測 B'


def test_export_saved_comparison_writes_analysis_csv(
    tmp_path, monkeypatch
) -> None:
    app = _app()
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    controller, measurement_repository, _ = _controller(
        scene_repository, revision.document_id
    )
    workspace = MeasurementPageWorkspace(controller)
    try:
        measured = _commit(controller, b"20 70\n40 71\n80 69\n")
        predicted = _commit(
            controller, b"20 60\n40 62\n80 61\n", evidence_type="predicted"
        )
        workspace.refresh()
        views = {v.measurement_id: v for v in controller.measurement_views()}
        saved = controller.compare_datasets(
            views[measured.measurement_id].dataset_id,
            views[predicted.measurement_id].dataset_id,
            low_hz=20.0,
            high_hz=80.0,
        )
        workspace._last_comparison = saved

        target = tmp_path / 'comparison.csv'
        monkeypatch.setattr(
            'htdt.measurement_page_workspace.file_dialog_memory'
            '.get_save_file_name',
            lambda *args, **kwargs: (str(target), 'CSV (*.csv)'),
        )
        workspace._export_saved_comparison()

        text = target.read_text(encoding='utf-8-sig')
        rows = list(csv.reader(io.StringIO(text)))
        series_ids = {row[0] for row in rows if row and row[0].startswith('comparison:')}
        prefix = f'comparison:{saved.comparison_id}'
        assert f'{prefix}:a' in series_ids
        assert f'{prefix}:b' in series_ids
        assert prefix in series_ids  # the difference series
        meta_keys = {
            row[1] for row in rows if len(row) >= 2 and row[0] == 'metadata'
        }
        assert f'comparison.{saved.comparison_id}.rms_difference_db' in meta_keys
        assert '比較をCSVで書き出しました' in workspace.notice.text()
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_comparison_export_buttons_track_saved_state(
    tmp_path, monkeypatch
) -> None:
    app = _app()
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    controller, _, _ = _controller(scene_repository, revision.document_id)
    workspace = MeasurementPageWorkspace(controller)
    try:
        _commit(controller, b"20 70\n40 71\n80 69\n")
        _commit(
            controller, b"20 60\n40 62\n80 61\n", evidence_type="predicted"
        )
        workspace.refresh()
        assert not workspace.comparison_export_csv_button.isEnabled()
        workspace._show_comparison(_fake_comparison())
        assert workspace.comparison_export_csv_button.isEnabled()
        assert workspace.comparison_export_png_button.isEnabled()
        workspace._comparison_selection_changed()
        assert not workspace.comparison_export_csv_button.isEnabled()
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


# ---------------------------------------------------------------------
# Deliverables catalog honesty


def _catalog(repository, document_id, *, blockers=(), warnings=()):
    service = DeliverablesCatalogService(
        repository,
        document_id,
        overview_service=SimpleNamespace(
            read=lambda _doc: SimpleNamespace(
                blockers=blockers, warnings=warnings
            )
        ),
    )
    return service, {entry.deliverable_id: entry for entry in service.catalog()}


def test_catalog_rows_only_promise_real_formats(tmp_path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    _, entries = _catalog(repository, F1_DOCUMENT_ID)

    assert entries['installation.handoff'].expected_formats == (
        'csv',
        'html',
        'json',
    )
    assert entries['equipment.capture_catalog'].expected_formats == (
        'json',
    )
    # Rows without a standalone generator name the exact handoff member
    # file they ship as.
    assert (
        'dimension_sheets.csv'
        in (entries['installation.drawing_set'].reason or '')
    )
    assert (
        'installation_coordinates.csv'
        in (entries['installation.bom'].reason or '')
    )
    assert (
        'installation_coordinates.csv'
        in (entries['field.labels'].reason or '')
    )


def test_commissioning_report_is_blocked_not_phantom_when_planned(
    tmp_path, monkeypatch
) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    service, _ = _catalog(repository, F1_DOCUMENT_ID)
    monkeypatch.setattr(service, '_commissioning_plan_count', lambda: 1)
    entries = {e.deliverable_id: e for e in service.catalog()}
    report = entries['commissioning.report']
    assert report.availability == 'blocked'
    assert report.expected_formats == ()
    assert '実装されていません' in (report.reason or '')


def test_commissioning_report_not_applicable_without_plans(tmp_path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    _, entries = _catalog(repository, 'doc-x')
    report = entries['commissioning.report']
    assert report.availability == 'not_applicable'
    assert report.expected_formats == ()


# ---------------------------------------------------------------------
# Diagnostics package: staged ZIP, no truncated artifact on failure


def test_diagnostics_build_promotes_staged_zip(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    builder = DiagnosticPackageBuilder(data_dir)
    plan = builder.plan()
    destination = tmp_path / 'diag.zip'

    result = builder.build(destination, plan)
    assert destination.exists()
    with zipfile.ZipFile(destination) as archive:
        assert 'manifest.json' in archive.namelist()
    assert result.path == destination

    # A mid-build failure leaves neither the chosen path nor the temp.
    class _Boom(Exception):
        pass

    def _exploding_zip(*args, **kwargs):
        raise _Boom('disk died')

    second = tmp_path / 'diag-2.zip'
    monkeypatch.setattr(zipfile, 'ZipFile', _exploding_zip)
    with pytest.raises(_Boom):
        builder.build(second, plan)
    assert not second.exists()
    assert [p.name for p in tmp_path.iterdir() if p.suffix == '.tmp'] == []
