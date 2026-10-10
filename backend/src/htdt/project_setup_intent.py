"""Project setup intent materialization (#898).

The Commissioning Wizard and :class:`ProjectTemplate` are intent-capture
layers; this module is the single boundary that materializes that intent
into the canonical project authorities:

- wizard/template intent converges into a versioned
  :class:`ProjectDesignBrief` — the record Overview, Optimize, standards
  evaluation and later commissioning consume;
- wizard qualitative goals become ``free_text`` ``BriefGoalRef`` entries —
  "迫力" is human intent and is never silently rewritten into an invented
  SPL target or seat-uniformity threshold;
- a template's ``TemplateMeasurementSpec`` stays a typed *pending* pattern
  on the instantiation record — a project-bound MeasurementPlan cannot be
  fabricated before room/listener/topology authority exists;
- provenance records which commissioning plan or template version seeded
  each authority.

Non-goals / fail-closed rules:

- materialization never overwrites an existing brief: resuming an old
  commissioning plan cannot rewind a newer ``ProjectDesignBrief`` — it
  returns ``None`` instead;
- template updates never mutate projects already created from them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol

from .cad_design_brief import (
    BriefGoalRef,
    ProjectDesignBrief,
    build_design_brief,
)

if TYPE_CHECKING:
    from .cad_design_brief_repository import CadDesignBriefRepository
    from .commissioning_plan import CommissioningIntent, CommissioningPlan



class _TemplateDesignBriefLike(Protocol):
    project_kind: str
    display_intent: str
    audio_only: bool
    notes: str | None


class _TemplateAuthorityRefLike(Protocol):
    kind: str
    ref_id: str
    version: str | None


class _UnresolvedRefLike(Protocol):
    kind: str
    ref_id: str


class _ProjectTemplateLike(Protocol):
    design_brief: _TemplateDesignBriefLike
    target_refs: tuple[_TemplateAuthorityRefLike, ...]
    name: str
    template_id: str
    version: str
    template_sha256: str


class _InstantiationLike(Protocol):
    unresolved_refs: tuple[_UnresolvedRefLike, ...]
    instantiation_id: str


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def brief_from_commissioning_intent(
    intent: 'CommissioningIntent',
    *,
    document_id: str,
    title: str,
    plan_id: str,
    created_at_utc: str | None = None,
) -> ProjectDesignBrief:
    """Map first-run wizard intent onto a canonical ProjectDesignBrief.

    Only semantics that can be represented honestly are mapped: qualitative
    goals become ``free_text`` refs, audio-only/existing-room become use
    labels, the planned speaker count and hybrid-prediction opt-in become
    informational free-text goals. Nothing invents numeric targets.
    """

    use_labels: list[str] = []
    if intent.audio_only:
        use_labels.append('audio_only')
    if intent.has_existing_room:
        use_labels.append('existing_room_authority')
    goal_refs: list[BriefGoalRef] = [
        BriefGoalRef(
            goal_id=f'goal-{index}',
            kind='free_text',
            requirement='preferred',
            label=goal,
        )
        for index, goal in enumerate(intent.goals, start=1)
    ]
    if intent.planned_speaker_count > 0:
        goal_refs.append(
            BriefGoalRef(
                goal_id='topology.planned_speakers',
                kind='free_text',
                requirement='informational',
                label=f'計画スピーカー数 {intent.planned_speaker_count} 台',
                rationale='declared setup intent, not installed truth',
            )
        )
    if intent.wants_hybrid_prediction:
        goal_refs.append(
            BriefGoalRef(
                goal_id='prediction.hybrid',
                kind='free_text',
                requirement='informational',
                label='ハイブリッド予測を利用する',
                rationale='requires measured evidence before it can run',
            )
        )
    notes: list[str] = []
    if intent.rew_available:
        notes.append('REW などの測定ソースを利用する')
    return build_design_brief(
        document_id=document_id,
        title=title,
        use_labels=tuple(use_labels),
        goal_refs=tuple(goal_refs),
        note=' / '.join(notes) or None,
        provenance=('commissioning_wizard', f'commissioning_plan:{plan_id}'),
        created_at_utc=created_at_utc or _utcnow(),
    )


def materialize_commissioning_brief(
    brief_repository: 'CadDesignBriefRepository',
    plan: 'CommissioningPlan',
    *,
    template: 'ProjectTemplate | None' = None,
    instantiation: 'ProjectTemplateInstantiation | None' = None,
    created_at_utc: str | None = None,
) -> ProjectDesignBrief | None:
    """Persist the plan's intent as the document's canonical brief.

    Returns ``None`` when the document already has a brief: resuming an old
    commissioning plan must never overwrite a newer project brief silently
    (#898). When the project was seeded from a template in the same first
    run, the template's design-brief defaults merge into the one brief the
    wizard declaration produces.
    """

    if brief_repository.latest_brief(plan.document_id) is not None:
        return None
    brief = brief_from_commissioning_intent(
        plan.intent,
        document_id=plan.document_id,
        title=plan.name,
        plan_id=plan.plan_id,
        created_at_utc=created_at_utc,
    )
    if template is not None and instantiation is not None:
        template_brief = brief_from_template_brief(
            template,
            document_id=plan.document_id,
            instantiation=instantiation,
            created_at_utc=brief.created_at_utc,
        )
        brief = build_design_brief(
            document_id=plan.document_id,
            title=brief.title,
            use_labels=template_brief.use_labels + tuple(
                label
                for label in brief.use_labels
                if label not in template_brief.use_labels
            ),
            goal_refs=brief.goal_refs + template_brief.goal_refs,
            note=' / '.join(
                part
                for part in (template_brief.note, brief.note)
                if part
            ) or None,
            provenance=brief.provenance + template_brief.provenance,
            created_at_utc=brief.created_at_utc,
        )
    brief_repository.save_brief(brief)
    return brief


    brief_repository.save_brief(brief)
    return brief


def brief_from_template_brief(
    template: '_ProjectTemplateLike',
    *,
    document_id: str,
    instantiation: '_InstantiationLike',
    created_at_utc: str | None = None,
) -> ProjectDesignBrief:
    """Materialize a template's design brief into a project-bound brief.

    Standards refs the template pins become typed ``standards_profile``
    goal refs with their exact semantic hash; refs without a pin stay
    ``free_text`` so an unresolved intent never masquerades as a bound
    authority (#898-3).
    """

    design_brief = template.design_brief
    use_labels: list[str] = [design_brief.project_kind]
    if design_brief.display_intent != 'not_configured':
        use_labels.append(design_brief.display_intent)
    if design_brief.audio_only:
        use_labels.append('audio_only')
    # Baseline refs stay informational free-text goals: standards profiles
    # are global authorities, not document-scoped, so a typed ref would be
    # an unprovable binding (#898-3). The exact resolution state lives on
    # the instantiation record; the brief carries the honest human intent.
    unresolved = {
        (ref.kind, ref.ref_id) for ref in instantiation.unresolved_refs
    }
    goal_refs: list[BriefGoalRef] = [
        BriefGoalRef(
            goal_id=f'target-{index}',
            kind='free_text',
            requirement='informational',
            label=(
                f'テンプレート基準 {ref.kind}:{ref.ref_id}'
                + (f' v{ref.version}' if ref.version else '')
                + (
                    '（未解決）'
                    if (ref.kind, ref.ref_id) in unresolved
                    else '（解決済み）'
                )
            ),
        )
        for index, ref in enumerate(template.target_refs, start=1)
    ]
    return build_design_brief(
        document_id=document_id,
        title=f'{template.name} デザイン概要',
        use_labels=tuple(use_labels),
        goal_refs=tuple(goal_refs),
        note=design_brief.notes,
        provenance=(
            'project_template',
            f'template:{template.template_id}@{template.version}',
            f'template_sha256:{template.template_sha256}',
            f'instantiation:{instantiation.instantiation_id}',
        ),
        created_at_utc=created_at_utc or _utcnow(),
    )


def materialize_template_brief(
    brief_repository,
    template: '_ProjectTemplateLike',
    *,
    document_id: str,
    instantiation: '_InstantiationLike',
    created_at_utc: str | None = None,
) -> ProjectDesignBrief | None:
    """Persist a fresh project-bound brief seeded from template defaults.

    Returns ``None`` when the document already has a brief — a template
    update or re-instantiation may never rewrite an existing project's
    declared intent (#898).
    """

    if brief_repository.latest_brief(document_id) is not None:
        return None
    brief = brief_from_template_brief(
        template,
        document_id=document_id,
        instantiation=instantiation,
        created_at_utc=created_at_utc,
    )
    brief_repository.save_brief(brief)
    return brief


__all__ = [
    'brief_from_commissioning_intent',
    'brief_from_template_brief',
    'materialize_commissioning_brief',
    'materialize_template_brief',
]
