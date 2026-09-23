from __future__ import annotations

from math import sqrt

import pytest

from htdt.comparison import FrequencyResponse
from htdt.optimization_objectives import (
    ObjectiveDefinition,
    ObjectiveError,
    ObjectiveMetric,
    ObjectiveValidDomain,
    ObjectiveVector,
    ResponseObjectiveSpec,
    merge_objective_vectors,
    movement_objectives,
    pair_response_objectives,
    seat_pairwise_objectives,
    target_response_objectives,
)
from htdt.pareto import ParetoError, dominates, pareto_front


def response(levels: tuple[float, ...]) -> FrequencyResponse:
    return FrequencyResponse(
        frequency_hz=(20.0, 40.0, 80.0, 160.0),
        level_db=levels,
    )


def test_target_response_keeps_level_shape_peak_and_dip_independent() -> None:
    spec = ResponseObjectiveSpec(
        low_hz=20.0,
        high_hz=160.0,
        reference_band_hz=(20.0, 160.0),
    )
    vector = target_response_objectives(
        'candidate-a',
        response((3.0, 3.0, 3.0, 3.0)),
        response((0.0, 0.0, 0.0, 0.0)),
        spec,
    )

    assert vector.metric('response.rms_difference_db').value == pytest.approx(3.0)
    assert vector.metric('response.peak_excess_db').value == pytest.approx(3.0)
    assert vector.metric('response.dip_deficit_db').value == pytest.approx(0.0)
    assert vector.metric('response.shape_rms_db').value == pytest.approx(0.0)


def test_pair_and_seat_objectives_do_not_collapse_to_single_score() -> None:
    aligned = ResponseObjectiveSpec(
        low_hz=20.0,
        high_hz=160.0,
        reference_band_hz=(20.0, 160.0),
    )
    pair = pair_response_objectives(
        'candidate-a',
        response((1.0, 1.0, 1.0, 1.0)),
        response((-1.0, -1.0, -1.0, -1.0)),
        aligned,
        prefix='left_right',
    )
    assert pair.metric('left_right.rms_difference_db').value == pytest.approx(2.0)
    assert pair.metric('left_right.shape_rms_db').value == pytest.approx(0.0)

    seat = seat_pairwise_objectives(
        'candidate-a',
        (
            response((0.0, 0.0, 0.0, 0.0)),
            response((1.0, 1.0, 1.0, 1.0)),
            response((-1.0, -1.0, -1.0, -1.0)),
        ),
        aligned,
    )
    assert seat.metric('seat.pairwise_rms_difference_max_db').value == pytest.approx(2.0)
    assert seat.metric('seat.pairwise_rms_difference_rms_db').value == pytest.approx(sqrt(2.0))
    assert seat.metric('seat.pairwise_shape_max_db').value == pytest.approx(0.0)
    assert seat.metric('seat.pairwise_shape_rms_db').value == pytest.approx(0.0)


def test_movement_objectives_report_total_and_max_separately() -> None:
    vector = movement_objectives(
        'candidate-a',
        {
            'speaker': {'x_m': 0.0, 'y_m': 0.0, 'z_m': 0.0},
            'seat': {'x_m': 1.0, 'y_m': 1.0, 'z_m': 0.0},
        },
        {
            'speaker': {'x_m': 3.0, 'y_m': 4.0, 'z_m': 0.0},
            'seat': {'x_m': 1.0, 'y_m': 1.0, 'z_m': 12.0},
        },
    )
    assert vector.metric('movement.total_m').value == pytest.approx(17.0)
    assert vector.metric('movement.max_m').value == pytest.approx(12.0)


