from __future__ import annotations

from pathlib import Path

import pytest

from htdt.acoustic_bakeoff import (
    BakeoffHardGateEvidence,
    BakeoffPlatform,
    BakeoffRun,
    load_bakeoff_candidate_manifest,
    validate_bakeoff_run,
)
from htdt.acoustic_benchmark import load_acoustic_benchmark_manifest
from htdt.acoustic_pyroom_stochastic import (
    PyroomStochasticObservation,
    PyroomStochasticRawEvidence,
    evaluate_pyroom_stochastic_fixture,
    load_pyroom_stochastic_authority,
    stochastic_fixture_semantic_hash,
    stochastic_sample_keys,
)


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / 'benchmarks' / 'acoustics' / 'r100a_manifest.json'
CANDIDATES_PATH = ROOT / 'benchmarks' / 'acoustics' / 'r100b_candidates.json'
AUTHORITY_PATH = ROOT / 'benchmarks' / 'acoustics' / 'r100b_pyroom_stochastic_authority.json'


def _inputs():
    benchmark = load_acoustic_benchmark_manifest(MANIFEST_PATH)
    candidates = load_bakeoff_candidate_manifest(CANDIDATES_PATH)
    authority = load_pyroom_stochastic_authority(AUTHORITY_PATH)
    fixture = next(item for item in benchmark.fixtures if item.fixture_id == authority.fixture_id)
    candidate = next(
        item for item in candidates.candidates if item.candidate_id == authority.candidate_id
    )
    return benchmark, candidates, authority, fixture, candidate


def _curve(count: int, *, offset: float, seed_delta: float = 0.0) -> tuple[float, ...]:
    return tuple(-2.0 - 0.8 * index + offset + seed_delta for index in range(count))


def _raw(*, offsets: tuple[float, float, float] = (0.08, 0.02, 0.0)):
    benchmark, candidates, authority, fixture, candidate = _inputs()
    keys = stochastic_sample_keys(authority)
    seed_deltas = (-0.01, -0.005, 0.005, 0.01)
    observations: list[PyroomStochasticObservation] = []

    for level_index, (budget, offset) in enumerate(zip(authority.ray_budgets, offsets)):
        for seed, seed_delta in zip(authority.independent_seeds, seed_deltas):
            observations.append(
                PyroomStochasticObservation(
                    observation_id=f'independent-seed-{seed}-rays-{budget}',
                    seed=seed,
                    ray_budget=budget,
                    receiver_radius_m=authority.receiver_radius_sequence_m[level_index],
                    histogram_bin_size_s=authority.histogram_bin_size_sequence_s[level_index],
                    replicate=0,
                    status='ok',
                    sample_keys=keys,
                    values_db=_curve(len(keys), offset=offset, seed_delta=seed_delta),
                    selected_band_centers_hz=authority.frequency_hz,
                    histogram_shape=(len(authority.frequency_hz), 100),
                    histogram_sha256=(f'{seed:064x}')[-64:],
                    archive_member=f'independent_{budget}_{seed}',
                    histogram_total_energy=12.0,
                    nonzero_bin_count=90,
                    setup_s=0.001,
                    solve_s=0.01,
                    postprocess_s=0.001,
                    peak_ram_mb=64.0,
                    raw_output_bytes=2400,
                    diagnostic='synthetic evaluator unit-test observation',
                )
            )

    replay_curve = _curve(len(keys), offset=0.0)
    replay_digest = 'a' * 64
    for replicate in range(authority.same_seed_repeats):
        observations.append(
            PyroomStochasticObservation(
                observation_id=f'replay-{replicate}',
                seed=fixture.random_seed,
                ray_budget=authority.ray_budgets[-1],
                receiver_radius_m=authority.receiver_radius_sequence_m[-1],
                histogram_bin_size_s=authority.histogram_bin_size_sequence_s[-1],
                replicate=replicate,
                status='ok',
                sample_keys=keys,
                values_db=replay_curve,
                selected_band_centers_hz=authority.frequency_hz,
                histogram_shape=(len(authority.frequency_hz), 100),
                histogram_sha256=replay_digest,
                archive_member=f'replay_{replicate}',
                histogram_total_energy=12.0,
                nonzero_bin_count=90,
                setup_s=0.001,
                solve_s=0.01,
                postprocess_s=0.001,
                peak_ram_mb=64.0,
                raw_output_bytes=2400,
                diagnostic='synthetic same-seed replay unit-test observation',
            )
        )

    raw = PyroomStochasticRawEvidence(
        evidence_ref='artifact:test/pyroom-stochastic.json',
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        fixture_id=fixture.fixture_id,
        fixture_semantic_hash=stochastic_fixture_semantic_hash(fixture),
        stochastic_authority_id=authority.authority_id,
        stochastic_authority_semantic_hash=authority.semantic_hash(),
        candidate_manifest_hash=candidates.semantic_hash(),
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        backend_version=authority.backend_version,
        adapter_id='test-pyroom-stochastic',
        adapter_version='1',
        wheel_filename='pyroomacoustics-0.10.1-test.whl',
        wheel_sha256='b' * 64,
        raw_archive_sha256='c' * 64,
        raw_archive_bytes=sum(item.raw_output_bytes for item in observations),
        dependencies=('pyroomacoustics==0.10.1', 'numpy==test'),
        observations=tuple(observations),
    )
    return benchmark, candidates, authority, fixture, candidate, raw


