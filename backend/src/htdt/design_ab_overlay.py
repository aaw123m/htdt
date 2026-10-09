"""A/B design-alternative diff overlay model (#1007).

Display model for the dedicated comparison surface over persisted
``DesignComparisonSet`` manifests: given two pinned
``ComparisonAlternative``\\ s it produces the color-coded overlay plan the
viewport draws — A-only entities removed at their old position, B-only
entities added at their new position, moved/rotated entities as an
origin→destination arrow, and material/constraint-only changes as an
outline plus an attribute-diff card row.

Honesty contract:

- :func:`htdt.cad_design_comparison.diff_alternatives` is the SOLE
  comparison authority — verdicts here come only from its
  ``SceneDiff``/evidence output, never from mesh-level guesses;
- the 3-D overlay is restricted to alternatives of the same
  ``document_id`` sharing the same coordinate frame — anything else is a
  'compare impossible' state with explicit reasons, never a guessed
  overlay;
- when the room shell, wall topology, or R120 semantic geometry differs
  between the two pinned documents, positional correspondence becomes
  *reference only*: moved entities render as paired transparent
  old/new outlines instead of an authoritative arrow;
- change reasons come from the alternatives' recorded evidence
  (``semantic_change_summary``, evidence-ref labels) — a missing record
  renders 「理由未記録」, never a fabricated rationale;
- evidence availability is reported with its exact availability state —
  unmeasured, stale, or unsupported evidence is never colored as an
  improvement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from .cad_authority_refs import AuthorityRefResolver
from .cad_design_comparison import (
    ComparisonAlternative,
    DesignComparisonSet,
    diff_alternatives,
    evaluate_comparison_set,
)
from .cad_scene import SceneDocument, SceneEntity


#: Category vocabulary — Japanese legend label + display color. Colors are
#: explanation symbols only; they never encode improvement/winner.
AB_OVERLAY_CATEGORY_VOCAB: dict[str, tuple[str, str]] = {
    'removed': ('削除（案Aのみ）', '#e05555'),
    'added': ('追加（案Bのみ）', '#59d98c'),
    'moved': ('移動・回転（元→先）', '#ffb340'),
    'position_unverified': ('位置関係（参考表示）', '#b8892a'),
    'attribute_only': ('属性変更のみ', '#4da3ff'),
}

#: Faint wireframe for entities identical on both sides (orientation
#: context in 重畳 mode; never drawn in 差分のみ mode).
AB_OVERLAY_CONTEXT_COLOR = '#77808c'

#: Categories a positional glyph is drawn for even when the shared frame
#: is degraded — the glyph pair is reference-only there.
_POSITION_FIELDS = frozenset({'position', 'orientation'})

#: Evidence-availability states rendered on the diff card — neutral
#: wording only; availability is never presented as a quality verdict.
AB_EVIDENCE_STATE_LABELS: dict[str, str] = {
    'available': '利用可',
    'missing_reference': '参照未解決',
    'foreign_project': '別プロジェクト',
    'semantic_hash_conflict': 'ハッシュ不一致',
    'incompatible_baseline': '基準不一致',
    'context_mismatch': '文脈不一致',
    'unresolvable': '解決不能',
    'unsupported': '未対応',
}

MISSING_REASON_LABEL = '理由未記録'

#: Standing honesty line rendered beside the overlay.
AB_OVERLAY_DISCLAIMER = (
    'A/B比較オーバーレイ: 保存済み案がピン留めしたリビジョン同士を表示します。'
    '現在のシーン・先頭リビジョンは変更しません。'
    '証跡が欠落・stale・未対応の項目は「改善」として色づけしません。'
)

#: Appended when the room shell / wall topology / semantic geometry
#: differs — positional correspondence is then reference-only.
AB_OVERLAY_DEGRADED_FRAME_NOTE = (
    '部屋形状・壁構造・意味ジオメトリが異なるため、'
    '位置関係は参考表示です（元→先の矢印は描きません）。'
)

AbDiffState = Literal['ready', 'impossible']
AbDiffCategory = Literal[
    'removed', 'added', 'moved', 'position_unverified', 'attribute_only'
]


@dataclass(frozen=True, slots=True)
class AbDiffEntityItem:
    """One entity row of the A/B diff overlay."""

    entity_id: str
    name: str
    kind: str
    category: AbDiffCategory
    #: Persisted entity on side A (None for added-only entities).
    before_entity: SceneEntity | None
    #: Persisted entity on side B (None for removed-only entities).
    after_entity: SceneEntity | None
    #: Field names the authority diff reports as changed.
    changed_fields: tuple[str, ...]
    #: Recorded change reason — alternative summary / evidence labels,
    #: else 理由未記録.
    reason: str


@dataclass(frozen=True, slots=True)
class AbEvidenceRow:
    """One evidence-availability row on the diff card."""

    alternative_label: str
    kind: str
    ref_id: str
    state: str
    state_label: str
    reason: str


@dataclass(frozen=True, slots=True)
class DesignAbOverlayPreview:
    """Complete render plan for one A/B pair of a comparison set."""

    state: AbDiffState
    impossible_reasons: tuple[str, ...]
    set_name: str
    set_revision: int
    before_label: str
    after_label: str
    before_revision_id: str
    after_revision_id: str
    #: Honest staleness note — the pinned pair may not include the current
    #: document head, and must never be mistaken for a result vs HEAD.
    staleness_note: str | None
    items: tuple[AbDiffEntityItem, ...]
    #: Entities identical on both sides — context wireframes in 重畳 mode.
    unchanged_entities: tuple[SceneEntity, ...]
    #: Scene-level structural changes (room/walls/semantic geometry/…).
    context_changed: tuple[str, ...]
    #: 'shared' = same document + coordinate frame AND identical room /
    #: wall / semantic-geometry authorities, so position claims hold;
    #: 'degraded' = positional correspondence is reference-only.
    frame_state: Literal['shared', 'degraded']
    evidence_rows: tuple[AbEvidenceRow, ...]
    summary: str
    disclaimer: str
    #: (label, color) legend rows for the categories actually drawn.
    legend: tuple[tuple[str, str], ...]
    #: Resolved pinned documents (None on the impossible path).
    before_document: SceneDocument | None
    after_document: SceneDocument | None


def _resolve_document(
    resolve_document: Callable[[ComparisonAlternative], SceneDocument],
    alternative: ComparisonAlternative,
    side: str,
    problems: list[str],
) -> SceneDocument | None:
    try:
        return resolve_document(alternative)
    except Exception as exc:
        problems.append(
            f'案{side}「{alternative.label}」のシーンを再現できません: {exc}'
        )
        return None


def _item_reason(
    before: ComparisonAlternative, after: ComparisonAlternative
) -> str:
    """Recorded change reason, or the explicit 理由未記録 marker.

    Evidence the comparison was pinned with is the only source — the
    overlay never invents a rationale for a difference.
    """

    if after.semantic_change_summary:
        return after.semantic_change_summary
    if before.semantic_change_summary:
        return before.semantic_change_summary
    labels = [
        ref.label
        for ref in (*after.evidence_refs, *before.evidence_refs)
        if ref.label
    ]
    if labels:
        return ' / '.join(dict.fromkeys(labels))
    return MISSING_REASON_LABEL


def _evidence_rows(
    comparison_set: DesignComparisonSet,
    before: ComparisonAlternative,
    after: ComparisonAlternative,
    evidence_resolver: AuthorityRefResolver | None,
) -> tuple[AbEvidenceRow, ...]:
    resolved_evidence: dict[tuple[str, str], object] = {}
    if evidence_resolver is not None:
        for alternative in (before, after):
            for ref in alternative.evidence_refs:
                key = (ref.kind, ref.ref_id)
                if key in resolved_evidence:
                    continue
                if not evidence_resolver.knows(ref.kind):
                    resolved_evidence[key] = None
                    continue
                try:
                    resolved_evidence[key] = evidence_resolver.resolve(
                        ref.kind, ref.ref_id, comparison_set.document_id
                    )
                except Exception:
                    resolved_evidence[key] = None
    availability = evaluate_comparison_set(
        comparison_set, resolved_evidence=resolved_evidence
    )
    labels = {
        before.alternative_id: f'案A「{before.label}」',
        after.alternative_id: f'案B「{after.label}」',
    }
    rows: list[AbEvidenceRow] = []
    for item in availability.items:
        if item.alternative_id not in labels:
            continue
        rows.append(
            AbEvidenceRow(
                alternative_label=labels[item.alternative_id],
                kind=item.kind,
                ref_id=item.ref_id,
                state=item.state,
                state_label=AB_EVIDENCE_STATE_LABELS.get(item.state, item.state),
                reason=item.reason,
            )
        )
    return tuple(rows)


def build_ab_overlay_preview(
    comparison_set: DesignComparisonSet,
    before: ComparisonAlternative,
    after: ComparisonAlternative,
    *,
    resolve_document: Callable[[ComparisonAlternative], SceneDocument],
    evidence_resolver: AuthorityRefResolver | None = None,
    head_revision_id: str | None = None,
) -> DesignAbOverlayPreview:
    """Build the A/B overlay plan for one alternative pair.

    ``resolve_document`` materializes the exact scene an alternative pins
    (revision + content-hash + variant re-verified — the workspace passes
    ``CadPresentationRepository.alternative_document``). Any resolution
    failure becomes an explicit 'compare impossible' reason; the overlay
    is never guessed.
    """

    evidence_rows = _evidence_rows(
        comparison_set, before, after, evidence_resolver
    )
    base = {
        'set_name': comparison_set.name,
        'set_revision': comparison_set.revision,
        'before_label': before.label,
        'after_label': after.label,
        'before_revision_id': before.scene_revision_id,
        'after_revision_id': after.scene_revision_id,
        'staleness_note': _staleness_note(before, after, head_revision_id),
        'evidence_rows': evidence_rows,
    }

    problems: list[str] = []
    if before.alternative_id == after.alternative_id:
        problems.append('同一の案同士は比較できません')
    before_doc = _resolve_document(resolve_document, before, 'A', problems)
    after_doc = _resolve_document(resolve_document, after, 'B', problems)
    if (
        before_doc is not None
        and after_doc is not None
        and not problems
    ):
        if (
            before_doc.document_id != after_doc.document_id
            or before_doc.document_id != comparison_set.document_id
        ):
            problems.append(
                '比較対象が別ドキュメントを参照しています'
                '（共通座標フレームなし — 重畳しません）'
            )
        elif before_doc.coordinate_system != after_doc.coordinate_system:
            problems.append(
                '座標フレームが異なります'
                f'（{before_doc.coordinate_system} / '
                f'{after_doc.coordinate_system}）'
            )
    if problems:
        return DesignAbOverlayPreview(
            state='impossible',
            impossible_reasons=tuple(problems),
            items=(),
            unchanged_entities=(),
            context_changed=(),
            frame_state='shared',
            summary='比較不可 — ' + ' / '.join(problems),
            disclaimer=AB_OVERLAY_DISCLAIMER,
            legend=(),
            before_document=before_doc,
            after_document=after_doc,
            **base,
        )

    assert before_doc is not None and after_doc is not None

    # diff_alternatives is the sole verdict authority — the UI derives no
    # changed/unchanged judgement from meshes or coordinates of its own.
    diff = diff_alternatives(
        before, after, before_document=before_doc, after_document=after_doc
    )
    scene_diff = diff.scene_diff

    frame_state: Literal['shared', 'degraded'] = 'degraded' if (
        scene_diff is not None
        and (
            scene_diff.room_changed
            or scene_diff.wall_topology_changed
            or scene_diff.semantic_geometry_changed
        )
    ) else 'shared'

    before_entities = {
        entity.entity_id: entity for entity in before_doc.entities
    }
    after_entities = {
        entity.entity_id: entity for entity in after_doc.entities
    }
    reason = _item_reason(before, after)
    items: list[AbDiffEntityItem] = []
    if scene_diff is not None:
        for entity_id in scene_diff.removed_entity_ids:
            entity = before_entities[entity_id]
            items.append(
                AbDiffEntityItem(
                    entity_id=entity_id,
                    name=entity.name,
                    kind=entity.kind,
                    category='removed',
                    before_entity=entity,
                    after_entity=None,
                    changed_fields=(),
                    reason=reason,
                )
            )
        for entity_id in scene_diff.added_entity_ids:
            entity = after_entities[entity_id]
            items.append(
                AbDiffEntityItem(
                    entity_id=entity_id,
                    name=entity.name,
                    kind=entity.kind,
                    category='added',
                    before_entity=None,
                    after_entity=entity,
                    changed_fields=(),
                    reason=reason,
                )
            )
        for change in scene_diff.entity_changes:
            fields = change.fields
            if 'kind' in fields:
                # Surface identity is in doubt — a kind change can mean a
                # replaced object, so no positional correspondence claim.
                category: AbDiffCategory = 'attribute_only'
            elif _POSITION_FIELDS & set(fields):
                category = (
                    'moved' if frame_state == 'shared'
                    else 'position_unverified'
                )
            else:
                category = 'attribute_only'
            items.append(
                AbDiffEntityItem(
                    entity_id=change.entity_id,
                    name=change.entity_name,
                    kind=change.kind,
                    category=category,
                    before_entity=before_entities[change.entity_id],
                    after_entity=after_entities[change.entity_id],
                    changed_fields=fields,
                    reason=reason,
                )
            )
    changed_ids = {
        item.entity_id for item in items
    }
    unchanged = tuple(
        entity
        for entity in after_doc.entities
        if entity.entity_id in before_entities
        and entity.entity_id not in changed_ids
    )

    context_changed: list[str] = []
    if scene_diff is not None:
        if scene_diff.room_changed:
            context_changed.append('部屋形状が異なります')
        if scene_diff.wall_topology_changed:
            context_changed.append('壁構造（壁・開口部）が異なります')
        if scene_diff.semantic_geometry_changed:
            context_changed.append('意味ジオメトリ（R120）が異なります')
        if scene_diff.attachments_changed:
            context_changed.append('取付・マウント関係が異なります')
        if scene_diff.construction_assemblies_changed:
            context_changed.append('構造アセンブリが異なります')
        if scene_diff.entity_order_changed:
            context_changed.append('オブジェクトの順序が異なります')

    counts = {
        category: sum(1 for item in items if item.category == category)
        for category in AB_OVERLAY_CATEGORY_VOCAB
    }
    summary = (
        f'比較セット「{comparison_set.name}」rev{comparison_set.revision} · '
        f'案A「{before.label}」→ 案B「{after.label}」: '
        f'削除{counts["removed"]} · 追加{counts["added"]} · '
        f'移動{counts["moved"]}'
        + (
            f'（参考{counts["position_unverified"]}）'
            if counts['position_unverified']
            else ''
        )
        + f' · 属性のみ{counts["attribute_only"]} · 同一{len(unchanged)}'
    )
    if base['staleness_note']:
        summary += f' — {base["staleness_note"]}'

    disclaimer = AB_OVERLAY_DISCLAIMER
    if frame_state == 'degraded':
        disclaimer += ' ' + AB_OVERLAY_DEGRADED_FRAME_NOTE

    used_categories = dict.fromkeys(item.category for item in items)
    legend = tuple(
        AB_OVERLAY_CATEGORY_VOCAB[category] for category in used_categories
    ) + (
        (('同一（変更なし）', AB_OVERLAY_CONTEXT_COLOR),)
        if unchanged
        else ()
    )

    return DesignAbOverlayPreview(
        state='ready',
        impossible_reasons=(),
        items=tuple(items),
        unchanged_entities=unchanged,
        context_changed=tuple(context_changed),
        frame_state=frame_state,
        summary=summary,
        disclaimer=disclaimer,
        legend=legend,
        **base,
        before_document=before_doc,
        after_document=after_doc,
    )


def _staleness_note(
    before: ComparisonAlternative,
    after: ComparisonAlternative,
    head_revision_id: str | None,
) -> str | None:
    """Honest line naming the pinned revisions vs the current head."""

    if head_revision_id is None:
        return '現在の先頭版を解決できませんでした — ピン留め版のみを表示します'
    pinned = {
        before.scene_revision_id: '案A',
        after.scene_revision_id: '案B',
    }
    if head_revision_id in pinned:
        side = pinned[head_revision_id]
        return f'{side}は現在の先頭版です — もう一方はピン留めの履歴版です'
    return '両案とも現在の先頭版ではありません — ピン留めの履歴版同士の比較です'


__all__ = [
    'AB_EVIDENCE_STATE_LABELS',
    'AB_OVERLAY_CATEGORY_VOCAB',
    'AB_OVERLAY_CONTEXT_COLOR',
    'AB_OVERLAY_DEGRADED_FRAME_NOTE',
    'AB_OVERLAY_DISCLAIMER',
    'AbDiffEntityItem',
    'AbEvidenceRow',
    'DesignAbOverlayPreview',
    'MISSING_REASON_LABEL',
    'build_ab_overlay_preview',
]
