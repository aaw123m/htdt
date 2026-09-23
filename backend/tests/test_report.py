from htdt.report import build_report_payload, render_report_html


def _comparison() -> dict:
    return {
        'id': 'cmp-1',
        'project_id': 'project-1',
        'dataset_a_id': 'dataset-a',
        'dataset_b_id': 'dataset-b',
        'created_at': '2026-09-16T00:00:00+00:00',
        'spec': {'low_hz': 60, 'high_hz': 200, 'expected_change_paths': ['speakers.FL.position']},
        'result': {
            'algorithm_version': 'fr-compare-1',
            'comparison_role': 'configuration_ab',
            'grid_hz': [60.0, 80.0, 100.0, 160.0, 200.0],
            'a_db': [70.0, 72.0, 71.0, 69.0, 70.0],
            'b_db': [69.0, 70.0, 70.0, 70.0, 69.5],
            'difference_db': [1.0, 2.0, 1.0, -1.0, 0.5],
            'mean_difference_db': 0.7,
            'rms_difference_db': 1.2,
            'level_offset_db': 0.5,
            'shape_rms_db': 0.9,
            'valid_points': 5,
            'total_grid_points': 5,
            'measurement_a': {'measurement_id': 'ma', 'context_id': 'ca', 'channel_role': 'front_left', 'evidence_type': 'measured', 'quality_status': 'usable', 'repeat_group': None},
            'measurement_b': {'measurement_id': 'mb', 'context_id': 'cb', 'channel_role': 'front_left', 'evidence_type': 'measured', 'quality_status': 'warning', 'repeat_group': None},
            'intended_changes': [{'path': 'speakers.FL.position.x_m', 'a': 1.0, 'b': 1.1}],
            'confounders': [{'path': 'avr.volume_db', 'a': -30, 'b': -29}],
            'interpretation_warnings': ['B has measurement quality warnings'],
        },
    }


def test_report_html_is_self_contained_and_embeds_snapshot() -> None:
    payload = build_report_payload({'id': 'project-1', 'name': 'Living room'}, _comparison())
    html = render_report_html(payload)
    assert '<svg' in html
    assert 'dataset-a' in html
    assert 'speakers.FL.position.x_m' in html
    assert 'B has measurement quality warnings' in html
    assert 'application/json' in html
    assert 'fr-compare-1' in html
    assert 'cdn.' not in html.lower()
    assert '<script src=' not in html.lower()


def test_report_payload_preserves_saved_comparison() -> None:
    comparison = _comparison()
    payload = build_report_payload({'id': 'project-1', 'name': 'Living room'}, comparison)
    assert payload['report_schema_version'] == 1
    assert payload['comparison'] is comparison
    assert payload['comparison']['result']['measurement_a']['measurement_id'] == 'ma'
