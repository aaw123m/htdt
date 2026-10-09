"""IFC revision-diff review authority (issue #981).

Builds the operator-facing correspondence between a *prior* IFC import
(``IfcImportArtifact`` + ``IfcEntityMapping`` set pinned by the current
intake subject's ``source_refs``) and a *revised* IFC file, then records
the operator's accept/skip decision per correspondence row.

Correspondence keys on the IFC ``GlobalId`` — the only identity an IFC
exporter guarantees across revisions. Elements without a GlobalId are
never auto-matched into ``removed``/``added``: a unique structural
identity (type + name + parent) pairs them as an *inferred* match, and
anything left over lands in the honest ``unknown`` bucket — gray in the
UI and never counted as removed. Contested identities (duplicate
GlobalIds, renames, multi-candidate fuzzy keys) land in ``ambiguous``.

Apply semantics — accepted rows take the new file's state; skipped or
still-pending rows keep the prior state:

* ``matched``    — carry the new mapping (identical content either way)
* ``changed``    — accept: new mapping;   skip: prior mapping kept, flagged
* ``added``      — accept: include new;   skip: excluded
* ``removed``    — accept: prior dropped; skip: prior kept, flagged
* ``ambiguous``  — accept: new mapping (explicit rebind); skip: prior kept
* ``unknown``    — prior-side: skip keeps it (conservative — nothing is
                   silently deleted), accept drops it; new-side: accept
                   includes it, skip excludes it

Applying writes a NEW sealed :class:`IfcDiffApply` record plus a merged
:class:`GeometryIntakeSubject` (``source_kind='ifc_diff_merge'``) — the
pinned import artifact and its mappings are never mutated.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import (
    canonical_sha256 as _digest,
    canonicalize_payload as _canon,
)
from .cad_ifc_interop import (
    IfcDeltaChangeKind,
    IfcEntityMapping,
    IfcImportArtifact,
    IfcReconciliationState,
)


_SHA256_PATTERN = r'^[0-9a-f]{64}$'


class IfcDiffError(ValueError):
    """A diff-review operation violated the correspondence contract."""


#: Operator-facing correspondence categories (#981). ``unknown`` is the
#: gray bucket — reported but never counted as removed.
IfcDiffCategory = Literal[
    'matched',
    'changed',
    'added',
    'removed',
    'ambiguous',
    'unknown',
]

#: Delta vocabulary plus diff-review-only kinds for correspondence that
#: the sealed ``IfcRevisionDelta`` buckets under added/removed but the
#: review surface must never silently commit.
IfcDiffChangeKind = Literal[
    'unchanged',
    'geometry_changed',
    'semantics_changed',
    'added',
    'removed',
    'ambiguous_rebind',
    'duplicate_global_id',
    'unmatched_identity',
]

IfcDiffDecision = Literal['accepted', 'skipped', 'pending']


class IfcDiffRow(BaseModel):
    """One correspondence row in the diff review.

    ``decision_required`` rows need an explicit operator accept/skip;
    ``matched`` rows are informational (the merged subject carries them
    regardless). ``prior_part_id``/``new_part_id`` are the subject-side
    ``ifc:<step>`` ids the workspace locate flow resolves.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    row_key: str = Field(min_length=1)
    category: IfcDiffCategory
    change_kind: IfcDiffChangeKind
    decision_required: bool
    ifc_global_id: str | None = None
    ifc_type: str = Field(min_length=1)
    name: str | None = None
    parent_global_id: str | None = None
    storey_global_id: str | None = None
    prior_mapping_id: str | None = None
    new_mapping_id: str | None = None
    prior_geometry_fingerprint: str | None = None
    new_geometry_fingerprint: str | None = None
    prior_semantic_fingerprint: str | None = None
    new_semantic_fingerprint: str | None = None
    prior_part_id: str | None = None
    new_part_id: str | None = None
    acoustically_relevant: bool = False
    detail: str | None = None


