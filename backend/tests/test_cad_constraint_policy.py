from htdt.cad_constraint_models import CadConstraintEvaluation, CadConstraintResult
from htdt.cad_constraint_policy import blocking_candidate_violations


def result(*, result_id: str, passed: bool, actual_m: float | None = None, minimum: float | None = None) -> CadConstraintResult:
    return CadConstraintResult(
        result_id=result_id,
        constraint_id=result_id.split(':')[0],
        kind='wall_clearance',
        name='test',
        entity_ids=('speaker',),
        wall_id='wall:test',
        passed=passed,
        reason_code='test',
        reason_ja='test',
        actual_m=actual_m,
        required_min_m=minimum,
    )


def evaluation(*items: CadConstraintResult) -> CadConstraintEvaluation:
    return CadConstraintEvaluation(
        constraints_satisfied=not any(not item.passed for item in items),
        results=items,
    )


def test_new_violation_blocks_candidate() -> None:
    before = evaluation(result(result_id='clearance:speaker', passed=True, actual_m=0.5, minimum=0.4))
    candidate = evaluation(result(result_id='clearance:speaker', passed=False, actual_m=0.3, minimum=0.4))
    blocked = blocking_candidate_violations(before, candidate, {'speaker'})
    assert [item.result_id for item in blocked] == ['clearance:speaker']


def test_existing_scalar_violation_can_improve_but_not_worsen() -> None:
    before = evaluation(result(result_id='clearance:speaker', passed=False, actual_m=0.2, minimum=0.4))
    improving = evaluation(result(result_id='clearance:speaker', passed=False, actual_m=0.3, minimum=0.4))
    worsening = evaluation(result(result_id='clearance:speaker', passed=False, actual_m=0.1, minimum=0.4))
    assert blocking_candidate_violations(before, improving, {'speaker'}) == ()
    assert len(blocking_candidate_violations(before, worsening, {'speaker'})) == 1


def test_existing_boolean_violation_does_not_trap_object() -> None:
    before_item = CadConstraintResult(
        result_id='walkway:speaker',
        constraint_id='walkway',
        kind='exclusion_region',
        name='walkway',
        entity_ids=('speaker',),
        region_role='walkway',
        passed=False,
        reason_code='walkway',
        reason_ja='walkway',
    )
    candidate_item = before_item.model_copy()
    assert blocking_candidate_violations(
        evaluation(before_item),
        evaluation(candidate_item),
        {'speaker'},
    ) == ()


def test_unrelated_violation_does_not_block_other_entity() -> None:
    violation = CadConstraintResult(
        result_id='walkway:seat',
        constraint_id='walkway',
        kind='exclusion_region',
        name='walkway',
        entity_ids=('seat',),
        region_role='walkway',
        passed=False,
        reason_code='walkway',
        reason_ja='walkway',
    )
    assert blocking_candidate_violations(evaluation(), evaluation(violation), {'speaker'}) == ()
