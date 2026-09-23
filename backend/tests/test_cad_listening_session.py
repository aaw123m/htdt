from __future__ import annotations

from hashlib import sha256

import pytest

from htdt.cad_listening_session import (
    AbxAnalysisSpec,
    BlindListeningSession,
    LevelMatchingPolicy,
    ListeningAlternative,
    RandomizationCommitment,
    assess_session_currency,
    build_listening_session_spec,
    exact_binomial_two_sided_p,
    stimulus_parity_preflight,
)


def _hash(label: str) -> str:
    return sha256(label.encode('utf-8')).hexdigest()


def _alternatives() -> tuple[ListeningAlternative, ListeningAlternative]:
    return (
        ListeningAlternative(
            presentation_label='A',
            alternative_id='artifact-A',
            artifact_id='auralization-artifact:a' * 1,
            artifact_sha256=_hash('artifact-A'),
            evidence_kind='predicted_auralization',
        ),
        ListeningAlternative(
            presentation_label='B',
            alternative_id='artifact-B',
            artifact_id='auralization-artifact:b' * 1,
            artifact_sha256=_hash('artifact-B'),
            evidence_kind='measured_auralization',
        ),
    )


def _abx_spec(**overrides):
    kwargs = dict(
        protocol='abx_discrimination',
        alternatives=_alternatives(),
        level_policy=LevelMatchingPolicy(
            kind='rms_matched_window',
            method_id='rms-window',
            method_version='1',
            rms_window_s=0.5,
        ),
        program_material_sha256=_hash('program'),
        excerpt_start_s=1.0,
        excerpt_end_s=5.0,
        planned_scored_trials=12,
        planned_training_trials=2,
        randomization=RandomizationCommitment(
            committed_seed=42, sequence_length=14
        ),
        analysis=AbxAnalysisSpec(),
    )
    kwargs.update(overrides)
    return build_listening_session_spec(**kwargs)


def _preference_spec(**overrides):
    kwargs = dict(
        protocol='ab_preference',
        alternatives=_alternatives(),
        level_policy=LevelMatchingPolicy(
            kind='declared_method',
            method_id='panel-match',
            method_version='1',
        ),
        program_material_sha256=_hash('program'),
        excerpt_start_s=0.0,
        excerpt_end_s=10.0,
        planned_scored_trials=4,
        planned_training_trials=0,
        randomization=RandomizationCommitment(
            committed_seed=7, sequence_length=4
        ),
        analysis=None,
    )
    kwargs.update(overrides)
    return build_listening_session_spec(**kwargs)


def test_spec_identity_and_preregistration_invariants() -> None:
    spec = _abx_spec()
    assert spec.spec_id.startswith('listening-session-spec:')
    with pytest.raises(ValueError, match='analysis spec'):
        _abx_spec(analysis=None)
    with pytest.raises(ValueError, match='sequence length'):
        _abx_spec(
            randomization=RandomizationCommitment(
                committed_seed=42, sequence_length=5
            )
        )
    with pytest.raises(ValueError, match='descriptive'):
        _preference_spec(analysis=AbxAnalysisSpec())


def test_assignment_sequence_is_deterministic_and_committed() -> None:
    spec_a = _abx_spec()
    spec_b = _abx_spec()
    session_a = BlindListeningSession(spec_a)
    session_b = BlindListeningSession(spec_b)
    assert (
        session_a.randomization_commitment_sha256
        == session_b.randomization_commitment_sha256
    )
    spec_c = _abx_spec(
        randomization=RandomizationCommitment(
            committed_seed=43, sequence_length=14
        )
    )
    session_c = BlindListeningSession(spec_c)
    assert (
        session_c.randomization_commitment_sha256
        != session_a.randomization_commitment_sha256
    )


def test_blinded_view_hides_assignment_until_close() -> None:
    spec = _abx_spec()
    session = BlindListeningSession(spec)
    view = session.trial_view(0)
    assert view.kind == 'training'
    assert view.responses_allowed == ('a', 'b')
    # order enforced: cannot skip trial 0
    with pytest.raises(ValueError, match='committed order'):
        session.trial_view(1)


