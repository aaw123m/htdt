from pathlib import Path

import pytest

from htdt.cad_treatment_search import (
    TreatmentCandidateSlot,
    TreatmentHardConstraints,
    TreatmentSearchSpace,
    TreatmentSolverCapability,
    build_treatment_search_spec,
    candidate_hard_violations,
    enumerate_treatment_candidates,
)
from htdt.optimization_objectives import ObjectiveDefinition

from test_cad_joint_optimization import _fixture


NOW = '2026-09-19T15:00:00+00:00'


def _space(**overrides) -> TreatmentSearchSpace:
    args = dict(
        allowed_definition_ids=('absorber-a', 'absorber-b'),
        candidate_slots=(
            TreatmentCandidateSlot(
                host_surface_id='front-wall', region_id='cell-1'
            ),
            TreatmentCandidateSlot(
                host_surface_id='front-wall', region_id='cell-2'
            ),
            TreatmentCandidateSlot(host_surface_id='left-wall'),
        ),
        max_treatment_count=2,
    )
    args.update(overrides)
    return TreatmentSearchSpace(**args)


def _capability() -> TreatmentSolverCapability:
    return TreatmentSolverCapability(
        provider_id='room-simulation',
        provider_version='1.0.0',
        supported_physics=('absorption', 'boundary_impedance'),
        usable_band_hz=(20.0, 500.0),
    )


def _spec(fixture, **overrides):
    args = dict(
        scene_revision=fixture.revision,
        base_variant=fixture.base_variant,
        space=_space(),
        solver_capability=_capability(),
        metric_definitions=[
            ObjectiveDefinition.legacy(
                objective_id='seat_variance_db', unit='dB', direction='minimize'
            ),
            ObjectiveDefinition.legacy(
                objective_id='treatment_cost', unit='JPY', direction='minimize'
            ),
        ],
        candidate_budget=500,
        created_at_utc=NOW,
    )
    args.update(overrides)
    return build_treatment_search_spec(**args)


def test_spec_binds_baseline_and_self_hashes(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    spec = _spec(fixture)

    assert spec.scene_revision_id == fixture.revision.revision_id
    assert spec.base_system_variant_id == fixture.base_variant.variant_id
    assert spec.spec_id.startswith('treatment-search-')
    assert len(spec.metric_definitions) == 2
    assert spec.solver_capability.supported_physics == (
        'absorption',
        'boundary_impedance',
    )


def test_enumeration_is_deterministic_and_bounded(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    spec = _spec(fixture)

    first = enumerate_treatment_candidates(spec)
    second = enumerate_treatment_candidates(spec)
    assert [c.candidate_id for c in first] == [
        c.candidate_id for c in second
    ]
    # 1 empty + 3 slots*2 defs + C(3,2)*4 = 1 + 6 + 12 = 19 members.
    assert len(first) == 19
    for candidate in first:
        assert candidate.spec_sha256 == spec.spec_sha256
        for assignment in candidate.assignments:
            assert assignment.definition_id in (
                'absorber-a',
                'absorber-b',
            )

    capped = _spec(fixture, candidate_budget=5)
    assert len(enumerate_treatment_candidates(capped)) == 5
    no_empty = _spec(fixture, space=_space(include_empty_baseline=False))
    assert all(
        c.assignments for c in enumerate_treatment_candidates(no_empty)
    )


def test_hard_constraints_flag_infeasible_evidence(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    spec = _spec(
        fixture,
        hard_constraints=TreatmentHardConstraints(
            max_coverage_m2=4.0,
            budget_ceiling=100.0,
            cost_scenario_id='cost-scenario-abc',
            cost_scenario_sha256='a' * 64,
        ),
    )
    candidates = enumerate_treatment_candidates(spec)
    full = next(c for c in candidates if len(c.assignments) == 2)

    violations = candidate_hard_violations(
        full,
        spec=spec,
        definition_coverage_m2={'absorber-a': 2.5, 'absorber-b': 2.5},
        definition_cost={'absorber-a': 60.0, 'absorber-b': 60.0},
    )
    assert any('coverage' in item for item in violations)
    assert any('cost' in item for item in violations)

    assert (
        candidate_hard_violations(
            next(c for c in candidates if not c.assignments), spec=spec
        )
        == []
    )
    assert 'belong to this search spec' in candidate_hard_violations(
        full, spec=_spec(fixture, candidate_budget=5)
    )[0]


def test_spec_rejects_excluded_surface_in_space(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    with pytest.raises(ValueError, match='excluded'):
        _spec(
            fixture,
            hard_constraints=TreatmentHardConstraints(
                excluded_surface_ids=('front-wall',),
            ),
        )


def test_space_rejects_unbounded_count_and_duplicates(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match='slot count'):
        _space(max_treatment_count=4)
    with pytest.raises(ValueError, match='unique'):
        _space(allowed_definition_ids=('a', 'a'))


def test_capability_band_must_be_ordered_range() -> None:
    with pytest.raises(ValueError, match='band'):
        TreatmentSolverCapability(
            provider_id='room-simulation',
            provider_version='1.0.0',
            supported_physics=('absorption',),
            usable_band_hz=(500.0, 20.0),
        )
