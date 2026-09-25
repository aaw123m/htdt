from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_design_comparison import (
    build_alternative,
    build_comparison_set,
)
from htdt.cad_design_comparison_repository import CadDesignComparisonRepository
from htdt.cad_design_decision import (
    DecisionAuthorityRef,
    build_decision_record,
    current_decisions,
)
from htdt.cad_design_decision_repository import (
    CadDesignDecisionRepository,
    DesignDecisionConflictError,
    DesignDecisionRefError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import SceneDocument, make_empty_scene

NOW = '2026-09-24T00:00:00+00:00'


def _ref(
    kind: str,
    ref_id: str,
    label: str | None = None,
    ref_sha256: str | None = None,
) -> DecisionAuthorityRef:
    return DecisionAuthorityRef(
        kind=kind, ref_id=ref_id, label=label, ref_sha256=ref_sha256
    )


def _authorities(tmp_path: Path):
    """Real canonical authorities the test decisions can name (#797).

    Every decision ref below resolves to a persisted authority inside
    doc-1: two alternatives inside one comparison set, bound to the
    document's scene revision.
    """

    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_empty_scene('doc-1'), parent_revision_id=None
    ).revision
    comparison_repository = CadDesignComparisonRepository(scene_repository)
    alt_a = build_alternative(
        label='案A', scene_revision=revision,
        created_at_utc=NOW, alternative_id='alt-a',
    )
    alt_b = build_alternative(
        label='案B', scene_revision=revision,
        created_at_utc=NOW, alternative_id='alt-b',
    )
    comparison_set = build_comparison_set(
        document_id='doc-1',
        name='speaker layout options',
        alternatives=(alt_a, alt_b),
        created_at_utc=NOW,
        set_id='set-1',
    )
    comparison_repository.save_set(comparison_set)
    repository = CadDesignDecisionRepository(scene_repository)
    return repository, comparison_set, (alt_a, alt_b)


def _alt_ref(alternative, label: str) -> DecisionAuthorityRef:
    return _ref(
        'comparison_alternative',
        alternative.alternative_id,
        label,
        ref_sha256=alternative.alternative_sha256,
    )


def _set_ref(comparison_set) -> DecisionAuthorityRef:
    return _ref(
        'design_comparison_set',
        comparison_set.set_id,
        ref_sha256=comparison_set.set_sha256,
    )


def _decision(comparison_set=None, alternatives=(), **overrides):
    if alternatives:
        selected = _alt_ref(alternatives[1], '案B')
        considered = (
            _alt_ref(alternatives[0], '案A'),
            _alt_ref(alternatives[1], '案B'),
        )
    else:
        selected = _ref('comparison_alternative', 'alt-b', '案B')
        considered = (
            _ref('comparison_alternative', 'alt-a', '案A'),
            _ref('comparison_alternative', 'alt-b', '案B'),
        )
    options = dict(
        document_id='doc-1',
        title='5.1.4 のスピーカー構成を採用',
        decision_scope='design_direction',
        lifecycle_intent='choose_for_design',
        selected_ref=selected,
        considered_refs=considered,
        comparison_set_ref=(
            _set_ref(comparison_set)
            if comparison_set is not None
            else _ref('design_comparison_set', 'set-1')
        ),
        created_at_utc=NOW,
        rationale_tags=('acoustic_performance', 'installation_feasibility'),
        rationale_note='案Bはサブの配置自由度が高い',
        accepted_tradeoffs=('設置コストがやや増える',),
    )
    options.update(overrides)
    return build_decision_record(**options)


def test_decision_record_is_deterministic_and_hashed() -> None:
    record = _decision()
    assert len(record.decision_sha256) == 64
    assert record.selected_ref.ref_id == 'alt-b'
    assert record.decision_scope == 'design_direction'
    assert 'acoustic_performance' in record.rationale_tags


def test_selected_ref_must_be_among_considered() -> None:
    with pytest.raises(ValidationError, match='one of considered_refs'):
        _decision(selected_ref=_ref('comparison_alternative', 'alt-c'))


def test_considered_refs_must_be_unique() -> None:
    with pytest.raises(ValidationError, match='unique'):
        _decision(
            selected_ref=_ref('comparison_alternative', 'alt-a'),
            considered_refs=(
                _ref('comparison_alternative', 'alt-a'),
                _ref('comparison_alternative', 'alt-a'),
            ),
        )


def test_custom_scope_requires_label() -> None:
    with pytest.raises(ValidationError, match='custom_scope_label'):
        _decision(decision_scope='custom', custom_scope_label=None)
    record = _decision(decision_scope='custom', custom_scope_label='配線優先')
    assert record.custom_scope_label == '配線優先'


