"""REV59-BENCH2 regression tests — band semantics (#763), mixing time
(#764), field interpolation (#755), compute budget (#770)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_compute_budget import (
    AccuracyCostEnvelope,
    ComputeObservation,
    PredictedCost,
    SolverBudgetProfile,
    evaluate_budget,
    fidelity_cost_gate,
)
from htdt.cad_field_interpolation import (
    FieldCell,
    FieldSurfaceRecord,
    InterpolationProfile,
    evaluate_field_claim,
)
from htdt.cad_field_metric_repository import (
    CadFieldMetricRepository,
    FieldMetricIntegrityError,
)
from htdt.cad_fractional_octave import (
    BandIntegrationRecord,
    FractionalOctaveProfile,
    compare_band_semantics,
    evaluate_band_semantics,
)
from htdt.cad_mixing_time import (
    EchoDensityProfile,
    LateFieldTransitionAssessment,
    MixingTimeEstimate,
    evaluate_transition,
    late_handoff_gate,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.canonical_json import canonical_sha256


DOC = 'doc-bench2'
_SHA = canonical_sha256({'fixture': 'sha'})
_OTHER_SHA = canonical_sha256({'fixture': 'other'})


def _ref(kind: str, rid: str = 'x1', sha: str = _SHA) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)


def _band_profile(**kwargs) -> FractionalOctaveProfile:
    payload = dict(
        document_id=DOC,
        band_kind='third_octave',
        frequency_standard='iec61260_exact',
        filter_class='class_1',
        process_domain='time_domain_filter',
        integration_semantics='energy_sum',
    )
    payload.update(kwargs)
    return FractionalOctaveProfile.create(**payload)


class TestBandProfile:
    def test_sealed_create(self) -> None:
        p = _band_profile()
        assert p.profile_id.startswith('foct-')
        assert len(p.profile_sha256) == 64

    def test_iec_standard_requires_iec_class(self) -> None:
        with pytest.raises(ValidationError):
            _band_profile(filter_class='non_iec_filter')

    def test_iec_standard_requires_filtered_domain(self) -> None:
        with pytest.raises(ValidationError):
            _band_profile(
                process_domain='frequency_domain_bin_integration'
            )

    def test_custom_table_requires_table_ref(self) -> None:
        with pytest.raises(ValidationError):
            _band_profile(
                frequency_standard='custom_table',
                filter_class='non_iec_filter',
                process_domain='fft_then_band',
            )
        ok = _band_profile(
            frequency_standard='custom_table',
            filter_class='non_iec_filter',
            process_domain='fft_then_band',
            centre_frequency_table_ref=_ref('centre_table', 't1'),
        )
        assert ok.profile_id

    def test_verdict_pinned(self) -> None:
        assert evaluate_band_semantics(_band_profile()) == (
            'semantics_pinned'
        )

    def test_verdict_unpinned_on_unknown(self) -> None:
        p = _band_profile(
            frequency_standard='computed_pow10',
            filter_class='no_filter',
            process_domain='fft_then_band',
        )
        assert evaluate_band_semantics(p) == 'partially_pinned'
        p2 = _band_profile(
            frequency_standard='computed_pow10',
            filter_class='no_filter',
            process_domain='fft_then_band',
            integration_semantics='unknown',
        )
        assert evaluate_band_semantics(p2) == 'unpinned'

    def test_compare_comparable(self) -> None:
        verdict, reasons = compare_band_semantics(
            _band_profile(), _band_profile()
        )
        assert verdict == 'semantics_pinned'
        assert reasons == ()

    def test_compare_incomparable_domain(self) -> None:
        a = _band_profile()
        b = _band_profile(
            frequency_standard='computed_pow10',
            filter_class='no_filter',
            process_domain='fft_then_band',
        )
        verdict, reasons = compare_band_semantics(a, b)
        assert verdict == 'incomparable'
        assert 'process_domain_mismatch' in reasons

    def test_compare_incomparable_kind(self) -> None:
        verdict, reasons = compare_band_semantics(
            _band_profile(), _band_profile(band_kind='octave')
        )
        assert verdict == 'incomparable'
        assert 'band_kind_mismatch' in reasons

    def test_record_seals(self) -> None:
        profile = _band_profile()
        record = BandIntegrationRecord.create(
            document_id=DOC,
            profile_ref=_ref(
                'fractional_octave_profile',
                profile.profile_id,
                profile.profile_sha256,
            ),
            source_result_ref=_ref('measurement_result', 'm1'),
            band_count=31,
            verdict='semantics_pinned',
        )
        assert record.record_id.startswith('band-')


def _echo_profile(**kwargs) -> EchoDensityProfile:
    payload = dict(
        document_id=DOC,
        estimator_kind='normalized_echo_density',
        window_ms=2.5,
        threshold=0.8,
    )
    payload.update(kwargs)
    return EchoDensityProfile.create(**payload)


def _estimate(
    profile: EchoDensityProfile | None = None, **kwargs
) -> MixingTimeEstimate:
    if profile is None:
        profile = _echo_profile()
    payload = dict(
        document_id=DOC,
        profile_ref=_ref(
            'echo_density_profile',
            profile.profile_id,
            profile.profile_sha256,
        ),
        basis='measured_rir',
        source_ref=_ref('measurement_run', 'run-1'),
        position_ref=_ref('seat_position', 'seat-1'),
        estimate_ms=42.0,
        uncertainty_ms=5.0,
    )
    payload.update(kwargs)
    return MixingTimeEstimate.create(**payload)


class TestMixingTime:
    def test_sealed_create(self) -> None:
        p = _echo_profile()
        e = _estimate(p)
        assert p.profile_id.startswith('edp-')
        assert e.estimate_id.startswith('mixt-')

    def test_no_estimate_is_insufficient(self) -> None:
        verdict, reasons = evaluate_transition(_echo_profile(), None)
        assert verdict == 'insufficient_evidence'
        assert 'no_estimate' in reasons

    def test_unknown_basis_insufficient(self) -> None:
        verdict, _ = evaluate_transition(
            _echo_profile(), _estimate(basis='unknown')
        )
        assert verdict == 'insufficient_evidence'

    def test_profile_mismatch_incomparable(self) -> None:
        e = _estimate()
        other = _echo_profile(window_ms=5.0)
        verdict, reasons = evaluate_transition(other, e)
        assert verdict == 'incomparable'
        assert 'profile_mismatch' in reasons

    def test_user_declared_not_mixed(self) -> None:
        p = _echo_profile(estimator_kind='user_declared')
        verdict, reasons = evaluate_transition(p, _estimate(p))
        assert verdict == 'in_transition'
        assert 'declared_only' in reasons

    def test_no_threshold_in_transition(self) -> None:
        p = _echo_profile(threshold=None)
        verdict, _ = evaluate_transition(p, _estimate(p))
        assert verdict == 'in_transition'

    def test_threshold_mixed(self) -> None:
        verdict, _ = evaluate_transition(
            _echo_profile(), _estimate()
        )
        assert verdict == 'sufficiently_mixed'

    def test_rt_alone_not_evidence(self) -> None:
        verdict, reasons = late_handoff_gate(1.2, None)
        assert verdict == 'insufficient_evidence'
        assert 'rt_alone_not_evidence' in reasons

    def test_handoff_supported(self) -> None:
        assessment = LateFieldTransitionAssessment.create(
            document_id=DOC,
            estimate_refs=(_ref('mixing_time_estimate', 'm1'),),
            verdict='sufficiently_mixed',
        )
        verdict, _ = late_handoff_gate(1.2, assessment)
        assert verdict == 'handoff_supported'

    def test_handoff_unsupported(self) -> None:
        assessment = LateFieldTransitionAssessment.create(
            document_id=DOC,
            estimate_refs=(_ref('mixing_time_estimate', 'm1'),),
            verdict='in_transition',
        )
        verdict, _ = late_handoff_gate(1.2, assessment)
        assert verdict == 'handoff_unsupported'


def _interp_profile(**kwargs) -> InterpolationProfile:
    payload = dict(
        document_id=DOC,
        method='idw',
        quantity='spl',
    )
    payload.update(kwargs)
    return InterpolationProfile.create(**payload)


def _surface(
    profile: InterpolationProfile | None = None, **kwargs
) -> FieldSurfaceRecord:
    if profile is None:
        profile = _interp_profile()
    payload = dict(
        document_id=DOC,
        profile_ref=_ref(
            'interpolation_profile',
            profile.profile_id,
            profile.profile_sha256,
        ),
        measured_point_refs=(
            _ref('measurement_position', 'p1'),
            _ref('measurement_position', 'p2'),
        ),
        cells=(
            FieldCell(cell_id='c1', support_kind='measured_support'),
            FieldCell(cell_id='c2', support_kind='interpolated'),
        ),
    )
    payload.update(kwargs)
    return FieldSurfaceRecord.create(**payload)


class TestFieldInterpolation:
    def test_method_unknown_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _interp_profile(method='unknown')

    def test_model_assisted_requires_capable_method(self) -> None:
        with pytest.raises(ValidationError):
            _interp_profile(model_assisted=True)
        ok = _interp_profile(
            method='solver_assisted', model_assisted=True
        )
        assert ok.profile_id

    def test_measured_surface_requires_all_measured(self) -> None:
        verdict, reasons = evaluate_field_claim(
            _interp_profile(), _surface(), 'measured_surface'
        )
        assert verdict == 'overclaimed'
        assert 'non_measured_cells_present' in reasons

    def test_measured_surface_all_measured(self) -> None:
        record = _surface(
            cells=(
                FieldCell(cell_id='c1', support_kind='measured_support'),
                FieldCell(cell_id='c2', support_kind='measured_support'),
            )
        )
        verdict, _ = evaluate_field_claim(
            _interp_profile(), record, 'measured_surface'
        )
        assert verdict == 'admissible'

    def test_no_measured_points_insufficient(self) -> None:
        record = _surface(measured_point_refs=())
        verdict, _ = evaluate_field_claim(
            _interp_profile(), record, 'interpolated_surface'
        )
        assert verdict == 'insufficient_evidence'

    def test_complex_quantity_via_linear_overclaimed(self) -> None:
        profile = _interp_profile(
            method='linear', quantity='complex_pressure'
        )
        verdict, reasons = evaluate_field_claim(
            profile, _surface(profile), 'reconstructed_field'
        )
        assert verdict == 'overclaimed'
        assert 'method_not_complex_capable' in reasons

    def test_model_aided_is_model_dependent(self) -> None:
        profile = _interp_profile(
            method='solver_assisted', model_assisted=True,
            quantity='ir',
        )
        verdict, _ = evaluate_field_claim(
            profile, _surface(profile), 'model_aided_field'
        )
        assert verdict == 'model_dependent'

    def test_profile_mismatch_overclaimed(self) -> None:
        other = _interp_profile(method='kriging')
        verdict, reasons = evaluate_field_claim(
            other, _surface(), 'interpolated_surface'
        )
        assert verdict == 'overclaimed'
        assert 'profile_mismatch' in reasons


def _budget_profile(**kwargs) -> SolverBudgetProfile:
    payload = dict(
        document_id=DOC,
        fidelity_axes={'max_frequency': 4000.0, 'ir_duration': 1.0},
        predicted_costs=(
            PredictedCost(
                metric='runtime_s', value=300.0,
                means='vendor_documented',
            ),
        ),
    )
    payload.update(kwargs)
    return SolverBudgetProfile.create(**payload)


def _observation(**kwargs) -> ComputeObservation:
    payload = dict(
        document_id=DOC,
        run_ref=_ref('solver_run', 'run-1'),
        metrics={'runtime_s': 120.0, 'peak_memory_bytes': 8e9},
        hardware_label='test-gpu',
        observed_at_utc='2026-01-01T00:00:00+00:00',
    )
    payload.update(kwargs)
    return ComputeObservation.create(**payload)


class TestComputeBudget:
    def test_predicted_cost_observed_run_requires_ref(self) -> None:
        with pytest.raises(ValidationError):
            PredictedCost(
                metric='runtime_s', value=1.0, means='observed_run'
            )

    def test_metric_unknown_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PredictedCost(metric='unknown', value=1.0)

    def test_profile_unknown_axis_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _budget_profile(
                fidelity_axes={'unknown': 1.0}
            )

    def test_observation_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _observation(metrics={'unknown': 1.0})

    def test_budget_within(self) -> None:
        verdict, _ = evaluate_budget(
            _observation(), {'runtime_s': 200.0}
        )
        assert verdict == 'within_budget'

    def test_budget_over(self) -> None:
        verdict, reasons = evaluate_budget(
            _observation(), {'runtime_s': 60.0}
        )
        assert verdict == 'over_budget'
        assert 'over_runtime_s' in reasons

    def test_budget_unknown_without_limits(self) -> None:
        verdict, _ = evaluate_budget(_observation(), {})
        assert verdict == 'budget_unknown'

    def test_budget_unknown_unobserved(self) -> None:
        verdict, reasons = evaluate_budget(
            _observation(), {'gpu_seconds': 10.0}
        )
        assert verdict == 'budget_unknown'
        assert 'unobserved_gpu_seconds' in reasons

    def test_budget_no_observation(self) -> None:
        verdict, _ = evaluate_budget(None, {'runtime_s': 1.0})
        assert verdict == 'budget_unknown'

    def test_fidelity_gate_missing(self) -> None:
        profile = _budget_profile(predicted_costs=())
        assert fidelity_cost_gate(profile) == 'cost_evidence_missing'

    def test_fidelity_gate_partial_on_assumed(self) -> None:
        profile = _budget_profile(
            predicted_costs=(
                PredictedCost(
                    metric='runtime_s', value=1.0, means='assumed'
                ),
            )
        )
        assert fidelity_cost_gate(profile) == 'cost_evidence_partial'

    def test_fidelity_gate_present(self) -> None:
        assert fidelity_cost_gate(_budget_profile()) == (
            'cost_evidence_present'
        )

    def test_envelope_seals(self) -> None:
        env = AccuracyCostEnvelope.create(
            document_id=DOC,
            profile_refs=(
                _ref(
                    'solver_budget_profile', 'p1', _SHA),
            ),
            observation_refs=(
                _ref('compute_observation', 'o1'),
            ),
        )
        assert env.envelope_id.startswith('cenv-')


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


def _repo(tmp_path: Path) -> CadFieldMetricRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    scene = SceneRepository(db)
    return CadFieldMetricRepository(scene)


def test_repository_roundtrip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    band = _band_profile()
    record = BandIntegrationRecord.create(
        document_id=DOC,
        profile_ref=_ref(
            'fractional_octave_profile',
            band.profile_id,
            band.profile_sha256,
        ),
        band_count=31,
        verdict='semantics_pinned',
    )
    echo = _echo_profile()
    estimate = _estimate(echo)
    assessment = LateFieldTransitionAssessment.create(
        document_id=DOC,
        estimate_refs=(
            _ref(
                'mixing_time_estimate',
                estimate.estimate_id,
                estimate.estimate_sha256,
            ),
        ),
        verdict='in_transition',
    )
    interp = _interp_profile()
    surface = _surface(interp)
    budget = _budget_profile()
    observation = _observation()
    envelope = AccuracyCostEnvelope.create(
        document_id=DOC,
        profile_refs=(
            _ref(
                'solver_budget_profile',
                budget.profile_id,
                budget.profile_sha256,
            ),
        ),
    )

    repo.save_band_profile(band)
    repo.save_band_integration(record)
    repo.save_echo_profile(echo)
    repo.save_mixing_estimate(estimate)
    repo.save_late_assessment(assessment)
    repo.save_interp_profile(interp)
    repo.save_field_surface(surface)
    repo.save_budget_profile(budget)
    repo.save_compute_observation(observation)
    repo.save_cost_envelope(envelope)

    assert repo.get_band_profile(band.profile_id) == band
    assert repo.get_band_integration(record.record_id) == record
    assert repo.get_echo_profile(echo.profile_id) == echo
    assert repo.get_mixing_estimate(estimate.estimate_id) == estimate
    assert repo.get_late_assessment(
        assessment.assessment_id
    ) == assessment
    assert repo.get_interp_profile(interp.profile_id) == interp
    assert repo.get_field_surface(surface.record_id) == surface
    assert repo.get_budget_profile(budget.profile_id) == budget
    assert repo.get_compute_observation(
        observation.observation_id
    ) == observation
    assert repo.get_cost_envelope(envelope.envelope_id) == envelope


def test_repository_idempotent_resave(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    band = _band_profile()
    repo.save_band_profile(band)
    repo.save_band_profile(band)


def test_repository_detects_column_tamper(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    surface = _surface()
    repo.save_field_surface(surface)
    with connect_sqlite(repo.path) as connection:
        connection.execute(
            'UPDATE cad_field_surface_records SET verdict=? '
            'WHERE record_id=?',
            ('admissible', surface.record_id),
        )
        connection.commit()
    with pytest.raises(FieldMetricIntegrityError):
        repo.get_field_surface(surface.record_id)


def test_bench2_tables_exist_after_fresh_migrate(tmp_path: Path) -> None:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    with sqlite3.connect(db) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    expected = {
        'cad_fractional_octave_profiles',
        'cad_band_integrations',
        'cad_echo_density_profiles',
        'cad_mixing_time_estimates',
        'cad_late_field_assessments',
        'cad_interpolation_profiles',
        'cad_field_surface_records',
        'cad_solver_budget_profiles',
        'cad_compute_observations',
        'cad_accuracy_cost_envelopes',
    }
    assert expected <= names
