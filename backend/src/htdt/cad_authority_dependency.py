"""Authority dependency / staleness graph (#729, REV59-DEPS).

Dozens of domain authorities already declare that some change should make
affected predictions, evidence and approvals stale — but without one
canonical dependency/invalidation layer each domain invents its own hidden
``stale`` boolean and HTDT risks both under-invalidation (an obsolete PASS
survives a material change) and over-invalidation (a harmless metadata
change forces a full re-commission). This module is that layer:

- :class:`DependencyEdgeDeclaration` — the sealed typed edge. A dependent
  record (subject) declares exactly which authority it depends on, under
  which semantics (:class:`DependencyEdgeKind`), and — optionally — over
  which :class:`DependencyScope` (which subject observables the edge
  carries, which target fields it tracks, which observables are declared
  independent). ``references_only`` edges never propagate invalidation:
  a report may reference a decorative image without depending on it
  computationally (#729 §2/§3).

- :class:`DependencyRuleProfile` — the sealed, versioned invalidation
  ruleset. Each :class:`DependencyRuleEntry` maps an
  ``(edge kind, change class)`` pair to an :class:`InvalidationEffect`
  and an optional explicit :class:`RevalidationActionKind`. Rules are
  versioned data, so changing invalidation logic never silently
  reinterprets historical verdicts (#729 §5).

- :class:`SemanticChangeEvent` — the sealed semantic change declaration:
  which authority identity changed, which semantic fields changed
  (``changed_fields``), its class, and its successor. Invalidation
  consumes semantic change, not only hash difference (#729 §4).

- :func:`evaluate_staleness` → :class:`StalenessAssessment` — the sealed
  verdict. For every subject reachable from the changed authority over
  firing influence edges, the evaluator applies the rule profile and
  reports the strongest applicable :class:`StalenessState` with the
  explainable path that produced it (#729 §18). Undeclared dependencies
  fail closed in the other direction: a subject with no declared lineage
  cannot claim staleness, and a subject whose lineage is declared
  partial/unknown is forced to ``unknown_dependency`` review rather than
  silently preserved (#729 §16).

- :func:`build_revalidation_plan` → :class:`RevalidationPlan` — the
  smallest safe work set derived from an assessment: recompute a
  prediction, regenerate a derived metric, remeasure a channel, read
  back a device, requalify a profile, or review evidence — never a
  default full re-commission (#729 §15).

Composition: #590 ``authority_graph`` remains the read-side lineage
projection for the explorer UI — it never persists invalidation verdicts.
This module persists the *declared* typed dependencies and the
*evaluated* staleness/revalidation verdicts the domain authorities
consume.

Literature basis
----------------
- W3C PROV-DM (Recommendation) — entities/activities/agents, derivation
  vs. influence vs. association, revision and invalidation semantics.
  HTDT adopts the concepts, not the RDF serialization (#729 research).
- Mokhov, Mitchell & Peyton Jones, "Build Systems à la Carte" (ICFP
  2018) — fine-grained typed dependencies and minimal recomputation as a
  function of the dependency topology and the changed inputs.
- Make/linear-build DAG theory — change propagation over a typed acyclic
  graph; cycles are avoided because every iteration persists as a new
  immutable node (#729 §17).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


DEPENDENCY_GRAPH_SCHEMA_VERSION = 'authority-dependency-1'
STALENESS_EVALUATION_VERSION = 'staleness-eval-1'
REVALIDATION_PLAN_VERSION = 'revalidation-plan-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{
            sha_field: digest,
            id_field: _semantic_id(prefix, digest),
        },
    )


def _require_ref_sha(ref: AuthorityRef | None, label: str) -> None:
    if ref is not None and ref.ref_sha256 is None:
        raise ValueError(f'{label} must pin its sha256')


# ----------------------------------------------------------------------
# Taxonomies

DependencyEdgeKind = Literal[
    'used_as_input',
    'derived_from',
    'calibrated_from',
    'validated_against',
    'registered_to',
    'configured_by',
    'measured_under_state',
    'applicable_under_profile',
    'assumes',
    'requires_capability',
    'approved_for_use_by',
    'supersedes',
    'references_only',
]
"""#729 §2 — derivation/use/calibration/validation/state/profile/
approval semantics are distinct edge types, never one generic
``depends_on``. ``references_only`` is a non-influencing association:
it is kept for inspection but never propagates invalidation (#729 §3)."""

INFLUENCE_EDGE_KINDS: frozenset[str] = frozenset({
    'used_as_input',
    'derived_from',
    'calibrated_from',
    'validated_against',
    'registered_to',
    'configured_by',
    'measured_under_state',
    'applicable_under_profile',
    'assumes',
    'requires_capability',
    'approved_for_use_by',
    'supersedes',
})
"""Edge kinds along which a semantic change can propagate. The dependent
side of a ``supersedes`` edge is the *successor* revision lineage —
the predecessor is the target, so a declared supersession makes the
target's *current applicability* superseded, not physically false
(#729 §9/§14)."""

SemanticChangeClass = Literal[
    'geometry_material',
    'label_metadata',
    'equipment_definition',
    'device_firmware',
    'device_configuration',
    'measurement_state',
    'calibration_parameters',
    'standard_profile_revision',
    'solver_algorithm',
    'derived_transform',
    'project_metadata',
    'approval_scope',
    'other_declared',
]
"""#729 §4/§10 — the semantic class of a change. A speaker label rename
(``label_metadata``) and a speaker position change
(``geometry_material``) are different events with different rule
outcomes; a hash alone cannot tell them apart, so the class and the
exact ``changed_fields`` are declared."""

InvalidationEffect = Literal[
    'none',
    'recompute',
    'remeasure',
    'readback_required',
    'review_required',
    'incompatible',
    'superseded',
]
"""What happens to the subject when the rule fires. ``none`` means the
edge does not propagate this change class (e.g. a label rename through
``used_as_input``); ``superseded`` marks the claim's *current
applicability* — the record stays historically intact (#729 §14)."""

StalenessState = Literal[
    'current',
    'stale_recompute',
    'stale_remeasure',
    'stale_readback_required',
    'stale_review_required',
    'valid_with_limitations',
    'incompatible',
    'superseded',
    'unknown_dependency',
]
"""#729 §6 — richer than valid/invalid: stale means the current evidence
no longer proves the claim, not that the claim is physically false."""

RevalidationActionKind = Literal[
    'recompute_prediction',
    'regenerate_derived',
    'remeasure_channel',
    'readback_device',
    'requalify_profile',
    'review_evidence',
    'full_recommission',
]
"""#729 §15 — the minimal-work taxonomy the revalidation plan emits.
``full_recommission`` is never a default; a rule entry must declare it
explicitly."""

LineageCompleteness = Literal['complete', 'partial', 'unknown']
"""#729 §16 — how complete the subject's declared dependency lineage
is. Legacy/imported artifacts may be ``partial`` or ``unknown`` and
then fail conservatively instead of silently staying current."""

_EFFECT_SEVERITY: dict[str, int] = {
    'none': 0,
    'superseded': 1,
    'review_required': 2,
    'recompute': 3,
    'readback_required': 4,
    'remeasure': 5,
    'incompatible': 6,
}
"""Worst-effect ordering used when several firing paths hit one subject:
conservative — the strongest applicable invalidation wins (#729 §16)."""

_EFFECT_TO_STATE: dict[str, StalenessState] = {
    'recompute': 'stale_recompute',
    'remeasure': 'stale_remeasure',
    'readback_required': 'stale_readback_required',
    'review_required': 'stale_review_required',
    'incompatible': 'incompatible',
    'superseded': 'superseded',
}

_DEFAULT_ACTION_FOR_EFFECT: dict[str, RevalidationActionKind] = {
    'recompute': 'recompute_prediction',
    'remeasure': 'remeasure_channel',
    'readback_required': 'readback_device',
    'review_required': 'review_evidence',
    'incompatible': 'full_recommission',
    'superseded': 'review_evidence',
}


class DependencyScope(BaseModel):
    """The declared footprint of one dependency edge (#729 §7/§8).

    ``target_fields`` names the target's semantic fields this edge
    actually tracks — a change outside them does not fire the edge
    (speaker ``label`` vs. speaker ``position``). ``observables`` names
    the subject observables carried through this edge; together with
    ``retained_observables`` and ``residual_independence='declared'`` it
    supports capability-level invalidation — absolute SPL stale while
    relative timing stays current — instead of whole-artifact
    invalidation. When residual independence is not declared, a firing
    edge stales the whole subject conservatively.
    """

    model_config = ConfigDict(frozen=True)

    target_fields: tuple[str, ...] = ()
    observables: tuple[str, ...] = ()
    retained_observables: tuple[str, ...] = ()
    residual_independence: Literal['declared', 'unknown'] = 'unknown'
    frequency_band_hz: tuple[float, float] | None = None
    channels: tuple[str, ...] = ()
    positions: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'DependencyScope':
        if self.frequency_band_hz is not None:
            lo, hi = self.frequency_band_hz
            if not (0.0 < lo <= hi):
                raise ValueError(
                    'scope frequency_band_hz must be positive (lo, hi)'
                )
        if self.retained_observables and (
            self.residual_independence != 'declared'
        ):
            raise ValueError(
                'retained observables require declared residual '
                'independence — undeclared independence is not proven'
            )
        return self


class DependencyEdgeDeclaration(BaseModel):
    """One sealed typed dependency: ``subject_ref`` depends on
    ``target_ref`` under ``kind`` semantics (#729 §1/§2)."""

    model_config = ConfigDict(frozen=True)

    edge_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    subject_ref: AuthorityRef
    kind: DependencyEdgeKind
    target_ref: AuthorityRef
    scope: DependencyScope | None = None
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=DEPENDENCY_GRAPH_SCHEMA_VERSION, min_length=1
    )
    edge_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'subject_ref': self.subject_ref.model_dump(mode='json'),
            'kind': self.kind,
            'target_ref': self.target_ref.model_dump(mode='json'),
            'scope': (
                self.scope.model_dump(mode='json')
                if self.scope is not None
                else None
            ),
            'declared_at_utc': self.declared_at_utc,
            'authority_version': self.authority_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'DependencyEdgeDeclaration':
        _require_iso8601(self.declared_at_utc, 'edge declared_at_utc')
        _require_ref_sha(self.subject_ref, 'edge subject_ref')
        _require_ref_sha(self.target_ref, 'edge target_ref')
        if self.subject_ref == self.target_ref:
            raise ValueError('an edge cannot depend on itself')
        expected = _hash(self.identity_payload())
        if self.edge_sha256 != expected:
            raise ValueError('dependency edge hash mismatch')
        if self.edge_id != _semantic_id('depedge', expected):
            raise ValueError('dependency edge id does not match its hash')
        return self


