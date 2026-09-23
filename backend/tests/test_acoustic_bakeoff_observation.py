from __future__ import annotations

from pathlib import Path

import pytest

from htdt.acoustic_bakeoff_observation import (
    RawFixtureObservation,
    RawObservableObservation,
    RawObservationSample,
    evaluate_sampled_fixture,
)
from htdt.acoustic_benchmark import load_acoustic_benchmark_manifest


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / 'benchmarks' / 'acoustics' / 'r100a_manifest.json'


def _fixture(fixture_id: str):
    manifest = load_acoustic_benchmark_manifest(MANIFEST_PATH)
    return next(item for item in manifest.fixtures if item.fixture_id == fixture_id)


def _direct_reflection_raw(*, direct_length: float = 3.0) -> RawFixtureObservation:
    return RawFixtureObservation(
        fixture_id='geometric-direct-first-reflection-v1',
        evidence_ref='artifact:test/direct-first-reflection.json',
        adapter_id='test-adapter',
        adapter_version='1',
        backend_version='test',
        precision='float64',
        compile_s=0.0,
        solve_s=0.01,
        postprocess_s=0.01,
        peak_ram_mb=64.0,
        disk_mb=0.02,
        output_mb=0.01,
        observations=(
            RawObservableObservation(
                observable_id='direct-length',
                kind='direct_path_length_m',
                unit='m',
                samples=(
                    RawObservationSample(
                        sample_key='src->rx',
                        scalar_value=direct_length,
                    ),
                ),
            ),
            RawObservableObservation(
                observable_id='direct-delay',
                kind='arrival_time_s',
                unit='s',
                samples=(
                    RawObservationSample(
                        sample_key='src->rx',
                        scalar_value=3.0 / 343.0,
                    ),
                ),
            ),
            RawObservableObservation(
                observable_id='first-reflection-point-ymin',
                kind='reflection_point_m',
                unit='m',
                samples=(
                    RawObservationSample(
                        sample_key='ymin',
                        vector_value=(2.5, 0.0, 1.0),
                    ),
                ),
            ),
            RawObservableObservation(
                observable_id='first-reflection-length-ymin',
                kind='reflected_path_length_m',
                unit='m',
                samples=(
                    RawObservationSample(
                        sample_key='ymin',
                        scalar_value=5.0,
                    ),
                ),
            ),
        ),
    )


def test_exact_raw_observations_pass_r100a_tolerances() -> None:
    fixture = _fixture('geometric-direct-first-reflection-v1')
    evidence = evaluate_sampled_fixture(fixture, _direct_reflection_raw())

    assert evidence.status == 'pass'
    assert all(item.status == 'pass' for item in evidence.observables)
    assert all(item.absolute_error == pytest.approx(0.0) for item in evidence.observables)


def test_raw_observation_outside_r100a_tolerance_becomes_fail_evidence() -> None:
    fixture = _fixture('geometric-direct-first-reflection-v1')
    evidence = evaluate_sampled_fixture(
        fixture,
        _direct_reflection_raw(direct_length=3.001),
    )

    assert evidence.status == 'fail'
    direct = next(item for item in evidence.observables if item.observable_id == 'direct-length')
    assert direct.status == 'fail'
    assert direct.absolute_error == pytest.approx(0.001)
    assert 'tolerance' in direct.summary


def test_raw_observable_unit_mismatch_is_rejected_fail_closed() -> None:
    fixture = _fixture('geometric-direct-first-reflection-v1')
    raw = _direct_reflection_raw()
    payload = raw.model_dump(mode='python')
    observations = list(payload['observations'])
    direct = dict(observations[0])
    direct['unit'] = 'cm'
    observations[0] = direct
    payload['observations'] = observations

    altered = RawFixtureObservation.model_validate(payload)
    with pytest.raises(ValueError, match='unit'):
        evaluate_sampled_fixture(fixture, altered)


def test_unsampled_reference_requires_specialized_evaluator() -> None:
    fixture = _fixture('wave-rectangular-convergence-v1')
    raw = RawFixtureObservation(
        fixture_id=fixture.fixture_id,
        evidence_ref='artifact:test/convergence.json',
        adapter_id='test-adapter',
        adapter_version='1',
        backend_version='test',
        precision='float64',
        compile_s=0.0,
        solve_s=0.01,
        postprocess_s=0.01,
        peak_ram_mb=64.0,
        disk_mb=0.02,
        output_mb=0.01,
        observations=(
            RawObservableObservation(
                observable_id='convergence',
                kind='complex_pressure_transfer_pa_per_m3_s',
                unit='Pa/(m3/s)',
                samples=(
                    RawObservationSample(sample_key='placeholder', scalar_value=1.0),
                ),
            ),
        ),
    )

    with pytest.raises(ValueError, match='specialized R100B evaluator'):
        evaluate_sampled_fixture(fixture, raw)
