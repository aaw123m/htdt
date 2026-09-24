from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_equipment import FrequencyDomain
from htdt.cad_standards import CriterionRule, CriterionSource
from htdt.cad_acoustic_target import (
    AcousticPerformanceTargetProfile,
    AcousticTargetBand,
    AcousticTargetCriterion,
    AcousticTargetObservation,
    build_acoustic_target_profile,
    evaluate_acoustic_targets,
)


NOW = '2026-09-24T00:00:00+00:00'
SOURCE = CriterionSource(
    publisher='Fixture publisher',
    document_title='Fixture acoustic guidance',
    document_version='1.0',
    reference='Fixture §2',
)


def _band(band_id: str = '125-250') -> AcousticTargetBand:
    return AcousticTargetBand(
        band_id=band_id,
        frequency=FrequencyDomain(minimum_hz=125.0, maximum_hz=250.0),
    )


def _criterion(
    criterion_id: str = 't30-main',
    *,
    metric: str = 'decay_t30',
    origin: str = 'standard',
    required_capability: str = 'predicted_t30',
    role: str = 'objective',
    allowed_basis: str = 'predicted_or_measured',
) -> AcousticTargetCriterion:
    return AcousticTargetCriterion(
        criterion_id=criterion_id,
        name=criterion_id,
        metric=metric,
        metric_version='iso3382-2:2008-method',
        origin=origin,
        source=SOURCE if origin != 'user_goal' else None,
        bands=(_band(),),
        rule=CriterionRule(operator='range', minimum=0.2, maximum=0.5),
        unit='s',
        aggregation='spatial_mean',
        population_entity_ids=('seat-1',),
        required_capability=required_capability,
        role=role,
        allowed_basis=allowed_basis,
    )


def _profile(criteria=None, **kwargs) -> AcousticPerformanceTargetProfile:
    payload = dict(
        profile_id='apt-1',
        profile_version='1',
        name='Main targets',
        document_id='doc-1',
        criteria=(_criterion(),) if criteria is None else criteria,
        created_at_utc=NOW,
    )
    payload.update(kwargs)
    return build_acoustic_target_profile(**payload)


def _observation(**overrides) -> AcousticTargetObservation:
    payload = dict(
        criterion_id='t30-main',
        band_id='125-250',
        observed_value=0.35,
        unit='s',
        evidence_basis='predicted',
        provided_capability='predicted_t30',
        entity_ids=('seat-1',),
    )
    payload.update(overrides)
    return AcousticTargetObservation(**payload)


def test_profile_is_content_addressed_and_versioned():
    profile = _profile()
    assert len(profile.target_semantic_hash) == 64
    same = _profile()
    assert same.target_semantic_hash == profile.target_semantic_hash
    revised = _profile(supersedes_profile_id=profile.profile_id)
    assert revised.target_semantic_hash != profile.target_semantic_hash


def test_metric_kinds_keep_decay_estimators_distinct():
    edt = _criterion('edt', metric='decay_edt', origin='user_goal')
    t30 = _criterion('t30', metric='decay_t30', origin='user_goal')
    assert edt.metric != t30.metric


def test_research_and_standard_origins_require_source():
    criterion = _criterion(origin='research')
    with pytest.raises(ValidationError):
        AcousticTargetCriterion(
            criterion_id='research-no-source',
            name='x',
            metric='decay_t30',
            metric_version='m1',
            origin='research',
            source=None,
            bands=(_band(),),
            rule=CriterionRule(operator='max', maximum=0.5),
            unit='s',
            aggregation='spatial_mean',
            required_capability='predicted_t30',
        )
    assert criterion.origin == 'research'
    _criterion(origin='user_goal')  # user goals need no external source


def test_evaluation_unsupported_when_capability_missing():
    evaluation = evaluate_acoustic_targets(
        profile=_profile(),
        observations=[_observation()],
        available_capabilities=(),
        created_at_utc=NOW,
    )
    result = evaluation.results[0]
    assert result.evaluability == 'UNSUPPORTED'
    assert result.verdict == 'NOT_EVALUATED'
    assert 'predicted_t30' in result.reason


def test_evaluation_unknown_when_band_observation_missing():
    evaluation = evaluate_acoustic_targets(
        profile=_profile(),
        observations=[],
        available_capabilities=('predicted_t30',),
        created_at_utc=NOW,
    )
    assert evaluation.results[0].evaluability == 'UNKNOWN'
    assert evaluation.results[0].verdict == 'NOT_EVALUATED'


def test_evaluation_blocked_when_basis_not_allowed():
    criterion = _criterion(allowed_basis='measured_only')
    evaluation = evaluate_acoustic_targets(
        profile=_profile(criteria=(criterion,)),
        observations=[_observation(evidence_basis='predicted')],
        available_capabilities=('predicted_t30',),
        created_at_utc=NOW,
    )
    result = evaluation.results[0]
    assert result.evaluability == 'BLOCKED'
    assert result.verdict == 'NOT_EVALUATED'


def test_evaluation_met_unmet_and_measured_distinction():
    met = evaluate_acoustic_targets(
        profile=_profile(),
        observations=[_observation(observed_value=0.3, evidence_basis='measured')],
        available_capabilities=('predicted_t30',),
        created_at_utc=NOW,
    )
    assert met.results[0].evaluability == 'AVAILABLE'
    assert met.results[0].verdict == 'MET'
    assert met.results[0].band_results[0].basis == 'measured'

    unmet = evaluate_acoustic_targets(
        profile=_profile(),
        observations=[_observation(observed_value=0.9)],
        available_capabilities=('predicted_t30',),
        created_at_utc=NOW,
    )
    assert unmet.results[0].verdict == 'UNMET'


def test_evaluation_is_bound_to_exact_profile_version():
    profile = _profile()
    evaluation = evaluate_acoustic_targets(
        profile=profile,
        observations=[_observation()],
        available_capabilities=('predicted_t30',),
        created_at_utc=NOW,
    )
    assert evaluation.is_bound_to(profile)
    revised = _profile(profile_version='2')
    assert not evaluation.is_bound_to(revised)


def test_invalid_profile_is_rejected():
    with pytest.raises(ValidationError):
        AcousticPerformanceTargetProfile(
            profile_id='apt-1',
            profile_version='1',
            name='x',
            document_id='doc-1',
            criteria=(_criterion(),),
            created_at_utc=NOW,
            target_semantic_hash='0' * 64,
        )
