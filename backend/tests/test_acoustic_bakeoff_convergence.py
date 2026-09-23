from __future__ import annotations

from pathlib import Path

import pytest

from htdt.acoustic_bakeoff_observation import (
    RawConvergenceLevel,
    RawObservationSample,
    evaluate_monotonic_convergence_observable,
)
from htdt.acoustic_benchmark import load_acoustic_benchmark_manifest


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / 'benchmarks' / 'acoustics' / 'r100a_manifest.json'


def _observable():
    manifest = load_acoustic_benchmark_manifest(MANIFEST_PATH)
    fixture = next(
        item for item in manifest.fixtures
        if item.fixture_id == 'wave-rectangular-convergence-v1'
    )
    return fixture.observables[0]


def _level(level_id: str, h_m: float, error: float) -> RawConvergenceLevel:
    samples = []
    for frequency_hz, reference in ((20.0, 1.0 + 0.0j), (100.0, 0.5 + 0.25j), (300.0, -0.2 + 0.1j)):
        value = reference + complex(error, -0.5 * error)
        samples.append(
            RawObservationSample(
                sample_key=f'p@{frequency_hz:g}Hz',
                frequency_hz=frequency_hz,
                real_value=value.real,
                imag_value=value.imag,
            )
        )
    return RawConvergenceLevel(
        level_id=level_id,
        refinement_parameter='h_m',
        refinement_value=h_m,
        samples=tuple(samples),
    )


def test_complex_field_convergence_passes_when_errors_decrease_below_tolerance() -> None:
    evidence = evaluate_monotonic_convergence_observable(
        _observable(),
        (
            _level('h=0.5', 0.5, 0.03),
            _level('h=0.25', 0.25, 0.01),
            _level('h=0.125', 0.125, 0.0),
        ),
    )

    assert evidence.status == 'pass'
    assert evidence.absolute_error == pytest.approx((1.25e-4) ** 0.5)
    assert evidence.relative_error is not None
    assert evidence.relative_error < 0.02


def test_complex_field_convergence_fails_when_refinement_stalls() -> None:
    evidence = evaluate_monotonic_convergence_observable(
        _observable(),
        (
            _level('h=0.5', 0.5, 0.01),
            _level('h=0.25', 0.25, 0.02),
            _level('h=0.125', 0.125, 0.0),
        ),
    )

    assert evidence.status == 'fail'
    assert 'not strictly decreasing' in evidence.summary


def test_complex_field_convergence_rejects_mismatched_frequency_grid() -> None:
    fine = _level('h=0.125', 0.125, 0.0)
    payload = fine.model_dump(mode='python')
    samples = list(payload['samples'])
    altered = dict(samples[0])
    altered['frequency_hz'] = 21.0
    samples[0] = altered
    payload['samples'] = samples
    altered_fine = RawConvergenceLevel.model_validate(payload)

    with pytest.raises(ValueError, match='frequency mismatch'):
        evaluate_monotonic_convergence_observable(
            _observable(),
            (
                _level('h=0.5', 0.5, 0.03),
                _level('h=0.25', 0.25, 0.01),
                altered_fine,
            ),
        )


def test_complex_field_convergence_requires_coarse_to_fine_order() -> None:
    with pytest.raises(ValueError, match='coarse-to-fine'):
        evaluate_monotonic_convergence_observable(
            _observable(),
            (
                _level('h=0.25', 0.25, 0.03),
                _level('h=0.5', 0.5, 0.01),
                _level('h=0.125', 0.125, 0.0),
            ),
        )
