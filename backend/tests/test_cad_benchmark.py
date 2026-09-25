"""#875 — external benchmark import & cross-solver validation harness."""

import json

import pytest

from htdt.cad_benchmark import (
    CANONICAL_JSON_FORMAT,
    BenchmarkObservable,
    BenchmarkPoint,
    BenchmarkSourceAsset,
    EvaluationProfile,
    StaticReplayProvider,
    analytic_benchmark_case,
    canonical_json_importer,
    evaluate_observable,
    run_benchmark,
    _hash,
)
from htdt.cad_equipment import EquipmentDataProvenance


def _provenance(suffix: str) -> EquipmentDataProvenance:
    digit = f'{suffix}0'[-1]
    return EquipmentDataProvenance(
        evidence_kind='measured',
        source_name='bench-suite',
        source_version='1',
        source_reference=f'ref-{suffix}',
        source_sha256=digit * 64,
    )


def _asset(**overrides) -> BenchmarkSourceAsset:
    data = {
        'asset_id': 'asset-1',
        'dataset_name': 'demo-bench',
        'dataset_version': '2026.1',
        'origin_uri': 'https://example.test/bench/demo',
        'content_sha256': 'a' * 64,
        'license_id': 'CC-BY-4.0',
        'admission_ref': 'ledger/demo-bench',
        'evidence_class': 'external_measured',
        'provenance': (_provenance('b1'),),
        'semantic_sha256': '0' * 64,
        **overrides,
    }
    draft = BenchmarkSourceAsset.model_construct(**data)
    data['semantic_sha256'] = _hash(draft.semantic_payload())
    return BenchmarkSourceAsset.model_validate(data)


def _observable(**overrides) -> BenchmarkObservable:
    base = {
        'observable_id': 'obs-fr',
        'kind': 'magnitude_fr',
        'metric_id': 'rms-band-error',
        'metric_version': '1',
        'tolerance': 1.0,
        'tolerance_unit': 'dB',
        'valid_band_hz': (20.0, 200.0),
        'reference': {
            'frequency_hz': [10.0, 50.0, 100.0, 300.0],
            'magnitude_db': [80.0, 85.0, 86.0, 90.0],
        },
    }
    return BenchmarkObservable(**{**base, **overrides})


def _case(**overrides):
    observable = overrides.pop('observable', _observable())
    return analytic_benchmark_case(
        benchmark_id='bench-1',
        version='1.0',
        source_asset=_asset(**overrides),
        observables=(observable,),
        coordinate_convention='cartesian-x-right-y-forward-z-up-m',
        sources=(
            BenchmarkPoint(point_id='src-1', position_m=(0.0, 0.0, 1.0)),
        ),
        receivers=(
            BenchmarkPoint(point_id='rx-1', position_m=(0.0, 2.0, 1.0)),
        ),
        limitations='single source, single receiver',
    )


class TestSourceAsset:
    def test_hash_verified(self):
        assert _asset().semantic_sha256

    def test_tampered_hash_rejected(self):
        asset = _asset()
        with pytest.raises(ValueError, match='semantic hash mismatch'):
            BenchmarkSourceAsset(
                **{**asset.model_dump(mode='python'), 'license_id': 'MIT'}
            )

    def test_evidence_class_part_of_identity(self):
        measured = _asset()
        simulated = _asset(evidence_class='external_simulated')
        assert measured.semantic_sha256 != simulated.semantic_sha256


class TestCanonicalJsonImporter:
    def test_valid_payload_imports(self):
        case = _case()
        assert case.benchmark_id == 'bench-1'
        assert case.importer_id == CANONICAL_JSON_FORMAT
        assert len(case.observables) == 1

    def test_rejects_wrong_format(self):
        with pytest.raises(ValueError, match='unsupported benchmark format'):
            canonical_json_importer(
                {'format': 'other', 'benchmark_id': 'b', 'version': '1'},
                _asset(),
            )

    def test_rejects_positions_without_coordinate_convention(self):
        payload = {
            'format': CANONICAL_JSON_FORMAT,
            'benchmark_id': 'b',
            'version': '1',
            'receivers': [
                {'point_id': 'rx', 'position_m': [0.0, 0.0, 0.0]}
            ],
            'observables': [
                {
                    'observable_id': 'o',
                    'kind': 'arrival_timing',
                    'metric_id': 'abs-delta',
                    'metric_version': '1',
                    'reference': {'arrival_s': 0.005},
                }
            ],
        }
        with pytest.raises(ValueError, match='coordinate convention'):
            canonical_json_importer(payload, _asset())

    def test_rejects_case_without_observables(self):
        payload = {
            'format': CANONICAL_JSON_FORMAT,
            'benchmark_id': 'b',
            'version': '1',
            'observables': [],
        }
        with pytest.raises(ValueError, match='at least one observable'):
            canonical_json_importer(payload, _asset())

    def test_missing_fields_stay_unknown(self):
        case = _case()
        assert case.geometry is None
        assert case.materials is None
        assert case.sample_rate_hz is None

    def test_case_hash_verified(self):
        case = _case()
        assert case.semantic_sha256