class IfcDiffRowDecision(BaseModel):
    """The operator's recorded call on one correspondence row."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    row_key: str = Field(min_length=1)
    decision: IfcDiffDecision
    decided_by: str = Field(min_length=1)
    decided_at_utc: str = Field(min_length=1)


class IfcDiffApply(BaseModel):
    """Sealed record of one diff-review apply (``ida:<sha256>``).

    Pins the delta it resolved, both import artifacts, the merged subject
    that resulted, and every row's decision — the persisted, hashed diff
    summary the issue requires. Append-only: a re-apply against the same
    delta writes a new record; the latest per (document, delta) is the
    current operator state.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    apply_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    delta_ref: AuthorityRef
    prior_artifact_ref: AuthorityRef
    new_artifact_ref: AuthorityRef
    prior_subject_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    merged_subject_id: str = Field(min_length=1)
    merged_subject_sha256: str = Field(pattern=_SHA256_PATTERN)
    decisions: tuple[IfcDiffRowDecision, ...] = ()
    applied_row_keys: tuple[str, ...] = ()
    skipped_row_keys: tuple[str, ...] = ()
    pending_row_keys: tuple[str, ...] = ()
    matched_count: int = Field(ge=0)
    changed_count: int = Field(ge=0)
    added_count: int = Field(ge=0)
    removed_count: int = Field(ge=0)
    ambiguous_count: int = Field(ge=0)
    unknown_count: int = Field(ge=0)
    applied_by: str = Field(min_length=1)
    applied_at_utc: str = Field(min_length=1)
    apply_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'IfcDiffApply':
        expected = _digest(self.identity_payload())
        if self.apply_sha256 != expected:
            raise ValueError('apply_sha256 does not match content')
        if self.apply_id != f'ida:{expected}':
            raise ValueError('apply_id must be ida:<sha256>')
        if self.delta_ref.kind != 'ifc_revision_delta':
            raise ValueError('delta_ref kind mismatch')
        for ref in (self.prior_artifact_ref, self.new_artifact_ref):
            if ref.kind != 'ifc_import_artifact':
                raise ValueError('artifact ref kind mismatch')
            if ref.ref_sha256 is None:
                raise ValueError('artifact refs must pin their sha256')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('apply_id', None)
        payload.pop('apply_sha256', None)
        return payload


# ---------------------------------------------------------------------------
# Correspondence computation
# ---------------------------------------------------------------------------


def _part_id(mapping: IfcEntityMapping) -> str:
    return f'ifc:{mapping.step_entity_id}'


def _row_key(
    change_kind: str,
    prior: IfcEntityMapping | None,
    new: IfcEntityMapping | None,
    ordinal: int,
) -> str:
    # Key on step_entity_id, not mapping_id: reconciliation marks re-seal an
    # accepted row's mappings under NEW mapping_ids, and row keys must stay
    # identical when rows are recomputed on reload so stored decisions match.
    return (
        f'{change_kind}:'
        f'{prior.step_entity_id if prior is not None else "-"}:'
        f'{new.step_entity_id if new is not None else "-"}:'
        f'{ordinal}'
    )


def _fuzzy_key(m: IfcEntityMapping) -> tuple[str, str, str]:
    return (m.ifc_type, m.name or '', m.parent_global_id or '')


def _row(
    category: IfcDiffCategory,
    change_kind: IfcDiffChangeKind,
    *,
    decision_required: bool = True,
    prior: IfcEntityMapping | None = None,
    new: IfcEntityMapping | None = None,
    ordinal: int = 0,
    acoustically_relevant: bool | None = None,
    detail: str | None = None,
    project_row: bool = False,
) -> IfcDiffRow:
    """Assemble one row; ``project_row`` marks the coordinate-level item."""
    acoustically_relevant = (
        bool(acoustically_relevant)
        if acoustically_relevant is not None
        else category in ('changed', 'removed', 'ambiguous')
    )
    anchor = new if new is not None else prior
    return IfcDiffRow(
        row_key=(
            f'project:{change_kind}:{ordinal}' if project_row
            else _row_key(change_kind, prior, new, ordinal)
        ),
        category=category,
        change_kind=change_kind,
        decision_required=decision_required,
        ifc_global_id=(
            anchor.ifc_global_id if anchor is not None else None
        ),
        ifc_type='IFCPROJECT' if project_row else anchor.ifc_type,
        name=anchor.name if anchor is not None else None,
        parent_global_id=(
            anchor.parent_global_id if anchor is not None else None
        ),
        prior_mapping_id=(
            prior.mapping_id if prior is not None else None
        ),
        new_mapping_id=new.mapping_id if new is not None else None,
        prior_geometry_fingerprint=(
            prior.geometry_fingerprint if prior is not None else None
        ),
        new_geometry_fingerprint=(
            new.geometry_fingerprint if new is not None else None
        ),
        prior_semantic_fingerprint=(
            prior.semantic_fingerprint if prior is not None else None
        ),
        new_semantic_fingerprint=(
            new.semantic_fingerprint if new is not None else None
        ),
        prior_part_id=(
            None if project_row or prior is None else _part_id(prior)
        ),
        new_part_id=(
            None if project_row or new is None else _part_id(new)
        ),
        acoustically_relevant=acoustically_relevant,
        detail=detail,
    )


