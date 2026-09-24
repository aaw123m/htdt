import json
import re

import pytest

from htdt.analysis_export import (
    AnalysisExportMeta,
    AnalysisSeries,
    AnalysisSeriesPoint,
    build_analysis_export,
    render_analysis_csv,
    render_analysis_html,
    render_analysis_json,
    series_from_comparison,
    series_from_measurement_dataset,
    series_from_prediction,
)
from htdt.cad_document import WorkingDocument
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import normalize_rew_text
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, make_f1_scene
from htdt.comparison import FrequencyResponse, compare_frequency_responses


def _bundle(**kwargs):
    return build_analysis_export(
        document_id='doc-1',
        title=kwargs.pop('title', 'Seat comparison export'),
        series=(
            AnalysisSeries(
                series_id='predicted-seat-1',
                label='Predicted seat 1',
                value_class='predicted',
                x_label='Hz',
                y_label='dB',
                unit='db',
                points=(
                    AnalysisSeriesPoint(x=40.0, y=91.2),
                    AnalysisSeriesPoint(x=20.0, y=88.5),
                ),
                source_kind='prediction',
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
        generated_at_utc=kwargs.pop(
            'generated_at_utc', '2026-09-24T00:00:00+00:00'
        ),
        **kwargs,
    )


def test_analysis_export_hash_and_deterministic_order() -> None:
    bundle = _bundle()
    assert len(bundle.export_sha256) == 64
    assert len(bundle.spec_sha256) == 64
    assert bundle.spec_sha256 != bundle.export_sha256
    assert [s.series_id for s in bundle.series] == [
        'measured-seat-1',
        'predicted-seat-1',
    ]
    measured = bundle.series[0]
    assert [p.x for p in measured.points] == [20.0, 40.0]
    restored = type(bundle).model_validate_json(bundle.model_dump_json())
    assert restored == bundle


def test_spec_identity_excludes_volatile_export_fields() -> None:
    first = _bundle()
    second = _bundle(
        generated_at_utc='2027-01-01T00:00:00+00:00',
        export_id='00000000-0000-0000-0000-000000000001',
    )
    assert first.export_id != second.export_id
    assert first.spec_sha256 == second.spec_sha256
    assert first.export_sha256 != second.export_sha256


def test_analysis_export_csv_is_deterministic() -> None:
    bundle = _bundle()
    first = render_analysis_csv(bundle)
    second = render_analysis_csv(bundle)
    assert first == second
    assert 'value_class' in first
    assert 'measured-seat-1' in first
    assert 'predicted' in first
    assert bundle.spec_sha256 in first


def test_analysis_export_json_is_canonical() -> None:
    bundle = _bundle()
    text = render_analysis_json(bundle)
    payload = json.loads(text)
    assert payload['export_id'] == bundle.export_id
    assert payload['document_id'] == 'doc-1'
    assert payload['series'][0]['value_class'] == 'raw'
    assert render_analysis_json(bundle) == text


def test_analysis_export_html_embeds_payload_exactly() -> None:
    bundle = _bundle(title='A < B & "quotes"')
    markup = render_analysis_html(bundle)
    assert '<svg' in markup
    match = re.search(
        r'<script type="application/json"[^>]*>(.*?)</script>',
        markup,
        re.DOTALL,
    )
    assert match is not None
    embedded = match.group(1)
    # Raw '<' must never appear inside the embedded payload — it is
    # escaped as \u003c so the block cannot break out of the script tag.
    assert '<' not in embedded
    assert '\\u003c' in embedded
    decoded = json.loads(embedded)
    assert decoded == json.loads(render_analysis_json(bundle))
    assert decoded['title'] == 'A < B & "quotes"'
    assert 'historical' in markup
    assert bundle.spec_sha256 in markup


def test_analysis_export_groups_incompatible_units() -> None:
    bundle = build_analysis_export(
        document_id='doc-1',
        title='mixed units',
        series=(
            AnalysisSeries(
                series_id='distance',
                label='distance',
                value_class='raw',
                unit='m',
                points=(AnalysisSeriesPoint(x=0.0, y=3.0),),
            ),
            AnalysisSeries(
                series_id='spl',
                label='spl',
                value_class='raw',
                unit='db',
                points=(AnalysisSeriesPoint(x=20.0, y=80.0),),
            ),
        ),
        generated_at_utc='2026-09-24T00:00:00+00:00',
    )
    markup = render_analysis_html(bundle)
    assert markup.count('<svg') == 2
    assert 'y unit: db' in markup
    assert 'y unit: m' in markup


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


def _measurement_fixture(tmp_path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    repository = CadMeasurementRepository(scene_repository)
    record, dataset, filename, raw = normalize_rew_text(
        revision,
        'point-mlp',
        b'20 70\n40 71\n80 69\n',
        filename='mlp.txt',
        imported_at='2026-09-17T09:30:00+00:00',
    )
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)
    return scene_repository, repository, revision, record, dataset


def test_dataset_series_adapter_derives_provenance_and_history(
    tmp_path,
) -> None:
    scene_repository, _repository, revision, record, dataset = (
        _measurement_fixture(tmp_path)
    )
    current = series_from_measurement_dataset(
        dataset,
        record,
        current_scene_revision_id=revision.revision_id,
    )
    assert current.value_class == 'raw'
    assert current.unit == 'db'
    assert current.source_kind == 'measurement_dataset'
    assert current.source_id == dataset.dataset_id
    assert current.source_sha256 == dataset.dataset_sha256
    assert current.historical is False
    assert [p.x for p in current.points] == list(dataset.frequency_hz)

    working = WorkingDocument(
        revision.document,
        source_revision_id=revision.revision_id,
        saved_content_hash=revision.content_hash,
    )
    working.move_entity('point-mlp', Position3(x_m=3.2, y_m=3.0, z_m=1.1))
    head = scene_repository.save(
        working.committed_document,
        parent_revision_id=revision.revision_id,
    ).revision
    historical = series_from_measurement_dataset(
        dataset,
        record,
        current_scene_revision_id=head.revision_id,
    )
    assert historical.historical is True


def test_comparison_series_adapter_uses_persisted_identity(tmp_path) -> None:
    scene_repository, repository, revision_a, _record_a, dataset_a = (
        _measurement_fixture(tmp_path)
    )
    working = WorkingDocument(
        revision_a.document,
        source_revision_id=revision_a.revision_id,
        saved_content_hash=revision_a.content_hash,
    )
    working.move_entity('speaker-fl', Position3(x_m=1.55, y_m=0.75, z_m=1.05))
    revision_b = scene_repository.save(
        working.committed_document,
        parent_revision_id=revision_a.revision_id,
    ).revision
    record_b, dataset_b, filename_b, raw_b = normalize_rew_text(
        revision_b,
        'point-mlp',
        b'20 69\n40 70\n80 68\n',
        filename='b.txt',
        imported_at='2026-09-17T09:31:00+00:00',
    )
    repository.save(
        record_b, dataset_b, raw_filename=filename_b, raw_bytes=raw_b
    )
    result = compare_frequency_responses(
        FrequencyResponse(dataset_a.frequency_hz, dataset_a.level_db),
        FrequencyResponse(dataset_b.frequency_hz, dataset_b.level_db),
        20.0,
        80.0,
    )
    comparison = repository.save_comparison(
        dataset_a.dataset_id, dataset_b.dataset_id, result
    )
    series = series_from_comparison(comparison)
    assert series.value_class == 'derived'
    assert series.source_sha256 == comparison.comparison_sha256
    assert len(series.points) == len(comparison.difference_db)


def test_prediction_series_adapter_pins_exact_authority() -> None:
    series = series_from_prediction(
        ((20.0, 90.0), (40.0, 91.0)),
        label='predicted seat-1',
        prediction_ref='pred-1',
        prediction_sha256='c' * 64,
        unit='db',
    )
    assert series.value_class == 'predicted'
    assert series.source_kind == 'prediction'
    assert series.source_sha256 == 'c' * 64