def test_merge_requires_same_candidate_and_unique_objective_ids() -> None:
    first = ObjectiveVector(
        candidate_id='a',
        metrics=(ObjectiveMetric(objective_id='x', value=1.0, unit='dB'),),
    )
    second = ObjectiveVector(
        candidate_id='a',
        metrics=(ObjectiveMetric(objective_id='y', value=2.0, unit='m'),),
    )
    merged = merge_objective_vectors('a', first, second)
    assert [metric.objective_id for metric in merged.metrics] == ['x', 'y']

    with pytest.raises(ObjectiveError):
        merge_objective_vectors('other', first)
    with pytest.raises(ObjectiveError):
        merge_objective_vectors('a', first, first)


def vector(candidate_id: str, x: float, y: float) -> ObjectiveVector:
    return ObjectiveVector(
        candidate_id=candidate_id,
        metrics=(
            ObjectiveMetric(objective_id='x', value=x, unit='u'),
            ObjectiveMetric(objective_id='y', value=y, unit='u'),
        ),
    )


def test_pareto_front_keeps_known_non_dominated_set_in_input_order() -> None:
    result = pareto_front((
        vector('a', 1.0, 4.0),
        vector('b', 2.0, 2.0),
        vector('c', 4.0, 1.0),
        vector('d', 3.0, 3.0),
        vector('e', 2.0, 2.0),
    ))

    assert result.non_dominated_candidate_ids == ('a', 'b', 'c', 'e')
    assert result.dominated_by['d'] == ('b', 'e')
    assert result.dominated_by['b'] == ()
    assert dominates((1.0, 1.0), (1.0, 2.0))
    assert not dominates((1.0, 1.0), (1.0, 1.0))


def test_pareto_rejects_missing_objective_or_duplicate_candidate() -> None:
    with pytest.raises(ParetoError):
        pareto_front((
            vector('a', 1.0, 2.0),
            ObjectiveVector(
                candidate_id='b',
                metrics=(ObjectiveMetric(objective_id='x', value=1.0, unit='u'),),
            ),
        ))

    with pytest.raises(ParetoError):
        pareto_front((vector('a', 1.0, 2.0), vector('a', 2.0, 1.0)))



def _explicit_definition(
    objective_id: str,
    *,
    quantity: str,
    unit: str,
    direction: str,
    model_id: str = 'fixture-physical-model',
    model_version: str = '1',
    minimum: float | None = None,
    maximum: float | None = None,
) -> ObjectiveDefinition:
    domain = (
        ObjectiveValidDomain(kind='finite_real')
        if minimum is None and maximum is None
        else ObjectiveValidDomain(
            kind='bounded_real',
            minimum=minimum,
            maximum=maximum,
        )
    )
    return ObjectiveDefinition(
        objective_id=objective_id,
        quantity=quantity,
        unit=unit,
        direction=direction,
        valid_domain=domain,
        comparison_model_id=model_id,
        comparison_model_version=model_version,
    )


def test_mixed_minimize_maximize_pareto_uses_physical_direction_without_sign_flip() -> None:
    error = _explicit_definition(
        'fixture.error',
        quantity='response_error',
        unit='dB',
        direction='minimize',
        minimum=0.0,
    )
    coverage = _explicit_definition(
        'fixture.coverage',
        quantity='coverage_fraction',
        unit='1',
        direction='maximize',
        minimum=0.0,
        maximum=1.0,
    )

    def mixed(candidate_id: str, error_value: float, coverage_value: float) -> ObjectiveVector:
        return ObjectiveVector(
            candidate_id=candidate_id,
            metrics=(
                ObjectiveMetric(
                    objective_id=error.objective_id,
                    value=error_value,
                    unit=error.unit,
                    direction=error.direction,
                    definition=error,
                ),
                ObjectiveMetric(
                    objective_id=coverage.objective_id,
                    value=coverage_value,
                    unit=coverage.unit,
                    direction=coverage.direction,
                    definition=coverage,
                ),
            ),
        )

    result = pareto_front((
        mixed('a', 1.0, 0.70),
        mixed('b', 2.0, 0.90),
        mixed('c', 3.0, 0.60),
    ))

    assert result.algorithm_version == 'pareto-front-2'
    assert result.non_dominated_candidate_ids == ('a', 'b')
    assert result.dominated_by['c'] == ('a', 'b')