def compute_ifc_diff_rows(
    *,
    prior_artifact: IfcImportArtifact,
    prior_mappings: Sequence[IfcEntityMapping],
    new_artifact: IfcImportArtifact,
    new_mappings: Sequence[IfcEntityMapping],
) -> tuple[IfcDiffRow, ...]:
    """Operator-facing correspondence rows between two IFC revisions.

    GlobalId is the primary key; elements without one never enter the
    confident ``removed``/``added`` buckets — they pair on a unique
    structural identity as an inferred match or land in ``unknown``.
    Contested identities (duplicate GlobalIds, renames, multi-candidate
    fuzzy keys) land in ``ambiguous`` — gray rows the apply never
    auto-commits.
    """
    rows: list[IfcDiffRow] = []
    ordinal = 0

    def _next() -> int:
        nonlocal ordinal
        ordinal += 1
        return ordinal

    prior_gid: dict[str, list[IfcEntityMapping]] = {}
    new_gid: dict[str, list[IfcEntityMapping]] = {}
    prior_nogid: list[IfcEntityMapping] = []
    new_nogid: list[IfcEntityMapping] = []
    for m in prior_mappings:
        (
            prior_gid.setdefault(m.ifc_global_id, [])
            if m.ifc_global_id else prior_nogid
        ).append(m)
    for m in new_mappings:
        (
            new_gid.setdefault(m.ifc_global_id, [])
            if m.ifc_global_id else new_nogid
        ).append(m)

    matched_prior: set[str] = set()
    matched_new: set[str] = set()

    # --- contested GlobalIds -> ambiguous, never silently collapsed ----
    contested = {
        gid for gid, ms in prior_gid.items() if len(ms) > 1
    } | {
        gid for gid, ms in new_gid.items() if len(ms) > 1
    }
    for gid in sorted(contested):
        for m in prior_gid.get(gid, ()):
            matched_prior.add(m.mapping_id)
            rows.append(_row(
                'ambiguous', 'duplicate_global_id', prior=m,
                ordinal=_next(),
                detail=(
                    f'GlobalId {gid} が旧リビジョンで複数要素に存在 '
                    '— 自動対応付け不可'
                ),
            ))
        for m in new_gid.get(gid, ()):
            matched_new.add(m.mapping_id)
            rows.append(_row(
                'ambiguous', 'duplicate_global_id', new=m,
                ordinal=_next(),
                detail=(
                    f'GlobalId {gid} が新リビジョンで複数要素に存在 '
                    '— 自動対応付け不可'
                ),
            ))

    # --- GlobalId-keyed pairs ------------------------------------------
    for gid in sorted(set(prior_gid) & set(new_gid) - contested):
        pm = prior_gid[gid][0]
        nm = new_gid[gid][0]
        matched_prior.add(pm.mapping_id)
        matched_new.add(nm.mapping_id)
        if (
            pm.semantic_fingerprint == nm.semantic_fingerprint
            and pm.geometry_fingerprint == nm.geometry_fingerprint
        ):
            rows.append(_row(
                'matched', 'unchanged', prior=pm, new=nm,
                ordinal=_next(), decision_required=False,
                acoustically_relevant=False,
            ))
            continue
        if pm.geometry_fingerprint != nm.geometry_fingerprint:
            rows.append(_row(
                'changed', 'geometry_changed', prior=pm, new=nm,
                ordinal=_next(),
                detail='placement or geometry fingerprints differ',
            ))
        else:
            buildup_changed = (
                pm.material_layers != nm.material_layers
                or pm.openings != nm.openings
            )
            rows.append(_row(
                'changed', 'semantics_changed', prior=pm, new=nm,
                ordinal=_next(),
                acoustically_relevant=buildup_changed,
                detail=(
                    'material layers or openings changed'
                    if buildup_changed
                    else 'non-geometric property change'
                ),
            ))

    # --- renames: prior-only gid vs new-only gid sharing identity ------
    orphan_prior = [
        m for gid, ms in prior_gid.items()
        for m in ms
        if gid not in new_gid and gid not in contested
        and m.mapping_id not in matched_prior
    ]
    orphan_new = [
        m for gid, ms in new_gid.items()
        for m in ms
        if gid not in prior_gid and gid not in contested
        and m.mapping_id not in matched_new
    ]
    paired_new: set[str] = set()
    for pm in orphan_prior:
        candidates = [
            n for n in orphan_new
            if n.mapping_id not in paired_new
            and n.ifc_type == pm.ifc_type
            and n.name == pm.name
            and n.parent_global_id == pm.parent_global_id
        ]
        if len(candidates) == 1:
            nm = candidates[0]
            paired_new.add(nm.mapping_id)
            matched_prior.add(pm.mapping_id)
            matched_new.add(nm.mapping_id)
            rows.append(_row(
                'ambiguous', 'ambiguous_rebind', prior=pm, new=nm,
                ordinal=_next(), acoustically_relevant=True,
                detail=(
                    'GlobalId changed but type/name/parent match a prior '
                    'mapping; requires explicit reconciliation'
                ),
            ))
        elif len(candidates) > 1:
            matched_prior.add(pm.mapping_id)
            rows.append(_row(
                'ambiguous', 'ambiguous_rebind', prior=pm,
                ordinal=_next(), acoustically_relevant=True,
                detail=(
                    f'GlobalId の変更候補が {len(candidates)} 件 '
                    '— 対応を特定できません'
                ),
            ))
    for pm in orphan_prior:
        if pm.mapping_id in matched_prior:
            continue
        rows.append(_row(
            'removed', 'removed', prior=pm, ordinal=_next(),
            acoustically_relevant=pm.htdt_role
            in ('room_candidate', 'boundary', 'opening'),
            detail='entity missing in new revision — stale',
        ))
    for nm in orphan_new:
        if nm.mapping_id in paired_new:
            continue
        rows.append(_row(
            'added', 'added', new=nm, ordinal=_next(),
            acoustically_relevant=nm.htdt_role
            in ('room_candidate', 'boundary', 'opening'),
        ))

    # --- no-GlobalId elements: fuzzy identity, never 'removed' ---------
    prior_fuzzy: dict[tuple[str, str, str], list[IfcEntityMapping]] = {}
    new_fuzzy: dict[tuple[str, str, str], list[IfcEntityMapping]] = {}
    for m in prior_nogid:
        prior_fuzzy.setdefault(_fuzzy_key(m), []).append(m)
    for m in new_nogid:
        new_fuzzy.setdefault(_fuzzy_key(m), []).append(m)
    for key in sorted(set(prior_fuzzy) & set(new_fuzzy)):
        ps, ns = prior_fuzzy[key], new_fuzzy[key]
        if len(ps) == 1 and len(ns) == 1:
            pm, nm = ps[0], ns[0]
            changed = (
                pm.semantic_fingerprint != nm.semantic_fingerprint
                or pm.geometry_fingerprint != nm.geometry_fingerprint
            )
            rows.append(_row(
                'changed' if changed else 'matched',
                (
                    'geometry_changed'
                    if pm.geometry_fingerprint != nm.geometry_fingerprint
                    else 'semantics_changed'
                ) if changed else 'unchanged',
                prior=pm, new=nm, ordinal=_next(),
                decision_required=changed,
                acoustically_relevant=changed,
                detail='identity inferred — no GlobalId',
            ))
            continue
        for m in ps + ns:
            rows.append(_row(
                'ambiguous', 'ambiguous_rebind',
                prior=m if m in ps else None,
                new=m if m in ns else None,
                ordinal=_next(), acoustically_relevant=True,
                detail=(
                    'GlobalId なしの同一識別候補が複数 '
                    '— 対応を特定できません'
                ),
            ))
    for key in set(prior_fuzzy) - set(new_fuzzy):
        for pm in prior_fuzzy[key]:
            rows.append(_row(
                'unknown', 'unmatched_identity', prior=pm,
                ordinal=_next(),
                detail=(
                    'GlobalId なし・対応候補もなし — '
                    '削除か改名か判別できません（除去とは数えません）'
                ),
            ))
    for key in set(new_fuzzy) - set(prior_fuzzy):
        for nm in new_fuzzy[key]:
            rows.append(_row(
                'unknown', 'unmatched_identity', new=nm,
                ordinal=_next(),
                detail=(
                    'GlobalId なし・対応候補もなし — '
                    '新規か改名か判別できません（追加とは数えません）'
                ),
            ))

    # --- project-level coordinate authority ----------------------------
    if prior_artifact.coordinate != new_artifact.coordinate:
        rows.append(_row(
            'changed', 'semantics_changed',
            ordinal=_next(), acoustically_relevant=True,
            project_row=True,
            detail=(
                'coordinate authority changed (units/north/georef) — '
                'all mapped transforms must be re-evaluated'
            ),
        ))

    return tuple(rows)


