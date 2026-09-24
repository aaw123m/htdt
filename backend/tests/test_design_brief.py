from pathlib import Path

import pytest

from htdt.cad_authority_resolver import ResolvedAuthority
from htdt.cad_design_brief import (
    BriefGoalRef,
    build_design_brief,
    evaluate_brief_coverage,
    revise_design_brief,
)
from htdt.cad_design_brief_repository import (
    CadDesignBriefRepository,
    DesignBriefConflictError,
)
from htdt.cad_repository import SceneRepository


DOC = 'doc-1'
PROFILE_SHA = 'a' * 64


def _repository(tmp_path: Path) -> CadDesignBriefRepository:
    return CadDesignBriefRepository(
        SceneRepository(tmp_path / 'scene.sqlite3'),
        kind_resolvers={
            'standards_profile': lambda ref_id: ResolvedAuthority(
                kind='standards_profile',
                ref_id=ref_id,
                document_id=DOC,
                semantic_sha256=PROFILE_SHA,
            )
        },
    )


def _brief(document_id: str = DOC, **kwargs):
    return build_design_brief(
        document_id=document_id,
        title='Theater goals',
        use_labels=('映画', 'ゲーム'),
        goal_refs=kwargs.pop(
            'goal_refs',
            (
                BriefGoalRef(
                    goal_id='goal-standards',
                    kind='standards_profile',
                    requirement='required',
                    ref_id='profile-smpte',
                    ref_sha256=PROFILE_SHA,
                    label='SMPTE RP 22 alignment',
                ),
                BriefGoalRef(
                    goal_id='goal-note',
                    kind='free_text',
                    requirement='informational',
                    label='Quiet HVAC preferred',
                ),
            ),
        ),
        created_at_utc='2026-09-24T00:00:00+00:00',
        **kwargs,
    )


def test_design_brief_hashes_and_goal_lookup() -> None:
    brief = _brief()
    assert brief.brief_sha256 == brief.brief_sha256.lower()
    assert len(brief.brief_sha256) == 64
    assert brief.goal('goal-standards') is not None
    assert brief.goal('missing') is None


def test_design_brief_rejects_duplicate_and_bad_refs() -> None:
    with pytest.raises(ValueError):
        BriefGoalRef(
            goal_id='g1',
            kind='free_text',
            requirement='required',
            ref_id='must-not-exist',
            label='free text cannot reference authority',
        )
    with pytest.raises(ValueError):
        BriefGoalRef(
            goal_id='g2',
            kind='target_curve',
            requirement='required',
            label='non-free-text requires ref_id',
        )
    with pytest.raises(ValueError):
        BriefGoalRef(
            goal_id='g2',
            kind='target_curve',
            requirement='required',
            ref_id='curve-1',
            ref_sha256='not-a-hash',
        )
    with pytest.raises(ValueError):
        build_design_brief(
            document_id=DOC,
            title='dup',
            goal_refs=(
                BriefGoalRef(
                    goal_id='g',
                    kind='other',
                    requirement='required',
                    ref_id='r1',
                    ref_sha256='a' * 64,
                ),
                BriefGoalRef(
                    goal_id='g',
                    kind='other',
                    requirement='required',
                    ref_id='r2',
                    ref_sha256='b' * 64,
                ),
            ),
            created_at_utc='2026-09-24T00:00:00+00:00',
        )


def test_design_brief_repository_is_append_only(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    brief = _brief()
    repository.save_brief(brief)
    assert repository.get_brief(brief.brief_id) == brief
    with pytest.raises(DesignBriefConflictError):
        repository.save_brief(brief)


def test_design_brief_revision_chain_and_latest(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    first = _brief()
    repository.save_brief(first)
    revised = revise_design_brief(
        first,
        title='Theater goals v2',
        created_at_utc='2026-09-24T01:00:00+00:00',
    )
    assert revised.supersedes_brief_id == first.brief_id
    assert revised.brief_sha256 != first.brief_sha256
    repository.save_brief(revised)
    assert repository.latest_brief(DOC) == revised
    assert repository.latest_brief('never-saved') is None


def test_design_brief_revision_requires_stored_prior(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    first = _brief()
    revised = revise_design_brief(
        first, created_at_utc='2026-09-24T01:00:00+00:00'
    )
    with pytest.raises(ValueError):
        repository.save_brief(revised)


def test_brief_save_resolves_exact_goal_refs(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    stale = _brief(
        goal_refs=(
            BriefGoalRef(
                goal_id='goal-standards',
                kind='standards_profile',
                requirement='required',
                ref_id='profile-smpte',
                ref_sha256='f' * 64,
                label='stale hash pin',
            ),
        ),
    )
    with pytest.raises(ValueError):
        repository.save_brief(stale)

    ghost = _brief(
        goal_refs=(
            BriefGoalRef(
                goal_id='goal-curve',
                kind='target_curve',
                requirement='required',
                ref_id='curve-1',
                ref_sha256='b' * 64,
                label='unresolvable kind',
            ),
        ),
    )
    with pytest.raises(ValueError):
        repository.save_brief(ghost)

    repository.save_brief(_brief())


def test_design_brief_not_configured_is_explicit(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    assert repository.latest_brief(DOC) is None


def test_brief_coverage_reports_current_stale_missing() -> None:
    brief = _brief()
    coverage = evaluate_brief_coverage(
        brief,
        resolve_sha256={
            ('standards_profile', 'profile-smpte'): PROFILE_SHA,
        },
    )
    by_goal = {item.goal_id: item for item in coverage.goals}
    assert by_goal['goal-standards'].state == 'current'
    assert by_goal['goal-note'].state == 'unevaluable'
    assert coverage.evaluable_goal_ids == ('goal-standards',)

    coverage = evaluate_brief_coverage(
        brief,
        resolve_sha256={
            ('standards_profile', 'profile-smpte'): 'b' * 64,
        },
    )
    by_goal = {item.goal_id: item for item in coverage.goals}
    assert by_goal['goal-standards'].state == 'stale'

    coverage = evaluate_brief_coverage(brief, resolve_sha256={})
    by_goal = {item.goal_id: item for item in coverage.goals}
    assert by_goal['goal-standards'].state == 'missing'


def test_design_brief_roundtrip_through_document() -> None:
    brief = _brief()
    restored = type(brief).model_validate_json(brief.model_dump_json())
    assert restored == brief
