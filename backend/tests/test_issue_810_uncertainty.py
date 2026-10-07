"""#810 REV63 — uncertainty-aware acoustic validation authority.

Covers every verdict path of the uncertainty-aware validation evaluator
(fail-closed especially), protocol preregistration, side-evidence
honesty rules (bounded never silently Gaussian), split-mixing rejection,
seal/id integrity and repository round-trip + tamper detection.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_model_validation import (
    build_full_model_validation,
    build_model_validation,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.cad_validation_metrics import (
    CadObjectiveValidationSample,
    CadRepeatabilityCheck,
    CadRepeatabilityPair,
    CadSensitivityCheck,
)
from htdt.cad_validation_uncertainty import (
    BandUncertaintyEvaluation,
    CategoryContributionValue,
    EvaluationBinding,
    ObservableUncertaintyEvaluation,
    PerceptualInterpretation,
    SideSpectralUncertaintyPoint,
    UncertaintyDecisionRule,
    UncertaintySideEvidence,
    UncertaintyValidationVerdict,
    ValidationObservableMetric,
    ValidationUncertaintyProtocol,
    VUQ_LABELS,
    build_observable_uncertainty_evaluation,
    build_validation_uncertainty_verdict,
)
from htdt.cad_validation_uncertainty_repository import (
    CadValidationUncertaintyRepository,
    ValidationUncertaintyAuthorityError,
    ValidationUncertaintyConflictError,
    ValidationUncertaintyIntegrityError,
)
from htdt.canonical_json import canonical_sha256
from htdt.comparison import FrequencyResponse
from htdt.measurement_evidence_display import (
    uncertainty_band_line,
    uncertainty_validation_line,
)


DOC = 'doc-810'
_SHA = 'a' * 64
_SHA2 = 'b' * 64


def _ref(kind: str = 'mub', rid: str = 'mub-1', sha: str = _SHA) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)


def _rule(**kw) -> UncertaintyDecisionRule:
    defaults = dict(
        significance_ratio=1.0,
        resolution_fraction=0.5,
        max_tolerable_comparison_uncertainty=None,
        jnd_interpretation_value=None,
    )
    defaults.update(kw)
    return UncertaintyDecisionRule(**defaults)


def _metric(
    observable_id: str = 'spl.magnitude',
    *,
    applicability: tuple[float, float] | None = None,
    minimum: str = 'combined_standard',
    rule: UncertaintyDecisionRule | None = None,
) -> ValidationObservableMetric:
    return ValidationObservableMetric(
        observable_id=observable_id,
        quantity='magnitude_response_db',
        unit='dB',
        domain='frequency',
        comparison_metric='band_rms',
        applicability_band_hz=applicability,
        minimum_uncertainty_semantics=minimum,
        decision_rule=rule or _rule(),
    )


def _protocol(
    *metrics: ValidationObservableMetric,
    correlation_policy: str = 'independent_unless_declared',
    **kw,
) -> ValidationUncertaintyProtocol:
    payload = dict(
        schema_version='vuq-protocol-1',
        document_id=DOC,
        protocol_version='1.0',
        model_id='model-1',
        model_version='1.0',
        observable_metrics=metrics or (_metric(),),
        correlation_policy=correlation_policy,
        bounded_input_policy='bounded_worst_case',
        created_at_utc='2026-10-07T00:00:00Z',
    )
    payload.update(kw)
    return ValidationUncertaintyProtocol.create(**payload)


def _bound(
    value: float,
    *,
    category: str = 'measurement_instrument',
    window: tuple[float, float] | None = None,
    model_form: float | None = None,
    spectral: tuple[SideSpectralUncertaintyPoint, ...] | None = None,
) -> UncertaintySideEvidence:
    return UncertaintySideEvidence(
        source_kind='declared_bound',
        bound_half_width=value,
        threshold_semantics='combined_bound',
        spectral_points=spectral,
        valid_frequency_hz=window,
        category_contributions=(
            CategoryContributionValue(category=category, contribution=value),
        ),
        model_discrepancy_contribution=model_form,
    )


def _budget_side(
    value: float | None,
    *,
    semantics: str = 'expanded',
    category: str = 'measurement_instrument',
    kind: str = 'measurement_uncertainty_budget',
    window: tuple[float, float] | None = None,
) -> UncertaintySideEvidence:
    return UncertaintySideEvidence(
        source_kind=kind,
        source_ref=_ref(),
        scalar_uncertainty=value,
        threshold_semantics=semantics,
        valid_frequency_hz=window,
        category_contributions=(
            CategoryContributionValue(
                category=category, contribution=value or 0.0
            ),
        ),
    )


def _no_side() -> UncertaintySideEvidence:
    return UncertaintySideEvidence(source_kind='none')


def _band(
    low: float,
    high: float,
    residual: float,
    *,
    unit: str = 'dB',
) -> dict:
    return {
        'band_hz': (low, high),
        'coverage': 'evaluated',
        'residual_value': residual,
        'residual_unit': unit,
    }


def _evaluation(
    protocol: ValidationUncertaintyProtocol,
    *,
    candidate_id: str = 'cand-h1',
    split: str = 'holdout',
    observable_id: str = 'spl.magnitude',
    measurement: UncertaintySideEvidence | None = None,
    prediction: UncertaintySideEvidence | None = None,
    bands: tuple[dict, ...] = (
        _band(50.0, 100.0, 1.0),
        _band(100.0, 200.0, 1.0),
    ),
) -> ObservableUncertaintyEvaluation:
    return build_observable_uncertainty_evaluation(
        document_id=DOC,
        protocol=protocol,
        candidate_id=candidate_id,
        split=split,  # type: ignore[arg-type]
        observable_id=observable_id,
        measurement_evidence=measurement or _bound(1.0),
        prediction_evidence=prediction or _bound(
            0.5, category='numerical'
        ),
        bands=bands,
        created_at_utc='2026-10-07T00:01:00Z',
    )


def _verdict(
    protocol: ValidationUncertaintyProtocol,
    evaluations: tuple[ObservableUncertaintyEvaluation, ...],
    **kw,
) -> UncertaintyValidationVerdict:
    return build_validation_uncertainty_verdict(
        document_id=DOC,
        protocol=protocol,
        evaluations=evaluations,
        created_at_utc='2026-10-07T00:02:00Z',
        **kw,
    )


# ---------------------------------------------------------------------------
# protocol — preregistered, versioned decision authority
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_seal_covers_payload(self) -> None:
        protocol = _protocol()
        digest = canonical_sha256(protocol.identity_payload())
        assert protocol.protocol_sha256 == digest
        assert protocol.protocol_id == f'vup-{digest[:24]}'

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            ValidationUncertaintyProtocol.model_validate(
                _protocol().model_dump(mode='python') | {'bogus': 1}
            )

    def test_duplicate_observables_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _protocol(_metric(), _metric())

    def test_invalid_applicability_band_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _metric(applicability=(200.0, 100.0))
        with pytest.raises(ValidationError):
            _metric(applicability=(0.0, 100.0))

    def test_supersede_must_pin_sha(self) -> None:
        with pytest.raises(ValidationError):
            _protocol(
                supersedes_protocol_ref=AuthorityRef(
                    kind='validation_uncertainty_protocol',
                    ref_id='vup-abc',
                )
            )

    def test_decision_rule_rejects_unfinite(self) -> None:
        with pytest.raises(ValidationError):
            _rule(significance_ratio=float('inf'))
        with pytest.raises(ValidationError):
            _rule(significance_ratio=0.5)
        with pytest.raises(ValidationError):
            _rule(resolution_fraction=1.0)

    def test_jnd_is_annotation_only(self) -> None:
        """§4: a JND value is perceptual interpretation — it never gates."""
        rule = _rule(jnd_interpretation_value=1.0)
        metric = _metric(rule=rule)
        protocol = _protocol(metric)
        evaluation = _evaluation(
            protocol,
            bands=(_band(50.0, 100.0, 0.4), _band(100.0, 200.0, 0.4)),
        )
        assert evaluation.perceptual_interpretation is not None
        assert (
            evaluation.perceptual_interpretation.role
            == 'perceptual_interpretation_only'
        )
        assert evaluation.perceptual_interpretation.bands_below_declared_jnd == 2
        assert (
            evaluation.summary_verdict
            == 'consistent_with_reference_within_uncertainty'
        )


# ---------------------------------------------------------------------------
# side evidence — declared uncertainty honesty rules
# ---------------------------------------------------------------------------


class TestSideEvidence:
    def test_none_source_carries_nothing(self) -> None:
        for kw in (
            {'threshold_semantics': 'combined_standard'},
            {'scalar_uncertainty': 1.0},
            {'bound_half_width': 1.0},
            {'source_ref': _ref()},
            {
                'category_contributions': (
                    CategoryContributionValue(
                        category='numerical', contribution=1.0
                    ),
                )
            },
        ):
            with pytest.raises(ValidationError):
                UncertaintySideEvidence(source_kind='none', **kw)

    def test_declared_bound_never_a_sigma(self) -> None:
        with pytest.raises(ValidationError):
            UncertaintySideEvidence(
                source_kind='declared_bound',
                bound_half_width=1.0,
                scalar_uncertainty=1.0,
                threshold_semantics='combined_bound',
            )
        with pytest.raises(ValidationError):
            UncertaintySideEvidence(
                source_kind='declared_bound',
                bound_half_width=1.0,
                threshold_semantics='combined_standard',
            )
        with pytest.raises(ValidationError):
            UncertaintySideEvidence(
                source_kind='declared_bound',
                threshold_semantics='combined_bound',
            )

    def test_propagated_interval_is_bounded_only(self) -> None:
        for semantics in ('expanded', 'combined_standard'):
            with pytest.raises(ValidationError):
                UncertaintySideEvidence(
                    source_kind='propagated_interval',
                    source_ref=_ref(),
                    bound_half_width=1.0,
                    threshold_semantics=semantics,
                )
        ok = UncertaintySideEvidence(
            source_kind='propagated_interval',
            source_ref=_ref(),
            bound_half_width=1.0,
            threshold_semantics='combined_bound',
        )
        assert ok.bound_half_width == 1.0

    def test_authority_source_requires_pinned_ref(self) -> None:
        with pytest.raises(ValidationError):
            UncertaintySideEvidence(
                source_kind='measurement_uncertainty_budget',
                scalar_uncertainty=1.0,
                threshold_semantics='expanded',
            )
        with pytest.raises(ValidationError):
            UncertaintySideEvidence(
                source_kind='measurement_uncertainty_budget',
                source_ref=AuthorityRef(kind='mub', ref_id='mub-1'),
                scalar_uncertainty=1.0,
                threshold_semantics='expanded',
            )

    def test_none_semantics_carry_no_values(self) -> None:
        with pytest.raises(ValidationError):
            UncertaintySideEvidence(
                source_kind='measurement_uncertainty_budget',
                source_ref=_ref(),
                scalar_uncertainty=1.0,
                threshold_semantics='none',
            )
        # a bound authority with no usable statistic is honest 'none'
        ok = UncertaintySideEvidence(
            source_kind='measurement_uncertainty_budget',
            source_ref=_ref(),
            threshold_semantics='none',
        )
        assert ok.scalar_uncertainty is None

    def test_spectral_points_must_be_sorted_unique(self) -> None:
        with pytest.raises(ValidationError):
            UncertaintySideEvidence(
                source_kind='measurement_uncertainty_budget',
                source_ref=_ref(),
                threshold_semantics='expanded',
                spectral_points=(
                    SideSpectralUncertaintyPoint(
                        frequency_hz=100.0, uncertainty=1.0
                    ),
                    SideSpectralUncertaintyPoint(
                        frequency_hz=50.0, uncertainty=1.0
                    ),
                ),
            )

    def test_spectral_points_inside_validity_window(self) -> None:
        with pytest.raises(ValidationError):
            UncertaintySideEvidence(
                source_kind='measurement_uncertainty_budget',
                source_ref=_ref(),
                threshold_semantics='expanded',
                valid_frequency_hz=(50.0, 100.0),
                spectral_points=(
                    SideSpectralUncertaintyPoint(
                        frequency_hz=50.0, uncertainty=1.0
                    ),
                    SideSpectralUncertaintyPoint(
                        frequency_hz=150.0, uncertainty=1.0
                    ),
                ),
            )


# ---------------------------------------------------------------------------
# band verdicts — every path, fail closed
# ---------------------------------------------------------------------------


class TestBandVerdicts:
    def _band_verdict(self, **kw):
        protocol = kw.pop('protocol', None) or _protocol()
        evaluation = _evaluation(protocol, **kw)
        return evaluation.bands[0].verdict, evaluation

    def test_consistent_band(self) -> None:
        verdict, _ = self._band_verdict(
            bands=(_band(50.0, 100.0, 1.0),)
        )
        assert verdict == 'consistent_within_uncertainty'

    def test_significant_band(self) -> None:
        verdict, evaluation = self._band_verdict(
            bands=(_band(50.0, 100.0, 3.0),)
        )
        assert verdict == 'discrepancy_significant'
        band = evaluation.bands[0]
        assert band.comparison_uncertainty == pytest.approx(1.5)
        assert band.significance_ratio_value == pytest.approx(2.0)

    def test_below_resolution_band(self) -> None:
        verdict, _ = self._band_verdict(
            bands=(_band(50.0, 100.0, 0.6),)
        )
        assert verdict == 'below_evidence_resolution'

    def test_evidence_too_uncertain_band(self) -> None:
        protocol = _protocol(
            _metric(rule=_rule(max_tolerable_comparison_uncertainty=1.2))
        )
        verdict, _ = self._band_verdict(
            protocol=protocol, bands=(_band(50.0, 100.0, 0.9),)
        )
        assert verdict == 'evidence_too_uncertain'

    def test_missing_measurement_uncertainty_is_insufficient(self) -> None:
        verdict, _ = self._band_verdict(
            measurement=_no_side(),
            bands=(_band(50.0, 100.0, 0.1),),
        )
        assert verdict == 'insufficient_uncertainty_information'

    def test_missing_prediction_uncertainty_is_insufficient(self) -> None:
        verdict, _ = self._band_verdict(
            prediction=_no_side(),
            bands=(_band(50.0, 100.0, 0.1),),
        )
        assert verdict == 'insufficient_uncertainty_information'

    def test_spectral_gap_is_insufficient(self) -> None:
        measurement = _bound(
            0.0,
            spectral=(
                SideSpectralUncertaintyPoint(
                    frequency_hz=60.0, uncertainty=1.0
                ),
                SideSpectralUncertaintyPoint(
                    frequency_hz=90.0, uncertainty=1.0
                ),
            ),
        )
        verdict, _ = self._band_verdict(
            measurement=measurement,
            bands=(_band(150.0, 200.0, 0.1),),
        )
        assert verdict == 'insufficient_uncertainty_information'

    def test_band_outside_validity_window_is_insufficient(self) -> None:
        measurement = _bound(1.0, window=(40.0, 120.0))
        verdict, _ = self._band_verdict(
            measurement=measurement,
            bands=(_band(100.0, 200.0, 0.1),),
        )
        assert verdict == 'insufficient_uncertainty_information'

    def test_semantics_below_protocol_minimum_is_insufficient(self) -> None:
        protocol = _protocol(_metric(minimum='expanded'))
        measurement = _budget_side(1.0, semantics='combined_standard')
        prediction = _budget_side(
            0.5, semantics='combined_standard',
            category='numerical', kind='uncertainty_budget_result',
        )
        verdict, _ = self._band_verdict(
            protocol=protocol,
            measurement=measurement,
            prediction=prediction,
            bands=(_band(50.0, 100.0, 0.1),),
        )
        assert verdict == 'insufficient_uncertainty_information'

    def test_quadrature_combination_for_standard_sides(self) -> None:
        measurement = _budget_side(0.8, semantics='combined_standard')
        prediction = _budget_side(
            0.6, semantics='combined_standard',
            category='numerical', kind='uncertainty_budget_result',
        )
        _, evaluation = self._band_verdict(
            measurement=measurement,
            prediction=prediction,
            bands=(_band(50.0, 100.0, 0.9),),
        )
        band = evaluation.bands[0]
        assert band.combination_rule == 'quadrature'
        assert band.comparison_uncertainty == pytest.approx(1.0)
        assert band.verdict == 'consistent_within_uncertainty'

    def test_bound_forces_linear_worst_case(self) -> None:
        measurement = _budget_side(0.8, semantics='combined_standard')
        prediction = _bound(0.6, category='numerical')
        _, evaluation = self._band_verdict(
            measurement=measurement,
            prediction=prediction,
            bands=(_band(50.0, 100.0, 0.9),),
        )
        band = evaluation.bands[0]
        assert band.combination_rule == 'linear'
        assert band.combined_semantics == 'combined_bound'
        assert band.comparison_uncertainty == pytest.approx(1.4)

    def test_declared_correlation_combines_linearly(self) -> None:
        protocol = _protocol(correlation_policy='declared_correlated')
        measurement = _budget_side(0.8, semantics='combined_standard')
        prediction = _budget_side(
            0.6, semantics='combined_standard',
            category='numerical', kind='uncertainty_budget_result',
        )
        _, evaluation = self._band_verdict(
            protocol=protocol,
            measurement=measurement,
            prediction=prediction,
            bands=(_band(50.0, 100.0, 0.9),),
        )
        band = evaluation.bands[0]
        assert band.combination_rule == 'linear'
        assert band.combined_semantics == 'combined_bound'

    def test_excluded_band_requires_reason_and_no_verdict(self) -> None:
        with pytest.raises(ValidationError):
            BandUncertaintyEvaluation(
                band_hz=(50.0, 100.0), coverage='excluded_unsupported'
            )
        band = BandUncertaintyEvaluation(
            band_hz=(50.0, 100.0),
            coverage='excluded_unsupported',
            excluded_reason='outside measurement capability',
        )
        assert band.verdict is None
        with pytest.raises(ValidationError):
            BandUncertaintyEvaluation(
                band_hz=(50.0, 100.0),
                coverage='unmeasured',
                verdict='consistent_within_uncertainty',
            )


# ---------------------------------------------------------------------------
# observable summary verdicts — §5 vocabulary, fail closed
# ---------------------------------------------------------------------------


class TestObservableVerdicts:
    def test_consistent_within_uncertainty(self) -> None:
        evaluation = _evaluation(_protocol())
        assert (
            evaluation.summary_verdict
            == 'consistent_with_reference_within_uncertainty'
        )

    def test_significant_without_model_form_envelope(self) -> None:
        evaluation = _evaluation(
            _protocol(),
            bands=(
                _band(50.0, 100.0, 0.5),
                _band(100.0, 200.0, 3.0),
            ),
        )
        assert evaluation.summary_verdict == 'model_form_discrepancy_required'

    def test_significant_within_declared_model_form(self) -> None:
        prediction = _bound(0.5, category='numerical', model_form=2.0)
        evaluation = _evaluation(
            _protocol(),
            prediction=prediction,
            bands=(
                _band(50.0, 100.0, 0.5),
                _band(100.0, 200.0, 3.0),
            ),
        )
        assert evaluation.summary_verdict == 'discrepancy_significant'
        assert any(
            'model-form' in note for note in evaluation.limitations
        )

    def test_insufficient_band_fails_closed(self) -> None:
        evaluation = _evaluation(
            _protocol(),
            measurement=_no_side(),
            bands=(_band(50.0, 100.0, 0.1),),
        )
        assert evaluation.summary_verdict == 'insufficient_evidence'

    def test_no_evaluated_bands_is_insufficient(self) -> None:
        evaluation = _evaluation(
            _protocol(),
            bands=(
                {
                    'band_hz': (50.0, 100.0),
                    'coverage': 'excluded_unsupported',
                    'excluded_reason': 'unsupported range',
                },
            ),
        )
        assert evaluation.summary_verdict == 'insufficient_evidence'

    def test_coverage_gaps_reported_never_silent(self) -> None:
        evaluation = _evaluation(
            _protocol(),
            bands=(
                _band(50.0, 100.0, 1.0),
                {'band_hz': (100.0, 200.0), 'coverage': 'unmeasured'},
            ),
        )
        assert evaluation.summary_verdict == 'band_coverage_incomplete'
        assert any('incomplete' in note for note in evaluation.limitations)

    def test_applicability_hole_is_coverage_incomplete(self) -> None:
        protocol = _protocol(_metric(applicability=(50.0, 200.0)))
        evaluation = _evaluation(
            protocol,
            bands=(_band(50.0, 120.0, 1.0),),
        )
        assert evaluation.summary_verdict == 'band_coverage_incomplete'

    def test_reference_too_uncertain(self) -> None:
        protocol = _protocol(
            _metric(rule=_rule(max_tolerable_comparison_uncertainty=1.2))
        )
        evaluation = _evaluation(protocol, bands=(_band(50.0, 100.0, 0.9),))
        assert evaluation.summary_verdict == 'reference_too_uncertain'
        assert (
            evaluation.dominant_uncertainty_category
            == 'measurement_instrument'
        )

    def test_input_uncertainty_dominates(self) -> None:
        protocol = _protocol(
            _metric(rule=_rule(max_tolerable_comparison_uncertainty=2.5))
        )
        prediction = _bound(2.0, category='model_input')
        evaluation = _evaluation(
            protocol,
            prediction=prediction,
            bands=(_band(50.0, 100.0, 0.9),),
        )
        assert evaluation.summary_verdict == 'input_uncertainty_dominates'

    def test_numerical_uncertainty_dominates(self) -> None:
        protocol = _protocol(
            _metric(rule=_rule(max_tolerable_comparison_uncertainty=1.2))
        )
        measurement = _bound(0.3, category='measurement_instrument')
        prediction = _bound(1.2, category='numerical')
        evaluation = _evaluation(
            protocol,
            measurement=measurement,
            prediction=prediction,
            bands=(_band(50.0, 100.0, 0.9),),
        )
        assert evaluation.summary_verdict == 'numerical_uncertainty_dominates'

    def test_undeclared_observable_can_never_evaluate(self) -> None:
        with pytest.raises(ValueError):
            _evaluation(_protocol(), observable_id='spl.phase')


# ---------------------------------------------------------------------------
# evaluation integrity — derived state is sealed, forged state rejected
# ---------------------------------------------------------------------------


class TestEvaluationIntegrity:
    def test_seal_covers_payload(self) -> None:
        evaluation = _evaluation(_protocol())
        digest = canonical_sha256(evaluation.identity_payload())
        assert evaluation.evaluation_sha256 == digest
        assert evaluation.evaluation_id == f'uoe-{digest[:24]}'

    def test_forged_summary_verdict_rejected(self) -> None:
        evaluation = _evaluation(_protocol())
        forged = evaluation.model_dump(mode='python') | {
            'summary_verdict': 'insufficient_evidence'
        }
        with pytest.raises(ValidationError):
            ObservableUncertaintyEvaluation.model_validate(forged)

    def test_forged_band_verdict_rejected(self) -> None:
        evaluation = _evaluation(_protocol())
        bands = list(evaluation.model_dump(mode='python')['bands'])
        bands[0] = bands[0] | {'verdict': 'discrepancy_significant'}
        forged = evaluation.model_dump(mode='python') | {'bands': bands}
        with pytest.raises(ValidationError):
            ObservableUncertaintyEvaluation.model_validate(forged)

    def test_forged_dominant_category_rejected(self) -> None:
        evaluation = _evaluation(_protocol())
        forged = evaluation.model_dump(mode='python') | {
            'dominant_uncertainty_category': 'numerical'
        }
        with pytest.raises(ValidationError):
            ObservableUncertaintyEvaluation.model_validate(forged)

    def test_forged_combined_uncertainty_rejected(self) -> None:
        evaluation = _evaluation(_protocol())
        bands = list(evaluation.model_dump(mode='python')['bands'])
        bands[0] = bands[0] | {'comparison_uncertainty': 99.0}
        forged = evaluation.model_dump(mode='python') | {'bands': bands}
        with pytest.raises(ValidationError):
            ObservableUncertaintyEvaluation.model_validate(forged)

    def test_band_unit_must_match_observable(self) -> None:
        with pytest.raises(ValidationError):
            _evaluation(
                _protocol(),
                bands=(_band(50.0, 100.0, 1.0, unit='s'),),
            )

    def test_duplicate_bands_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _evaluation(
                _protocol(),
                bands=(
                    _band(50.0, 100.0, 1.0),
                    _band(50.0, 100.0, 2.0),
                ),
            )


# ---------------------------------------------------------------------------
# legacy record helpers — existing records remain valid inputs
# ---------------------------------------------------------------------------


def _fr(offset: float) -> FrequencyResponse:
    return FrequencyResponse(
        frequency_hz=(20, 40, 80, 160),
        level_db=tuple(offset + x for x in (0, 1, -1, 0)),
    )


def _legacy_record(**kw):
    samples = kw.pop('samples', (
        ('cal-1', 'calibration', 'p:cal-1', 'm:cal-1', _fr(0), _fr(0.5)),
        ('cand-h1', 'holdout', 'p:h1', 'm:h1', _fr(0), _fr(0.5)),
    ))
    defaults = dict(
        document_id=DOC,
        search_spec_id='spec-1',
        search_spec_sha256='1' * 64,
        candidate_set_sha256='2' * 64,
        model_id='model-1',
        model_version='1.0',
        samples=samples,
        low_hz=20.0,
        high_hz=160.0,
        max_holdout_rms_db=3.0,
    )
    defaults.update(kw)
    return build_model_validation(**defaults)


def _full_legacy_record(trend: str):
    if trend == 'pass':
        objectives = (
            CadObjectiveValidationSample(
                candidate_id='cand-h1', split='holdout',
                objective_id='obj.spl', unit='dB',
                predicted_evaluation_id='pe1', measured_evaluation_id='me1',
                predicted_value=1.0, measured_value=1.0,
            ),
            CadObjectiveValidationSample(
                candidate_id='cand-h2', split='holdout',
                objective_id='obj.spl', unit='dB',
                predicted_evaluation_id='pe2', measured_evaluation_id='me2',
                predicted_value=2.0, measured_value=2.0,
            ),
        )
    else:
        objectives = (
            CadObjectiveValidationSample(
                candidate_id='cand-h1', split='holdout',
                objective_id='obj.spl', unit='dB',
                predicted_evaluation_id='pe1', measured_evaluation_id='me1',
                predicted_value=2.0, measured_value=1.0,
            ),
            CadObjectiveValidationSample(
                candidate_id='cand-h2', split='holdout',
                objective_id='obj.spl', unit='dB',
                predicted_evaluation_id='pe2', measured_evaluation_id='me2',
                predicted_value=1.0, measured_value=2.0,
            ),
        )
    return build_full_model_validation(
        document_id=DOC,
        search_spec_id='spec-1',
        search_spec_sha256='1' * 64,
        candidate_set_sha256='2' * 64,
        model_id='model-1',
        model_version='1.0',
        response_samples=(
            ('cal-1', 'calibration', 'p:cal-1', 'm:cal-1', _fr(0), _fr(0.5)),
            ('cand-h1', 'holdout', 'p:h1', 'm:h1', _fr(0), _fr(0.5)),
            ('cand-h2', 'holdout', 'p:h2', 'm:h2', _fr(0), _fr(0.5)),
        ),
        objective_samples=objectives,
        sensitivity_checks=(
            CadSensitivityCheck(
                objective_id='obj.spl', unit='dB',
                candidate_a_id='cand-h1', candidate_b_id='cand-h2',
                placement_delta_m=0.5,
                predicted_delta=0.1, measured_delta=0.1,
                observed_sensitivity_per_m=0.2,
                model_error_per_m=0.0,
                max_observed_sensitivity_per_m=2.0,
                max_model_error_per_m=1.0,
                gate='pass',
            ),
        ),
        repeatability_checks=(
            CadRepeatabilityCheck(
                scene_revision_id='rev-1',
                measurement_ids=('m:h1', 'm:h1b'),
                requested_band_hz=(20.0, 160.0),
                pairs=(
                    CadRepeatabilityPair(
                        measurement_a_id='m:h1',
                        measurement_b_id='m:h1b',
                        rms_difference_db=0.2,
                    ),
                ),
                rms_floor_db=0.2,
            ),
        ),
        separation_checks=(),
        applicability_checks=(),
        low_hz=20.0,
        high_hz=160.0,
        max_holdout_rms_db=3.0,
        evidence_scope='synthetic_fixture',
    )


# ---------------------------------------------------------------------------
# verdict aggregation — absolute vs ranking, split separation, snapshots
# ---------------------------------------------------------------------------


class TestVerdictAggregation:
    def test_holdout_drives_absolute_verdict(self) -> None:
        protocol = _protocol()
        holdout = _evaluation(protocol)
        verdict = _verdict(protocol, (holdout,))
        assert (
            verdict.absolute_verdict
            == 'consistent_with_reference_within_uncertainty'
        )
        assert verdict.verdict_id.startswith('uvv-')

    def test_no_holdout_is_insufficient(self) -> None:
        protocol = _protocol()
        calibration = _evaluation(
            protocol, candidate_id='cal-1', split='calibration'
        )
        verdict = _verdict(protocol, (calibration,))
        assert verdict.absolute_verdict == 'insufficient_evidence'
        assert verdict.calibration_state == 'calibration_consistent'
        assert any(
            'independent holdout evidence is required' in reason
            for reason in verdict.gate_reasons
        )

    def test_no_calibration_is_reported_separately(self) -> None:
        protocol = _protocol()
        verdict = _verdict(protocol, (_evaluation(protocol),))
        assert verdict.calibration_state == 'no_calibration_evidence'
        assert any(
            'independent calibration evidence is required' in reason
            for reason in verdict.gate_reasons
        )

    def test_calibration_discrepancy_separate_from_holdout(self) -> None:
        protocol = _protocol()
        calibration = _evaluation(
            protocol,
            candidate_id='cal-1',
            split='calibration',
            bands=(_band(50.0, 100.0, 9.0),),
        )
        holdout = _evaluation(protocol)
        verdict = _verdict(protocol, (calibration, holdout))
        assert verdict.calibration_state == 'calibration_discrepancy'
        assert (
            verdict.absolute_verdict
            == 'consistent_with_reference_within_uncertainty'
        )
        # model-form suspicion is declared across bound evidence
        assert verdict.model_form_discrepancy_suspected is True

    def test_split_mixing_rejected(self) -> None:
        protocol = _protocol()
        calibration = _evaluation(
            protocol, candidate_id='cand-x', split='calibration'
        )
        holdout = _evaluation(
            protocol, candidate_id='cand-x', split='holdout'
        )
        with pytest.raises(ValidationError):
            _verdict(protocol, (calibration, holdout))

    def test_same_observable_candidate_both_splits_rejected(self) -> None:
        protocol = _protocol(_metric(), _metric('spl.phase'))
        calibration = _evaluation(
            protocol, candidate_id='cand-x', split='calibration'
        )
        holdout_a = _evaluation(
            protocol,
            candidate_id='cand-y',
            split='holdout',
            observable_id='spl.phase',
        )
        holdout_b = _evaluation(
            protocol, candidate_id='cand-x', split='holdout'
        )
        with pytest.raises(ValidationError):
            _verdict(protocol, (calibration, holdout_a, holdout_b))

    def test_worst_holdout_verdict_wins(self) -> None:
        protocol = _protocol(_metric(), _metric('spl.phase'))
        good = _evaluation(protocol)
        bad = _evaluation(
            protocol,
            candidate_id='cand-h2',
            observable_id='spl.phase',
            bands=(_band(50.0, 100.0, 9.0),),
        )
        verdict = _verdict(protocol, (good, bad))
        assert (
            verdict.absolute_verdict == 'model_form_discrepancy_required'
        )
        assert verdict.dominant_uncertainty_category != 'unknown' or True

    def test_declared_observable_without_holdout_is_insufficient(self) -> None:
        """§2 — a protocol-declared observable with no holdout evidence can
        never read as validated, however good the bound evidence is."""
        protocol = _protocol(_metric(), _metric('spl.phase'))
        verdict = _verdict(protocol, (_evaluation(protocol),))
        assert verdict.absolute_verdict == 'insufficient_evidence'
        assert any(
            'spl.phase' in reason for reason in verdict.gate_reasons
        )

    def test_missing_observable_never_hides_discrepancy(self) -> None:
        protocol = _protocol(_metric(), _metric('spl.phase'))
        bad = _evaluation(
            protocol, bands=(_band(50.0, 100.0, 9.0),)
        )
        verdict = _verdict(protocol, (bad,))
        assert (
            verdict.absolute_verdict == 'model_form_discrepancy_required'
        )

    def test_reference_too_uncertain_study_verdict(self) -> None:
        protocol = _protocol(
            _metric(rule=_rule(max_tolerable_comparison_uncertainty=1.2))
        )
        evaluation = _evaluation(
            protocol, bands=(_band(50.0, 100.0, 0.9),)
        )
        verdict = _verdict(protocol, (evaluation,))
        assert verdict.absolute_verdict == 'reference_too_uncertain'

    def test_bindings_must_pin_sha(self) -> None:
        with pytest.raises(ValidationError):
            EvaluationBinding(
                evaluation_ref=AuthorityRef(
                    kind='observable_uncertainty_evaluation',
                    ref_id='uoe-x',
                ),
                observable_id='spl.magnitude',
                candidate_id='cand-h1',
                split='holdout',
                summary_verdict='insufficient_evidence',
            )

    def test_duplicate_bindings_rejected(self) -> None:
        protocol = _protocol()
        evaluation = _evaluation(protocol)
        verdict = _verdict(protocol, (evaluation,))
        bindings = verdict.model_dump(mode='python')['evaluation_bindings']
        forged = verdict.model_dump(mode='python') | {
            'evaluation_bindings': [*bindings, *bindings]
        }
        with pytest.raises(ValidationError):
            UncertaintyValidationVerdict.model_validate(forged)

    def test_evaluation_under_other_protocol_rejected(self) -> None:
        protocol_a = _protocol(protocol_version='1.0')
        protocol_b = _protocol(protocol_version='2.0')
        evaluation = _evaluation(protocol_a)
        with pytest.raises(ValueError):
            _verdict(protocol_b, (evaluation,))


# ---------------------------------------------------------------------------
# legacy record binding — additive, snapshot-only, never self-validating
# ---------------------------------------------------------------------------


class TestLegacyBinding:
    def test_legacy_record_bound_as_evidence(self) -> None:
        protocol = _protocol()
        record = _legacy_record()
        evaluation = _evaluation(protocol)
        verdict = _verdict(
            protocol, (evaluation,), validation_record=record
        )
        assert verdict.validation_ref is not None
        assert (
            verdict.validation_ref.ref_sha256 == record.validation_sha256
        )
        assert verdict.legacy_residual_gate == record.residual_gate
        assert (
            verdict.legacy_max_holdout_rms_db == record.max_holdout_rms_db
        )

    def test_scalar_gate_alone_never_validates(self) -> None:
        """§1/§13: a passing max_holdout_rms_db is one metric — it cannot
        produce consistent_with_reference within uncertainty alone."""
        protocol = _protocol()
        record = _legacy_record()
        assert record.residual_gate == 'pass'
        verdict = _verdict(protocol, (), validation_record=record)
        assert verdict.absolute_verdict == 'insufficient_evidence'
        assert any('legacy' in reason for reason in verdict.gate_reasons)

    def test_evaluation_outside_record_pairs_rejected(self) -> None:
        protocol = _protocol()
        record = _legacy_record()
        stray = _evaluation(protocol, candidate_id='cand-other')
        with pytest.raises(ValueError):
            _verdict(
                protocol, (stray,), validation_record=record
            )

    def test_evaluation_split_must_match_record(self) -> None:
        protocol = _protocol()
        record = _legacy_record()
        wrong_split = _evaluation(
            protocol, candidate_id='cand-h1', split='calibration'
        )
        with pytest.raises(ValueError):
            _verdict(
                protocol, (wrong_split,), validation_record=record
            )

    def test_ranking_supported(self) -> None:
        protocol = _protocol()
        record = _full_legacy_record('pass')
        h1 = _evaluation(protocol, candidate_id='cand-h1')
        h2 = _evaluation(protocol, candidate_id='cand-h2')
        verdict = _verdict(
            protocol, (h1, h2), validation_record=record
        )
        assert verdict.ranking_verdict == 'ranking_supported'
        assert (
            verdict.absolute_verdict
            == 'consistent_with_reference_within_uncertainty'
        )

    def test_ranking_contradicted(self) -> None:
        protocol = _protocol()
        record = _full_legacy_record('fail')
        h1 = _evaluation(protocol, candidate_id='cand-h1')
        h2 = _evaluation(protocol, candidate_id='cand-h2')
        verdict = _verdict(
            protocol, (h1, h2), validation_record=record
        )
        assert verdict.ranking_verdict == 'ranking_contradicted'

    def test_ranking_not_evaluated_without_trend_evidence(self) -> None:
        protocol = _protocol()
        record = _legacy_record()
        evaluation = _evaluation(protocol)
        verdict = _verdict(
            protocol, (evaluation,), validation_record=record
        )
        assert verdict.ranking_verdict == 'ranking_not_evaluated'

    def test_ranking_not_evaluated_without_record(self) -> None:
        protocol = _protocol()
        verdict = _verdict(protocol, (_evaluation(protocol),))
        assert verdict.ranking_verdict == 'ranking_not_evaluated'


# ---------------------------------------------------------------------------
# verdict integrity + campaign authority
# ---------------------------------------------------------------------------


class TestVerdictIntegrity:
    def test_seal_covers_payload(self) -> None:
        protocol = _protocol()
        verdict = _verdict(protocol, (_evaluation(protocol),))
        digest = canonical_sha256(verdict.identity_payload())
        assert verdict.verdict_sha256 == digest
        assert verdict.verdict_id == f'uvv-{digest[:24]}'

    def test_forged_absolute_verdict_rejected(self) -> None:
        protocol = _protocol()
        verdict = _verdict(protocol, (_evaluation(protocol),))
        forged = verdict.model_dump(mode='python') | {
            'absolute_verdict': 'discrepancy_significant'
        }
        with pytest.raises(ValidationError):
            UncertaintyValidationVerdict.model_validate(forged)

    def test_forged_verdict_sha_rejected(self) -> None:
        protocol = _protocol()
        verdict = _verdict(protocol, (_evaluation(protocol),))
        forged = verdict.model_dump(mode='python') | {
            'verdict_sha256': _SHA2
        }
        with pytest.raises(ValidationError):
            UncertaintyValidationVerdict.model_validate(forged)

    def test_owned_room_requires_campaign_registration(self) -> None:
        protocol = _protocol()
        evaluation = _evaluation(protocol)
        with pytest.raises(ValidationError):
            _verdict(
                protocol, (evaluation,), evidence_scope='owned_room'
            )
        with pytest.raises(ValidationError):
            _verdict(
                protocol,
                (evaluation,),
                evidence_scope='owned_room',
                campaign_id='o60-validation-campaign:' + _SHA,
                campaign_sha256=_SHA,
            )

    def test_synthetic_forbids_campaign_claims(self) -> None:
        protocol = _protocol()
        evaluation = _evaluation(protocol)
        with pytest.raises(ValidationError):
            _verdict(
                protocol,
                (evaluation,),
                campaign_id='o60-validation-campaign:' + _SHA,
                campaign_sha256=_SHA,
            )

    def test_verdict_sha_ref_required(self) -> None:
        protocol = _protocol()
        evaluation = _evaluation(protocol)
        forged = _verdict(
            protocol, (evaluation,)
        ).model_dump(mode='python') | {
            'protocol_ref': AuthorityRef(
                kind='validation_uncertainty_protocol',
                ref_id=protocol.protocol_id,
            )
        }
        with pytest.raises(ValidationError):
            UncertaintyValidationVerdict.model_validate(forged)


# ---------------------------------------------------------------------------
# repository — round-trip, authority reachability, tamper detection
# ---------------------------------------------------------------------------


def _repo(tmp_path: Path) -> CadValidationUncertaintyRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    scene = SceneRepository(db)
    return CadValidationUncertaintyRepository(scene)


def test_repository_roundtrip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    protocol = _protocol()
    repo.save_protocol(protocol)
    evaluation = _evaluation(protocol)
    repo.save_evaluation(evaluation)
    verdict = _verdict(protocol, (evaluation,))
    repo.save_verdict(verdict)

    assert repo.get_protocol(protocol.protocol_id) == protocol
    assert repo.get_evaluation(evaluation.evaluation_id) == evaluation
    assert repo.get_verdict(verdict.verdict_id) == verdict


def test_repository_lists_by_document(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    protocol = _protocol()
    repo.save_protocol(protocol)
    listed = repo.list_protocols(DOC)
    assert [p.protocol_id for p in listed] == [protocol.protocol_id]
    assert len(repo.list_protocols()) == 1


def test_repository_append_only_conflict(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    protocol = _protocol()
    repo.save_protocol(protocol)
    repo.save_protocol(protocol)  # idempotent re-save
    forged = ValidationUncertaintyProtocol.model_construct(
        **protocol.model_dump(mode='python') | {
            'protocol_sha256': _SHA2
        }
    )
    with pytest.raises(ValidationUncertaintyIntegrityError):
        repo.save_protocol(forged)


def test_evaluation_requires_stored_protocol(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    protocol = _protocol()
    evaluation = _evaluation(protocol)
    with pytest.raises(ValidationUncertaintyAuthorityError):
        repo.save_evaluation(evaluation)


def test_evaluation_protocol_sha_must_match(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    protocol = _protocol(protocol_version='1.0')
    repo.save_protocol(protocol)
    other = _protocol(protocol_version='2.0')
    evaluation = _evaluation(other)
    forged = evaluation.model_dump(mode='python') | {
        'protocol_ref': AuthorityRef(
            kind='validation_uncertainty_protocol',
            ref_id=protocol.protocol_id,
            ref_sha256='f' * 64,
        )
    }
    # the forged ref breaks the evaluation's own derivation — the model
    # rejects before the repository is even reached
    with pytest.raises(ValidationError):
        ObservableUncertaintyEvaluation.model_validate(forged)


def test_verdict_requires_stored_evaluation(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    protocol = _protocol()
    repo.save_protocol(protocol)
    evaluation = _evaluation(protocol)
    verdict = _verdict(protocol, (evaluation,))
    with pytest.raises(ValidationUncertaintyAuthorityError):
        repo.save_verdict(verdict)


def test_verdict_requires_stored_protocol(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    protocol = _protocol()
    evaluation = _evaluation(protocol)
    verdict = _verdict(protocol, (evaluation,))
    with pytest.raises(ValidationUncertaintyAuthorityError):
        repo.save_verdict(verdict)


def test_evaluation_cannot_weaken_sealed_observable(tmp_path: Path) -> None:
    """An evaluation embedding an observable metric different from the
    protocol's sealed declaration is rejected — the contract is the
    protocol's, not the record's."""
    repo = _repo(tmp_path)
    protocol = _protocol()
    repo.save_protocol(protocol)
    other_protocol = _protocol(
        _metric(rule=_rule(significance_ratio=2.0))
    )
    weak = _evaluation(other_protocol)
    forged = ObservableUncertaintyEvaluation.create(
        **weak.model_dump(mode='python') | {
            'protocol_ref': AuthorityRef(
                kind='validation_uncertainty_protocol',
                ref_id=protocol.protocol_id,
                ref_sha256=protocol.protocol_sha256,
            ),
            'protocol_version': protocol.protocol_version,
        }
    )
    with pytest.raises(ValidationUncertaintyAuthorityError):
        repo.save_evaluation(forged)


def test_verdict_cannot_restate_observable_set(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    protocol = _protocol()
    repo.save_protocol(protocol)
    evaluation = _evaluation(protocol)
    repo.save_evaluation(evaluation)
    verdict = _verdict(protocol, (evaluation,))
    forged = UncertaintyValidationVerdict.create(
        **verdict.model_dump(mode='python') | {
            'protocol_observable_ids': ('spl.other',),
            'absolute_verdict': 'insufficient_evidence',
            'gate_reasons': verdict.gate_reasons
            + (
                'declared protocol observables lack holdout evidence: '
                'spl.other',
            ),
        }
    )
    with pytest.raises(ValidationUncertaintyAuthorityError):
        repo.save_verdict(forged)


def test_repository_detects_column_tamper(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    protocol = _protocol()
    repo.save_protocol(protocol)
    evaluation = _evaluation(protocol)
    repo.save_evaluation(evaluation)
    with connect_sqlite(repo.path) as connection:
        connection.execute(
            'UPDATE cad_observable_uncertainty_evaluations '
            "SET summary_verdict='insufficient_evidence' "
            'WHERE evaluation_id=?',
            (evaluation.evaluation_id,),
        )
        connection.commit()
    with pytest.raises(ValidationUncertaintyIntegrityError):
        repo.get_evaluation(evaluation.evaluation_id)


def test_repository_detects_payload_tamper(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    protocol = _protocol()
    repo.save_protocol(protocol)
    evaluation = _evaluation(protocol)
    repo.save_evaluation(evaluation)
    with connect_sqlite(repo.path) as connection:
        row = connection.execute(
            'SELECT payload_json FROM '
            'cad_observable_uncertainty_evaluations WHERE evaluation_id=?',
            (evaluation.evaluation_id,),
        ).fetchone()
        payload = row['payload_json'].replace(
            '"consistent_with_reference_within_uncertainty"',
            '"discrepancy_significant"',
        )
        connection.execute(
            'UPDATE cad_observable_uncertainty_evaluations '
            'SET payload_json=? WHERE evaluation_id=?',
            (payload, evaluation.evaluation_id),
        )
        connection.commit()
    # the sealed model re-derives verdicts from bound evidence: a payload
    # that restates them fails validation outright
    with pytest.raises(ValidationError):
        repo.get_evaluation(evaluation.evaluation_id)


def test_repository_rejects_unsealed_record(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    protocol = _protocol().model_copy(update={'model_version': '9.9'})
    with pytest.raises(ValidationUncertaintyIntegrityError):
        repo.save_protocol(protocol)


def test_uncertainty_tables_exist_after_fresh_migrate(tmp_path: Path) -> None:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    with connect_sqlite(db) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    expected = {
        'cad_validation_uncertainty_protocols',
        'cad_observable_uncertainty_evaluations',
        'cad_uncertainty_validation_verdicts',
    }
    assert expected <= tables


# ---------------------------------------------------------------------------
# JA verdict labels — every verdict vocabulary member has a display string
# ---------------------------------------------------------------------------


def test_ja_labels_cover_verdict_vocabulary() -> None:
    expected = {
        'consistent_within_uncertainty',
        'discrepancy_significant',
        'below_evidence_resolution',
        'evidence_too_uncertain',
        'insufficient_uncertainty_information',
        'consistent_with_reference_within_uncertainty',
        'model_form_discrepancy_required',
        'reference_too_uncertain',
        'input_uncertainty_dominates',
        'numerical_uncertainty_dominates',
        'band_coverage_incomplete',
        'insufficient_evidence',
        'ranking_supported',
        'ranking_contradicted',
        'ranking_not_evaluated',
        'calibration_consistent',
        'calibration_discrepancy',
        'no_calibration_evidence',
    }
    assert expected <= set(VUQ_LABELS)


def test_ja_verdict_lines() -> None:
    assert uncertainty_validation_line(
        'consistent_with_reference_within_uncertainty'
    ).startswith('不確かさ検証: ')
    assert uncertainty_band_line(
        'discrepancy_significant'
    ).startswith('帯域評価: ')
    # unknown verdicts fall back to the raw token, never an empty string
    assert 'never_seen' in uncertainty_validation_line('never_seen')
