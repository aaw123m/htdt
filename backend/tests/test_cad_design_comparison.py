from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_authority_refs import ResolvedAuthority
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


class _StubResolver:
    """Canned AuthorityRefResolver for save-time ref validation."""

    def __init__(
        self,
        entries: dict[tuple[str, str], ResolvedAuthority | None],
    ) -> None:
        self.entries = entries

    def knows(self, kind: str) -> bool:
        return True

    def resolve(
        self, kind: str, ref_id: str, document_id: str
    ) -> ResolvedAuthority | None:
        return self.entries.get((kind, ref_id))


def _resolved_for(
    revision,
    semantic_sha256: str,
    **fields,
) -> ResolvedAuthority:
    return ResolvedAuthority(
        document_id=revision.document_id,
        semantic_sha256=semantic_sha256,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        **fields,
    )


def test_availability_never_drops_incompatible_evidence(tmp_path: Path) -> None:
    scenes, rev_a, _rev_b, _repo = _revisions(tmp_path)
    authorities = {
        ('prediction', 'p-ok'): _resolved_for(rev_a, 'a' * 64),
        ('prediction', 'p-stale'): _resolved_for(rev_a, 'b' * 64),
        ('validation', 'v-gone'): ResolvedAuthority(
            document_id=rev_a.document_id,
            semantic_sha256='c' * 64,
        ),
    }
    repository = CadDesignComparisonRepository(
        scenes, ref_resolver=_StubResolver(authorities)
    )
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
                    ComparisonEvidenceRef(
                        kind='validation',
                        ref_id='v-gone',
                        ref_sha256='c' * 64,
                    ),
                ),
            ),
        ),
        created_at_utc=NOW,
    )
    repository.save_set(comparison_set)
    availability = evaluate_comparison_set(
        comparison_set,
        resolved_evidence={
            **authorities,
            # p-stale drifted after save; v-gone no longer resolves.
            ('prediction', 'p-stale'): _resolved_for(rev_a, '9' * 64),
            ('validation', 'v-gone'): None,
        },
    )
    states = {item.ref_id: item.state for item in availability.items}
    assert states == {
        'p-ok': 'available',
        'p-stale': 'semantic_hash_conflict',
        'v-gone': 'missing_reference',
    }


def test_scene_bound_evidence_on_other_scene_is_baseline_mismatch(
    tmp_path: Path,
) -> None:
    scenes, rev_a, rev_b, _repo = _revisions(tmp_path)
    # A prediction produced under scene A attached to a scene-B alternative
    # is a baseline mismatch, not an availability or hash problem.
    authorities = {
        ('prediction', 'p-scene-a'): _resolved_for(rev_a, 'a' * 64),
    }
    repository = CadDesignComparisonRepository(
        scenes, ref_resolver=_StubResolver(authorities)
    )
    comparison_set = build_comparison_set(
        document_id=rev_a.document_id,
        name='set',
        alternatives=(
            _alternative(
                rev_b,
                '案B',
                evidence_refs=(
                    ComparisonEvidenceRef(
                        kind='prediction',
                        ref_id='p-scene-a',
                        ref_sha256='a' * 64,
                    ),
                ),
            ),
        ),
        created_at_utc=NOW,
    )
    repository.save_set(comparison_set)
    availability = evaluate_comparison_set(
        comparison_set, resolved_evidence=authorities
    )
    (item,) = availability.items
    assert item.state == 'incompatible_baseline'