@pytest.mark.parametrize(
    ('second_definition', 'second_unit', 'second_direction'),
    (
        (
            _explicit_definition(
                'fixture.metric',
                quantity='error',
                unit='dB',
                direction='minimize',
                model_id='other-model',
            ),
            'dB',
            'minimize',
        ),
        (
            _explicit_definition(
                'fixture.metric',
                quantity='error',
                unit='Pa',
                direction='minimize',
            ),
            'Pa',
            'minimize',
        ),
        (
            _explicit_definition(
                'fixture.metric',
                quantity='error',
                unit='dB',
                direction='maximize',
            ),
            'dB',
            'maximize',
        ),
    ),
)
def test_pareto_rejects_incompatible_definition_unit_direction_or_model(
    second_definition: ObjectiveDefinition,
    second_unit: str,
    second_direction: str,
) -> None:
    first_definition = _explicit_definition(
        'fixture.metric',
        quantity='error',
        unit='dB',
        direction='minimize',
    )
    first = ObjectiveVector(
        candidate_id='a',
        metrics=(
            ObjectiveMetric(
                objective_id='fixture.metric',
                value=1.0,
                unit='dB',
                direction='minimize',
                definition=first_definition,
            ),
        ),
    )
    second = ObjectiveVector(
        candidate_id='b',
        metrics=(
            ObjectiveMetric(
                objective_id='fixture.metric',
                value=2.0,
                unit=second_unit,
                direction=second_direction,
                definition=second_definition,
            ),
        ),
    )

    with pytest.raises(ParetoError, match='definition/unit/direction/model mismatch'):
        pareto_front((first, second))


def test_missing_and_unsupported_objectives_have_no_numeric_substitute() -> None:
    definition = _explicit_definition(
        'fixture.coverage',
        quantity='coverage_fraction',
        unit='1',
        direction='maximize',
        minimum=0.0,
        maximum=1.0,
    )
    unavailable = ObjectiveMetric(
        objective_id=definition.objective_id,
        value=None,
        unit=definition.unit,
        direction=definition.direction,
        state='unsupported',
        definition=definition,
    )
    assert unavailable.value is None

    available = ObjectiveMetric(
        objective_id=definition.objective_id,
        value=0.8,
        unit=definition.unit,
        direction=definition.direction,
        definition=definition,
    )
    with pytest.raises(ParetoError, match='not comparison-eligible: unsupported'):
        pareto_front((
            ObjectiveVector(candidate_id='a', metrics=(available,)),
            ObjectiveVector(candidate_id='b', metrics=(unavailable,)),
        ))


def test_objective_definition_identity_is_deterministic_and_domain_is_enforced() -> None:
    first = _explicit_definition(
        'fixture.coverage',
        quantity='coverage_fraction',
        unit='1',
        direction='maximize',
        minimum=0.0,
        maximum=1.0,
    )
    second = _explicit_definition(
        'fixture.coverage',
        quantity='coverage_fraction',
        unit='1',
        direction='maximize',
        minimum=0.0,
        maximum=1.0,
    )

    assert first.definition_id == second.definition_id
    assert first.semantic_hash == second.semantic_hash
    with pytest.raises(ValueError, match='outside its declared valid domain'):
        ObjectiveMetric(
            objective_id=first.objective_id,
            value=1.1,
            unit=first.unit,
            direction=first.direction,
            definition=first,
        )
    with pytest.raises(ValueError, match='requires an explicit ObjectiveDefinition'):
        ObjectiveMetric(
            objective_id='fixture.implicit-max',
            value=1.0,
            unit='1',
            direction='maximize',
        )


def test_existing_minimize_only_pareto_retains_legacy_algorithm_identity() -> None:
    result = pareto_front((vector('a', 1.0, 2.0), vector('b', 2.0, 1.0)))
    assert result.algorithm_version == 'pareto-front-1'
