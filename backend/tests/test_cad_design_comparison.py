from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_design_comparison import (
    ComparisonAlternative,
    ComparisonEvidenceRef,
    build_alternative,
    build_comparison_set,
    diff_alternatives,
    evaluate_comparison_set,
)
from htdt.cad_design_comparison_repository import CadDesignComparisonRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)

NOW = '2026-09-23T00:00:00+00:00'


def _scene(document_id: str = 'doc-1', fl_x: float = 1.2) -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        room=RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=fl_x, y_m=0.8, z_m=1.0),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
            ),
        ),
    )


def _revisions(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision_a = scene_repository.save(
        _scene(fl_x=1.2), parent_revision_id=None
    ).revision
    revision_b = scene_repository.save(
        _scene(fl_x=1.6), parent_revision_id=revision_a.revision_id
    ).revision
    repository = CadDesignComparisonRepository(scene_repository)
    return scene_repository, revision_a, revision_b, repository


def _alternative(revision, label: str, **overrides) -> ComparisonAlternative:
    return build_alternative(
        label=label,
        scene_revision=revision,
        created_at_utc=NOW,
        **overrides,
    )


def test_alternative_pins_exact_scene_revision(tmp_path: Path) -> None:
    _scenes, rev_a, _rev_b, _repo = _revisions(tmp_path)
    alternative = _alternative(
        rev_a,
        '案A: 5.1.2',
        evidence_refs=(
            ComparisonEvidenceRef(
                kind='prediction', ref_id='pred-1', ref_sha256='a' * 64
            ),
        ),
        view_ref='view-mlp-wide',
        semantic_change_summary='サラウンド追加',
    )
    assert alternative.scene_revision_id == rev_a.revision_id
    assert alternative.scene_content_hash == rev_a.content_hash
    assert alternative.view_ref == 'view-mlp-wide'


def test_set_supersede_chain_tracks_revisions(tmp_path: Path) -> None:
    _scenes, rev_a, rev_b, repository = _revisions(tmp_path)
    first = build_comparison_set(
        document_id=rev_a.document_id,
        name='2026-09 設計比較',
        alternatives=(
            _alternative(rev_a, '案A'),
            _alternative(rev_b, '案B'),
        ),
        created_at_utc=NOW,
    )
    repository.save_set(first)
    second = build_comparison_set(
        document_id=rev_a.document_id,
        name='2026-09 設計比較',
        alternatives=(
            _alternative(rev_a, '案A'),
            _alternative(rev_b, '案B (改)'),
        ),
        supersedes=first,
        created_at_utc='2026-09-23T01:00:00+00:00',
    )
    repository.save_set(second)
    assert second.revision == 2
    assert second.supersedes_set_id == first.set_id
    latest = repository.latest_sets(rev_a.document_id)
    assert [item.set_id for item in latest] == [second.set_id]


def test_supersede_across_documents_is_rejected(tmp_path: Path) -> None:
    _scenes, rev_a, _rev_b, _repo = _revisions(tmp_path)
    first = build_comparison_set(
        document_id=rev_a.document_id,
        name='set',
        alternatives=(_alternative(rev_a, '案A'),),
        created_at_utc=NOW,
    )
    with pytest.raises(ValueError, match='another document'):
        build_comparison_set(
            document_id='other-doc',
            name='set',
            alternatives=(_alternative(rev_a, '案A'),),
            supersedes=first,
            created_at_utc=NOW,
        )


def test_alternative_labels_must_be_unique(tmp_path: Path) -> None:
    _scenes, rev_a, _rev_b, _repo = _revisions(tmp_path)
    with pytest.raises(ValidationError, match='label'):
        build_comparison_set(
            document_id=rev_a.document_id,
            name='set',
            alternatives=(
                _alternative(rev_a, '案A'),
                _alternative(rev_a, '案A'),
            ),
            created_at_utc=NOW,
        )


def test_diff_is_semantic_not_revision_id(tmp_path: Path) -> None:
    _scenes, rev_a, rev_b, _repo = _revisions(tmp_path)
    same = _alternative(rev_a, '案A')
    changed = _alternative(rev_b, '案B')
    diff = diff_alternatives(
        same,
        changed,
        before_document=rev_a.document,
        after_document=rev_b.document,
    )
    assert diff.scene_changed
    assert diff.scene_diff is not None
    assert not diff.scene_diff.is_empty
    # identical pins never report a scene change
    identical = diff_alternatives(
        same,
        _alternative(rev_a, '案A-別名'),
        before_document=rev_a.document,
        after_document=rev_a.document,
    )
    assert not identical.scene_changed


def test_evidence_diff_reports_added_removed_changed(tmp_path: Path) -> None:
    _scenes, rev_a, rev_b, _repo = _revisions(tmp_path)
    before = _alternative(
        rev_a,
        '案A',
        evidence_refs=(
            ComparisonEvidenceRef(
                kind='prediction', ref_id='p-old', ref_sha256='a' * 64
            ),
            ComparisonEvidenceRef(kind='measurement', ref_id='m-1'),
        ),
    )
    after = _alternative(
        rev_b,
        '案B',
        evidence_refs=(
            ComparisonEvidenceRef(
                kind='prediction', ref_id='p-old', ref_sha256='b' * 64
            ),
            ComparisonEvidenceRef(kind='robustness', ref_id='r-1'),
        ),
    )
    diff = diff_alternatives(before, after)
    assert diff.evidence_added == ('robustness:r-1',)
    assert diff.evidence_removed == ('measurement:m-1',)
    assert diff.evidence_changed == ('prediction:p-old',)


def test_availability_never_drops_incompatible_evidence(tmp_path: Path) -> None:
    _scenes, rev_a, _rev_b, repository = _revisions(tmp_path)
    comparison_set = build_comparison_set(
        document_id=rev_a.document_id,
        name='set',
        alternatives=(
            _alternative(
                rev_a,
                '案A',
                evidence_refs=(
                    ComparisonEvidenceRef(
                        kind='prediction', ref_id='p-ok', ref_sha256='a' * 64
                    ),
                    ComparisonEvidenceRef(
                        kind='prediction', ref_id='p-stale', ref_sha256='b' * 64
                    ),
                    ComparisonEvidenceRef(kind='validation', ref_id='v-gone'),
                ),
            ),
        ),
        created_at_utc=NOW,
    )
    repository.save_set(comparison_set)
    availability = evaluate_comparison_set(
        comparison_set,
        evidence_exists={
            ('prediction', 'p-ok'): 'a' * 64,
            ('prediction', 'p-stale'): '9' * 64,
        },
    )
    states = {item.ref_id: item.state for item in availability.items}
    assert states == {
        'p-ok': 'available',
        'p-stale': 'incompatible_baseline',
        'v-gone': 'unresolvable',
    }


def test_save_set_rejects_unpersisted_scene_revision(tmp_path: Path) -> None:
    _scenes, rev_a, _rev_b, repository = _revisions(tmp_path)
    ghost_revision = replace(rev_a, revision_id='ghost-revision')
    alternative = _alternative(ghost_revision, '案A')
    comparison_set = build_comparison_set(
        document_id=rev_a.document_id,
        name='set',
        alternatives=(alternative,),
        created_at_utc=NOW,
    )
    with pytest.raises(ValueError, match='not persisted'):
        repository.save_set(comparison_set)