def test_assumption_refs_kind_is_checked() -> None:
    with pytest.raises(ValidationError, match='assumption'):
        _decision(accepted_assumption_refs=(_ref('scene_revision', 'rev-1'),))
    record = _decision(accepted_assumption_refs=(_ref('assumption_decision', 'asum-1'),))
    assert record.accepted_assumption_refs[0].kind == 'assumption_decision'


def test_repository_round_trip_and_idempotent_save(tmp_path: Path) -> None:
    repository, comparison_set, alternatives = _authorities(tmp_path)
    record = _decision(comparison_set=comparison_set, alternatives=alternatives)
    repository.save_decision(record)
    repository.save_decision(record)  # same content is a no-op
    loaded = repository.get_decision(record.decision_id)
    assert loaded == record


def test_repository_rejects_conflicting_reuse(tmp_path: Path) -> None:
    repository, comparison_set, alternatives = _authorities(tmp_path)
    record = _decision(comparison_set=comparison_set, alternatives=alternatives)
    repository.save_decision(record)
    conflicting = _decision(
        comparison_set=comparison_set, alternatives=alternatives,
        title='別タイトル', decision_id=record.decision_id,
    )
    with pytest.raises(DesignDecisionConflictError):
        repository.save_decision(conflicting)


def test_supersede_links_and_projects_current(tmp_path: Path) -> None:
    repository, comparison_set, alternatives = _authorities(tmp_path)
    first = _decision(comparison_set=comparison_set, alternatives=alternatives)
    repository.save_decision(first)
    second = _decision(
        comparison_set=comparison_set, alternatives=alternatives,
        supersedes=first, created_at_utc='2026-09-24T01:00:00+00:00',
    )
    repository.save_decision(second)
    all_decisions = repository.list_decisions('doc-1')
    assert len(all_decisions) == 2
    current = current_decisions(all_decisions)
    assert [item.decision_id for item in current] == [second.decision_id]


def test_supersede_requires_persisted_target_same_document(tmp_path: Path) -> None:
    repository, comparison_set, alternatives = _authorities(tmp_path)
    first = _decision(comparison_set=comparison_set, alternatives=alternatives)
    other = _decision(
        comparison_set=comparison_set, alternatives=alternatives,
        document_id='doc-2',
    )
    with pytest.raises(ValueError, match='another document'):
        _decision(supersedes=other)
    repository.save_decision(first)
    with pytest.raises(ValueError, match='not persisted'):
        repository.save_decision(
            _decision(
                comparison_set=comparison_set, alternatives=alternatives,
                supersedes=_decision(title='ghost'),
            )
        )


def test_list_decisions_filters(tmp_path: Path) -> None:
    repository, comparison_set, alternatives = _authorities(tmp_path)
    repository.save_decision(
        _decision(comparison_set=comparison_set, alternatives=alternatives)
    )
    repository.save_decision(
        _decision(
            comparison_set=comparison_set, alternatives=alternatives,
            title='設置案の妥協',
            decision_scope='installation_concession',
            created_at_utc='2026-09-24T02:00:00+00:00',
        )
    )
    assert len(repository.list_decisions('doc-1')) == 2
    installs = repository.list_decisions(
        'doc-1', decision_scope='installation_concession'
    )
    assert len(installs) == 1
    assert installs[0].title == '設置案の妥協'
    by_selected = repository.list_decisions('doc-1', selected_ref_id='alt-b')
    assert len(by_selected) == 2


# -- ref integrity (#797) ------------------------------------------------------


def test_save_rejects_unknown_authority(tmp_path: Path) -> None:
    repository, comparison_set, alternatives = _authorities(tmp_path)
    ghost = _decision(
        comparison_set=comparison_set,
        alternatives=alternatives,
        selected_ref=_ref(
            'comparison_alternative', 'alt-ghost', '幽霊案',
            ref_sha256='f' * 64,
        ),
        considered_refs=(
            _alt_ref(alternatives[0], '案A'),
            _ref(
                'comparison_alternative', 'alt-ghost', '幽霊案',
                ref_sha256='f' * 64,
            ),
        ),
    )
    with pytest.raises(DesignDecisionRefError, match='unknown'):
        repository.save_decision(ghost)


