"""Regression tests for the REV55 accuracy-envelope benchmark (#566)."""

from __future__ import annotations

import math

import pytest

from htdt.acoustic_validation_envelope import (
    ANALYTIC_FIXTURE_POLICY_V1,
    FixtureExpectedSample,
    HybridOverlapSample,
    MeasuredCorpusSlot,
    ObservableThresholdRule,
    REV55_ANALYTIC_FIXTURES,
    ValidationFixtureRecord,
    build_accuracy_envelope,
    build_benchmark_report,
    build_corpus_slot,
    build_threshold_policy,
    build_validation_fixture,
    derive_validation_state,
    direct_arrival_time_s,
    error_statistic,
    evaluate_fixture_observations,
    evaluate_hybrid_boundary,
    free_field_level_drop_db,
    image_source_path,
    rectangular_mode_frequency_hz,
    sabine_decay_time_s,
)
from htdt.canonical_json import canonical_sha256


def _fixture(fixture_id_prefix: str) -> ValidationFixtureRecord:
    return next(
        f for f in REV55_ANALYTIC_FIXTURES
        if f.fixture_id.startswith(fixture_id_prefix)
    )


def _expected_map(fixture: ValidationFixtureRecord) -> dict[str, float]:
    return {s.sample_key: s.expected_value for s in fixture.expected_samples}


def _populated_slot(**overrides) -> MeasuredCorpusSlot:
    """A populated corpus slot with its hash computed like the builders do."""

    payload = dict(
        slot_id='MC-01',
        state='populated',
        geometry_provenance='laser scan 2026-01-01',
        equipment_provenance='NTi XL2 #1234',
        calibration_provenance='cal cert 2025-12',
        registration_provenance='fiducial registration log',
        processing_provenance='ir-analysis-2',
        environment_provenance='21.0 C / 50 %RH logged',
        measured_observations=(
            FixtureExpectedSample(
                sample_key='rt60@500',
                observable='decay_time_s',
                unit='s',
                expected_value=0.42,
            ),
        ),
        measurement_evidence_sha256='a' * 64,
    )
    payload.update(overrides)
    provisional = MeasuredCorpusSlot.model_construct(
        **dict(payload, slot_sha256='0' * 64)
    )
    return MeasuredCorpusSlot(
        **payload, slot_sha256=canonical_sha256(provisional.identity_payload())
    )


# ---------------------------------------------------------------------------
# Analytic reference functions — expectations derived in-code (#566 §A)
# ---------------------------------------------------------------------------


class TestAnalyticReferences:
    def test_rectangular_mode_frequency_matches_closed_form(self):
        # f = c/2 * sqrt((nx/Lx)^2 + (ny/Ly)^2 + (nz/Lz)^2)
        Lx, Ly, Lz = 6.0, 4.0, 3.0
        c = 343.0
        assert rectangular_mode_frequency_hz(
            Lx, Ly, Lz, 1, 0, 0, c
        ) == pytest.approx(c / 2.0 / Lx)
        f210 = rectangular_mode_frequency_hz(Lx, Ly, Lz, 2, 1, 0, c)
        expected = (c / 2.0) * math.sqrt((2 / Lx) ** 2 + (1 / Ly) ** 2)
        assert f210 == pytest.approx(expected)

    def test_free_field_drop_is_6db_per_doubling(self):
        # 20*log10(2) ≈ 6.02 dB — the canonical −6 dB/doubling law.
        drop = free_field_level_drop_db(1.0, 2.0)
        assert drop == pytest.approx(20.0 * math.log10(2.0))
        assert drop == pytest.approx(6.02, abs=0.01)
        # Four doublings ≈ 24.08 dB.
        assert free_field_level_drop_db(1.0, 16.0) == pytest.approx(
            24.0, abs=0.1
        )

    def test_direct_arrival_time(self):
        assert direct_arrival_time_s(3.43, 343.0) == pytest.approx(0.01)

    def test_image_source_path_against_pythagoras(self):
        # source x=1, receiver x=4, plane x=0 → image at x=-1, path 5 m.
        path, point = image_source_path(
            (1.0, 2.0, 1.5), (4.0, 2.0, 1.5), plane_axis=0, plane_coord_m=0.0
        )
        expected = math.dist((-1.0, 2.0, 1.5), (4.0, 2.0, 1.5))
        assert path == pytest.approx(expected)
        assert path == pytest.approx(5.0)
        assert point[0] == pytest.approx(0.0)

    def test_sabine_decay_time_formula(self):
        # T60 = 55.3 * V / (c * A); V=72 m³, A=6 m² → ≈1.94 s at c=343.
        t = sabine_decay_time_s(72.0, 6.0, 343.0)
        assert t == pytest.approx(55.3 * 72.0 / (343.0 * 6.0))
        assert t == pytest.approx(1.935, abs=0.01)


