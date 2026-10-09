"""#1007: A/B design-alternative diff overlay — authority-driven ghosts.

Covers the overlay preview model (categories straight from
``diff_alternatives``, impossible states for mismatched frames / stale or
unresolvable pins, recorded-reason wording, evidence availability rows),
the viewport overlay lifecycle (named non-pickable ``abdiff-*`` actors,
arrows only for evidenced moves on a shared frame, cleanup), and the
Presentation compare page wiring (横並び / 重畳 / 差分のみ modes, diff
card, narrow/DPI200/UIA reachability).
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from htdt.cad_authority_refs import ResolvedAuthority
from htdt.cad_design_comparison import (
    ComparisonEvidenceRef,
    build_alternative,
    build_comparison_set,
)
from htdt.cad_design_comparison_repository import CadDesignComparisonRepository
from htdt.cad_presentation_repository import CadPresentationRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.design_ab_overlay import (
    AB_EVIDENCE_STATE_LABELS,
    AB_OVERLAY_CATEGORY_VOCAB,
    AB_OVERLAY_DEGRADED_FRAME_NOTE,
    AB_OVERLAY_DISCLAIMER,
    MISSING_REASON_LABEL,
    build_ab_overlay_preview,
)

NOW = '2026-10-09T00:00:00+00:00'


def _entity(entity_id: str, *, x: float = 1.0, name: str | None = None,
            size_x: float = 0.24, kind: str = 'speaker') -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind=kind,
        name=name or entity_id,
        speaker_role='FL' if kind == 'speaker' else None,
        position=Position3(x_m=x, y_m=0.8, z_m=1.0),
        size_m=Size3(x_m=size_x, y_m=0.28, z_m=0.42),
    )


def _scene(*entities: SceneEntity, document_id: str = 'doc-1',
           width: float = 6.0) -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        room=RoomPrism(width_m=width, depth_m=4.5, height_m=2.4),
        entities=entities,
    )


def _save(repository: SceneRepository, scene: SceneDocument,
          parent: str | None = None):
    return repository.save(scene, parent_revision_id=parent).revision


def _alt(revision, label: str, **overrides):
    return build_alternative(
        label=label, scene_revision=revision, created_at_utc=NOW, **overrides,
    )


def _set(revisions, document_id: str, name: str = '設計案比較'):
    return build_comparison_set(
        document_id=document_id,
        name=name,
        alternatives=tuple(
            _alt(revision, label)
            for revision, label in revisions
        ),
        created_at_utc=NOW,
    )


def _resolver_for(overlay_docs: dict):
    """resolve_document stub: ``alternative -> pinned SceneDocument``."""

    def _resolve(alternative):
        return overlay_docs[alternative.scene_revision_id]

    return _resolve


def _ready_fixture(tmp_path: Path):
    """Real repository + presentation repository with an A/B pair.

    doc rev A: speakers fl@x=1.2 + sl + cabinet (cab).
    doc rev B: fl moved to x=1.6, sl removed, sub added, cabinet resized
    (attribute-only), tag entity renamed (attribute-only).
    """
    scenes = SceneRepository(tmp_path / 'cad.sqlite3')
    rev_a = _save(scenes, _scene(
        _entity('fl', x=1.2),
        _entity('sl', x=2.2),
        _entity('cab', x=3.0, kind='furniture', size_x=0.6),
    ))
    rev_b = _save(scenes, _scene(
        _entity('fl', x=1.6),
        _entity('cab', x=3.0, kind='furniture', size_x=0.9),
        _entity('sub', x=4.0, name='サブウーファー'),
    ), parent=rev_a.revision_id)
    presentation = CadPresentationRepository(scenes)
    comparison = CadDesignComparisonRepository(scenes)
    alt_a = _alt(rev_a, '案A')
    alt_b = _alt(rev_b, '案B')
    comparison_set = _set(
        ((rev_a, '案A'), (rev_b, '案B')), rev_a.document_id,
    )
    comparison.save_set(comparison_set)
    return scenes, rev_a, rev_b, presentation, comparison, comparison_set, alt_a, alt_b


# -- preview model ---------------------------------------------------------------


def test_preview_categories_follow_diff_authority(tmp_path: Path) -> None:
    scenes, rev_a, rev_b, presentation, _comp, comparison_set, alt_a, alt_b = (
        _ready_fixture(tmp_path)
    )
    preview = build_ab_overlay_preview(
        comparison_set, alt_a, alt_b,
        resolve_document=presentation.alternative_document,
        head_revision_id=rev_b.revision_id,
    )
    assert preview.state == 'ready'
    by_id = {item.entity_id: item for item in preview.items}
    assert by_id['sl'].category == 'removed'
    assert by_id['sub'].category == 'added'
    assert by_id['fl'].category == 'moved'
    assert by_id['cab'].category == 'attribute_only'
    assert by_id['cab'].changed_fields == ('size_m',)
    assert preview.frame_state == 'shared'
    # No entity was added/removed on both sides silently.
    assert {item.entity_id for item in preview.items} == {
        'sl', 'sub', 'fl', 'cab',
    }


def test_preview_unchanged_entities_are_context_only(tmp_path: Path) -> None:
    scenes, rev_a, _rev_b, presentation, _comp, comparison_set, alt_a, alt_b = (
        _ready_fixture(tmp_path)
    )
    preview = build_ab_overlay_preview(
        comparison_set, alt_a, alt_b,
        resolve_document=presentation.alternative_document,
    )
    # Nothing unchanged in this fixture — pin a pair where fl is identical.
    # A detached revision models a pinned alternative that is not the head.
    rev_c = scenes.save_detached_revision(
        _scene(_entity('fl', x=1.2), _entity('sub', x=9.9)),
        parent_revision_id=rev_a.revision_id,
        reason='test: pinned non-head alternative',
    ).revision
    alt_c = _alt(rev_c, '案C')
    other = build_comparison_set(
        document_id=rev_a.document_id, name='同一チェック',
        alternatives=(alt_a, alt_c), created_at_utc=NOW,
    )
    preview = build_ab_overlay_preview(
        other, alt_a, alt_c,
        resolve_document=presentation.alternative_document,
    )
    assert preview.state == 'ready'
    unchanged = {e.entity_id for e in preview.unchanged_entities}
    assert unchanged == {'fl'}
    # Legend includes the context row only because unchanged exist.
    legend_labels = [label for label, _c in preview.legend]
    assert '同一（変更なし）' in legend_labels


def test_preview_impossible_on_cross_document(tmp_path: Path) -> None:
    scenes = SceneRepository(tmp_path / 'cad.sqlite3')
    rev_a = _save(scenes, _scene(_entity('fl'), document_id='doc-a'))
    rev_foreign = _save(
        scenes, _scene(_entity('fl'), document_id='doc-b'),
    )
    presentation = CadPresentationRepository(scenes)
    comparison_set = build_comparison_set(
        document_id='doc-a',
        name='別プロジェクト混入',
        alternatives=(_alt(rev_a, '案A'), _alt(rev_foreign, '外部案')),
        created_at_utc=NOW,
    )
    alt_a, alt_foreign = comparison_set.alternatives
    preview = build_ab_overlay_preview(
        comparison_set, alt_a, alt_foreign,
        resolve_document=presentation.alternative_document,
    )
    assert preview.state == 'impossible'
    assert any('別ドキュメント' in r for r in preview.impossible_reasons)
    assert preview.items == () and preview.legend == ()
    assert '比較不可' in preview.summary


def test_preview_impossible_on_coordinate_frame_mismatch(
    tmp_path: Path,
) -> None:
    scenes = SceneRepository(tmp_path / 'cad.sqlite3')
    rev_a = _save(scenes, _scene(_entity('fl')))
    alt_a = _alt(rev_a, '案A')
    # Side B resolves to a document in the same document_id but a different
    # coordinate frame (e.g. an imported variant) — a stub resolver lets the
    # preview's honesty check fire without fabricating a persisted doc.
    doc_b = _scene(_entity('fl', x=3.0)).model_copy(
        update={'coordinate_system': 'blueprint_import'}
    )
    alt_b = _alt(rev_a, '案B(別フレーム)')
    comparison_set = build_comparison_set(
        document_id=rev_a.document_id, name='フレーム不一致',
        alternatives=(alt_a, alt_b), created_at_utc=NOW,
    )
    def _resolve(alternative):
        if alternative is alt_b:
            return doc_b
        return scenes.get(alternative.scene_revision_id).document

    preview = build_ab_overlay_preview(
        comparison_set, alt_a, alt_b, resolve_document=_resolve,
    )
    assert preview.state == 'impossible'
    assert any('座標フレーム' in r for r in preview.impossible_reasons)
    assert '比較不可' in preview.summary


def test_preview_impossible_when_pin_cannot_resolve(tmp_path: Path) -> None:
    scenes, rev_a, _rev_b, presentation, _comp, comparison_set, alt_a, alt_b = (
        _ready_fixture(tmp_path)
    )

    def _broken(alternative):
        raise ValueError('scene content hash mismatch')

    preview = build_ab_overlay_preview(
        comparison_set, alt_a, alt_b, resolve_document=_broken,
    )
    assert preview.state == 'impossible'
    assert len(preview.impossible_reasons) == 2  # both sides failed
    assert any('案A' in r for r in preview.impossible_reasons)
    assert any('hash mismatch' in r or '再現できません' in r
               for r in preview.impossible_reasons)


def test_preview_same_alternative_rejected(tmp_path: Path) -> None:
    _s, _ra, _rb, presentation, _c, comparison_set, alt_a, _ab = (
        _ready_fixture(tmp_path)
    )
    preview = build_ab_overlay_preview(
        comparison_set, alt_a, alt_a,
        resolve_document=presentation.alternative_document,
    )
    assert preview.state == 'impossible'
    assert any('同一の案' in r for r in preview.impossible_reasons)


def test_preview_degraded_frame_disables_arrows(tmp_path: Path) -> None:
    """Room/wall/semantic-geometry change -> position rows are
    reference-only, never an authoritative origin->destination arrow."""
    scenes = SceneRepository(tmp_path / 'cad.sqlite3')
    rev_a = _save(scenes, _scene(_entity('fl', x=1.2), width=6.0))
    # Same document lineage, but the room prism differs AND fl moved.
    rev_b = _save(scenes, _scene(_entity('fl', x=1.6), width=7.0),
                  parent=rev_a.revision_id)
    presentation = CadPresentationRepository(scenes)
    comparison_set = build_comparison_set(
        document_id=rev_a.document_id, name='部屋変更',
        alternatives=(_alt(rev_a, '案A'), _alt(rev_b, '案B')),
        created_at_utc=NOW,
    )
    alt_a, alt_b = comparison_set.alternatives
    preview = build_ab_overlay_preview(
        comparison_set, alt_a, alt_b,
        resolve_document=presentation.alternative_document,
    )
    assert preview.state == 'ready'
    assert preview.frame_state == 'degraded'
    item = {i.entity_id: i for i in preview.items}['fl']
    assert item.category == 'position_unverified'
    assert '部屋形状' in ''.join(preview.context_changed)
    assert AB_OVERLAY_DEGRADED_FRAME_NOTE in preview.disclaimer


def test_reasons_come_from_recorded_evidence_only(tmp_path: Path) -> None:
    scenes = SceneRepository(tmp_path / 'cad.sqlite3')
    rev_a = _save(scenes, _scene(_entity('fl', x=1.2)))
    rev_b = _save(scenes, _scene(_entity('fl', x=1.6)),
                  parent=rev_a.revision_id)
    presentation = CadPresentationRepository(scenes)

    # No recorded reason anywhere -> the explicit marker, never a guess.
    plain = build_comparison_set(
        document_id=rev_a.document_id, name='無記録',
        alternatives=(_alt(rev_a, '案A'), _alt(rev_b, '案B')),
        created_at_utc=NOW,
    )
    preview = build_ab_overlay_preview(
        plain, plain.alternatives[0], plain.alternatives[1],
        resolve_document=presentation.alternative_document,
    )
    assert all(item.reason == MISSING_REASON_LABEL for item in preview.items)

    # Alternative B's semantic summary wins; else labels; else marker.
    summarized = build_comparison_set(
        document_id=rev_a.document_id, name='理由あり',
        alternatives=(
            _alt(rev_a, '案A'),
            _alt(rev_b, '案B', semantic_change_summary='座席を20cm前に'),
        ),
        created_at_utc=NOW,
    )
    preview = build_ab_overlay_preview(
        summarized, summarized.alternatives[0], summarized.alternatives[1],
        resolve_document=presentation.alternative_document,
    )
    assert all(
        item.reason == '座席を20cm前に' for item in preview.items
    )

    labelled = build_comparison_set(
        document_id=rev_a.document_id, name='証跡ラベル',
        alternatives=(
            _alt(rev_a, '案A'),
            _alt(
                rev_b, '案B',
                evidence_refs=(
                    ComparisonEvidenceRef(
                        kind='other', ref_id='note-1',
                        label='設計レビュー議事録',
                    ),
                ),
            ),
        ),
        created_at_utc=NOW,
    )
    preview = build_ab_overlay_preview(
        labelled, labelled.alternatives[0], labelled.alternatives[1],
        resolve_document=presentation.alternative_document,
    )
    assert all(
        item.reason == '設計レビュー議事録' for item in preview.items
    )


def test_staleness_note_names_which_side_is_head(tmp_path: Path) -> None:
    _s, rev_a, rev_b, presentation, _c, comparison_set, alt_a, alt_b = (
        _ready_fixture(tmp_path)
    )
    # Neither side is head (a detached third pin — a genuinely
    # historical comparison, never the current scene).
    rev_c = _s.save_detached_revision(
        _scene(_entity('fl', x=9.9)), parent_revision_id=rev_b.revision_id,
        reason='test: third pinned alternative',
    ).revision
    preview = build_ab_overlay_preview(
        comparison_set, alt_a, alt_b,
        resolve_document=presentation.alternative_document,
        head_revision_id=rev_c.revision_id,
    )
    assert '両案とも現在の先頭版ではありません' in preview.staleness_note
    preview = build_ab_overlay_preview(
        comparison_set, alt_a, alt_b,
        resolve_document=presentation.alternative_document,
        head_revision_id=rev_b.revision_id,
    )
    assert '案Bは現在の先頭版です' in preview.staleness_note
    # Unresolvable head stays honest.
    preview = build_ab_overlay_preview(
        comparison_set, alt_a, alt_b,
        resolve_document=presentation.alternative_document,
        head_revision_id=None,
    )
    assert '先頭版を解決できませんでした' in preview.staleness_note


def test_evidence_rows_report_exact_availability(tmp_path: Path) -> None:
    scenes = SceneRepository(tmp_path / 'cad.sqlite3')
    rev_a = _save(scenes, _scene(_entity('fl', x=1.2)))
    rev_b = _save(scenes, _scene(_entity('fl', x=1.6)),
                  parent=rev_a.revision_id)
    presentation = CadPresentationRepository(scenes)
    comparison_set = build_comparison_set(
        document_id=rev_a.document_id, name='証跡あり',
        alternatives=(
            _alt(
                rev_a, '案A',
                evidence_refs=(
                    ComparisonEvidenceRef(
                        kind='prediction', ref_id='pred-1',
                        ref_sha256='f' * 64,
                    ),
                    ComparisonEvidenceRef(
                        kind='other', ref_id='memo-1', label='メモ',
                    ),
                ),
            ),
            _alt(rev_b, '案B'),
        ),
        created_at_utc=NOW,
    )

    class _Resolver:
        def knows(self, kind: str) -> bool:
            return kind == 'prediction'

        def resolve(self, kind, ref_id, document_id):
            return ResolvedAuthority(
                document_id=document_id,
                semantic_sha256='f' * 64,
            )

    preview = build_ab_overlay_preview(
        comparison_set, *comparison_set.alternatives,
        resolve_document=presentation.alternative_document,
        evidence_resolver=_Resolver(),
    )
    by_kind = {row.kind: row for row in preview.evidence_rows}
    assert by_kind['prediction'].state == 'available'
    assert by_kind['prediction'].state_label == (
        AB_EVIDENCE_STATE_LABELS['available']
    )
    # 'other' has no canonical authority -> unsupported, never improved.
    assert by_kind['other'].state == 'unsupported'


def test_vocabulary_covers_every_category_and_state() -> None:
    assert set(AB_OVERLAY_CATEGORY_VOCAB) == {
        'removed', 'added', 'moved', 'position_unverified', 'attribute_only',
    }
    for label, color in AB_OVERLAY_CATEGORY_VOCAB.values():
        assert label and color.startswith('#')
    for state in (
        'available', 'missing_reference', 'foreign_project',
        'semantic_hash_conflict', 'incompatible_baseline',
        'context_mismatch', 'unresolvable', 'unsupported',
    ):
        assert AB_EVIDENCE_STATE_LABELS[state]


# -- viewport overlay -------------------------------------------------------------


def _viewport():
    from PySide6.QtWidgets import QApplication

    from htdt.room_viewport import RoomViewport3D

    QApplication.instance() or QApplication(['htdt-test'])
    return RoomViewport3D()


def _abdiff_actor_names(viewport) -> list[str]:
    return [
        name
        for name in viewport.plotter.renderer.actors
        if isinstance(name, str) and name.startswith('abdiff-')
    ]


def test_viewport_draws_each_category_and_cleans_up(tmp_path: Path) -> None:
    _s, _ra, _rb, presentation, _c, comparison_set, alt_a, alt_b = (
        _ready_fixture(tmp_path)
    )
    preview = build_ab_overlay_preview(
        comparison_set, alt_a, alt_b,
        resolve_document=presentation.alternative_document,
    )
    viewport = _viewport()
    viewport.render_design_ab_overlay(preview)
    names = _abdiff_actor_names(viewport)
    assert 'abdiff-removed-sl' in names
    assert 'abdiff-added-sub' in names
    assert 'abdiff-movedfrom-fl' in names
    assert 'abdiff-movedto-fl' in names
    assert 'abdiff-arrow-fl' in names
    assert 'abdiff-attr-cab' in names
    assert 'abdiff-room' in names
    assert 'abdiff-summary' in names
    assert 'abdiff-disclaimer' in names
    assert 'abdiff-legend' in names
    for name in names:
        assert not viewport.plotter.renderer.actors[name].GetPickable()
    viewport.clear_design_ab_overlay()
    assert _abdiff_actor_names(viewport) == []


def test_viewport_diff_only_hides_context(tmp_path: Path) -> None:
    scenes, rev_a, _rb, presentation, _c, _set_, alt_a, _ab = (
        _ready_fixture(tmp_path)
    )
    rev_c = scenes.save_detached_revision(
        _scene(_entity('fl', x=1.2), _entity('sub', x=9.0)),
        parent_revision_id=rev_a.revision_id,
        reason='test: pinned non-head alternative',
    ).revision
    alt_c = _alt(rev_c, '案C')
    comparison_set = build_comparison_set(
        document_id=rev_a.document_id, name='差分のみ',
        alternatives=(alt_a, alt_c), created_at_utc=NOW,
    )
    preview = build_ab_overlay_preview(
        comparison_set, alt_a, alt_c,
        resolve_document=presentation.alternative_document,
    )
    viewport = _viewport()
    viewport.render_design_ab_overlay(preview, show_context=True)
    names = _abdiff_actor_names(viewport)
    assert any(n.startswith('abdiff-context-') for n in names)
    viewport.render_design_ab_overlay(preview, show_context=False)
    names = _abdiff_actor_names(viewport)
    assert not any(n.startswith('abdiff-context-') for n in names)
    # Diff actors remain.
    assert 'abdiff-removed-sl' in names
    assert 'abdiff-added-sub' in names


def test_viewport_degraded_frame_has_no_arrows(tmp_path: Path) -> None:
    scenes = SceneRepository(tmp_path / 'cad.sqlite3')
    rev_a = _save(scenes, _scene(_entity('fl', x=1.2), width=6.0))
    rev_b = _save(scenes, _scene(_entity('fl', x=1.6), width=7.0),
                  parent=rev_a.revision_id)
    presentation = CadPresentationRepository(scenes)
    comparison_set = build_comparison_set(
        document_id=rev_a.document_id, name='部屋変更',
        alternatives=(_alt(rev_a, '案A'), _alt(rev_b, '案B')),
        created_at_utc=NOW,
    )
    preview = build_ab_overlay_preview(
        comparison_set, *comparison_set.alternatives,
        resolve_document=presentation.alternative_document,
    )
    assert preview.frame_state == 'degraded'
    viewport = _viewport()
    viewport.render_design_ab_overlay(preview)
    names = _abdiff_actor_names(viewport)
    assert 'abdiff-movedfrom-fl' in names
    assert 'abdiff-movedto-fl' in names
    # Never an authoritative arrow on a degraded frame.
    assert not any(n.startswith('abdiff-arrow-') for n in names)
    # Both room shells drawn in their A/B category colors.
    assert 'abdiff-room-a' in names
    assert 'abdiff-room-b' in names


def test_viewport_impossible_draws_reason_only(tmp_path: Path) -> None:
    _s, _ra, _rb, presentation, _c, comparison_set, alt_a, _ab = (
        _ready_fixture(tmp_path)
    )
    preview = build_ab_overlay_preview(
        comparison_set, alt_a, alt_a,
        resolve_document=presentation.alternative_document,
    )
    viewport = _viewport()
    viewport.render_design_ab_overlay(preview)
    names = _abdiff_actor_names(viewport)
    assert 'abdiff-blocked' in names
    assert 'abdiff-disclaimer' in names
    # No guessed geometry.
    assert not any(
        n.startswith(('abdiff-removed-', 'abdiff-added-',
                      'abdiff-moved', 'abdiff-attr-', 'abdiff-context-'))
        for n in names
    )
    viewport.render_design_ab_overlay(None)
    assert _abdiff_actor_names(viewport) == []


def test_overlay_actor_prefix_is_swept() -> None:
    """`abdiff-` is in the shared overlay-prefix sweep so any bulk clear
    (scene rebuild / toggle-off / project switch) drops the layer."""
    from htdt.room_viewport import RoomViewport3D

    assert 'abdiff-' in RoomViewport3D._OVERLAY_ACTOR_PREFIXES


# -- workspace wiring --------------------------------------------------------------


def _workspace(tmp_path: Path):
    from PySide6.QtWidgets import QApplication

    from htdt.presentation_workspace import PresentationWorkspace

    (scenes, rev_a, _rev_b, _presentation, comparison,
     comparison_set, alt_a, alt_b) = _ready_fixture(tmp_path)
    QApplication.instance() or QApplication(['htdt-test'])
    workspace = PresentationWorkspace(scenes, rev_a.document_id)
    return workspace, comparison_set, (alt_a, alt_b)


def test_compare_page_overlay_mode_loads_and_switches(tmp_path: Path) -> None:
    from PySide6.QtCore import Qt

    workspace, _comparison_set, _alts = _workspace(tmp_path)
    try:
        workspace.set_context('compare')
        workspace.compare_set_combo.setCurrentIndex(0)
        workspace.left_combo.setCurrentIndex(0)
        workspace.right_combo.setCurrentIndex(1)
        workspace.view_mode_combo.setCurrentIndex(1)  # 重畳
        workspace._load_comparison()

        assert workspace.compare_stack.currentIndex() == 1
        assert workspace._ab_preview is not None
        assert workspace._ab_preview.state == 'ready'
        names = _abdiff_actor_names(workspace.overlay_viewport)
        assert 'abdiff-removed-sl' in names
        assert 'abdiff-arrow-fl' in names
        # Diff card rows: one per entity + evidence/disclaimer lines.
        texts = [
            workspace.diff_card.item(i).text()
            for i in range(workspace.diff_card.count())
        ]
        assert any('SL' in t or 'sl' in t for t in texts)
        assert any(AB_OVERLAY_DISCLAIMER.split('。')[0][:8] in t
                   for t in texts)
        # 差分のみ: same pair, context ghosts dropped.
        workspace.view_mode_combo.setCurrentIndex(2)
        assert workspace.compare_stack.currentIndex() == 1
        names = _abdiff_actor_names(workspace.overlay_viewport)
        assert not any(n.startswith('abdiff-context-') for n in names)
        # Back to 横並び: overlay cleared, side view active.
        workspace.view_mode_combo.setCurrentIndex(0)
        assert workspace.compare_stack.currentIndex() == 0
        assert _abdiff_actor_names(workspace.overlay_viewport) == []
        assert workspace._ab_preview is None
        assert workspace.diff_card.count() == 0
    finally:
        workspace.close()
        workspace.deleteLater()


def test_compare_page_overlay_impossible_state(tmp_path: Path, monkeypatch) -> None:
    workspace, comparison_set, (alt_a, _alt_b) = _workspace(tmp_path)
    try:
        # Pin the same alternative on both sides -> honest 比較不可, no warn.
        monkeypatch.setattr(
            type(workspace), '_selected_alternative_pair',
            lambda self, show_dialogs: (
                comparison_set, alt_a, alt_a,
            ),
        )
        workspace.set_context('compare')
        workspace.view_mode_combo.setCurrentIndex(1)
        workspace._load_comparison()
        assert workspace._ab_preview is not None
        assert workspace._ab_preview.state == 'impossible'
        names = _abdiff_actor_names(workspace.overlay_viewport)
        assert 'abdiff-blocked' in names
        texts = [
            workspace.diff_card.item(i).text()
            for i in range(workspace.diff_card.count())
        ]
        assert any('比較不可' in t for t in texts)
    finally:
        workspace.close()
        workspace.deleteLater()


def test_compare_page_row_click_highlights(tmp_path: Path) -> None:
    from PySide6.QtCore import Qt

    workspace, _comparison_set, _alts = _workspace(tmp_path)
    try:
        workspace.set_context('compare')
        workspace.view_mode_combo.setCurrentIndex(1)
        workspace._load_comparison()
        target = None
        for i in range(workspace.diff_card.count()):
            item = workspace.diff_card.item(i)
            if item.data(Qt.ItemDataRole.UserRole):
                target = item
                break
        assert target is not None
        # Clicking a card row re-renders with the entity highlighted.
        workspace._on_diff_card_row(target)
        assert _abdiff_actor_names(workspace.overlay_viewport)
    finally:
        workspace.close()
        workspace.deleteLater()


def test_overlay_set_switch_clears_actors(tmp_path: Path) -> None:
    workspace, _set_a, _alts = _workspace(tmp_path)
    try:
        workspace.set_context('compare')
        workspace.view_mode_combo.setCurrentIndex(1)
        workspace._load_comparison()
        assert _abdiff_actor_names(workspace.overlay_viewport)
        # Project/set refresh must drop stale ghosts (actor hygiene).
        workspace._refresh_compare_alternatives()
        assert _abdiff_actor_names(workspace.overlay_viewport) == []
        assert workspace._ab_preview is None
        assert workspace.diff_card.count() == 0
    finally:
        workspace.close()
        workspace.deleteLater()


def test_compare_page_narrow_dpi200_uia(tmp_path: Path) -> None:
    from PySide6.QtCore import Qt

    workspace, _comparison_set, _alts = _workspace(tmp_path)
    try:
        workspace.set_context('compare')
        workspace.resize(360, 640)
        workspace.show()
        # UIA-reachable: every new control carries an accessible name.
        assert workspace.view_mode_combo.accessibleName() == '比較表示モード'
        assert workspace.diff_card.accessibleName() == '差分理由カード'
        assert workspace.diff_summary_label.accessibleName() == '比較サマリ'
        assert workspace.view_mode_combo.focusPolicy() != Qt.NoFocus
        assert workspace.diff_card.focusPolicy() != Qt.NoFocus
        # DPI-200-style doubling: bigger font, no crash, layout survives.
        font = workspace.font()
        font.setPointSizeF(font.pointSizeF() * 2.0)
        workspace.setFont(font)
        workspace.adjustSize()
        workspace.resize(360, 640)
    finally:
        workspace.close()
        workspace.deleteLater()


def test_large_scene_overlay_stays_bounded(tmp_path: Path) -> None:
    """100+ entities: the overlay draws every diff row without leaks —
    actor count is a function of changed entities, not scene size."""
    scenes = SceneRepository(tmp_path / 'cad.sqlite3')
    entities_a = tuple(
        _entity(f'e{i:03d}', x=float(i % 10), size_x=0.2)
        for i in range(120)
    )
    entities_b = tuple(
        _entity(f'e{i:03d}', x=float(i % 10) + (0.4 if i % 10 == 0 else 0.0),
                size_x=0.2)
        for i in range(120)
    )
    rev_a = _save(scenes, _scene(*entities_a))
    rev_b = _save(scenes, _scene(*entities_b), parent=rev_a.revision_id)
    presentation = CadPresentationRepository(scenes)
    comparison_set = build_comparison_set(
        document_id=rev_a.document_id, name='大規模',
        alternatives=(_alt(rev_a, '案A'), _alt(rev_b, '案B')),
        created_at_utc=NOW,
    )
    preview = build_ab_overlay_preview(
        comparison_set, *comparison_set.alternatives,
        resolve_document=presentation.alternative_document,
    )
    assert preview.state == 'ready'
    moved = [i for i in preview.items if i.category == 'moved']
    assert len(moved) == 12  # every 10th entity moved
    assert len(preview.unchanged_entities) == 108
    viewport = _viewport()
    viewport.render_design_ab_overlay(preview)
    names = _abdiff_actor_names(viewport)
    moved_arrows = [n for n in names if n.startswith('abdiff-arrow-')]
    assert len(moved_arrows) == 12
    viewport.clear_design_ab_overlay()
    assert _abdiff_actor_names(viewport) == []