def build_dependency_edge(
    *,
    document_id: str,
    subject_ref: AuthorityRef,
    kind: DependencyEdgeKind,
    target_ref: AuthorityRef,
    scope: DependencyScope | None = None,
    declared_at_utc: str | None = None,
) -> DependencyEdgeDeclaration:
    """Seal one typed dependency declaration."""
    return _seal(
        DependencyEdgeDeclaration,
        {
            'document_id': document_id,
            'subject_ref': subject_ref.model_dump(mode='json'),
            'kind': kind,
            'target_ref': target_ref.model_dump(mode='json'),
            'scope': (
                scope.model_dump(mode='json')
                if scope is not None
                else None
            ),
            'declared_at_utc': declared_at_utc or _utc_now(),
        },
        'edge_id',
        'edge_sha256',
        'depedge',
    )


def dependency_edge_binding(
    edge: DependencyEdgeDeclaration,
) -> AuthorityRef:
    return AuthorityRef(
        kind='dependency_edge_declaration',
        ref_id=edge.edge_id,
        ref_sha256=edge.edge_sha256,
    )


class DependencyRuleEntry(BaseModel):
    """One versioned invalidation rule (#729 §5/§19).

    Maps ``(edge kind, change class)`` to the effect on the dependent
    subject plus an optional explicit revalidation action. An explicit
    ``action`` lets a profile say e.g. a standards-profile revision
    requires ``requalify_profile`` rather than generic recompute.
    """

    model_config = ConfigDict(frozen=True)

    entry_id: str = Field(min_length=1)
    edge_kind: DependencyEdgeKind
    change_class: SemanticChangeClass
    effect: InvalidationEffect
    action: RevalidationActionKind | None = None
    note: str | None = None


