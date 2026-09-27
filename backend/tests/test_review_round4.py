"""Round-4 numerics/benchmark-harness regression tests.

Covers the fixes documented in docs/reviews/round4-numerics.md:

- ``evaluate_monotonic_convergence_observable`` produced ``inf`` relative
  error for a zero-valued finest reference field, which crashed the
  ``BakeoffObservableEvidence`` finiteness validator instead of returning
  ``fail`` evidence.
- ``evaluate_sampled_observable`` never checked ``acceptance_relation``, so
  a sampled ``must_differ_from_peer``/``monotonic_convergence`` observable
  would be scored by plain tolerance without evaluating its contract.
- ``compare_frequency_responses`` trusted ``FrequencyResponse`` inputs:
  empty axes crashed with ``IndexError``, non-positive/non-finite
  frequencies leaked raw ``math.log2`` domain errors, and non-finite
  levels propagated into metrics that later poison ``allow_nan=False``
  canonical JSON.
- ``cad_benchmark.evaluate_observable`` paired reference/prediction values
  positionally after band filtering without checking the frequency grids
  match, and silently dropped samples on non-finite frequencies.
- ``EvaluationProfile.tolerance_overrides`` and ``BenchmarkObservable.tolerance``
  accepted non-finite bounds — ``inf`` made every comparison PASS.
"""
from __future__ import annotations

import math

import pytest

from htdt.acoustic_bakeoff_observation import (
    RawConvergenceLevel,
    RawObservableObservation,
    RawObservationSample,
    evaluate_monotonic_convergence_observable,
    evaluate_sampled_observable,
)
from htdt.acoustic_benchmark import (
    BenchmarkExpectedObservable,
    BenchmarkExpectedSample,
    BenchmarkTolerance,
)
from htdt.cad_benchmark import (
    BenchmarkObservable,
    EvaluationProfile,
    evaluate_observable,
)
from htdt.comparison import (
    ComparisonError,
    FrequencyResponse,
    compare_frequency_responses,
)


def _expected(
    *,
    relation: str = 'matches_reference',
    samples: tuple = (),
    kind: str = 'transfer_magnitude_db',
    unit: str = 'dB',
    tolerance: BenchmarkTolerance | None = None,
    reference_kind: str = 'independent_solver',
    peer_fixture_id: str | None = None,
) -> BenchmarkExpectedObservable:
    return BenchmarkExpectedObservable(
        observable_id='obs-1',
        kind=kind,
        unit=unit,
        reference_kind=reference_kind,
        reference_description='test reference',
        acceptance_relation=relation,
        tolerance=tolerance or BenchmarkTolerance(absolute=1.0),
        samples=samples,
        peer_fixture_id=peer_fixture_id,
    )


def _level(level_id: str, order: float, real: float) -> RawConvergenceLevel:
    return RawConvergenceLevel(
        level_id=level_id,
        refinement_parameter='order',
        refinement_value=order,
        samples=(
            RawObservationSample(
                sample_key='a', frequency_hz=100.0,
                real_value=real, imag_value=0.0,
            ),
            RawObservationSample(
                sample_key='b', frequency_hz=200.0,
                real_value=real, imag_value=0.0,
            ),
        ),
    )


def test_convergence_zero_reference_field_yields_fail_evidence_not_a_crash() -> None:
    """A solver emitting an all-zero finest field crashed the evaluator
    with ``ValidationError`` (relative RMS = inf hit the evidence
    finiteness contract) instead of returning ``fail`` evidence."""
    expected = _expected(
        relation='monotonic_convergence',
        kind='complex_pressure_transfer_pa_per_m3_s',
        unit='Pa/(m3/s)',
    )
    levels = (
        _level('L0', 4.0, 3.0),
        _level('L1', 2.0, 2.0),
        _level('L2', 1.0, 0.0),  # finest representation is identically zero
    )

    evidence = evaluate_monotonic_convergence_observable(expected, levels)

    assert evidence.status == 'fail'
    assert evidence.relative_error is None
    assert 'not strictly decreasing' in evidence.summary