# ---------------------------------------------------------------------------
# Fixture records — taxonomy + fail-closed validators
# ---------------------------------------------------------------------------


def _fixture_kwargs(**overrides):
    base = dict(
        fixture_id='VAL99',
        fixture_class='analytic_semi_analytic',
        title='test fixture',
        description='test fixture for validators',
        domain_applicability='synthetic',
        solver_consumer='any',
        reference_kind='analytical_closed_form',
        reference_description='closed form',
        expected_samples=(
            dict(
                sample_key='k1',
                observable='direct_arrival_time_s',
                unit='s',
                expected_value=0.01,
            ),
        ),
    )
    base.update(overrides)
    return base


class TestFixtureRecords:
    def test_analytic_fixture_builds_sealed(self):
        record = build_validation_fixture(**_fixture_kwargs())
        assert record.fixture_sha256

    def test_self_comparison_requires_declared_limitation(self):
        with pytest.raises(ValueError, match='independence'):
            build_validation_fixture(
                **_fixture_kwargs(
                    fixture_class='numerical_reference',
                    reference_kind='self_comparison_declared_limitation',
                )
            )

    def test_self_comparison_with_limitation_builds(self):
        record = build_validation_fixture(
            **_fixture_kwargs(
                fixture_class='numerical_reference',
                reference_kind='self_comparison_declared_limitation',
                reference_independence='same_algorithm_declared_limitation',
                independence_limitation='same solver — implementation check only',
            )
        )
        assert record.reference_independence == (
            'same_algorithm_declared_limitation'
        )

    def test_hybrid_class_requires_overlap_band(self):
        with pytest.raises(ValueError, match='overlap'):
            build_validation_fixture(
                **_fixture_kwargs(fixture_class='hybrid_boundary')
            )

    def test_corpus_class_requires_slot(self):
        with pytest.raises(ValueError, match='corpus'):
            build_validation_fixture(
                **_fixture_kwargs(
                    fixture_class='measured_room_corpus',
                    reference_kind='measured_corpus',
                )
            )

    def test_corpus_class_requires_measured_reference_kind(self):
        with pytest.raises(ValueError, match='measured_corpus'):
            build_validation_fixture(
                **_fixture_kwargs(
                    fixture_class='measured_room_corpus',
                    corpus_slot=build_corpus_slot(slot_id='s1'),
                )
            )


# ---------------------------------------------------------------------------
# Corpus slots — fail closed without real measurements (#566 §D)
# ---------------------------------------------------------------------------


class TestCorpusSlots:
    def test_empty_slot_forbids_observations(self):
        with pytest.raises(ValueError, match='measured'):
            MeasuredCorpusSlot(
                slot_id='MC-01',
                state='empty_unknown',
                measured_observations=(
                    dict(
                        sample_key='x',
                        observable='decay_time_s',
                        unit='s',
                        expected_value=0.4,
                    ),
                ),
                slot_sha256='0' * 64,
            )

    def test_empty_slot_forbids_evidence_hash(self):
        with pytest.raises(ValueError, match='evidence'):
            MeasuredCorpusSlot(
                slot_id='MC-01',
                state='empty_unknown',
                measurement_evidence_sha256='a' * 64,
                slot_sha256='0' * 64,
            )

    def test_populated_slot_requires_full_provenance(self):
        for key in (
            'geometry_provenance',
            'equipment_provenance',
            'calibration_provenance',
            'registration_provenance',
            'processing_provenance',
            'environment_provenance',
        ):
            with pytest.raises(ValueError):
                _populated_slot(**{key: None})

    def test_populated_slot_requires_evidence_hash(self):
        with pytest.raises(ValueError):
            _populated_slot(measurement_evidence_sha256=None)

    def test_populated_slot_happy_path(self):
        slot = _populated_slot()
        assert slot.state == 'populated'

    def test_builder_creates_empty_slot(self):
        slot = build_corpus_slot(slot_id='MC-02')
        assert slot.state == 'empty_unknown'