def test_historical_scene_evidence_stays_available_after_head_advances(
    tmp_path: Path,
) -> None:
    scenes, rev_a, _rev_b, _repo = _revisions(tmp_path)
    # Advance head past the evidence's pinned baseline.
    scenes.save(
        _scene(fl_x=2.0), parent_revision_id=_rev_b.revision_id
    )
    authorities = {
        ('prediction', 'p-old'): _resolved_for(rev_a, 'a' * 64),
    }
    repository = CadDesignComparisonRepository(
        scenes, ref_resolver=_StubResolver(authorities)
    )
    comparison_set = build_comparison_set(
        document_id=rev_a.document_id,
        name='set',
        alternatives=(
            _alternative(
                rev_a,
                '案A',
                evidence_refs=(
                    ComparisonEvidenceRef(
                        kind='prediction',
                        ref_id='p-old',
                        ref_sha256='a' * 64,
                    ),
                ),
            ),
        ),
        created_at_utc=NOW,
    )
    repository.save_set(comparison_set)
    availability = evaluate_comparison_set(
        comparison_set, resolved_evidence=authorities
    )
    (item,) = availability.items
    assert item.state == 'available'


def test_save_rejects_unresolved_system_variant(tmp_path: Path) -> None:
    scenes, rev_a, _rev_b, _repo = _revisions(tmp_path)
    repository = CadDesignComparisonRepository(
        scenes, ref_resolver=_StubResolver({})
    )
    comparison_set = build_comparison_set(
        document_id=rev_a.document_id,
        name='set',
        alternatives=(
            _alternative(
                rev_a,
                '案A',
                system_variant_id='variant-ghost',
                system_variant_sha256='f' * 64,
            ),
        ),
        created_at_utc=NOW,
    )
    with pytest.raises(ValueError, match='does not'):
        repository.save_set(comparison_set)


def test_save_rejects_system_variant_hash_drift(tmp_path: Path) -> None:
    scenes, rev_a, _rev_b, _repo = _revisions(tmp_path)
    authorities = {
        ('system_variant', 'variant-1'): ResolvedAuthority(
            document_id=rev_a.document_id,
            semantic_sha256='a' * 64,
            system_variant_id='variant-1',
        ),
    }
    repository = CadDesignComparisonRepository(
        scenes, ref_resolver=_StubResolver(authorities)
    )
    comparison_set = build_comparison_set(
        document_id=rev_a.document_id,
        name='set',
        alternatives=(
            _alternative(
                rev_a,
                '案A',
                system_variant_id='variant-1',
                system_variant_sha256='b' * 64,
            ),
        ),
        created_at_utc=NOW,
    )
    with pytest.raises(ValueError, match='hash does not match'):
        repository.save_set(comparison_set)


def test_save_rejects_foreign_project_checkpoint(tmp_path: Path) -> None:
    scenes, rev_a, _rev_b, _repo = _revisions(tmp_path)
    authorities = {
        ('design_checkpoint', 'cp-foreign'): ResolvedAuthority(
            document_id='other-doc',
            semantic_sha256='a' * 64,
            scene_revision_id='rev-foreign',
            scene_content_hash='e' * 64,
        ),
    }
    repository = CadDesignComparisonRepository(
        scenes, ref_resolver=_StubResolver(authorities)
    )
    comparison_set = build_comparison_set(
        document_id=rev_a.document_id,
        name='set',
        alternatives=(
            _alternative(
                rev_a,
                '案A',
                design_checkpoint_id='cp-foreign',
                design_checkpoint_sha256='a' * 64,
            ),
        ),
        created_at_utc=NOW,
    )
    with pytest.raises(ValueError, match='another document'):
        repository.save_set(comparison_set)


def test_save_rejects_hashless_ref_to_hash_bearing_authority(
    tmp_path: Path,
) -> None:
    scenes, rev_a, _rev_b, _repo = _revisions(tmp_path)
    authorities = {
        ('prediction', 'p-1'): _resolved_for(rev_a, 'a' * 64),
    }
    repository = CadDesignComparisonRepository(
        scenes, ref_resolver=_StubResolver(authorities)
    )
    comparison_set = build_comparison_set(
        document_id=rev_a.document_id,
        name='set',
        alternatives=(
            _alternative(
                rev_a,
                '案A',
                evidence_refs=(
                    ComparisonEvidenceRef(
                        kind='prediction', ref_id='p-1'
                    ),
                ),
            ),
        ),
        created_at_utc=NOW,
    )
    with pytest.raises(ValueError, match='must pin'):
        repository.save_set(comparison_set)