def test_save_rejects_foreign_project_ref(tmp_path: Path) -> None:
    """An alternative living only in another document cannot be named."""

    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_empty_scene('doc-1'), parent_revision_id=None
    ).revision
    foreign_revision = scene_repository.save(
        make_empty_scene('doc-2'), parent_revision_id=None
    ).revision
    comparison_repository = CadDesignComparisonRepository(scene_repository)
    local_set = build_comparison_set(
        document_id='doc-1', name='local',
        alternatives=(
            build_alternative(
                label='A', scene_revision=revision,
                created_at_utc=NOW, alternative_id='alt-a',
            ),
            build_alternative(
                label='B', scene_revision=revision,
                created_at_utc=NOW, alternative_id='alt-b',
            ),
        ),
        created_at_utc=NOW, set_id='set-1',
    )
    foreign_set = build_comparison_set(
        document_id='doc-2', name='foreign',
        alternatives=(
            build_alternative(
                label='X', scene_revision=foreign_revision,
                created_at_utc=NOW, alternative_id='alt-x',
            ),
        ),
        created_at_utc=NOW, set_id='set-2',
    )
    comparison_repository.save_set(local_set)
    comparison_repository.save_set(foreign_set)
    repository = CadDesignDecisionRepository(scene_repository)
    foreign_alt = foreign_set.alternatives[0]
    record = _decision(
        comparison_set=local_set,
        alternatives=(local_set.alternatives[0], local_set.alternatives[1]),
        selected_ref=_alt_ref(foreign_alt, '外国案'),
        considered_refs=(
            _alt_ref(local_set.alternatives[0], '案A'),
            _alt_ref(foreign_alt, '外国案'),
        ),
    )
    with pytest.raises(DesignDecisionRefError, match='unknown'):
        repository.save_decision(record)


def test_save_rejects_missing_and_mismatched_hashes(tmp_path: Path) -> None:
    repository, comparison_set, alternatives = _authorities(tmp_path)
    # A hash-bearing authority named by bare id is not an exact ref.
    bare = _decision(
        comparison_set=comparison_set,
        alternatives=alternatives,
        comparison_set_ref=_ref('design_comparison_set', 'set-1'),
    )
    with pytest.raises(DesignDecisionRefError, match='ref_sha256 is required'):
        repository.save_decision(bare)
    wrong = _decision(
        comparison_set=comparison_set,
        alternatives=alternatives,
        comparison_set_ref=_ref(
            'design_comparison_set', 'set-1', ref_sha256='0' * 64
        ),
    )
    with pytest.raises(DesignDecisionRefError, match='semantic hash mismatch'):
        repository.save_decision(wrong)


def test_save_rejects_alternative_not_in_named_set(tmp_path: Path) -> None:
    """Membership claim: named alternatives must belong to the named set."""

    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_empty_scene('doc-1'), parent_revision_id=None
    ).revision
    comparison_repository = CadDesignComparisonRepository(scene_repository)
    set_a = build_comparison_set(
        document_id='doc-1', name='a',
        alternatives=(
            build_alternative(
                label='A', scene_revision=revision,
                created_at_utc=NOW, alternative_id='alt-a',
            ),
        ),
        created_at_utc=NOW, set_id='set-a',
    )
    set_b = build_comparison_set(
        document_id='doc-1', name='b',
        alternatives=(
            build_alternative(
                label='B', scene_revision=revision,
                created_at_utc=NOW, alternative_id='alt-b',
            ),
        ),
        created_at_utc=NOW, set_id='set-b',
    )
    comparison_repository.save_set(set_a)
    comparison_repository.save_set(set_b)
    repository = CadDesignDecisionRepository(scene_repository)
    alt_b = set_b.alternatives[0]
    record = _decision(
        comparison_set=set_a,
        selected_ref=_alt_ref(alt_b, 'B'),
        considered_refs=(_alt_ref(alt_b, 'B'),),
    )
    with pytest.raises(DesignDecisionRefError, match='not a member'):
        repository.save_decision(record)


def test_save_rejects_unknown_kind(tmp_path: Path) -> None:
    """Kinds without a canonical resolver fail closed — 'other' is the only
    escape hatch, and it must name a genuinely external authority."""

    repository, comparison_set, alternatives = _authorities(tmp_path)
    record = _decision(
        comparison_set=comparison_set,
        alternatives=alternatives,
        # 'evidence_gap' is a legal ref kind but has no canonical owner
        # repository, so it cannot prove existence and must fail closed.
        selected_ref=_ref('evidence_gap', 'gap-1'),
        considered_refs=(
            _ref('evidence_gap', 'gap-1'),
            _alt_ref(alternatives[0], '案A'),
        ),
    )
    with pytest.raises(DesignDecisionRefError, match='no canonical resolver'):
        repository.save_decision(record)