def ifc_diff_summary(rows: Sequence[IfcDiffRow]) -> dict[str, int]:
    """Per-category counts — ``unknown`` is reported, never folded into
    ``removed``."""
    summary = {
        'matched': 0, 'changed': 0, 'added': 0, 'removed': 0,
        'ambiguous': 0, 'unknown': 0,
    }
    for row in rows:
        summary[row.category] += 1
    return summary


def latest_entity_mappings(
    mappings: Sequence[IfcEntityMapping],
) -> tuple[IfcEntityMapping, ...]:
    """Dedupe mapping rows to the newest record per (artifact, entity).

    ``mark_mapping_reconciliation`` appends a re-sealed row under the same
    (import_artifact_id, step_entity_id) — the append-only store keeps the
    transition history. Any view that treats the list as one row per
    entity (diff computation, merged-subject build) must reduce to the
    latest record; a ``list_entity_mappings`` result is seq-ordered, so
    the LAST occurrence wins.
    """
    latest: dict[tuple[str, int], IfcEntityMapping] = {}
    order: list[tuple[str, int]] = []
    for mapping in mappings:
        key = (mapping.import_artifact_id, mapping.step_entity_id)
        if key not in latest:
            order.append(key)
        latest[key] = mapping
    return tuple(latest[k] for k in order)