def test_convergence_zero_reference_identical_zero_levels_still_passes() -> None:
    """All-zero levels everywhere: relative RMS stays defined (0.0) and the
    monotonicity verdict governs — no regression for the finite path."""
    expected = _expected(
        relation='monotonic_convergence',
        kind='complex_pressure_transfer_pa_per_m3_s',
        unit='Pa/(m3/s)',
    )
    levels = (
        _level('L0', 4.0, 0.0),
        _level('L1', 2.0, 0.0),
        _level('L2', 1.0, 0.0),
    )

    evidence = evaluate_monotonic_convergence_observable(expected, levels)

    assert evidence.status == 'fail'
    assert evidence.relative_error == 0.0


def test_sampled_evaluator_rejects_non_reference_acceptance_relations() -> None:
    """The sampled evaluator scores plain tolerance only; a sampled
    ``must_differ_from_peer`` observable would otherwise produce a verdict
    without ever evaluating the peer contract."""
    samples = (
        BenchmarkExpectedSample(sample_key='s1', scalar_value=1.0),
    )
    raw = RawObservableObservation(
        observable_id='obs-1',
        kind='transfer_magnitude_db',
        unit='dB',
        samples=(RawObservationSample(sample_key='s1', scalar_value=1.1),),
    )

    for relation in (
        'must_differ_from_peer',
        'matches_peer',
        'monotonic_convergence',
        'repeatable_same_seed',
        'continuous_overlap',
    ):
        expected = _expected(
            relation=relation,
            samples=samples,
            tolerance=BenchmarkTolerance(
                absolute=1.0,
                minimum_difference=(
                    0.5 if relation == 'must_differ_from_peer' else None
                ),
            ),
            peer_fixture_id=(
                'peer-fixture'
                if relation in {'must_differ_from_peer', 'matches_peer'}
                else None
            ),
        )
        with pytest.raises(ValueError, match='specialized evaluator'):
            evaluate_sampled_observable(expected, raw)


def _response(count: int = 400, *, offset: float = 0.0) -> FrequencyResponse:
    frequencies = tuple(20.0 * 2 ** (i / 96.0) for i in range(count))
    return FrequencyResponse(
        frequencies, tuple(70.0 + offset for _ in range(count))
    )


def test_comparison_rejects_empty_and_mismatched_responses() -> None:
    valid = _response()
    with pytest.raises(ComparisonError, match='no frequency samples'):
        compare_frequency_responses(FrequencyResponse((), ()), valid, 20.0, 20000.0)
    with pytest.raises(ComparisonError, match='no frequency samples'):
        compare_frequency_responses(valid, FrequencyResponse((), ()), 20.0, 20000.0)
    with pytest.raises(ComparisonError, match='length mismatch'):
        compare_frequency_responses(
            FrequencyResponse((20.0, 40.0, 80.0), (70.0, 70.0)),
            valid,
            20.0,
            20000.0,
        )


def test_comparison_rejects_non_finite_or_non_positive_frequencies() -> None:
    valid = _response()
    for bad in (0.0, -20.0, float('nan'), float('inf')):
        response = FrequencyResponse((bad, 100.0, 200.0), (70.0, 70.0, 70.0))
        with pytest.raises(ComparisonError, match='finite and positive'):
            compare_frequency_responses(response, valid, 20.0, 20000.0)


def test_comparison_rejects_non_finite_levels() -> None:
    """A NaN sample previously blended into plausible-looking wrong metrics
    instead of surfacing; it now fails at the boundary."""
    valid = _response()
    bad = _response()
    object.__setattr__(
        bad, 'level_db',
        tuple(float('nan') if i == 200 else 70.0 for i in range(400)),
    )
    with pytest.raises(ComparisonError, match='levels must be finite'):
        compare_frequency_responses(valid, bad, 20.0, 20000.0)


