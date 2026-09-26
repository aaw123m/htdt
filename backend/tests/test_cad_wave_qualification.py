"""#1080 — CTA WAVE qualification import tests."""

import json

from htdt.cad_wave_qualification import (
    build_wave_report,
    import_wave_report_json,
    wave_capability_observations,
    wave_report_provenance,
    WaveTestRow,
)


SAMPLE_REPORT = json.dumps(
    {
        'suite': 'dpctf',
        'version': 'v1.2.3',
        'device': 'Acme SmartTV-9000',
        'app': 'WAVE-web-player',
        'app_version': '4.0',
        'firmware': 'fw-9.9',
        'os': 'tvos-5',
        'executed_at': '2026-01-10T12:00:00Z',
        'results': [
            {'test': 'playback-over-dash', 'status': 'PASS'},
            {'test': 'sequential-track', 'status': 'FAIL',
             'detail': 'dropout at boundary'},
            {'test': 'fullscreen-switch', 'status': 'WARNING'},
            {'test': 'buffer-underrun', 'status': 'NOT_EXECUTED'},
        ],
    }
)


class TestImport:
    def test_import_valid_report(self):
        r = import_wave_report_json(
            SAMPLE_REPORT,
            report_id='rep-1',
            source_uri='https://example.org/rep1.json',
        )
        assert r.verdict == 'imported'
        rep = r.report
        assert rep.suite == 'dpctf'
        assert rep.suite_version == 'v1.2.3'
        assert rep.device_identity == 'Acme SmartTV-9000'
        assert len(rep.tests) == 4
        assert rep.tests[1].verdict == 'fail'

    def test_invalid_json(self):
        r = import_wave_report_json(
            'not json', report_id='x', source_uri='u'
        )
        assert r.verdict == 'invalid'

    def test_missing_device_incomplete(self):
        doc = json.loads(SAMPLE_REPORT)
        del doc['device']
        r = import_wave_report_json(
            json.dumps(doc), report_id='x', source_uri='u'
        )
        assert r.verdict == 'incomplete'

    def test_unknown_verdict_rejected(self):
        doc = json.loads(SAMPLE_REPORT)
        doc['results'] = [{'test': 't1', 'status': 'SHINY'}]
        r = import_wave_report_json(
            json.dumps(doc), report_id='x', source_uri='u'
        )
        assert r.verdict == 'invalid'

    def test_empty_results(self):
        doc = json.loads(SAMPLE_REPORT)
        doc['results'] = []
        r = import_wave_report_json(
            json.dumps(doc), report_id='x', source_uri='u'
        )
        assert r.verdict == 'incomplete'

    def test_report_hash_pinned(self):
        r = import_wave_report_json(
            SAMPLE_REPORT, report_id='rep-1', source_uri='u'
        )
        assert len(r.report.report_sha256) == 64
        assert len(r.report.semantic_sha256) == 64


class TestCapabilityMapping:
    def test_observations_per_test(self):
        r = import_wave_report_json(
            SAMPLE_REPORT, report_id='rep-1', source_uri='u'
        )
        obs = wave_capability_observations(r.report)
        assert len(obs) == 4
        by_aspect = {o.aspect: o for o in obs}
        assert (
            by_aspect['wave:dpctf:playback-over-dash'].state
            == 'observed_working'
        )
        assert (
            by_aspect['wave:dpctf:sequential-track'].state
            == 'observed_failure'
        )
        assert by_aspect['wave:dpctf:sequential-track'].detail
        assert (
            by_aspect['wave:dpctf:buffer-underrun'].state == 'unknown'
        )
        # warning keeps detail lane but still observed_working
        assert (
            by_aspect['wave:dpctf:fullscreen-switch'].state
            == 'observed_working'
        )

    def test_provenance_measured(self):
        r = import_wave_report_json(
            SAMPLE_REPORT, report_id='rep-1', source_uri='u'
        )
        p = wave_report_provenance(r.report)
        assert p.evidence_kind == 'measured'
        assert p.source_sha256 == r.report.report_sha256


class TestModel:
    def test_duplicate_test_ids_rejected(self):
        import pytest

        with pytest.raises(ValueError, match='duplicate test'):
            build_wave_report(
                report_id='r',
                suite='dpctf',
                suite_version='1',
                device_identity='d',
                tests=(
                    WaveTestRow(test_id='a', verdict='pass'),
                    WaveTestRow(test_id='a', verdict='fail'),
                ),
                report_sha256='0' * 64,
                source_uri='u',
            )