# ---------------------------------------------------------------------------
# Threshold policy — versioned, justified, no universal ±dB (#566 §5)
# ---------------------------------------------------------------------------


class TestThresholdPolicy:
    def test_rule_requires_rationale(self):
        with pytest.raises(ValueError):
            ObservableThresholdRule(
                observable='reflection_level_db',
                scope='synthetic_fixture',
                limit=1.0,
                unit='dB',
                rationale='short',
            )

    def test_none_limit_means_distribution_only(self):
        policy = build_threshold_policy(
            policy_id='P',
            policy_revision=1,
            rules=(
                dict(
                    observable='reflection_level_db',
                    scope='synthetic_fixture',
                    limit=None,
                    unit='dB',
                    rationale='no defensible limit — report distribution only',
                ),
            ),
        )
        assert policy.rules[0].limit is None

    def test_policy_is_sealed(self):
        assert ANALYTIC_FIXTURE_POLICY_V1.policy_sha256


# ---------------------------------------------------------------------------
# Evaluation — honest pass/fail/insufficient aggregation
# ---------------------------------------------------------------------------


class TestFixtureEvaluation:
    def test_perfect_observations_pass(self):
        fixture = _fixture('VAL10')
        result = evaluate_fixture_observations(
            fixture, _expected_map(fixture), ANALYTIC_FIXTURE_POLICY_V1
        )
        assert result.overall_gate == 'pass'
        assert all(item.gate == 'pass' for item in result.evaluations)

    def test_wrong_physics_fails(self):
        fixture = _fixture('VAL10')
        observed = {k: 99.0 for k in _expected_map(fixture)}
        result = evaluate_fixture_observations(
            fixture, observed, ANALYTIC_FIXTURE_POLICY_V1
        )
        assert result.overall_gate == 'fail'

    def test_missing_observations_are_insufficient_not_pass(self):
        fixture = _fixture('VAL20')
        result = evaluate_fixture_observations(
            fixture, {}, ANALYTIC_FIXTURE_POLICY_V1
        )
        assert result.overall_gate == 'insufficient'

    def test_no_threshold_rule_yields_insufficient(self):
        # An observable with no policy rule must not silently pass.
        fixture = _fixture('VAL10')
        policy = build_threshold_policy(
            policy_id='EMPTY',
            policy_revision=1,
            rules=(
                dict(
                    observable='modal_frequency_hz',
                    scope='synthetic_fixture',
                    metric='max_abs',
                    limit=1e-9,
                    unit='Hz',
                    rationale='unrelated rule for another observable',
                ),
            ),
        )
        result = evaluate_fixture_observations(
            fixture, _expected_map(fixture), policy
        )
        assert result.overall_gate == 'insufficient'
        assert all(item.gate == 'insufficient' for item in result.evaluations)


class TestErrorStatistic:
    def test_statistic_fields(self):
        stat = error_statistic('reflection_level_db', 'dB', [1.0, -2.0, 0.5])
        assert stat.sample_count == 3
        assert stat.max_abs_error == pytest.approx(2.0)
        assert stat.mean_abs_error == pytest.approx(7.0 / 6.0)
        assert stat.rms_error == pytest.approx(
            math.sqrt((1.0 + 4.0 + 0.25) / 3.0)
        )

    def test_empty_errors_are_zero_sample_not_crash(self):
        stat = error_statistic('reflection_level_db', 'dB', [])
        assert stat.sample_count == 0
        assert stat.max_abs_error is None


# ---------------------------------------------------------------------------
# Hybrid boundary evaluation (#566 §C)
# ---------------------------------------------------------------------------