# ---------------------------------------------------------------------------
# Merge (accept -> new state, skip/pending -> prior state preserved)
# ---------------------------------------------------------------------------


def merge_ifc_diff_mappings(
    *,
    rows: Sequence[IfcDiffRow],
    decisions: Mapping[str, IfcDiffDecision],
    prior_mappings: Sequence[IfcEntityMapping],
    new_mappings: Sequence[IfcEntityMapping],
) -> tuple[IfcEntityMapping, ...]:
    """The merged mapping set an apply materializes.

    ``accepted`` rows take the new file's mapping; ``skipped`` and
    ``pending`` rows keep the prior side — nothing the operator has not
    accepted is silently committed, and nothing they skipped is silently
    deleted. ``matched`` rows always carry the new mapping (identical
    content).
    """
    prior_by_id = {m.mapping_id: m for m in prior_mappings}
    new_by_id = {m.mapping_id: m for m in new_mappings}
    merged: dict[str, IfcEntityMapping] = {}
    order: list[str] = []

    def _keep(mapping_id: str | None, table) -> None:
        if mapping_id is None or mapping_id not in table:
            return
        if mapping_id not in merged:
            order.append(mapping_id)
            merged[mapping_id] = table[mapping_id]

    for row in rows:
        if not row.decision_required or row.category == 'matched':
            _keep(row.new_mapping_id or row.prior_mapping_id, new_by_id)
            if row.new_mapping_id is None:
                _keep(row.prior_mapping_id, prior_by_id)
            continue
        decision = decisions.get(row.row_key, 'pending')
        adopt_new = decision == 'accepted'
        if row.category in ('added',):
            if adopt_new:
                _keep(row.new_mapping_id, new_by_id)
            continue
        if row.category in ('removed',):
            if not adopt_new:
                _keep(row.prior_mapping_id, prior_by_id)
            continue
        if row.category == 'unknown':
            # prior-side unknown: skip keeps, accept drops.
            # new-side unknown: accept includes, skip excludes.
            if row.prior_mapping_id is not None:
                if not adopt_new:
                    _keep(row.prior_mapping_id, prior_by_id)
            elif adopt_new:
                _keep(row.new_mapping_id, new_by_id)
            continue
        # changed / ambiguous: adopt the new side on accept, keep prior.
        if adopt_new:
            _keep(row.new_mapping_id, new_by_id)
        else:
            _keep(row.prior_mapping_id, prior_by_id)

    # Prior mappings the diff never saw (defensive: row coverage should
    # be total) survive — never silently dropped.
    covered_prior = {r.prior_mapping_id for r in rows}
    covered_new = {r.new_mapping_id for r in rows}
    for m in prior_mappings:
        if m.mapping_id not in covered_prior:
            _keep(m.mapping_id, prior_by_id)
    for m in new_mappings:
        if m.mapping_id not in covered_new:
            _keep(m.mapping_id, new_by_id)
    return tuple(merged[k] for k in order)


