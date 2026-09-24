"""#592 cross-workspace result trust presentation tests."""

from __future__ import annotations

import pytest

from htdt.localization import PresentationLocale
from htdt.result_trust import (
    ApplicabilityDimension,
    ApplicabilityNote,
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
            )
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
