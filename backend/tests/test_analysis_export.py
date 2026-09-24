import json

import pytest

from htdt.analysis_export import (
    AnalysisExportMeta,
    AnalysisSeries,
    AnalysisSeriesPoint,
    build_analysis_export,
    render_analysis_csv,
    render_analysis_html,
    render_analysis_json,
)


def _bundle(**kwargs):
    return build_analysis_export(
        document_id='doc-1',
        title='Seat comparison export',
        series=(
            AnalysisSeries(
                series_id='predicted-seat-1',
                label='Predicted seat 1',
                value_class='predicted',
                x_label='Hz',
                y_label='dB',
                unit='dB',
                points=(
                    AnalysisSeriesPoint(x=40.0, y=91.2),
                    AnalysisSeriesPoint(x=20.0, y=88.5),
                ),
                source_kind='prediction_result',
                source_id='pred-1',
                source_sha256='b' * 64,
            ),
            AnalysisSeries(
                series_id='measured-seat-1',
                label='Measured seat 1',
                value_class='raw',
                points=(
                    AnalysisSeriesPoint(x=20.0, y=87.9),
                    AnalysisSeriesPoint(x=40.0, y=90.4),
                ),
                source_kind='measurement_dataset',
                source_id='meas-1',
                source_sha256='a' * 64,
                historical=True,
            ),
        ),
        metadata=(
            AnalysisExportMeta(key='study_id', value='study-1'),
            AnalysisExportMeta(
                key='smoothing', value='octave-1/6-v1'
            ),
        ),
        generated_at_utc='2026-09-24T00:00:00+00:00',
        **kwargs,
    )


def test_analysis_export_hash_and_deterministic_order() -> None:
    bundle = _bundle()
    assert len(bundle.export_sha256) == 64
    assert [s.series_id for s in bundle.series] == [
        'measured-seat-1',
        'predicted-seat-1',
    ]
    measured = bundle.series[0]
    assert [p.x for p in measured.points] == [20.0, 40.0]
    restored = type(bundle).model_validate_json(bundle.model_dump_json())
    assert restored == bundle


def test_analysis_export_csv_is_deterministic() -> None:
    bundle = _bundle()
    first = render_analysis_csv(bundle)
    second = render_analysis_csv(bundle)
    assert first == second
    assert 'value_class' in first
    assert 'measured-seat-1' in first
    assert 'predicted' in first
    assert bundle.export_sha256 in first


def test_analysis_export_json_is_canonical() -> None:
    bundle = _bundle()
    text = render_analysis_json(bundle)
    payload = json.loads(text)
    assert payload['export_id'] == bundle.export_id
    assert payload['series'][0]['value_class'] == 'raw'
    assert render_analysis_json(bundle) == text


def test_analysis_export_html_embeds_payload() -> None:
    bundle = _bundle()
    markup = render_analysis_html(bundle)
    assert '<svg' in markup
    assert 'application/json' in markup
    assert 'historical' in markup
    assert bundle.export_sha256 in markup
    assert 'Predicted seat 1' in markup


def test_analysis_export_rejects_duplicate_ids() -> None:
    with pytest.raises(ValueError):
        build_analysis_export(
            document_id='doc-1',
            title='dup',
            series=(
                AnalysisSeries(
                    series_id='s',
                    label='a',
                    value_class='raw',
                ),
                AnalysisSeries(
                    series_id='s',
                    label='b',
                    value_class='derived',
                ),
            ),
            generated_at_utc='2026-09-24T00:00:00+00:00',
        )