class DependencyRuleProfile(BaseModel):
    """The sealed, versioned invalidation ruleset (#729 §5).

    Rules are data with a ``ruleset_version`` so a rule change never
    silently reinterprets a historical staleness verdict — the verdict
    pins the exact profile it was evaluated under.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    ruleset_version: str = Field(min_length=1)
    entries: tuple[DependencyRuleEntry, ...] = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=DEPENDENCY_GRAPH_SCHEMA_VERSION, min_length=1
    )
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'ruleset_version': self.ruleset_version,
            'entries': [e.model_dump(mode='json') for e in self.entries],
            'declared_at_utc': self.declared_at_utc,
            'authority_version': self.authority_version,
        }

    def lookup(
        self, edge_kind: str, change_class: str
    ) -> DependencyRuleEntry | None:
        for entry in self.entries:
            if (
                entry.edge_kind == edge_kind
                and entry.change_class == change_class
            ):
                return entry
        return None

    @model_validator(mode='after')
    def _check(self) -> 'DependencyRuleProfile':
        _require_iso8601(self.declared_at_utc, 'profile declared_at_utc')
        seen: set[tuple[str, str]] = set()
        for entry in self.entries:
            key = (entry.edge_kind, entry.change_class)
            if key in seen:
                raise ValueError(
                    f'duplicate rule entry {key[0]}/{key[1]}'
                )
            seen.add(key)
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('dependency rule profile hash mismatch')
        if self.profile_id != _semantic_id('deprule', expected):
            raise ValueError(
                'dependency rule profile id does not match its hash'
            )
        return self


def build_rule_profile(
    *,
    document_id: str,
    ruleset_version: str,
    entries: Sequence[DependencyRuleEntry],
    declared_at_utc: str | None = None,
) -> DependencyRuleProfile:
    """Seal one versioned invalidation ruleset."""
    return _seal(
        DependencyRuleProfile,
        {
            'document_id': document_id,
            'ruleset_version': ruleset_version,
            'entries': [e.model_dump(mode='json') for e in entries],
            'declared_at_utc': declared_at_utc or _utc_now(),
        },
        'profile_id',
        'profile_sha256',
        'deprule',
    )


def rule_profile_binding(
    profile: DependencyRuleProfile,
) -> AuthorityRef:
    return AuthorityRef(
        kind='dependency_rule_profile',
        ref_id=profile.profile_id,
        ref_sha256=profile.profile_sha256,
    )


class SemanticChangeEvent(BaseModel):
    """The sealed semantic change declaration (#729 §4).

    ``changed_ref`` pins the pre-change authority identity,
    ``successor_ref`` the post-change identity where one exists, and
    ``changed_fields`` the exact semantic fields — invalidation consumes
    this declared semantic delta, never a bare byte/hash difference.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    changed_ref: AuthorityRef
    successor_ref: AuthorityRef | None = None
    changed_fields: tuple[str, ...] = Field(min_length=1)
    change_class: SemanticChangeClass
    occurred_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=DEPENDENCY_GRAPH_SCHEMA_VERSION, min_length=1
    )
    event_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'changed_ref': self.changed_ref.model_dump(mode='json'),
            'successor_ref': (
                self.successor_ref.model_dump(mode='json')
                if self.successor_ref is not None
                else None
            ),
            'changed_fields': list(self.changed_fields),
            'change_class': self.change_class,
            'occurred_at_utc': self.occurred_at_utc,
            'authority_version': self.authority_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'SemanticChangeEvent':
        _require_iso8601(self.occurred_at_utc, 'event occurred_at_utc')
        _require_ref_sha(self.changed_ref, 'event changed_ref')
        _require_ref_sha(self.successor_ref, 'event successor_ref')
        expected = _hash(self.identity_payload())
        if self.event_sha256 != expected:
            raise ValueError('semantic change event hash mismatch')
        if self.event_id != _semantic_id('depevt', expected):
            raise ValueError(
                'semantic change event id does not match its hash'
            )
        return self