def test_hash_conflict_is_not_a_baseline_conflict(tmp_path: Path) -> None:
    scenes, rev_a, rev_b, _repo = _revisions(tmp_path)
    # Same-baseline evidence with a wrong hash is a semantic conflict;
    # a different-baseline evidence with its correct hash is not.
    authorities = {
        ('prediction', 'p-wrong-hash'): _resolved_for(rev_a, 'a' * 64),
        ('prediction', 'p-wrong-scene'): _resolved_for(rev_b, 'b' * 64),
    }
    repository = CadDesignComparisonRepository(
        scenes, ref_resolver=_StubResolver(authorities)
    )
    comparison_set = build_comparison_set(
        document_id=rev_a.document_id,
        name='set',
        alternatives=(
            _alternative(
                rev_a,
                '案A',
                evidence_refs=(
                    ComparisonEvidenceRef(
                        kind='prediction',
                        ref_id='p-wrong-hash',
                        ref_sha256='a' * 64,
                    ),
                    ComparisonEvidenceRef(
                        kind='prediction',
                        ref_id='p-wrong-scene',
                        ref_sha256='b' * 64,
                    ),
                ),
            ),
        ),
        created_at_utc=NOW,
    )
    repository.save_set(comparison_set)
    availability = evaluate_comparison_set(
        comparison_set,
        resolved_evidence={
            **authorities,
            # Same-scene authority whose hash drifted after save.
            ('prediction', 'p-wrong-hash'): _resolved_for(rev_a, '9' * 64),
        },
    )
    states = {item.ref_id: item.state for item in availability.items}
    assert states == {
        'p-wrong-hash': 'semantic_hash_conflict',
        'p-wrong-scene': 'incompatible_baseline',
    }


def test_other_evidence_kind_reports_unsupported(tmp_path: Path) -> None:
    scenes, rev_a, _rev_b, _repo = _revisions(tmp_path)
    repository = CadDesignComparisonRepository(
        scenes, ref_resolver=_StubResolver({})
    )
    comparison_set = build_comparison_set(
        document_id=rev_a.document_id,
        name='set',
        alternatives=(
            _alternative(
                rev_a,
                '案A',
                evidence_refs=(
                    ComparisonEvidenceRef(kind='other', ref_id='x-1'),
                ),
            ),
        ),
        created_at_utc=NOW,
    )
    repository.save_set(comparison_set)
    availability = evaluate_comparison_set(
        comparison_set, resolved_evidence={}
    )
    (item,) = availability.items
    assert item.state == 'unsupported'


def test_variant_scoped_evidence_on_other_variant_is_context_mismatch(
    tmp_path: Path,
) -> None:
    scenes, rev_a, _rev_b, _repo = _revisions(tmp_path)
    authorities = {
        ('standards', 'std-1'): ResolvedAuthority(
            document_id=rev_a.document_id,
            semantic_sha256='a' * 64,
            system_variant_id='variant-2',
        ),
        ('system_variant', 'variant-1'): ResolvedAuthority(
            document_id=rev_a.document_id,
            semantic_sha256='d' * 64,
            system_variant_id='variant-1',
        ),
    }
    repository = CadDesignComparisonRepository(
        scenes, ref_resolver=_StubResolver(authorities)
    )
    comparison_set = build_comparison_set(
        document_id=rev_a.document_id,
        name='set',
        alternatives=(
            _alternative(
                rev_a,
                '案A',
                system_variant_id='variant-1',
                system_variant_sha256='d' * 64,
                evidence_refs=(
                    ComparisonEvidenceRef(
                        kind='standards',
                        ref_id='std-1',
                        ref_sha256='a' * 64,
                        scene_bound=False,
                    ),
                ),
            ),
        ),
        created_at_utc=NOW,
    )
    repository.save_set(comparison_set)
    availability = evaluate_comparison_set(
        comparison_set, resolved_evidence=authorities
    )
    (item,) = availability.items
    assert item.state == 'context_mismatch'


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
