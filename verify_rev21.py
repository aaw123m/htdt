"""REV21-EXPORT verification harness: construct data -> export -> diff fields."""
import csv
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, 'backend/src')

from htdt.analysis_export import (
    AnalysisExportMeta,
    build_analysis_export,
    comparison_metadata_entries,
    render_analysis_csv,
    render_analysis_json,
    series_from_comparison,
    series_from_measurement_dataset,
)
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import normalize_rew_text
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.comparison import FrequencyResponse, compare_frequency_responses

NOW = '2026-09-30T00:00:00+00:00'


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    scene_repo = SceneRepository(tmp / 'cad.sqlite3')
    revision = scene_repo.save(make_f1_scene(), parent_revision_id=None).revision
    repo = CadMeasurementRepository(scene_repo)

    record_a, dataset_a, fn_a, raw_a = normalize_rew_text(
        revision, 'point-mlp', b'20 70\n40 71\n80 69\n',
        filename='a.txt', imported_at=NOW,
    )
    repo.save(record_a, dataset_a, raw_filename=fn_a, raw_bytes=raw_a)
    record_b, dataset_b, fn_b, raw_b = normalize_rew_text(
        revision, 'point-mlp', b'20 69\n40 70\n80 68\n',
        filename='b.txt', imported_at=NOW,
    )
    repo.save(record_b, dataset_b, raw_filename=fn_b, raw_bytes=raw_b)

    result = compare_frequency_responses(
        FrequencyResponse(dataset_a.frequency_hz, dataset_a.level_db),
        FrequencyResponse(dataset_b.frequency_hz, dataset_b.level_db),
        20.0, 80.0,
    )
    comparison = repo.save_comparison(
        dataset_a.dataset_id, dataset_b.dataset_id, result
    )
    print('comparison has a_db/b_db/difference_db lengths:',
          len(comparison.a_db), len(comparison.b_db), len(comparison.difference_db))

    # ---- replicate workflow_application._export_analysis_bundle ----
    records = repo.list_measurements(revision.document.document_id)
    comparisons = tuple(repo.list_comparisons(revision.document.document_id))
    metadata = []
    series = []
    for record in records:
        bundle = repo.get_evidence_bundle(record.measurement_id)
        series.append(series_from_measurement_dataset(
            bundle.dataset, record,
            current_scene_revision_id=revision.revision_id,
        ))
    for cmp_ in comparisons:
        series.append(series_from_comparison(
            cmp_, current_scene_revision_id=revision.revision_id,
        ))
        metadata.extend(comparison_metadata_entries(cmp_))

    export = build_analysis_export(
        document_id=revision.document.document_id,
        title='verify', generated_at_utc=NOW,
        series=tuple(series), metadata=tuple(metadata),
    )
    csv_text = render_analysis_csv(export)
    json_text = render_analysis_json(export)

    print('\n=== F1: analysis bundle comparison side curves ===')
    ids = [s.series_id for s in export.series]
    print('series ids:', ids)
    want_a = f'comparison:{comparison.comparison_id}:a'
    want_b = f'comparison:{comparison.comparison_id}:b'
    print(f'F1 side-A present: {want_a in ids}  side-B present: {want_b in ids}')

    print('\n=== F4: analysis CSV identity/label columns ===')
    rows = list(csv.reader(io.StringIO(csv_text)))
    flat = {cell for row in rows for cell in row}
    for key in ('document_id', 'schema_version', 'authority_version',
                'x_label', 'y_label'):
        print(f'F4 {key!r} in csv: {key in flat or any(key in r for r in [row[:2] for row in rows])}')
    doc_rows = [r for r in rows if len(r) >= 2 and r[1] == 'document_id']
    print('F4 document_id row count:', len(doc_rows))
    header = next(r for r in rows if r and r[0] == 'series_id')
    print('F4 series header:', header)

    print('\n=== JSON parity check ===')
    payload = json.loads(json_text)
    print('json has document_id:', 'document_id' in payload)
    print('json series has x_label/y_label:',
          'x_label' in payload['series'][0], 'y_label' in payload['series'][0])


if __name__ == '__main__':
    main()