def reconciliation_marks(
    *,
    rows: Sequence[IfcDiffRow],
    decisions: Mapping[str, IfcDiffDecision],
    prior_mappings: Sequence[IfcEntityMapping],
    new_mappings: Sequence[IfcEntityMapping],
) -> tuple[tuple[str, IfcReconciliationState], ...]:
    """Mapping ids to re-seal with a new reconciliation state on apply.

    Only *accepted* rows mark anything: skipped/pending rows stay
    unreconciled — the review record carries the flag. ``removed``
    accepts mark the PRIOR mapping ``stale``; ``ambiguous_rebind``
    accepts mark the new mapping ``unchanged`` and the prior ``stale``.
    """
    prior_by_id = {m.mapping_id: m for m in prior_mappings}
    new_by_id = {m.mapping_id: m for m in new_mappings}
    marks: list[tuple[str, IfcReconciliationState]] = []
    for row in rows:
        if decisions.get(row.row_key) != 'accepted':
            continue
        if row.category == 'changed':
            state: IfcReconciliationState = (
                'geometry_changed'
                if row.change_kind == 'geometry_changed'
                else 'semantics_changed'
            )
            if row.new_mapping_id is not None:
                marks.append((row.new_mapping_id, state))
            if row.prior_mapping_id is not None:
                marks.append((row.prior_mapping_id, 'stale'))
        elif row.category == 'removed' and row.prior_mapping_id is not None:
            marks.append((row.prior_mapping_id, 'stale'))
        elif row.category == 'ambiguous':
            if row.new_mapping_id is not None:
                marks.append((row.new_mapping_id, 'unchanged'))
            if row.prior_mapping_id is not None:
                marks.append((row.prior_mapping_id, 'stale'))
        elif row.category == 'unknown' and row.prior_mapping_id is not None:
            marks.append((row.prior_mapping_id, 'stale'))
    return tuple(marks)


# ---------------------------------------------------------------------------
# Sealed apply record
# ---------------------------------------------------------------------------