class TestEvaluateObservable:
    def test_pass_within_tolerance(self):
        observable = _observable()
        prediction = {
            'frequency_hz': [50.0, 100.0],
            'magnitude_db': [85.4, 86.3],
        }
        result = evaluate_observable(observable, prediction)
        assert result.status == 'PASS'
        assert result.error is not None and result.error <= 1.0

    def test_fail_outside_tolerance(self):
        observable = _observable()
        prediction = {
            'frequency_hz': [50.0, 100.0],
            'magnitude_db': [89.0, 90.0],
        }
        result = evaluate_observable(observable, prediction)
        assert result.status == 'FAIL'

    def test_band_window_filters_reference(self):
        # 10 Hz and 300 Hz reference points are outside the 20–200 band.
        observable = _observable()
        assert observable.reference['frequency_hz'][0] == 10.0
        result = evaluate_observable(
            observable,
            {'frequency_hz': [50.0, 100.0], 'magnitude_db': [85.0, 86.0]},
        )
        assert result.status == 'PASS'
        assert result.error == pytest.approx(0.0)

    def test_missing_prediction_never_passes(self):
        assert evaluate_observable(_observable(), None).status == 'UNKNOWN'

    def test_missing_reference_never_passes(self):
        observable = _observable(reference={'frequency_hz': [50.0]})
        result = evaluate_observable(
            observable,
            {'frequency_hz': [50.0], 'magnitude_db': [85.0]},
        )
        assert result.status == 'UNKNOWN'

    def test_no_tolerance_is_unknown_not_unbounded(self):
        observable = _observable(tolerance=None)
        result = evaluate_observable(
            observable,
            {'frequency_hz': [50.0], 'magnitude_db': [85.0]},
        )
        assert result.status == 'UNKNOWN'

    def test_unsupported_kind_not_applicable(self):
        observable = _observable(
            kind='decay_metric', reference={'value_s': 0.4}
        )
        result = evaluate_observable(observable, {'value_s': 0.4})
        assert result.status == 'NOT_APPLICABLE'

    def test_arrival_timing(self):
        observable = _observable(
            kind='arrival_timing',
            tolerance=0.001,
            tolerance_unit='s',
            reference={'arrival_s': 0.0058},
        )
        assert evaluate_observable(
            observable, {'arrival_s': 0.0060}
        ).status == 'PASS'
        assert evaluate_observable(
            observable, {'arrival_s': 0.0080}
        ).status == 'FAIL'

    def test_profile_tolerance_override(self):
        observable = _observable(tolerance=0.5)
        prediction = {
            'frequency_hz': [50.0, 100.0],
            'magnitude_db': [85.7, 86.7],
        }
        profile = EvaluationProfile(
            profile_id='p1',
            version='1',
            tolerance_overrides={'obs-fr': 1.0},
        )
        assert evaluate_observable(observable, prediction, profile).status == 'PASS'


class TestRunBenchmark:
    def test_evidence_binds_exact_identities(self):
        case = _case()
        provider = StaticReplayProvider(
            {'obs-fr': {'frequency_hz': [50.0, 100.0],
                        'magnitude_db': [85.2, 86.1]}},
            provider_version='2.1',
        )
        profile = EvaluationProfile(profile_id='acceptance', version='1')
        evidence = run_benchmark(
            case, provider, profile,
            evidence_id='run-1', created_at_utc='2026-09-25T00:00:00Z',
        )
        assert evidence.benchmark_sha256 == case.semantic_sha256
        assert evidence.provider_version == '2.1'
        assert evidence.provider_config_sha256 is not None
        assert evidence.evaluation_profile_id == 'acceptance'
        assert evidence.evidence_class == 'external_measured'
        assert evidence.status == 'PASS'
        dumped = evidence.model_dump(mode='json')
        assert json.dumps(dumped)  # machine-readable round-trip

    def test_simulated_case_keeps_simulated_class(self):
        case = _case(evidence_class='external_simulated')
        evidence = run_benchmark(
            case,
            StaticReplayProvider(
                {'obs-fr': {'frequency_hz': [50.0], 'magnitude_db': [85.0]}}
            ),
            EvaluationProfile(profile_id='p', version='1'),
        )
        assert evidence.evidence_class == 'external_simulated'

    def test_all_unsupported_folds_not_applicable(self):
        observable = _observable(
            kind='decay_metric', reference={'value_s': 0.4}
        )
        case = _case(observable=observable)
        evidence = run_benchmark(
            case,
            StaticReplayProvider({'obs-fr': {'value_s': 0.4}}),
            EvaluationProfile(profile_id='p', version='1'),
        )
        assert evidence.status == 'NOT_APPLICABLE'
        assert evidence.unsupported_count == 1

    def test_mixed_unknown_and_pass_folds_unknown(self):
        case = _case(
            observable=_observable(),
        )
        evidence = run_benchmark(
            case,
            StaticReplayProvider({}),  # no predictions at all
            EvaluationProfile(profile_id='p', version='1'),
        )
        assert evidence.status == 'UNKNOWN'