class TestHybridBoundary:
    def _samples(self):
        return (
            HybridOverlapSample(
                frequency_hz=200.0, wave_level_db=80.0,
                geometric_level_db=80.1,
            ),
            HybridOverlapSample(
                frequency_hz=250.0, wave_level_db=82.0,
                geometric_level_db=82.2,
            ),
        )

    def _limits(self):
        return dict(
            max_allowed_overlap_delta_db=0.5,
            max_allowed_transition_jump_db=0.5,
            max_allowed_timing_gap_s=0.005,
        )

    def test_consistent_overlap_passes(self):
        report = evaluate_hybrid_boundary(
            overlap_band_hz=(150.0, 300.0),
            overlap_samples=self._samples(),
            transition_jump_db=0.3,
            timing_gap_s=0.001,
            **self._limits(),
        )
        assert report.state == 'consistent'
        assert 'overlap_consistent' in report.gates

    def test_transition_discontinuity_detected(self):
        report = evaluate_hybrid_boundary(
            overlap_band_hz=(150.0, 300.0),
            overlap_samples=self._samples(),
            transition_jump_db=3.0,
            timing_gap_s=0.001,
            **self._limits(),
        )
        assert report.state == 'discontinuity_detected'
        assert report.transition_level_jump_db == pytest.approx(3.0)

    def test_overlap_delta_detected(self):
        report = evaluate_hybrid_boundary(
            overlap_band_hz=(150.0, 300.0),
            overlap_samples=(
                HybridOverlapSample(
                    frequency_hz=200.0, wave_level_db=80.0,
                    geometric_level_db=90.0,
                ),
            ),
            transition_jump_db=0.1,
            timing_gap_s=0.001,
            **self._limits(),
        )
        assert report.state == 'discontinuity_detected'

    def test_missing_limits_raise(self):
        with pytest.raises(ValueError):
            evaluate_hybrid_boundary(
                overlap_band_hz=(150.0, 300.0),
                overlap_samples=self._samples(),
                transition_jump_db=0.1,
                timing_gap_s=0.001,
                max_allowed_overlap_delta_db=float('nan'),
                max_allowed_transition_jump_db=0.5,
                max_allowed_timing_gap_s=0.005,
            )

    def test_unsampled_band_is_insufficient(self):
        report = evaluate_hybrid_boundary(
            overlap_band_hz=(150.0, 300.0),
            overlap_samples=(),
            transition_jump_db=0.1,
            timing_gap_s=0.001,
            **self._limits(),
        )
        assert report.state == 'insufficient_evidence'


# ---------------------------------------------------------------------------
# Accuracy envelope + state derivation (#566 §4/§8)
# ---------------------------------------------------------------------------