def build_ifc_diff_apply(
    *,
    document_id: str,
    delta,
    prior_artifact: IfcImportArtifact,
    new_artifact: IfcImportArtifact,
    prior_subject_sha256: str | None,
    merged_subject,
    decisions: Sequence[IfcDiffRowDecision],
    rows: Sequence[IfcDiffRow],
    applied_by: str,
    applied_at_utc: str,
) -> IfcDiffApply:
    """Seal the apply record — the persisted, hashed diff summary."""
    summary = ifc_diff_summary(rows)
    decision_map = {d.row_key: d.decision for d in decisions}
    applied = sorted(
        k for k, v in decision_map.items() if v == 'accepted'
    )
    skipped = sorted(k for k, v in decision_map.items() if v == 'skipped')
    pending = sorted(
        row.row_key for row in rows
        if row.decision_required and row.row_key not in decision_map
    )
    probe = IfcDiffApply.model_construct(
        **_canon(
            IfcDiffApply,
            dict(
                apply_id='',
                document_id=document_id,
                delta_ref=AuthorityRef(
                    kind='ifc_revision_delta',
                    ref_id=delta.delta_id,
                    ref_sha256=delta.delta_sha256,
                ),
                prior_artifact_ref=AuthorityRef(
                    kind='ifc_import_artifact',
                    ref_id=prior_artifact.artifact_id,
                    ref_sha256=prior_artifact.artifact_sha256,
                ),
                new_artifact_ref=AuthorityRef(
                    kind='ifc_import_artifact',
                    ref_id=new_artifact.artifact_id,
                    ref_sha256=new_artifact.artifact_sha256,
                ),
                prior_subject_sha256=prior_subject_sha256,
                merged_subject_id=merged_subject.subject_id,
                merged_subject_sha256=merged_subject.subject_sha256,
                decisions=tuple(decisions),
                applied_row_keys=tuple(applied),
                skipped_row_keys=tuple(skipped),
                pending_row_keys=tuple(pending),
                matched_count=summary['matched'],
                changed_count=summary['changed'],
                added_count=summary['added'],
                removed_count=summary['removed'],
                ambiguous_count=summary['ambiguous'],
                unknown_count=summary['unknown'],
                applied_by=applied_by,
                applied_at_utc=applied_at_utc,
                apply_sha256='',
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return IfcDiffApply(
        **probe.model_dump(exclude={'apply_id', 'apply_sha256'}),
        apply_id=f'ida:{sha}',
        apply_sha256=sha,
    )


# ---------------------------------------------------------------------------
# Operator-facing labels
# ---------------------------------------------------------------------------

IFC_DIFF_LABELS: dict[str, str] = {
    'section.diff_review': 'IFC 差分レビュー',
    'category.matched': '一致',
    'category.changed': '変更',
    'category.added': '追加',
    'category.removed': '削除',
    'category.ambiguous': '要確認',
    'category.unknown': '不明',
    'change.unchanged': '変更なし',
    'change.geometry_changed': '形状変更',
    'change.semantics_changed': '属性変更',
    'change.added': '新規要素',
    'change.removed': 'ソース削除',
    'change.ambiguous_rebind': 'GlobalId 再採番',
    'change.duplicate_global_id': 'GlobalId 重複',
    'change.unmatched_identity': '識別不能',
    'decision.accepted': '承認',
    'decision.skipped': 'スキップ',
    'decision.pending': '未決定',
    'ui.import_revision': '改訂 IFC を取り込む',
    'ui.reload_diff': '差分を再読込',
    'ui.accept_all': '全て承認',
    'ui.skip_all': '全てスキップ',
    'ui.apply': '選択を適用',
    'ui.locate': '位置を表示',
    'column.category': '区分',
    'column.element': '要素',
    'column.global_id': 'GlobalId',
    'column.prior_hash': '旧ジオメトリ',
    'column.new_hash': '新ジオメトリ',
    'column.decision': '決定',
    'column.accept': '承認',
    'column.skip': 'スキップ',
    'column.locate': '位置',
}


def ifc_diff_label(key: str) -> str:
    """JA operator-facing label; unknown keys fall back to the key."""
    return IFC_DIFF_LABELS.get(key, key)


__all__ = [
    'IfcDiffApply',
    'IfcDiffCategory',
    'IfcDiffChangeKind',
    'IfcDiffDecision',
    'IfcDiffError',
    'IfcDiffRow',
    'IfcDiffRowDecision',
    'build_ifc_diff_apply',
    'compute_ifc_diff_rows',
    'ifc_diff_label',
    'ifc_diff_summary',
    'latest_entity_mappings',
    'merge_ifc_diff_mappings',
    'reconciliation_marks',
]
