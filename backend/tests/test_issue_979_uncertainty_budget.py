"""Issue #979: uncertainty budgets — declared representations, linear
propagation, bounded intervals, correlation groups, model discrepancy
kept separate."""

from __future__ import annotations

from math import isclose, sqrt

import pytest
from pydantic import ValidationError

from htdt.cad_uncertainty_budget import (
    UncertaintyBudgetResult,
    UncertaintyBudgetSpec,
    UncertaintyComponent,
    build_uncertainty_budget_spec,
    propagate_uncertainty_budget,
)

_H = '1' * 64


def _component(component_id='u1', **overrides):
    kwargs = dict(
        component_id=component_id,
        subject_quantity='spl_at_seat',
        category='measurement_instrument',
        representation='standard_uncertainty',
        standard_uncertainty=0.5,
        quantity_unit='dB',
    )
    kwargs.update(overrides)
    return UncertaintyComponent(**kwargs)


def _spec(components, **overrides):
    kwargs = dict(
        spec_id='ub-1',
        schema_version='ub_v1',
        document_id='doc-1',
        measurand='spl_at_seat',
        measurand_unit='dB',
        propagation_method='first_order_linear',
        components=components,
        created_at_utc='2026-09-25T00:00:00+00:00',
    )
    kwargs.update(overrides)
    return build_uncertainty_budget_spec(**kwargs)


def _bounded(component_id='b1', low=-1.0, high=1.0, **overrides):
    kwargs = dict(
        component_id=component_id,
        subject_quantity='mic_pose_offset',
        category='spatial_operating_condition',
        representation='bounded_interval',
        bound_low=low,
        bound_high=high,
    )
    kwargs.update(overrides)
    return UncertaintyComponent(**kwargs)


def test_spec_is_sealed():
    spec = _spec((_component(),))
    assert len(spec.spec_sha256) == 64
    UncertaintyBudgetSpec.model_validate(spec.model_dump(mode='json'))
    payload = spec.model_dump(mode='json')
    payload['components'][0]['standard_uncertainty'] = 9.0
    with pytest.raises(ValidationError, match='hash mismatch'):
        UncertaintyBudgetSpec.model_validate(payload)


def test_linear_method_rejects_missing_std():
    with pytest.raises(ValidationError, match='bounded intervals'):
        _spec((_bounded(),))


def test_representation_specific_fields_required():
    with pytest.raises(ValidationError, match='standard_uncertainty'):
        UncertaintyComponent(
            component_id='u',
            subject_quantity='q',
            category='numerical',
            representation='standard_uncertainty',
        )
    with pytest.raises(ValidationError, match='bound_low'):
        UncertaintyComponent(
            component_id='u',
            subject_quantity='q',
            category='model_input',
            representation='bounded_interval',
        )
    with pytest.raises(ValidationError, match='empirical_samples'):
        UncertaintyComponent(
            component_id='u',
            subject_quantity='q',
            category='model_input',
            representation='empirical_samples',
        )
    with pytest.raises(ValidationError, match='category'):
        _component(category='unknown')
    with pytest.raises(ValidationError, match='representation'):
        _component(representation='unknown')


def test_standard_rss_propagation():
    spec = _spec(
        (
            _component('a', standard_uncertainty=0.3),
            _component('b', standard_uncertainty=0.4,
                       category='spatial_operating_condition'),
        )
    )
    result = propagate_uncertainty_budget(
        spec, result_id='r1', created_at_utc='2026-09-25T00:01:00+00:00'
    )
    assert result.combination_state == 'propagated'
    assert isclose(result.combined_standard_uncertainty, 0.5, rel_tol=1e-9)


def test_correlated_components_sum_linearly_within_group():
    spec = _spec(
        (
            _component('a', standard_uncertainty=0.2, correlation_group_id='g1'),
            _component('b', standard_uncertainty=0.3, correlation_group_id='g1'),
            _component('c', standard_uncertainty=0.5),
        ),
        correlation_policy='correlation_groups',
    )
    result = propagate_uncertainty_budget(
        spec, result_id='r2', created_at_utc='2026-09-25T00:02:00+00:00'
    )
    expected = sqrt(0.5**2 + 0.5**2)
    assert isclose(result.combined_standard_uncertainty, expected, rel_tol=1e-9)


def test_sensitivity_scales_contribution():
    spec = _spec((_component('s', standard_uncertainty=0.5, sensitivity=2.0),))
    result = propagate_uncertainty_budget(
        spec, result_id='r2b', created_at_utc='2026-09-25T00:02:00+00:00'
    )
    assert isclose(result.combined_standard_uncertainty, 1.0)


def test_bounded_interval_never_read_as_gaussian():
    spec = _spec(
        (
            _bounded('a', low=-1.0, high=1.0),
            _bounded('b', low=-0.5, high=0.5),
        ),
        propagation_method='bounded_worst_case',
    )
    result = propagate_uncertainty_budget(
        spec, result_id='r3', created_at_utc='2026-09-25T00:03:00+00:00'
    )
    assert isclose(result.combined_bound_half_width, 1.5)
    assert result.combined_standard_uncertainty is None


def test_expanded_requires_coverage_factor():
    with pytest.raises(ValidationError, match='coverage_factor'):
        UncertaintyBudgetResult(
            result_id='r',
            spec_id='s',
            spec_sha256=_H,
            document_id='d',
            measurand='m',
            combination_state='propagated',
            combined_standard_uncertainty=0.5,
            expanded_uncertainty=1.0,
            created_at_utc='t',
            result_sha256=_H,
        )


def test_expanded_from_coverage_factor():
    spec = _spec((_component('a', standard_uncertainty=0.5),))
    result = propagate_uncertainty_budget(
        spec,
        result_id='r3b',
        created_at_utc='2026-09-25T00:03:30+00:00',
        coverage_factor=2.0,
    )
    assert isclose(result.expanded_uncertainty, 1.0)


def test_discrepancy_separate_not_absorbed():
    spec = _spec(
        (
            _component('a', standard_uncertainty=0.2),
            _component(
                'd',
                category='model_discrepancy',
                representation='standard_uncertainty',
                standard_uncertainty=2.0,
            ),
        )
    )
    result = propagate_uncertainty_budget(
        spec, result_id='r4', created_at_utc='2026-09-25T00:04:00+00:00'
    )
    assert isclose(result.combined_standard_uncertainty, 0.2)
    assert isclose(result.model_discrepancy_contribution, 2.0)


def test_empirical_samples_std():
    spec = _spec(
        (
            _component(
                'e',
                representation='empirical_samples',
                standard_uncertainty=None,
                empirical_samples=(0.2, -0.2, 0.1, -0.1),
            ),
        ),
    )
    result = propagate_uncertainty_budget(
        spec, result_id='r5', created_at_utc='2026-09-25T00:05:00+00:00'
    )
    assert result.combined_standard_uncertainty is not None
    assert result.combined_standard_uncertainty > 0.1


def test_declared_only_method_passes_through():
    spec = _spec(
        (_bounded('a', low=-1.0, high=1.0),),
        propagation_method='declared_only',
    )
    result = propagate_uncertainty_budget(
        spec, result_id='r6', created_at_utc='2026-09-25T00:06:00+00:00'
    )
    assert result.combination_state == 'declared_only'
    assert result.combined_standard_uncertainty is None
