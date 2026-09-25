from pathlib import Path

import pytest

from htdt.cad_intervention_study import (
    GuardrailDomain,
    InterventionAuthorityRef,
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


def _search_spec_ref(fixture) -> InterventionAuthorityRef:
    return InterventionAuthorityRef(
        authority_kind='search_spec',
        authority_id=fixture.search_spec.search_spec_id,
        authority_sha256=fixture.search_spec.search_spec_sha256,
    )


def _joint_spec_ref(fixture) -> InterventionAuthorityRef:
    return InterventionAuthorityRef(
        authority_kind='joint_optimization_spec',
        authority_id=fixture.spec.spec_id,
        authority_sha256=fixture.spec.semantic_sha256,
    )


def _measurement_ref(fixture) -> InterventionAuthorityRef:
    from htdt.cad_measurement_quality import dataset_sha256

    return InterventionAuthorityRef(
        authority_kind='measurement',
        authority_id=fixture.measurement.measurement_id,
        authority_sha256=dataset_sha256(fixture.dataset),
    )


def _target_metric(fixture, objective_id: str, value: float):
    return InterventionMetric(
        objective_id=objective_id,
        value=value,
        domain='target_roi',
        producer=_search_spec_ref(fixture),
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
            _target_metric(fixture, objective_id, -0.4),
            InterventionMetric(
                objective_id=objective_id,
                value=0.1,
                domain='guardrail',
                observable='seat_variance',
                producer=_search_spec_ref(fixture),
            ),
            *(
                InterventionMetric(
                    objective_id=item.objective_id,
                    state='missing',
                    domain='target_roi',
                )
                for item in spec.metric_definitions[1:]
            ),
        ),
        generated_authorities=(_search_spec_ref(fixture),),
        regressions=('90-120 Hz slightly worse',),
        application_note='apply the recorded variant then re-measure MLP',
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
                    objective_id='undeclared-metric',
                    value=1.0,
                    producer=_search_spec_ref(fixture),
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
            _target_metric(fixture, objective_id, -0.2),
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


def test_application_note_is_hashed_into_identity(tmp_path: Path) -> None:
    """#959: the application note is presentation-only but still hashed."""
    fixture = _fixture(tmp_path)
    planner = _planner(fixture)
    spec = _study(planner)
    assert spec is not None
    objective_id = spec.metric_definitions[0].objective_id
    base_metrics = tuple(
        _target_metric(fixture, item.objective_id, -0.1)
        for item in spec.metric_definitions
    )

    first = planner.record_alternative(
        spec=spec,
        family='geometry',
        diff_summary='move seat +180 mm',
        evidence_state='predicted_unvalidated',
        fidelity_label=spec.fidelity_label,
        metrics=base_metrics,
        application_note='seat move only',
    )
    second = planner.record_alternative(
        spec=spec,
        family='geometry',
        diff_summary='move seat +180 mm',
        evidence_state='predicted_unvalidated',
        fidelity_label=spec.fidelity_label,
        metrics=base_metrics,
        application_note='seat move plus recalibration',
    )
    # Meaningfully different recorded actions cannot share one authority.
    assert first.alternative_id != second.alternative_id
    assert first.alternative_sha256 != second.alternative_sha256
    # The field is hashed but never drives apply semantics.
    assert first.application_note == 'seat move only'


def test_evidence_state_floor_is_enforced(tmp_path: Path) -> None:
    """#960: a preregistered floor rejects weaker caller-asserted states."""
    fixture = _fixture(tmp_path)
    planner = _planner(fixture)
    spec = _study(planner, evidence_state_floor='predicted_validated')
    assert spec is not None

    with pytest.raises(ValueError, match='below the study floor'):
        planner.record_alternative(
            spec=spec,
            family='geometry',
            diff_summary='move seat',
            evidence_state='predicted_unvalidated',
            fidelity_label=spec.fidelity_label,
            metrics=(),
        )
    with pytest.raises(ValueError, match='model-validation'):
        planner.record_alternative(
            spec=spec,
            family='geometry',
            diff_summary='move seat',
            evidence_state='predicted_validated',
            fidelity_label=spec.fidelity_label,
            metrics=(),
        )


def test_measured_verified_requires_measurement_authority(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    planner = _planner(fixture)
    spec = _study(planner)
    assert spec is not None

    with pytest.raises(ValueError, match='measurement'):
        planner.record_alternative(
            spec=spec,
            family='geometry',
            diff_summary='move seat',
            evidence_state='measured_verified',
            fidelity_label=spec.fidelity_label,
            metrics=(),
        )
    alternative = planner.record_alternative(
        spec=spec,
        family='geometry',
        diff_summary='move seat',
        evidence_state='measured_verified',
        fidelity_label=spec.fidelity_label,
        metrics=(
            InterventionMetric(
                objective_id=spec.metric_definitions[0].objective_id,
                state='unsupported',
                domain='target_roi',
            ),
            *(
                InterventionMetric(
                    objective_id=item.objective_id,
                    state='missing',
                    domain='target_roi',
                )
                for item in spec.metric_definitions[1:]
            ),
        ),
        evidence_authorities=(_measurement_ref(fixture),),
    )
    assert alternative.evidence_state == 'measured_verified'


def test_unresolvable_generated_authority_fails_closed(
    tmp_path: Path,
) -> None:
    """#960: generated authorities are typed + hash-pinned + resolved."""
    fixture = _fixture(tmp_path)
    planner = _planner(fixture)
    spec = _study(planner)
    assert spec is not None

    with pytest.raises(ValueError, match='not registered for replay'):
        planner.record_alternative(
            spec=spec,
            family='geometry',
            diff_summary='move seat',
            evidence_state='exploratory',
            fidelity_label=spec.fidelity_label,
            metrics=(),
            generated_authorities=(
                InterventionAuthorityRef(
                    authority_kind='search_spec',
                    authority_id='ghost-spec',
                    authority_sha256='0' * 64,
                ),
            ),
        )


def test_metric_producer_must_resolve(tmp_path: Path) -> None:
    """#960: an available metric pins the authority that produced it."""
    fixture = _fixture(tmp_path)
    planner = _planner(fixture)
    spec = _study(planner)
    assert spec is not None

    with pytest.raises(ValueError, match='not registered for replay'):
        planner.record_alternative(
            spec=spec,
            family='geometry',
            diff_summary='move seat',
            evidence_state='exploratory',
            fidelity_label=spec.fidelity_label,
            metrics=(
                InterventionMetric(
                    objective_id=spec.metric_definitions[0].objective_id,
                    value=-0.4,
                    producer=InterventionAuthorityRef(
                        authority_kind='objective_evaluation',
                        authority_id='evaluation-not-stored',
                        authority_sha256='1' * 64,
                    ),
                ),
            ),
        )


def test_finding_source_must_resolve(tmp_path: Path) -> None:
    """#960: a source-kind-pinned finding resolves the exact authority."""
    fixture = _fixture(tmp_path)
    planner = _planner(fixture)
    finding = InterventionFinding(
        source_kind='measurement',
        source_authority_id=fixture.measurement.measurement_id,
        source_authority_sha256='2' * 64,
        observable='target_response_error',
    )
    with pytest.raises(ValueError, match='not registered for replay'):
        _study(planner, finding=finding)


def test_target_only_metrics_are_not_complete(tmp_path: Path) -> None:
    """#962: declared guardrail cells missing -> partial, not comparable."""
    fixture = _fixture(tmp_path)
    planner = _planner(fixture)
    spec = _study(planner)
    assert spec is not None
    assert spec.guardrail_domain.guardrail_bands_hz
    assert spec.guardrail_domain.guardrail_observables

    alternative = planner.record_alternative(
        spec=spec,
        family='geometry',
        diff_summary='move seat',
        evidence_state='exploratory',
        fidelity_label=spec.fidelity_label,
        metrics=(
            _target_metric(
                fixture, spec.metric_definitions[0].objective_id, -0.4
            ),
        ),
    )
    assert alternative.comparability == 'comparable'
    assert alternative.evaluation_coverage == 'partial'
    assert not alternative.quantitatively_comparable


def test_empty_metrics_blocked_and_complete_coverage_comparable(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    planner = _planner(fixture)
    spec = _study(planner)
    assert spec is not None

    empty = planner.record_alternative(
        spec=spec,
        family='geometry',
        diff_summary='unevaluated alternative',
        evidence_state='exploratory',
        fidelity_label=spec.fidelity_label,
        metrics=(),
    )
    assert empty.evaluation_coverage == 'blocked'
    assert not empty.quantitatively_comparable

    full = planner.record_alternative(
        spec=spec,
        family='geometry',
        diff_summary='fully evaluated alternative',
        evidence_state='exploratory',
        fidelity_label=spec.fidelity_label,
        metrics=(
            *(
                _target_metric(fixture, item.objective_id, -0.2)
                for item in spec.metric_definitions
            ),
            InterventionMetric(
                objective_id=spec.metric_definitions[0].objective_id,
                value=0.05,
                domain='guardrail',
                band_hz=spec.guardrail_domain.guardrail_bands_hz[0],
                producer=_search_spec_ref(fixture),
            ),
            InterventionMetric(
                objective_id=spec.metric_definitions[0].objective_id,
                value=0.02,
                domain='guardrail',
                observable=spec.guardrail_domain.guardrail_observables[0],
                producer=_search_spec_ref(fixture),
            ),
        ),
    )
    assert full.evaluation_coverage == 'complete'
    assert full.quantitatively_comparable


def test_guardrail_metric_requires_coordinates(tmp_path: Path) -> None:
    """#962: guardrail metrics must bind an exact domain coordinate."""
    fixture = _fixture(tmp_path)
    with pytest.raises(ValueError, match='band, seat or observable'):
        InterventionMetric(
            objective_id='fixture.response_error_db',
            value=0.1,
            domain='guardrail',
            producer=_search_spec_ref(fixture),
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