def test_converged_independent_seed_sequence_passes_and_connects_to_bakeoff_run() -> None:
    benchmark, candidates, authority, fixture, candidate, raw = _raw()

    evidence, evaluation = evaluate_pyroom_stochastic_fixture(
        benchmark, candidates, fixture, authority, raw
    )

    assert evaluation.repeatability_status == 'pass'
    assert evaluation.exact_histogram_replay is True
    assert evaluation.exact_curve_replay is True
    assert evaluation.convergence_status == 'pass'
    assert evaluation.monotonic_budget_refinement is True
    assert evaluation.adjacent_budget_mean_rms_delta_db == pytest.approx((0.06, 0.02))
    assert evaluation.finest_budget_seed_stddev_max_db is not None
    assert evaluation.finest_budget_seed_stddev_max_db < 0.05
    assert evidence.status == 'pass'

    run = BakeoffRun(
        run_id='test-pyroom-stochastic',
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        candidate_manifest_hash=candidates.semantic_hash(),
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        platform=BakeoffPlatform(
            os='test',
            architecture='x86_64',
            python_version='3.12',
            cpu='test',
            logical_threads=4,
            thread_budget=4,
            device_notes='unit test',
        ),
        fixture_evidence=(evidence,),
        hard_gates=(
            BakeoffHardGateEvidence(
                category='physics_correctness',
                status='not_run',
                summary='candidate-wide gate remains open',
            ),
            BakeoffHardGateEvidence(
                category='reproducible_authority',
                status='pass',
                evidence_ref=raw.evidence_ref,
                summary='unit-test authority binding',
            ),
        ),
    )
    validate_bakeoff_run(benchmark, candidates, run)


def test_same_seed_repeatability_is_not_enough_when_budget_sequence_does_not_converge() -> None:
    benchmark, candidates, authority, fixture, _candidate, raw = _raw(
        offsets=(0.03, 0.10, 0.0)
    )

    evidence, evaluation = evaluate_pyroom_stochastic_fixture(
        benchmark, candidates, fixture, authority, raw
    )

    assert evaluation.repeatability_status == 'pass'
    assert evaluation.convergence_status == 'fail'
    assert evaluation.monotonic_budget_refinement is False
    assert evidence.status == 'fail'


def test_insufficient_ray_support_is_retained_as_fail_not_zero_response() -> None:
    benchmark, candidates, authority, fixture, _candidate, raw = _raw()
    payload = raw.model_dump(mode='python')
    observations = list(payload['observations'])
    first = dict(observations[0])
    first['status'] = 'insufficient_support'
    first['values_db'] = ()
    first['diagnostic'] = 'no positive cumulative energy at one or more required samples'
    observations[0] = first
    payload['observations'] = observations
    insufficient_raw = PyroomStochasticRawEvidence.model_validate(payload)

    evidence, evaluation = evaluate_pyroom_stochastic_fixture(
        benchmark, candidates, fixture, authority, insufficient_raw
    )

    assert evidence.status == 'fail'
    assert evaluation.convergence_status == 'fail'
    assert evaluation.insufficient_observation_ids == (first['observation_id'],)
    assert tuple(item.ray_budget for item in evaluation.budget_variation) == (
        authority.ray_budgets[1], authority.ray_budgets[2]
    )
    assert tuple(item.ray_budget for item in evaluation.resource_scaling) == authority.ray_budgets
    assert 'zero response' in evidence.observables[0].summary


def test_insufficient_same_seed_curve_keeps_raw_histogram_repeatability_separate() -> None:
    benchmark, candidates, authority, fixture, _candidate, raw = _raw()
    payload = raw.model_dump(mode='python')
    observations = list(payload['observations'])
    replay_seed = fixture.random_seed
    for index, observation in enumerate(observations):
        if observation['seed'] != replay_seed:
            continue
        changed = dict(observation)
        changed['status'] = 'insufficient_support'
        changed['values_db'] = ()
        changed['diagnostic'] = 'same raw histogram replay, estimator support unavailable'
        observations[index] = changed
    payload['observations'] = observations
    replay_insufficient = PyroomStochasticRawEvidence.model_validate(payload)

    evidence, evaluation = evaluate_pyroom_stochastic_fixture(
        benchmark, candidates, fixture, authority, replay_insufficient
    )

    assert evaluation.exact_histogram_replay is True
    assert evaluation.exact_curve_replay is False
    assert evaluation.repeatability_status == 'fail'
    assert evaluation.convergence_status == 'pass'
    assert evidence.status == 'fail'
    assert len(evaluation.budget_variation) == len(authority.ray_budgets)


def test_stale_r100a_semantic_hash_is_rejected() -> None:
    benchmark, candidates, authority, fixture, _candidate, raw = _raw()
    stale = raw.model_copy(update={'r100a_semantic_hash': '0' * 64})

    with pytest.raises(ValueError, match='semantic hash is stale'):
        evaluate_pyroom_stochastic_fixture(benchmark, candidates, fixture, authority, stale)


def test_stale_candidate_manifest_and_source_commit_are_rejected() -> None:
    benchmark, candidates, authority, fixture, _candidate, raw = _raw()

    stale_manifest = raw.model_copy(update={'candidate_manifest_hash': '0' * 64})
    with pytest.raises(ValueError, match='candidate manifest hash is stale'):
        evaluate_pyroom_stochastic_fixture(
            benchmark, candidates, fixture, authority, stale_manifest
        )

    stale_source = raw.model_copy(update={'candidate_source_commit_sha': '0' * 40})
    with pytest.raises(ValueError, match='candidate source commit is stale'):
        evaluate_pyroom_stochastic_fixture(
            benchmark, candidates, fixture, authority, stale_source
        )