class TestAccuracyEnvelope:
    def _envelope(self, state='VALIDATED_FOR_DECLARED_DOMAIN', **kw):
        fixture = _fixture('VAL10')
        kwargs = dict(
            solver_id='htdt-wave',
            solver_algorithm='pffdtd',
            solver_version='1.0.0',
            observable='reflection_level_db',
            geometry_domain='free_field',
            boundary_material_assumptions='none (free field)',
            source_capability='point_monopole',
            receiver_capability='point_receiver',
            fixture_ids=(fixture.fixture_id,),
            fixture_sha256s=(fixture.fixture_sha256,),
            error_statistic_definition='max_abs dB',
            error_distribution=error_statistic(
                'reflection_level_db', 'dB', [0.1, 0.2]
            ),
            threshold_policy_id='rev55-analytic-fixture-policy',
            threshold_policy_revision=1,
            validation_state=state,
            validated_at_utc='2026-10-05T00:00:00Z',
            frequency_range_hz=(100.0, 4000.0),
        )
        kwargs.update(kw)
        return build_accuracy_envelope(**kwargs)

    def test_envelope_sealed_and_no_global_score(self):
        env = self._envelope()
        assert env.envelope_sha256
        assert not hasattr(env, 'accuracy_score')

    def test_covers_matches_version_observable_band(self):
        env = self._envelope()
        assert env.covers(
            solver_version='1.0.0',
            observable='reflection_level_db',
            frequency_hz=500.0,
        )
        assert not env.covers(
            solver_version='2.0.0',
            observable='reflection_level_db',
            frequency_hz=500.0,
        )
        assert not env.covers(
            solver_version='1.0.0',
            observable='direct_arrival_time_s',
            frequency_hz=500.0,
        )
        assert not env.covers(
            solver_version='1.0.0',
            observable='reflection_level_db',
            frequency_hz=50.0,
        )

    def test_validated_domain_requires_threshold_policy(self):
        with pytest.raises(ValueError, match='threshold policy'):
            self._envelope(threshold_policy_id=None, threshold_policy_revision=None)

    def test_derive_state_pass(self):
        fixture = _fixture('VAL10')
        result = evaluate_fixture_observations(
            fixture, _expected_map(fixture), ANALYTIC_FIXTURE_POLICY_V1
        )
        assert derive_validation_state(
            evaluated=result, has_defensible_threshold=True
        ) == 'VALIDATED_FOR_DECLARED_DOMAIN'

    def test_derive_state_fail_is_limitations_not_pass(self):
        fixture = _fixture('VAL10')
        observed = {k: 99.0 for k in _expected_map(fixture)}
        result = evaluate_fixture_observations(
            fixture, observed, ANALYTIC_FIXTURE_POLICY_V1
        )
        assert derive_validation_state(
            evaluated=result, has_defensible_threshold=True
        ) == 'VALIDATED_WITH_LIMITATIONS'

    def test_derive_state_insufficient(self):
        fixture = _fixture('VAL20')
        result = evaluate_fixture_observations(
            fixture, {}, ANALYTIC_FIXTURE_POLICY_V1
        )
        assert derive_validation_state(
            evaluated=result, has_defensible_threshold=False
        ) == 'INSUFFICIENT_EVIDENCE'


# ---------------------------------------------------------------------------
# Benchmark report — machine + human readable (#566 §9)
# ---------------------------------------------------------------------------


class TestBenchmarkReport:
    def test_report_sealed_and_renders(self):
        fixture = _fixture('VAL10')
        result = evaluate_fixture_observations(
            fixture, _expected_map(fixture), ANALYTIC_FIXTURE_POLICY_V1
        )
        report = build_benchmark_report(
            generated_at_utc='2026-10-05T00:00:00Z',
            code_version='test',
            solver_versions=('htdt-wave@1.0.0',),
            policy=ANALYTIC_FIXTURE_POLICY_V1,
            fixtures=REV55_ANALYTIC_FIXTURES,
            fixture_results=(result,),
        )
        assert report.report_sha256
        text = report.render_text()
        assert 'VAL10' in text


# ---------------------------------------------------------------------------
# Retained fixture set (#566 §10)
# ---------------------------------------------------------------------------


class TestRetainedFixtures:
    def test_fixture_ids(self):
        prefixes = {f.fixture_id.split('-')[0] for f in REV55_ANALYTIC_FIXTURES}
        assert prefixes == {'VAL10', 'VAL20', 'VAL30', 'VAL50', 'VAL60'}

    def test_val20_expected_modes_match_formula(self):
        fixture = _fixture('VAL20')
        dims = (6.0, 4.0, 3.0)
        for sample in fixture.expected_samples:
            # sample_key 'mode_100' → indices (1,0,0)
            nx, ny, nz = (
                int(d) for d in sample.sample_key.removeprefix('mode_')
            )
            expected = (343.0 / 2.0) * math.sqrt(
                (nx / dims[0]) ** 2
                + (ny / dims[1]) ** 2
                + (nz / dims[2]) ** 2
            )
            assert sample.expected_value == pytest.approx(expected)

    def test_val60_is_an_empty_corpus_slot(self):
        fixture = _fixture('VAL60')
        assert fixture.fixture_class == 'measured_room_corpus'
        assert fixture.corpus_slot is not None
        assert fixture.corpus_slot.state == 'empty_unknown'

    def test_val50_declares_overlap_band(self):
        fixture = _fixture('VAL50')
        assert fixture.fixture_class == 'hybrid_boundary'
        assert fixture.overlap_band_hz == (150.0, 300.0)