def build_change_event(
    *,
    document_id: str,
    changed_ref: AuthorityRef,
    change_class: SemanticChangeClass,
    changed_fields: Sequence[str],
    successor_ref: AuthorityRef | None = None,
    occurred_at_utc: str | None = None,
) -> SemanticChangeEvent:
    """Seal one semantic change event."""
    return _seal(
        SemanticChangeEvent,
        {
            'document_id': document_id,
            'changed_ref': changed_ref.model_dump(mode='json'),
            'successor_ref': (
                successor_ref.model_dump(mode='json')
                if successor_ref is not None
                else None
            ),
            'changed_fields': list(changed_fields),
            'change_class': change_class,
            'occurred_at_utc': occurred_at_utc or _utc_now(),
        },
        'event_id',
        'event_sha256',
        'depevt',
    )


def change_event_binding(event: SemanticChangeEvent) -> AuthorityRef:
    return AuthorityRef(
        kind='semantic_change_event',
        ref_id=event.event_id,
        ref_sha256=event.event_sha256,
    )


# ----------------------------------------------------------------------
# Staleness assessment

class StalenessEntry(BaseModel):
    """One subject's staleness verdict with its explainable path (#729 §18)."""

    model_config = ConfigDict(frozen=True)

    subject_ref: AuthorityRef
    state: StalenessState
    path: tuple[AuthorityRef, ...] = ()
    """The dependency path subject → … → changed authority that produced
    the strongest verdict — the explanation, never an unexplained badge."""
    edge_kinds: tuple[DependencyEdgeKind, ...] = ()
    rule_entry_id: str | None = None
    required_action: RevalidationActionKind | None = None
    affected_observables: tuple[str, ...] = ()
    retained_observables: tuple[str, ...] = ()
    conservative: bool = False
    reason: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'StalenessEntry':
        if self.state != 'current' and not self.path:
            raise ValueError(
                'a non-current staleness verdict must carry its '
                'dependency path'
            )
        if self.state == 'valid_with_limitations' and (
            not self.affected_observables or not self.retained_observables
        ):
            raise ValueError(
                'valid_with_limitations must name affected and retained '
                'observables'
            )
        return self