def test_abx_full_completion_gives_exact_binomial() -> None:
    spec = _abx_spec(planned_scored_trials=4, planned_training_trials=0,
                     randomization=RandomizationCommitment(
                         committed_seed=42, sequence_length=4))
    session = BlindListeningSession(spec)
    for index in range(4):
        view = session.trial_view(index)
        assert view.trial_index == index
        record = session.commit_response(
            'a', committed_at_utc='2026-01-01T00:00:00Z'
        )
        assert record.correct is not None
    result = session.close()
    assert result.inferential_state == 'fixed_n_evaluable'
    assert result.exact_binomial_p is not None
    assert result.result_id.startswith('listening-result:')
    assert result.assignments
    # p matches the exact computation over the recorded sequence
    expected = exact_binomial_two_sided_p(
        result.correct_count, result.completed_scored_trials, 0.5
    )
    assert result.exact_binomial_p == expected


def test_aborted_session_loses_fixed_n_inference() -> None:
    spec = _abx_spec()
    session = BlindListeningSession(spec)
    for index in range(4):  # complete only training + 2 scored of 12
        session.trial_view(index)
        session.commit_response('a', committed_at_utc='2026-01-01T00:00:00Z')
    result = session.close()
    assert result.aborted is True
    assert result.inferential_state == 'fixed_n_not_reached'
    assert result.exact_binomial_p is None


def test_preference_results_are_descriptive_only() -> None:
    spec = _preference_spec()
    session = BlindListeningSession(spec)
    for index, response in enumerate(
        ('prefer_a', 'prefer_a', 'prefer_b', 'no_preference')
    ):
        session.trial_view(index)
        session.commit_response(
            response, committed_at_utc='2026-01-01T00:00:00Z'
        )
    result = session.close()
    assert result.inferential_state == 'descriptive_only'
    assert result.exact_binomial_p is None
    assert result.correct_count is None
    assert result.preference_counts == (2, 1, 1)


def test_append_only_trials_cannot_mutate() -> None:
    spec = _abx_spec()
    session = BlindListeningSession(spec)
    session.trial_view(0)
    session.commit_response('a', committed_at_utc='t0')
    # a completed trial index can never be replayed
    with pytest.raises(ValueError, match='committed order'):
        session.trial_view(0)
    with pytest.raises(ValueError, match='closed'):
        session.commit_response('b', committed_at_utc='t1')
        session.close()
        session.commit_response('b', committed_at_utc='t2')


def test_exact_binomial_values() -> None:
    # 0/4 correct under p0=0.5: 2 * P(X<=0) = 2/16 = 0.125
    assert exact_binomial_two_sided_p(0, 4, 0.5) == pytest.approx(0.125)
    # 4/4 correct: same by symmetry
    assert exact_binomial_two_sided_p(4, 4, 0.5) == pytest.approx(0.125)
    # 2/4 correct: 2 * min(P<=2, P>=2) = 2 * 11/16 capped at 1 → 1.0
    assert exact_binomial_two_sided_p(2, 4, 0.5) == pytest.approx(1.0)


def test_stimulus_parity_flags_cues() -> None:
    findings = stimulus_parity_preflight(
        sample_rate_hz_a=48000,
        sample_rate_hz_b=48000,
        channel_count_a=1,
        channel_count_b=1,
        excerpt_bounds_a_s=(1.0, 5.0),
        excerpt_bounds_b_s=(1.0, 5.0),
        clipped_a=False,
        clipped_b=False,
        seamless_switch_supported=True,
    )
    assert all(item.state == 'PASS' for item in findings)
    findings = stimulus_parity_preflight(
        sample_rate_hz_a=48000,
        sample_rate_hz_b=44100,
        channel_count_a=1,
        channel_count_b=1,
        excerpt_bounds_a_s=(1.0, 5.0),
        excerpt_bounds_b_s=(2.0, 5.0),
        clipped_a=True,
        clipped_b=False,
        seamless_switch_supported=False,
    )
    assert all(item.state == 'FAIL' for item in findings)


def test_currency_follows_pinned_authorities() -> None:
    spec = _abx_spec()
    currency = assess_session_currency(
        spec,
        current_alternative_sha256=(
            spec.alternatives[0].artifact_sha256,
            spec.alternatives[1].artifact_sha256,
        ),
        current_program_material_sha256=_hash('program'),
    )
    assert currency.state == 'CURRENT'
    stale = assess_session_currency(
        spec,
        current_alternative_sha256=(_hash('new-A'), _hash('artifact-B')),
        current_program_material_sha256=_hash('program'),
    )
    assert stale.state == 'STALE'
    assert stale.stale_reasons
