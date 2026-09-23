from __future__ import annotations

from .cad_constraint_models import CadConstraintEvaluation, CadConstraintResult

_EPS = 1e-9


def scalar_violation_amount(result: CadConstraintResult) -> float | None:
    """Return scalar distance outside the accepted range when one is available."""

    actual = result.actual_m
    if actual is None:
        return None
    shortfall = 0.0
    if result.required_min_m is not None:
        shortfall = max(shortfall, result.required_min_m - actual)
    if result.required_max_m is not None:
        shortfall = max(shortfall, actual - result.required_max_m)
    return max(0.0, float(shortfall))


def blocking_candidate_violations(
    before: CadConstraintEvaluation,
    candidate: CadConstraintEvaluation,
    changed_entity_ids: tuple[str, ...] | list[str] | set[str],
) -> tuple[CadConstraintResult, ...]:
    """Return hard violations that should block a candidate transform.

    A new violation involving a moved entity always blocks the commit. Existing
    boolean region violations are allowed to remain temporarily so a user can
    drag an already-invalid object out of the region. Existing scalar distance
    violations may improve or stay equal, but a larger shortfall is blocked.
    This prevents the constraint layer from trapping an object that starts in an
    invalid state while still preventing silent introduction/worsening of hard
    distance violations.
    """

    changed = set(changed_entity_ids)
    if not changed:
        return ()
    before_violations = {item.result_id: item for item in before.violations}
    blocked: list[CadConstraintResult] = []
    for item in candidate.violations:
        if changed.isdisjoint(item.entity_ids):
            continue
        previous = before_violations.get(item.result_id)
        if previous is None:
            blocked.append(item)
            continue
        previous_amount = scalar_violation_amount(previous)
        candidate_amount = scalar_violation_amount(item)
        if previous_amount is not None and candidate_amount is not None:
            if candidate_amount > previous_amount + _EPS:
                blocked.append(item)
    return tuple(blocked)
