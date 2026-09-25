"""#592 cross-workspace result trust presentation tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.localization import PresentationLocale
from htdt.result_trust import (
    ApplicabilityDimension,
    ApplicabilityNote,
    ApplicabilityStatus,
    EvidenceClass,
    FreshnessState,
    ResultTrustSummary,
    UncertaintyKind,
    UncertaintyPresentation,
    ValidationScope,
    trace_identity,
)


def _trust(**over) -> ResultTrustSummary:
    kwargs = {
        'evidence_class': EvidenceClass.PREDICTED,
        'validation_scope': ValidationScope.SYNTHETIC_FIXTURE,
        # #774: freshness is an explicit declaration — no optimistic default.
        'freshness': FreshnessState.CURRENT,
    }
    kwargs.update(over)
    return ResultTrustSummary(**kwargs)


def test_compact_text_carries_trust_qualifiers() -> None:
    trust = _trust(
        uncertainty=UncertaintyPresentation(
            kind=UncertaintyKind.BOUNDED, summary='±1.8 dB'
        ),
        applicability=(
            ApplicabilityNote(
                dimension=ApplicabilityDimension.FREQUENCY_BAND,
                description='20–120 Hz',
            ),
        ),
        applicability_status=ApplicabilityStatus.CHARACTERIZED,
    )
    text = trust.compact_text('72.4 dB SPL', locale=PresentationLocale.ENGLISH)
    assert text == '72.4 dB SPL · Predicted · synthetic fixture · ±1.8 dB · 20–120 Hz'


def test_no_composite_score_field() -> None:
    assert 'score' not in ResultTrustSummary.model_fields
    assert 'confidence' not in ResultTrustSummary.model_fields


def test_stale_requires_invalidating_dependency() -> None:
    with pytest.raises(ValueError):
        _trust(freshness=FreshnessState.STALE)
    with pytest.raises(ValueError):
        _trust(invalidating_dependencies=('scene-rev:9',))


def test_stale_result_names_dependency() -> None:
    trust = _trust(
        freshness=FreshnessState.STALE,
        invalidating_dependencies=('scene-rev:10 supersedes scene-rev:9',),
    )
    assert trust.invalidating_dependencies


def test_uncertainty_validation() -> None:
    with pytest.raises(ValueError):
        UncertaintyPresentation(kind=UncertaintyKind.SAMPLED_DISTRIBUTION)
    with pytest.raises(ValueError):
        UncertaintyPresentation(
            kind=UncertaintyKind.UNKNOWN, summary='±2 dB'
        )
    ok = UncertaintyPresentation(
        kind=UncertaintyKind.SAMPLED_DISTRIBUTION,
        sample_count=200,
        feasible_fraction=0.82,
    )
    assert ok.sample_count == 200


def test_sampled_uncertainty_renders_fraction() -> None:
    trust = _trust(
        uncertainty=UncertaintyPresentation(
            kind=UncertaintyKind.SAMPLED_DISTRIBUTION,
            sample_count=200,
            feasible_fraction=0.82,
        ),
    )
    text = trust.compact_text('RT60 0.41 s', locale=PresentationLocale.ENGLISH)
    assert '82% / 200 samples' in text


def test_applicability_dimensions_unique() -> None:
    with pytest.raises(ValueError):
        _trust(
            applicability=(
                ApplicabilityNote(
                    dimension=ApplicabilityDimension.FREQUENCY_BAND,
                    description='a',
                ),
                ApplicabilityNote(
                    dimension=ApplicabilityDimension.FREQUENCY_BAND,
                    description='b',
                ),
            ),
            applicability_status=ApplicabilityStatus.CHARACTERIZED,
        )


def test_trace_legend_tokens_localized() -> None:
    stale = _trust(
        freshness=FreshnessState.STALE,
        invalidating_dependencies=('dep',),
    )
    ident = trace_identity(stale)
    assert ident.legend_token(PresentationLocale.ENGLISH) == 'Predicted (stale)'
    assert ident.legend_token(PresentationLocale.JAPANESE) == '予測 (古い)'
    current = trace_identity(_trust())
    assert current.legend_token(PresentationLocale.ENGLISH) == 'Predicted'


def test_freshness_has_no_optimistic_default() -> None:
    # #774: CURRENT must be declared — omitting freshness fails closed.
    with pytest.raises(ValidationError):
        ResultTrustSummary(
            evidence_class=EvidenceClass.PREDICTED,
            validation_scope=ValidationScope.UNVALIDATED,
        )


def test_compact_text_never_erases_validation_scope() -> None:
    # #774 D: the strongest scope renders explicitly — its absence can never
    # be misread as "weaker than production-qualified".
    trust = _trust(
        validation_scope=ValidationScope.PRODUCTION_QUALIFIED,
        provenance_ref='gate:production-1',
    )
    text = trust.compact_text('72.4 dB SPL', locale=PresentationLocale.ENGLISH)
    assert 'production qualified' in text


def test_strong_scope_requires_provenance() -> None:
    with pytest.raises(ValueError):
        _trust(validation_scope=ValidationScope.PRODUCTION_QUALIFIED)
    with pytest.raises(ValueError):
        _trust(validation_scope=ValidationScope.OWNED_ROOM_VALIDATED)
    ok = _trust(
        validation_scope=ValidationScope.OWNED_ROOM_VALIDATED,
        provenance_ref='model-validation:val-1',
    )
    assert ok.validation_scope == ValidationScope.OWNED_ROOM_VALIDATED


def test_applicability_status_consistency() -> None:
    # Empty notes may not claim 'characterized'; notes require it.
    with pytest.raises(ValueError):
        _trust(applicability_status=ApplicabilityStatus.CHARACTERIZED)
    with pytest.raises(ValueError):
        _trust(
            applicability=(
                ApplicabilityNote(
                    dimension=ApplicabilityDimension.GEOMETRY,
                    description='rectangular room',
                ),
            ),
            applicability_status=ApplicabilityStatus.UNKNOWN,
        )
    uncharacterized = _trust()
    assert uncharacterized.applicability_status == ApplicabilityStatus.UNKNOWN
