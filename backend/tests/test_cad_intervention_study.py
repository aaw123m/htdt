from pathlib import Path

import pytest

from htdt.cad_intervention_study import (
    GuardrailDomain,
    InterventionFinding,
    InterventionMetric,
    build_intervention_study_spec,
)
from htdt.intervention_planner import InterventionPlanner
from htdt.joint_optimization_context import JointOptimizationContext

from test_cad_joint_optimization import DOCUMENT_ID, _fixture


NOW = '2026-09-19T14:00:00+00:00'


def _context(fixture) -> JointOptimizationContext:
    return JointOptimizationContext(
        fixture.scene_repository,
        DOCUMENT_ID,
        objective_repository=fixture.objective_repository,
    )


def _planner(fixture) -> InterventionPlanner:
    return InterventionPlanner(_context(fixture))


def _explicit_finding() -> InterventionFinding:
    return InterventionFinding(
        source_kind='explicit_user_region',
        observable='target_response_error',
        target_band_hz=(55.0, 75.0),
        target_seat_entity_ids=('seat-1',),
        detail='elevated magnitude at MLP',
    )


def _study(planner: InterventionPlanner, **overrides):
    args = dict(
        finding=_explicit_finding(),
        allowed_families=('geometry', 'calibration'),
        fidelity_label='native-model-v1',
        provider_id='room-simulation',
        provider_version='1.0.0',
        candidate_budget=16,
        created_at_utc=NOW,
        guardrail_domain=GuardrailDomain(
            guardrail_bands_hz=((90.0, 120.0),),
            guardrail_observables=('seat_variance',),
        ),
    )
    args.update(overrides)
    return planner.create_study(**args)


def test_create_study_binds_exact_baseline(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    spec = _study(_planner(fixture))

    assert spec is not None
    assert spec.scene_revision_id == fixture.revision.revision_id
    assert spec.scene_content_hash == fixture.revision.content_hash
    assert spec.base_system_variant_id == fixture.base_variant.variant_id
    assert spec.allowed_families == ('geometry', 'calibration')
    # Reuses persisted objective definitions — never redeclared inline.
    assert [d.objective_id for d in spec.metric_definitions]
    # The fixture binds an O90 spec -> bounded robustness policy.
    assert spec.robustness_policy == 'o90_bounded'
    assert spec.spec_sha256
    assert spec.spec_id.startswith('intervention-study-')


def test_study_persists_and_replays(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    planner = _planner(fixture)
    spec = _study(planner)
    assert spec is not None

    loaded = planner.repository.get_spec(spec.spec_id)
    assert loaded == spec
    listed = planner.list_studies(
        scene_revision_id=fixture.revision.revision_id
    )
    assert [item.spec_id for item in listed] == [spec.spec_id]


def test_treatment_family_requires_capability_authority(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    with pytest.raises(ValueError, match='treatment'):
        _study(
            _planner(fixture),
            allowed_families=('treatment',),
        )


def test_alternative_records_diff_metrics_and_regressions(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    planner = _planner(fixture)
    spec = _study(planner)
    assert spec is not None
    objective_id = spec.metric_definitions[0].objective_id

    alternative = planner.record_alternative(
        spec=spec,
        family='geometry',
        diff_summary='move seat +180 mm along x',
        changed_entity_ids=('seat-1',),
        evidence_state='predicted_unvalidated',
        fidelity_label=spec.fidelity_label,
        metrics=(
            InterventionMetric(
                objective_id=objective_id,
                value=-0.4,
                domain='target_roi',
            ),
            InterventionMetric(
                objective_id=objective_id,
                value=0.1,
                domain='guardrail',
            ),
        ),
        generated_authority_ids=(fixture.search_spec.search_spec_id,),
        regressions=('90-120 Hz slightly worse',),
    )
    assert alternative.comparability == 'comparable'
    assert alternative.incomparability_reason is None
    assert alternative.regressions == ('90-120 Hz slightly worse',)

    loaded = planner.list_alternatives(spec.spec_id)
    assert [item.alternative_id for item in loaded] == [
        alternative.alternative_id
    ]


def test_alternative_rejects_undeclared_family_and_metrics(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    planner = _planner(fixture)
    spec = _study(planner)
    assert spec is not None

    with pytest.raises(ValueError, match='treatment'):
        planner.record_alternative(
            spec=spec,
            family='treatment',
            diff_summary='add treatment T-03',
            evidence_state='exploratory',
            fidelity_label=spec.fidelity_label,
            metrics=(),
        )
    with pytest.raises(ValueError, match='not declared'):
        planner.record_alternative(
            spec=spec,
            family='geometry',
            diff_summary='move seat',
            evidence_state='exploratory',
            fidelity_label=spec.fidelity_label,
            metrics=(
                InterventionMetric(
                    objective_id='undeclared-metric', value=1.0
                ),
            ),
        )


def test_cross_family_fidelity_mismatch_stays_incomparable(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    planner = _planner(fixture)
    spec = _study(planner)
    assert spec is not None
    objective_id = spec.metric_definitions[0].objective_id

    alternative = planner.record_alternative(
        spec=spec,
        family='calibration',
        diff_summary='PEQ -5.5 dB @ 63 Hz',
        dsp_parameters=('peq_gain_db',),
        evidence_state='exploratory',
        fidelity_label='approximate-transfer-transform',
        metrics=(
            InterventionMetric(objective_id=objective_id, value=-0.2),
        ),
    )
    assert alternative.comparability == 'incompatible_fidelity'
    assert alternative.incomparability_reason is not None


def test_explicit_finding_rejects_fake_authority(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match='authority'):
        InterventionFinding(
            source_kind='explicit_user_region',
            source_authority_id='prediction-1',
            observable='peak',
        )


def test_create_study_without_baseline_returns_none(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    empty_planner = InterventionPlanner(
        JointOptimizationContext(fixture.scene_repository, 'other-doc')
    )
    assert (
        empty_planner.create_study(
            finding=_explicit_finding(),
            allowed_families=('geometry',),
            fidelity_label='native-model-v1',
            provider_id='room-simulation',
            provider_version='1.0.0',
            candidate_budget=4,
            created_at_utc=NOW,
        )
        is None
    )