def test_comparison_rejects_non_finite_band_specification() -> None:
    valid = _response()
    with pytest.raises(ComparisonError, match='finite'):
        compare_frequency_responses(valid, valid, float('nan'), 20000.0)
    with pytest.raises(ComparisonError, match='finite'):
        compare_frequency_responses(valid, valid, 20.0, float('inf'))
    with pytest.raises(ComparisonError, match='finite'):
        compare_frequency_responses(
            valid, valid, 20.0, 20000.0, reference_band_hz=(float('inf'), 1000.0)
        )
    with pytest.raises(ComparisonError, match='finite'):
        compare_frequency_responses(
            valid, valid, 20.0, 20000.0, excluded_bands=((float('nan'), 1000.0),)
        )


def test_comparison_valid_path_unchanged() -> None:
    """Validation must not perturb the pinned algorithm: identical inputs
    still yield a zero difference."""
    a = _response()
    result = compare_frequency_responses(a, _response(), 100.0, 1000.0)
    assert result.mean_difference_db == pytest.approx(0.0, abs=1e-12)
    assert result.rms_difference_db == pytest.approx(0.0, abs=1e-12)


def _magnitude_observable(tolerance: float | None = 1.0) -> BenchmarkObservable:
    return BenchmarkObservable(
        observable_id='obs',
        kind='magnitude_fr',
        metric_id='rms',
        metric_version='1',
        tolerance=tolerance,
        reference={
            'frequency_hz': [100.0, 200.0],
            'magnitude_db': [0.0, 0.0],
        },
    )


def test_benchmark_misaligned_prediction_grid_is_unknown() -> None:
    """Identical counts at different frequencies used to pair positionally
    and could PASS against the wrong grid — now UNKNOWN."""
    observable = _magnitude_observable()
    shifted = evaluate_observable(
        observable,
        {'frequency_hz': [150.0, 250.0], 'magnitude_db': [0.0, 0.0]},
    )
    assert shifted.status == 'UNKNOWN'
    assert 'misaligned' in (shifted.reason or '')

    exact = evaluate_observable(
        observable,
        {'frequency_hz': [100.0, 200.0], 'magnitude_db': [0.0, 0.0]},
    )
    assert exact.status == 'PASS'
    assert exact.error == 0.0


def test_benchmark_non_finite_samples_are_unknown_not_dropped() -> None:
    """A NaN frequency used to silently drop the sample out of band;
    non-finite payloads now fail closed as UNKNOWN."""
    observable = _magnitude_observable()
    for payload in (
        {'frequency_hz': [float('nan'), 200.0], 'magnitude_db': [0.0, 0.0]},
        {'frequency_hz': [100.0, 200.0], 'magnitude_db': [0.0, float('nan')]},
        {'frequency_hz': ['oops', 200.0], 'magnitude_db': [0.0, 0.0]},
    ):
        result = evaluate_observable(observable, payload)
        assert result.status == 'UNKNOWN'


def test_evaluation_profile_rejects_non_finite_tolerance_overrides() -> None:
    observable = _magnitude_observable()
    for bad in (float('inf'), float('-inf'), float('nan'), 0.0, -1.0):
        with pytest.raises(ValueError, match='finite and positive'):
            EvaluationProfile(
                profile_id='p', version='1',
                tolerance_overrides={'obs': bad},
            )

    profile = EvaluationProfile(
        profile_id='p', version='1', tolerance_overrides={'obs': 2.0}
    )
    result = evaluate_observable(
        observable,
        {'frequency_hz': [100.0, 200.0], 'magnitude_db': [9e9, 9e9]},
        profile,
    )
    assert result.status == 'FAIL'


def test_observable_tolerance_rejects_non_finite() -> None:
    """``gt=0.0`` alone admits ``inf``, which made every comparison PASS."""
    with pytest.raises(ValueError):
        _magnitude_observable(tolerance=float('inf'))
    with pytest.raises(ValueError):
        _magnitude_observable(tolerance=float('nan'))
