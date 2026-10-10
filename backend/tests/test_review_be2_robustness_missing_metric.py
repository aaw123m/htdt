"""Regression tests for O90B robustness evaluation builders dropping
O90A's metric-availability guard.

A perturbed (or nominal) sample may legally carry an ObjectiveMetric whose
state is 'missing'/'unsupported' (definition-bound, value=None). O90A's
``_metric_for_sample`` treats such metrics as unscored; the O90B
multidimensional and uncertainty builders must do the same instead of
crashing on ``float(None)``, and a nominal metric that is not available
must fail with a deliberate error rather than a TypeError.
"""

from __future__ import annotations

import pytest

from htdt.optimization.domain.optimization_objectives import (
    ObjectiveDefinition,
    ObjectiveMetric,
    ObjectiveValidDomain,
    ObjectiveVector,
)
from htdt.optimization.domain.optimization_robustness import (
    PerturbationSample,
    canonical_robustness_sha256,
)
from htdt.optimization.domain.optimization_robustness_multidimensional import (
    build_multidimensional_evaluations_from_provenance,
)

DEFINITION = ObjectiveDefinition(
    objective_id='o1',
    quantity='o1',
    unit='dB',
    direction='minimize',
    valid_domain=ObjectiveValidDomain(kind='bounded_real', minimum=0.0),
    comparison_model_id='m1',
    comparison_model_version='1',
)


def _make_sample(
    sample_id: str,
    index: int,
    step: str,
    vector: ObjectiveVector | None,
) -> PerturbationSample:
    payload = {
        'schema_version': 1,
        'sample_id': sample_id,
        'robustness_spec_id': 'rob-x',
        'robustness_spec_sha256': 'a' * 64,
        'candidate_id': 'cand-1',
        'sample_index': index,
        'axis_id': None,
        'step': step,
        'parameter_deltas': {} if step == 'nominal' else {'ax': 0.1},
        'perturbed_scene_content_hash': 'b' * 64,
        'feasible': True,
        'g10_results': [],
        'o80_rejection_ids': [],
        'domain_rejection_ids': [],
        'model_id': 'm',
        'model_version': '1',
        'prediction_provider_id': 'p',
        'fidelity': 'f',
        'objective_evaluation_spec_sha256': 'c' * 64,
        'prediction_result_ref': 'ref' if vector is not None else None,
        'objective_vector': (
            None if vector is None else vector.identity_payload()
        ),
        'failure_reason': None,
    }
    return PerturbationSample(
        **payload,
        sample_sha256=canonical_robustness_sha256(payload),
        created_at_utc='2026-01-01T00:00:00+00:00',
    )


def _vector(candidate_id: str, metric: ObjectiveMetric) -> ObjectiveVector:
    return ObjectiveVector(candidate_id=candidate_id, metrics=(metric,))


def _available_metric(value: float) -> ObjectiveMetric:
    return ObjectiveMetric(
        objective_id='o1', value=value, unit='dB', definition=DEFINITION
    )


def _missing_metric() -> ObjectiveMetric:
    return ObjectiveMetric(
        objective_id='o1',
        value=None,
        unit='dB',
        state='missing',
        definition=DEFINITION,
    )


def _build(samples):
    return build_multidimensional_evaluations_from_provenance(
        robustness_spec_id='rob-x',
        robustness_spec_sha256='a' * 64,
        candidate_id='cand-1',
        expected_sample_ids=tuple(s.sample_id for s in samples),
        samples=samples,
        sampling_provenance={'k': 'v'},
    )


def test_multidimensional_skips_missing_state_metric() -> None:
    samples = (
        _make_sample(
            's-nominal', 0, 'nominal', _vector('s-nominal', _available_metric(1.0))
        ),
        _make_sample(
            's-p1',
            1,
            'multidimensional',
            _vector('s-p1', _missing_metric()),
        ),
        _make_sample(
            's-p2',
            2,
            'multidimensional',
            _vector('s-p2', _available_metric(3.0)),
        ),
    )
    evaluations = _build(samples)
    assert len(evaluations) == 1
    (evaluation,) = evaluations
    # The missing-state metric is unscored; the envelope/worst must reflect
    # only the two samples that actually produced a value (O90A semantics).
    assert evaluation.sampled_envelope.sampled_min_value == 1.0
    assert evaluation.sampled_envelope.sampled_max_value == 3.0
    assert evaluation.sampled_worst_value == 3.0
    assert evaluation.sampled_worst_sample_id == 's-p2'
    # The sample retained its vector, so it is not a failed sample.
    assert list(evaluation.failed_sample_ids) == []


def test_multidimensional_nominal_missing_metric_raises() -> None:
    samples = (
        _make_sample(
            's-nominal', 0, 'nominal', _vector('s-nominal', _missing_metric())
        ),
        _make_sample(
            's-p1',
            1,
            'multidimensional',
            _vector('s-p1', _available_metric(3.0)),
        ),
    )
    with pytest.raises(ValueError, match='nominal.*not available|not available'):
        _build(samples)