class StalenessAssessment(BaseModel):
    """The sealed staleness verdict for one change event (#729 §6/§18)."""

    model_config = ConfigDict(frozen=True)

    assessment_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    change_event_ref: AuthorityRef
    rule_profile_ref: AuthorityRef
    entries: tuple[StalenessEntry, ...] = ()
    unknown_lineage_refs: tuple[AuthorityRef, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_version: str = Field(
        default=STALENESS_EVALUATION_VERSION, min_length=1
    )
    assessment_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'change_event_ref': self.change_event_ref.model_dump(mode='json'),
            'rule_profile_ref': self.rule_profile_ref.model_dump(mode='json'),
            'entries': [e.model_dump(mode='json') for e in self.entries],
            'unknown_lineage_refs': [
                r.model_dump(mode='json') for r in self.unknown_lineage_refs
            ],
            'evaluated_at_utc': self.evaluated_at_utc,
            'evaluation_version': self.evaluation_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'StalenessAssessment':
        _require_iso8601(
            self.evaluated_at_utc, 'assessment evaluated_at_utc'
        )
        _require_ref_sha(self.change_event_ref, 'assessment change_event_ref')
        _require_ref_sha(self.rule_profile_ref, 'assessment rule_profile_ref')
        expected = _hash(self.identity_payload())
        if self.assessment_sha256 != expected:
            raise ValueError('staleness assessment hash mismatch')
        if self.assessment_id != _semantic_id('stale', expected):
            raise ValueError(
                'staleness assessment id does not match its hash'
            )
        return self


def _edge_fires(
    edge: DependencyEdgeDeclaration,
    event: SemanticChangeEvent,
) -> bool:
    """Whether this edge's declared scope tracks any changed field."""
    if edge.kind not in INFLUENCE_EDGE_KINDS:
        return False
    if edge.scope is None or not edge.scope.target_fields:
        return True
    return bool(set(edge.scope.target_fields) & set(event.changed_fields))


def _ref_key(ref: AuthorityRef) -> str:
    return f'{ref.kind}:{ref.ref_id}'


def evaluate_staleness(
    document_id: str,
    edges: Sequence[DependencyEdgeDeclaration],
    change_event: SemanticChangeEvent,
    rule_profile: DependencyRuleProfile,
    *,
    lineage: dict[str, LineageCompleteness] | None = None,
    unknown_subjects: Sequence[AuthorityRef] = (),
    evaluated_at_utc: str | None = None,
) -> StalenessAssessment:
    """Fail-closed staleness verdict for one semantic change (#729).

    Semantics:

    - Propagation walks *outgoing* influence edges subject → target: a
      subject is affected only when a path to the changed authority
      exists on which every edge fires (kind propagates, declared
      ``target_fields`` intersect the semantic delta, and the rule does
      not resolve to ``none``). ``references_only`` edges never
      propagate (#729 §3).
    - No matching rule entry → ``review_required`` marked
      ``conservative`` (#729 §8: insufficient dependency resolution is
      labeled CONSERVATIVE, not silently exact).
    - Several firing paths → the strongest applicable effect wins.
    - Scope-declared independence: when *every* firing edge reaching a
      subject carries a declared ``retained_observables`` set and a
      non-empty ``observables`` set, the subject is
      ``valid_with_limitations`` naming affected vs. retained
      observables (#729 §7) instead of whole-artifact stale.
    - ``unknown_subjects`` (caller's unknown/partial-lineage candidates)
      get ``unknown_dependency`` — review, never silent CURRENT
      (#729 §16). Subjects carrying declared lineage that also resolves
      stale keep their computed state.
    """

    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')

    changed_key = _ref_key(change_event.changed_ref)
    by_subject: dict[str, list[DependencyEdgeDeclaration]] = {}
    ref_index: dict[str, AuthorityRef] = {
        changed_key: change_event.changed_ref
    }
    for edge in edges:
        if edge.document_id != document_id:
            continue
        by_subject.setdefault(_ref_key(edge.subject_ref), []).append(edge)
        ref_index[_ref_key(edge.subject_ref)] = edge.subject_ref
        ref_index[_ref_key(edge.target_ref)] = edge.target_ref

    # affected[target] = list of (path, edge_kinds, rule_ids, severity,
    # scope, action, no_rule) from target back to the changed authority.
    # Path severity accumulates: a descendant inherits the strongest
    # limitation anywhere on its dependency path (#729 §11) — a
    # recomputed verdict from an ineligible source cannot keep the old
    # PASS. The winning-edge annotation (rule, action, scope, no_rule)
    # belongs to the edge that produced that strongest effect.
    _Path = tuple[
        tuple[AuthorityRef, ...],
        tuple[str, ...],
        tuple[str, ...],
        int,
        DependencyScope | None,
        RevalidationActionKind | None,
        bool,
    ]
    affected: dict[str, list[_Path]] = {
        changed_key: [
            ((change_event.changed_ref,), (), (), 0, None, None, False)
        ]
    }
    # Fixpoint propagation with path-set merging: a subject whose only
    # route to the change surfaces late still reaches its verdict, and
    # a worse later-discovered path can still raise an already-affected
    # subject — a single pass could under-invalidate (#729 §16). Paths
    # are simple (no repeated node), so the fixpoint terminates.
    progressed = True
    while progressed:
        progressed = False
        for subject_key, subject_edges in by_subject.items():
            known = affected.setdefault(subject_key, [])
            existing_paths = {p[0] for p in known}
            for edge in subject_edges:
                if not _edge_fires(edge, change_event):
                    continue
                target_key = _ref_key(edge.target_ref)
                if target_key not in affected:
                    continue
                rule = rule_profile.lookup(
                    edge.kind, change_event.change_class
                )
                effect = rule.effect if rule is not None else (
                    'review_required'
                )
                if effect == 'none':
                    continue
                severity = _EFFECT_SEVERITY[effect]
                rule_id = rule.entry_id if rule is not None else None
                action: RevalidationActionKind | None = (
                    rule.action
                    if rule is not None and rule.action is not None
                    else _DEFAULT_ACTION_FOR_EFFECT[effect]
                )
                no_rule = rule is None
                for path, kinds, rules, dseverity, _scope, _action, _nr in affected[target_key]:
                    new_path = (edge.subject_ref,) + path
                    if new_path in existing_paths:
                        continue
                    if edge.subject_ref in path:
                        continue  # simple paths only — no cycles
                    if severity >= dseverity:
                        p_severity, p_scope, p_action, p_nr = (
                            severity, edge.scope, action, no_rule,
                        )
                    else:
                        p_severity, p_scope, p_action, p_nr = (
                            dseverity, _scope, _action, _nr,
                        )
                    known.append(
                        (
                            new_path,
                            (edge.kind,) + kinds,
                            ((rule_id,) if rule_id else ()) + rules,
                            p_severity,
                            p_scope,
                            p_action,
                            p_nr or no_rule,
                        )
                    )
                    existing_paths.add(new_path)
                    progressed = True
            if not known:
                del affected[subject_key]

    entries: list[StalenessEntry] = []
    for subject_key in sorted(affected):
        if subject_key == changed_key:
            continue
        paths = affected[subject_key]
        best_severity = max(p[3] for p in paths)
        best = [p for p in paths if p[3] == best_severity]
        path, kinds, rules, _, scope, action, no_rule = best[0]
        effect = next(
            e for e, s in _EFFECT_SEVERITY.items() if s == best_severity
        )
        # Scope-declared residual independence: only when the winning
        # edge declares observables + a retained set may the subject
        # keep partial validity (#729 §7) instead of whole-artifact
        # staleness.
        scoped_ok = (
            scope is not None
            and scope.observables
            and scope.residual_independence == 'declared'
            and scope.retained_observables
        )
        conservative = no_rule
        if scoped_ok:
            state: StalenessState = 'valid_with_limitations'
            affected_obs = tuple(scope.observables)
            retained_obs = tuple(scope.retained_observables)
            reason = (
                'limited staleness — declared scope binds the affected '
                'observables; retained observables stay current'
            )
        else:
            state = _EFFECT_TO_STATE[effect]
            affected_obs = ()
            retained_obs = ()
            rule_note = (
                f'rule {rules[0]}' if rules else 'no matching rule'
            )
            reason = (
                f'subject stale because {path[-1].ref_id} changed '
                f'{"/".join(change_event.changed_fields)} '
                f'({change_event.change_class}) via {kinds[0]} — '
                f'{rule_note}'
            )
            if scope is not None:
                # A scope was declared but residual independence was
                # not proven — the narrowing is unverified (#729 §8).
                conservative = True
        entries.append(
            StalenessEntry(
                subject_ref=ref_index[subject_key],
                state=state,
                path=path,
                edge_kinds=kinds,
                rule_entry_id=rules[0] if rules else None,
                required_action=action,
                affected_observables=affected_obs,
                retained_observables=retained_obs,
                conservative=conservative,
                reason=reason,
            )
        )

    lineage = lineage or {}
    unknown_refs: list[AuthorityRef] = []
    seen_subjects = {
        _ref_key(e.subject_ref) for e in entries
    }
    for ref in unknown_subjects:
        key = _ref_key(ref)
        if key in seen_subjects:
            continue
        completeness = lineage.get(key, 'unknown')
        if completeness == 'complete':
            continue
        unknown_refs.append(ref)
        seen_subjects.add(key)
        entries.append(
            StalenessEntry(
                subject_ref=ref,
                state='unknown_dependency',
                path=(ref, change_event.changed_ref),
                conservative=True,
                reason=(
                    f'lineage {completeness} — a material change may '
                    'affect this artifact; review required instead of '
                    'silently preserving CURRENT'
                ),
            )
        )

    return _seal(
        StalenessAssessment,
        {
            'document_id': document_id,
            'change_event_ref': change_event_binding(
                change_event
            ).model_dump(mode='json'),
            'rule_profile_ref': rule_profile_binding(
                rule_profile
            ).model_dump(mode='json'),
            'entries': [e.model_dump(mode='json') for e in entries],
            'unknown_lineage_refs': [
                r.model_dump(mode='json') for r in unknown_refs
            ],
            'evaluated_at_utc': evaluated_at_utc,
        },
        'assessment_id',
        'assessment_sha256',
        'stale',
    )


def staleness_binding(assessment: StalenessAssessment) -> AuthorityRef:
    return AuthorityRef(
        kind='staleness_assessment',
        ref_id=assessment.assessment_id,
        ref_sha256=assessment.assessment_sha256,
    )


# ----------------------------------------------------------------------
# Revalidation plan

class PlanAction(BaseModel):
    """One minimal-work action the plan requires (#729 §15)."""

    model_config = ConfigDict(frozen=True)

    kind: RevalidationActionKind
    subject_refs: tuple[AuthorityRef, ...] = Field(min_length=1)
    reason: str = Field(min_length=1)


class RevalidationPlan(BaseModel):
    """The sealed smallest-safe-work-set verdict (#729 §15)."""

    model_config = ConfigDict(frozen=True)

    plan_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    assessment_ref: AuthorityRef
    actions: tuple[PlanAction, ...] = ()
    planned_at_utc: str = Field(min_length=1)
    plan_version: str = Field(
        default=REVALIDATION_PLAN_VERSION, min_length=1
    )
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'assessment_ref': self.assessment_ref.model_dump(mode='json'),
            'actions': [a.model_dump(mode='json') for a in self.actions],
            'planned_at_utc': self.planned_at_utc,
            'plan_version': self.plan_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'RevalidationPlan':
        _require_iso8601(self.planned_at_utc, 'plan planned_at_utc')
        _require_ref_sha(self.assessment_ref, 'plan assessment_ref')
        expected = _hash(self.identity_payload())
        if self.plan_sha256 != expected:
            raise ValueError('revalidation plan hash mismatch')
        if self.plan_id != _semantic_id('revplan', expected):
            raise ValueError('revalidation plan id does not match its hash')
        return self


def build_revalidation_plan(
    document_id: str,
    assessment: StalenessAssessment,
    *,
    planned_at_utc: str | None = None,
) -> RevalidationPlan:
    """Derive the smallest safe revalidation work set (#729 §15).

    Each stale entry contributes exactly the action its winning rule
    effect demanded (an explicit rule action, else the default
    state → action map); current / superseded /
    valid_with_limitations entries add no work beyond what they already
    say. Actions deduplicate per ``(kind, sorted subject set)``.
    """

    planned_at_utc = planned_at_utc or _utc_now()
    _require_iso8601(planned_at_utc, 'planned_at_utc')

    grouped: dict[tuple[str, tuple[AuthorityRef, ...]], PlanAction] = {}
    by_action_subjects: dict[str, list[AuthorityRef]] = {}
    for entry in assessment.entries:
        if entry.state in (
            'current',
            'superseded',
            'valid_with_limitations',
        ):
            continue
        action_kind = entry.required_action or 'review_evidence'
        by_action_subjects.setdefault(action_kind, []).append(
            entry.subject_ref
        )

    for kind in sorted(by_action_subjects):
        refs = tuple(
            sorted(
                by_action_subjects[kind],
                key=_ref_key,
            )
        )
        grouped[(kind, refs)] = PlanAction(
            kind=kind,  # type: ignore[arg-type]
            subject_refs=refs,
            reason=(
                f'{len(refs)} artifact(s) require {kind} — smallest '
                'defensible scope, not a default full re-commission'
            ),
        )

    return _seal(
        RevalidationPlan,
        {
            'document_id': document_id,
            'assessment_ref': staleness_binding(
                assessment
            ).model_dump(mode='json'),
            'actions': [a.model_dump(mode='json') for a in grouped.values()],
            'planned_at_utc': planned_at_utc,
        },
        'plan_id',
        'plan_sha256',
        'revplan',
    )


def revalidation_binding(plan: RevalidationPlan) -> AuthorityRef:
    return AuthorityRef(
        kind='revalidation_plan',
        ref_id=plan.plan_id,
        ref_sha256=plan.plan_sha256,
    )


__all__ = [
    'DEPENDENCY_GRAPH_SCHEMA_VERSION',
    'STALENESS_EVALUATION_VERSION',
    'REVALIDATION_PLAN_VERSION',
    'DependencyEdgeKind',
    'INFLUENCE_EDGE_KINDS',
    'SemanticChangeClass',
    'InvalidationEffect',
    'StalenessState',
    'RevalidationActionKind',
    'LineageCompleteness',
    'DependencyScope',
    'DependencyEdgeDeclaration',
    'build_dependency_edge',
    'dependency_edge_binding',
    'DependencyRuleEntry',
    'DependencyRuleProfile',
    'build_rule_profile',
    'rule_profile_binding',
    'SemanticChangeEvent',
    'build_change_event',
    'change_event_binding',
    'StalenessEntry',
    'StalenessAssessment',
    'evaluate_staleness',
    'staleness_binding',
    'PlanAction',
    'RevalidationPlan',
    'build_revalidation_plan',
    'revalidation_binding',
]
