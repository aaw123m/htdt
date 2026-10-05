"""External standards registry / revision authority (issue #599).

A standards number is not a timeless specification identity: editions are
revised, confirmed, superseded and withdrawn, and HTDT evaluations that cite
``ISO 3382`` or ``RP22`` without an exact edition become irreproducible the
day a successor publishes. This module is the canonical shared registry for
that identity — every subsystem that evaluates against an external document
pins (standard_id, edition) here instead of inventing its own revision
logic.

Composition with existing authorities:

- :class:`StandardsSourceRecord` (#807, ``cad_standards_registry``) remains
  the provenance/licensing authority. ``ExternalStandardDocument`` adds the
  revision/lifecycle layer: exact edition identity, lifecycle state, source
  priority, conflicting-source handling and the admission workflow.
- ``StandardProfileRef`` (#608, ``cad_stimulus_registry``) names which
  profile a test asset claims; this registry decides whether that exact
  revision is current, draft-only, superseded or source-ambiguous.
- ``StandardsSourceAuthority`` extractions (``cad_standards_authorities``)
  become ``StandardProfileMapping`` rows so a correct external revision
  with an old/broken HTDT mapping is still not a valid current evaluation.

Honesty rules (mechanically enforced):

- A citation of an unregistered (standard_id, edition) resolves to
  ``not_registered`` — an UNKNOWN, never an implicit current.
- Draft/public-review/DIS/FDIS/industry-review documents are
  ``draft_research_only``; they can never be a production conformance
  basis.
- ``superseded``/``withdrawn``/``replaced_by``/``revised``/``historical``
  documents remain valid *historical provenance* (old evaluations stay
  reproducible) but are blocked as a production citation.
- Equal-top-tier official sources that disagree produce ``status_conflict``
  — the registry keeps the competing observations rather than guessing.
- An evaluation pins (document edition, mapping version, calculation
  version, input evidence ids); a newer admitted edition never rewrites an
  old pin.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


_SHA256 = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


# ---------------------------------------------------------------------------
# Taxonomy (#599 §2, §3, §6, §12, §14)
# ---------------------------------------------------------------------------

StandardLifecycleStatus = Literal[
    'draft',
    'public_review',
    'dis_fdis_prepublication',
    'industry_review',
    'published_current',
    'reaffirmed',
    'under_revision',
    'superseded',
    'revised',
    'withdrawn',
    'replaced_by',
    'historical',
    'status_conflict',
    'unknown',
]
"""Lifecycle state of one (standard_id, edition). These states are not a
linear ranking: a ``superseded`` metric may still be required by an
external profile, and a ``published_current`` document can simultaneously
be ``under_revision`` (we record that as ``under_revision``)."""

StandardsSourceTier = Literal[
    'standards_body_catalog',
    'official_publisher_announcement',
    'official_standards_store',
    'official_committee_or_review_page',
    'secondary_industry_source',
    'third_party_summary',
]
"""Source-authority ordering (#599 §3). Lifecycle/revision claims prefer
the highest tier available; a lower tier may aid discovery but never
silently overrides primary metadata."""

_TIER_RANK = {
    'standards_body_catalog': 0,
    'official_publisher_announcement': 1,
    'official_standards_store': 2,
    'official_committee_or_review_page': 3,
    'secondary_industry_source': 4,
    'third_party_summary': 5,
}

StandardRightsClass = Literal[
    'public_metadata_only',
    'public_open_standard',
    'licensed_internal_profile',
    'user_provided_licensed_source',
    'derived_rules_allowed',
    'reference_only',
    'redistribution_prohibited',
    'unknown_rights',
]
"""Copyright/licensing boundary (#599 §6). The registry never stores
protected standard text — only lawful metadata, identifiers, derived
rules and citations."""

StandardsAdmissionState = Literal[
    'discovered',
    'primary_source_confirmed',
    'rights_reviewed',
    'profile_parsed_mapped',
    'mapping_reviewed',
    'validated',
    'production_eligible',
    'limited',
    'retired_for_new_projects',
]
"""Admission workflow (#599 §12): the raw fact that a standard exists does
not make an HTDT evaluator production-ready."""

StandardsEvaluationState = Literal[
    'pass',
    'fail',
    'indeterminate',
    'not_applicable',
    'missing_required_evidence',
    'unsupported_requirement',
    'profile_source_ambiguous',
    'profile_mapping_unvalidated',
    'license_profile_unavailable',
]
"""External-profile evaluation outcomes (#599 §14). A software inability
to evaluate a clause is never a physical FAIL."""

StandardsCapabilityVerdict = Literal[
    'production_eligible',
    'limited',
    'draft_research_only',
    'not_registered',
    'source_ambiguous',
    'mapping_unvalidated',
    'license_profile_unavailable',
    'superseded_historical_only',
    'retired_for_new_projects',
]
"""The capability query answer (#599 §13) — what a subsystem may claim
when it cites one (standard_id, edition)."""

StandardsCitationPurpose = Literal['production', 'historical', 'research']

RevisionChangeKind = Literal[
    'requirement_added',
    'requirement_removed',
    'metric_changed',
    'threshold_changed',
    'measurement_method_changed',
    'terminology_changed',
    'scope_changed',
]
"""Structured revision-diff vocabulary (#599 §8) — lawful metadata-level
differences between two editions, never protected text."""

_DRAFT_LIFECYCLES = frozenset({
    'draft',
    'public_review',
    'dis_fdis_prepublication',
    'industry_review',
})
_DEAD_LIFECYCLES = frozenset({
    'superseded',
    'revised',
    'withdrawn',
    'replaced_by',
    'historical',
})
_CURRENT_LIFECYCLES = frozenset({
    'published_current',
    'reaffirmed',
    'under_revision',
})


# ---------------------------------------------------------------------------
# Document identity (#599 §1, §10, §11)
# ---------------------------------------------------------------------------


class StandardDependencyRef(BaseModel):
    """One versioned dependency of a standard on another
    (#599 §10 — e.g. RP22 v1.2 pins the NCB lineage of S12.2)."""

    model_config = ConfigDict(frozen=True)

    standard_id: str = Field(min_length=1)
    edition: str = Field(min_length=1)
    role: str = Field(min_length=1)
    note: str = ''


class StandardReviewPolicy(BaseModel):
    """Currentness tracking for one document (#599 §11).

    ``next_review_due_utc``/``change_watch_sources`` describe when the
    *registry* should re-check official metadata — a review policy is
    never a project-evaluation input, and a newer edition never triggers
    automatic migration.
    """

    model_config = ConfigDict(frozen=True)

    last_checked_at_utc: str | None = None
    next_review_due_utc: str | None = None
    change_watch_sources: tuple[str, ...] = ()
    manual_review_required: bool = False

    @model_validator(mode='after')
    def _check(self) -> 'StandardReviewPolicy':
        for value, label in (
            (self.last_checked_at_utc, 'last_checked_at_utc'),
            (self.next_review_due_utc, 'next_review_due_utc'),
        ):
            if value is not None:
                _require_iso8601(value, label)
        return self


class ExternalStandardDocument(BaseModel):
    """Canonical identity + lifecycle of one exact external document
    edition. Registry key is ``(standard_id, edition)`` — the document
    number alone is never an identity (#599 §1)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    standard_id: str = Field(min_length=1)
    edition: str = Field(min_length=1)
    document_number: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    title: str = Field(min_length=1)
    amendment: str | None = None
    publication_date: str | None = None
    language: str | None = None
    lifecycle: StandardLifecycleStatus
    replaced_by: str | None = None
    """Registry key (``standard_id@edition``) of the successor — only when
    publisher evidence supports a formal replacement (#599 §9)."""
    supersedes: str | None = None
    dependency_refs: tuple[StandardDependencyRef, ...] = ()
    primary_source_url: str | None = None
    primary_source_tier: StandardsSourceTier | None = None
    secondary_source_urls: tuple[str, ...] = ()
    source_checked_at_utc: str | None = None
    document_sha256: str | None = Field(default=None, pattern=_SHA256)
    rights: StandardRightsClass
    admission: StandardsAdmissionState
    review: StandardReviewPolicy | None = None
    lifecycle_observation_ids: tuple[str, ...] = ()
    notes: str = ''
    registered_at_utc: str = Field(min_length=1)
    document_sha: str = Field(pattern=_SHA256)

    @property
    def registry_key(self) -> str:
        return f'{self.standard_id}@{self.edition}'

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='python', exclude={'document_sha'})

    @model_validator(mode='after')
    def _check(self) -> 'ExternalStandardDocument':
        _require_iso8601(self.registered_at_utc, 'registered_at_utc')
        if self.source_checked_at_utc is not None:
            _require_iso8601(
                self.source_checked_at_utc, 'source_checked_at_utc'
            )
        if self.primary_source_url is not None and (
            self.primary_source_tier is None
        ):
            raise ValueError(
                'a primary source URL requires its authority tier — an '
                'unlabelled source cannot be weighed'
            )
        if self.replaced_by == self.registry_key:
            raise ValueError('a document cannot replace itself')
        if self.supersedes == self.registry_key:
            raise ValueError('a document cannot supersede itself')
        if self.lifecycle == 'superseded' and self.replaced_by is None:
            # superseded without a named successor is legal (document was
            # superseded and withdrawn together) but must be explicit.
            pass
        if self.document_sha != _hash(self.semantic_payload()):
            raise ValueError('external standard document hash mismatch')
        return self


def build_standard_document(
    *,
    standard_id: str,
    edition: str,
    document_number: str,
    publisher: str,
    title: str,
    lifecycle: StandardLifecycleStatus,
    rights: StandardRightsClass,
    admission: StandardsAdmissionState,
    amendment: str | None = None,
    publication_date: str | None = None,
    language: str | None = None,
    replaced_by: str | None = None,
    supersedes: str | None = None,
    dependency_refs: tuple[StandardDependencyRef, ...] = (),
    primary_source_url: str | None = None,
    primary_source_tier: StandardsSourceTier | None = None,
    secondary_source_urls: tuple[str, ...] = (),
    source_checked_at_utc: str | None = None,
    document_sha256: str | None = None,
    review: StandardReviewPolicy | None = None,
    lifecycle_observation_ids: tuple[str, ...] = (),
    notes: str = '',
    registered_at_utc: str | None = None,
) -> ExternalStandardDocument:
    probe = ExternalStandardDocument.model_construct(
        **canonicalize_payload(
            ExternalStandardDocument,
            dict(
                schema_version=1,
                standard_id=standard_id,
                edition=edition,
                document_number=document_number,
                publisher=publisher,
                title=title,
                amendment=amendment,
                publication_date=publication_date,
                language=language,
                lifecycle=lifecycle,
                replaced_by=replaced_by,
                supersedes=supersedes,
                dependency_refs=tuple(dependency_refs),
                primary_source_url=primary_source_url,
                primary_source_tier=primary_source_tier,
                secondary_source_urls=tuple(secondary_source_urls),
                source_checked_at_utc=source_checked_at_utc,
                document_sha256=document_sha256,
                rights=rights,
                admission=admission,
                review=review,
                lifecycle_observation_ids=tuple(lifecycle_observation_ids),
                notes=notes,
                registered_at_utc=registered_at_utc or _utc_now(),
                document_sha='0' * 64,
            ),
        )
    )
    return ExternalStandardDocument(
        **probe.model_dump(mode='python', exclude={'document_sha'}),
        document_sha=_hash(probe.semantic_payload()),
    )


# ---------------------------------------------------------------------------
# Lifecycle observations + conflicting sources (#599 §4)
# ---------------------------------------------------------------------------


class StandardLifecycleObservation(BaseModel):
    """One observed lifecycle claim about a (standard_id, edition) from
    one source. Competing official observations are kept, never merged
    silently."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    observation_id: str = Field(min_length=1)
    standard_id: str = Field(min_length=1)
    edition: str = Field(min_length=1)
    claimed_lifecycle: StandardLifecycleStatus
    claimed_revision_text: str | None = None
    source_tier: StandardsSourceTier
    source_url: str | None = None
    observed_at_utc: str = Field(min_length=1)
    note: str = ''
    observation_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'observation_sha256', 'observation_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'StandardLifecycleObservation':
        _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        digest = _hash(self.semantic_payload())
        if self.observation_sha256 != digest:
            raise ValueError('lifecycle observation hash mismatch')
        if self.observation_id != 'stdobs-' + digest[:24]:
            raise ValueError('lifecycle observation id mismatch')
        return self


def build_lifecycle_observation(
    *,
    standard_id: str,
    edition: str,
    claimed_lifecycle: StandardLifecycleStatus,
    source_tier: StandardsSourceTier,
    claimed_revision_text: str | None = None,
    source_url: str | None = None,
    observed_at_utc: str | None = None,
    note: str = '',
) -> StandardLifecycleObservation:
    payload = dict(
        schema_version=1,
        observation_id='',
        standard_id=standard_id,
        edition=edition,
        claimed_lifecycle=claimed_lifecycle,
        claimed_revision_text=claimed_revision_text,
        source_tier=source_tier,
        source_url=source_url,
        observed_at_utc=observed_at_utc or _utc_now(),
        note=note,
        observation_sha256='0' * 64,
    )
    probe = StandardLifecycleObservation.model_construct(
        **canonicalize_payload(StandardLifecycleObservation, payload)
    )
    digest = _hash(probe.semantic_payload())
    return StandardLifecycleObservation(
        **probe.model_dump(
            mode='python',
            exclude={'observation_sha256', 'observation_id'},
        ),
        observation_id='stdobs-' + digest[:24],
        observation_sha256=digest,
    )


def detect_lifecycle_conflict(
    observations: tuple[StandardLifecycleObservation, ...],
    standard_id: str,
    edition: str,
) -> tuple[StandardLifecycleObservation, ...]:
    """Return the competing top-tier observations when official sources
    disagree about one edition's lifecycle (#599 §4).

    Only the single most authoritative tier present is compared; lower
    tiers never conflict with a higher tier (they are discovery aids).
    An empty tuple means no conflict.
    """
    matching = [
        obs
        for obs in observations
        if obs.standard_id == standard_id and obs.edition == edition
    ]
    if len(matching) < 2:
        return ()
    best_rank = min(_TIER_RANK[obs.source_tier] for obs in matching)
    top = [obs for obs in matching if _TIER_RANK[obs.source_tier] == best_rank]
    claims = {obs.claimed_lifecycle for obs in top}
    if len(claims) <= 1:
        return ()
    return tuple(top)


def effective_lifecycle(
    document: ExternalStandardDocument,
    observations: tuple[StandardLifecycleObservation, ...],
) -> StandardLifecycleStatus:
    """The lifecycle a citation sees: an unresolved top-tier conflict is
    honest ``status_conflict`` even when the registry row recorded a
    single claim (#599 §4)."""
    conflict = detect_lifecycle_conflict(
        observations, document.standard_id, document.edition
    )
    if conflict:
        return 'status_conflict'
    return document.lifecycle


# ---------------------------------------------------------------------------
# Profile mapping identity (#599 §5)
# ---------------------------------------------------------------------------


class StandardProfileMapping(BaseModel):
    """External document identity and HTDT implementation mapping are
    separate identities: this record pins the HTDT-side mapping version
    for one exact (standard_id, edition)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    mapping_id: str = Field(min_length=1)
    standard_id: str = Field(min_length=1)
    edition: str = Field(min_length=1)
    mapping_version: str = Field(min_length=1)
    mapped_requirement_ids: tuple[str, ...] = ()
    input_authorities: tuple[str, ...] = ()
    calculation_version: str = Field(min_length=1)
    output_semantics: str = ''
    unsupported_requirements: tuple[str, ...] = ()
    interpretation_notes: str = ''
    validation_evidence_refs: tuple[str, ...] = ()
    created_at_utc: str = Field(min_length=1)
    mapping_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'mapping_sha256', 'mapping_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'StandardProfileMapping':
        _require_iso8601(self.created_at_utc, 'created_at_utc')
        digest = _hash(self.semantic_payload())
        if self.mapping_sha256 != digest:
            raise ValueError('profile mapping hash mismatch')
        if self.mapping_id != 'stdmap-' + digest[:24]:
            raise ValueError('profile mapping id mismatch')
        return self


def build_profile_mapping(
    *,
    standard_id: str,
    edition: str,
    mapping_version: str,
    calculation_version: str,
    mapped_requirement_ids: tuple[str, ...] = (),
    input_authorities: tuple[str, ...] = (),
    output_semantics: str = '',
    unsupported_requirements: tuple[str, ...] = (),
    interpretation_notes: str = '',
    validation_evidence_refs: tuple[str, ...] = (),
    created_at_utc: str | None = None,
) -> StandardProfileMapping:
    payload = dict(
        schema_version=1,
        mapping_id='',
        standard_id=standard_id,
        edition=edition,
        mapping_version=mapping_version,
        mapped_requirement_ids=tuple(mapped_requirement_ids),
        input_authorities=tuple(input_authorities),
        calculation_version=calculation_version,
        output_semantics=output_semantics,
        unsupported_requirements=tuple(unsupported_requirements),
        interpretation_notes=interpretation_notes,
        validation_evidence_refs=tuple(validation_evidence_refs),
        created_at_utc=created_at_utc or _utc_now(),
        mapping_sha256='0' * 64,
    )
    probe = StandardProfileMapping.model_construct(
        **canonicalize_payload(StandardProfileMapping, payload)
    )
    digest = _hash(probe.semantic_payload())
    return StandardProfileMapping(
        **probe.model_dump(
            mode='python', exclude={'mapping_sha256', 'mapping_id'}
        ),
        mapping_id='stdmap-' + digest[:24],
        mapping_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Evaluation pin — historical immutability (#599 §7)
# ---------------------------------------------------------------------------


class StandardsEvaluationPin(BaseModel):
    """One evaluation's pin of external document edition + HTDT mapping
    revision + calculation version + input evidence. A newer admitted
    edition never rewrites an old pin — a recomputation produces a new
    parallel pin."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    pin_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    standard_id: str = Field(min_length=1)
    edition: str = Field(min_length=1)
    mapping_id: str | None = None
    mapping_version: str | None = None
    calculation_version: str | None = None
    input_evidence_ids: tuple[str, ...] = ()
    result: StandardsEvaluationState
    detail: str = ''
    evaluated_at_utc: str = Field(min_length=1)
    pin_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'pin_sha256', 'pin_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'StandardsEvaluationPin':
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        digest = _hash(self.semantic_payload())
        if self.pin_sha256 != digest:
            raise ValueError('evaluation pin hash mismatch')
        if self.pin_id != 'stdevpin-' + digest[:24]:
            raise ValueError('evaluation pin id mismatch')
        return self


def build_evaluation_pin(
    *,
    document_id: str,
    standard_id: str,
    edition: str,
    result: StandardsEvaluationState,
    mapping: StandardProfileMapping | None = None,
    mapping_version: str | None = None,
    calculation_version: str | None = None,
    input_evidence_ids: tuple[str, ...] = (),
    detail: str = '',
    evaluated_at_utc: str | None = None,
) -> StandardsEvaluationPin:
    if mapping is not None:
        if mapping.standard_id != standard_id or mapping.edition != edition:
            raise ValueError(
                'evaluation pin mapping does not match the pinned '
                'standard edition'
            )
        mapping_id = mapping.mapping_id
        if mapping_version is None:
            mapping_version = mapping.mapping_version
        if calculation_version is None:
            calculation_version = mapping.calculation_version
    else:
        mapping_id = None
    payload = dict(
        schema_version=1,
        pin_id='',
        document_id=document_id,
        standard_id=standard_id,
        edition=edition,
        mapping_id=mapping_id,
        mapping_version=mapping_version,
        calculation_version=calculation_version,
        input_evidence_ids=tuple(input_evidence_ids),
        result=result,
        detail=detail,
        evaluated_at_utc=evaluated_at_utc or _utc_now(),
        pin_sha256='0' * 64,
    )
    probe = StandardsEvaluationPin.model_construct(
        **canonicalize_payload(StandardsEvaluationPin, payload)
    )
    digest = _hash(probe.semantic_payload())
    return StandardsEvaluationPin(
        **probe.model_dump(mode='python', exclude={'pin_sha256', 'pin_id'}),
        pin_id='stdevpin-' + digest[:24],
        pin_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Revision diff (#599 §8)
# ---------------------------------------------------------------------------


class RevisionDiffEntry(BaseModel):
    """One metadata-level change between two editions."""

    model_config = ConfigDict(frozen=True)

    change_kind: RevisionChangeKind
    subject: str = Field(min_length=1)
    detail: str = ''


class StandardsRevisionDiff(BaseModel):
    """Structured difference record between two registered editions —
    identifies which pinned evaluations require recomputation, never a
    copy of protected text."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    diff_id: str = Field(min_length=1)
    from_standard_id: str = Field(min_length=1)
    from_edition: str = Field(min_length=1)
    to_standard_id: str = Field(min_length=1)
    to_edition: str = Field(min_length=1)
    entries: tuple[RevisionDiffEntry, ...] = ()
    note: str = ''
    recorded_at_utc: str = Field(min_length=1)
    diff_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'diff_sha256', 'diff_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'StandardsRevisionDiff':
        _require_iso8601(self.recorded_at_utc, 'recorded_at_utc')
        if (
            self.from_standard_id == self.to_standard_id
            and self.from_edition == self.to_edition
        ):
            raise ValueError('a revision diff needs two distinct editions')
        digest = _hash(self.semantic_payload())
        if self.diff_sha256 != digest:
            raise ValueError('revision diff hash mismatch')
        if self.diff_id != 'stddiff-' + digest[:24]:
            raise ValueError('revision diff id mismatch')
        return self


def build_revision_diff(
    *,
    from_standard_id: str,
    from_edition: str,
    to_standard_id: str,
    to_edition: str,
    entries: tuple[RevisionDiffEntry, ...] = (),
    note: str = '',
    recorded_at_utc: str | None = None,
) -> StandardsRevisionDiff:
    payload = dict(
        schema_version=1,
        diff_id='',
        from_standard_id=from_standard_id,
        from_edition=from_edition,
        to_standard_id=to_standard_id,
        to_edition=to_edition,
        entries=tuple(entries),
        note=note,
        recorded_at_utc=recorded_at_utc or _utc_now(),
        diff_sha256='0' * 64,
    )
    probe = StandardsRevisionDiff.model_construct(
        **canonicalize_payload(StandardsRevisionDiff, payload)
    )
    digest = _hash(probe.semantic_payload())
    return StandardsRevisionDiff(
        **probe.model_dump(
            mode='python', exclude={'diff_sha256', 'diff_id'}
        ),
        diff_id='stddiff-' + digest[:24],
        diff_sha256=digest,
    )


def affected_evaluation_pins(
    pins: tuple[StandardsEvaluationPin, ...],
    diff: StandardsRevisionDiff,
) -> tuple[StandardsEvaluationPin, ...]:
    """Pins whose evaluations were produced under the diff's source
    edition — the recomputation/remeasurement candidate set (#599 §8)."""
    return tuple(
        pin
        for pin in pins
        if pin.standard_id == diff.from_standard_id
        and pin.edition == diff.from_edition
    )


# ---------------------------------------------------------------------------
# Capability query + citation gate (#599 §13, §15)
# ---------------------------------------------------------------------------


def standards_profile_capability(
    document: ExternalStandardDocument | None,
    mapping: StandardProfileMapping | None,
    observations: tuple[StandardLifecycleObservation, ...] = (),
) -> StandardsCapabilityVerdict:
    """What a subsystem may claim for one (standard_id, edition) —
    the shared capability query, fail-closed on every ambiguity."""
    if document is None:
        return 'not_registered'
    lifecycle = effective_lifecycle(document, observations)
    if lifecycle in _DRAFT_LIFECYCLES:
        return 'draft_research_only'
    if lifecycle == 'status_conflict':
        return 'source_ambiguous'
    if lifecycle in _DEAD_LIFECYCLES:
        return 'superseded_historical_only'
    if lifecycle == 'unknown':
        return 'limited'
    if document.admission == 'retired_for_new_projects':
        return 'retired_for_new_projects'
    if document.admission == 'limited':
        return 'limited'
    if document.admission not in ('validated', 'production_eligible'):
        return 'mapping_unvalidated'
    if mapping is None:
        return 'mapping_unvalidated'
    if mapping.standard_id != document.standard_id or (
        mapping.edition != document.edition
    ):
        return 'mapping_unvalidated'
    if document.rights in ('reference_only', 'unknown_rights'):
        return 'limited'
    return 'production_eligible'


def standard_citation_allowed(
    document: ExternalStandardDocument | None,
    mapping: StandardProfileMapping | None,
    purpose: StandardsCitationPurpose,
    observations: tuple[StandardLifecycleObservation, ...] = (),
) -> tuple[bool, str]:
    """Fail-closed citation gate (#599 §15): returns ``(allowed, reason)``.

    ``production`` = a claim a shipped evaluation/report may make;
    ``historical`` = reproducing an old pinned evaluation;
    ``research`` = experimentation, never conformance wording.
    """
    capability = standards_profile_capability(
        document, mapping, observations
    )
    if capability == 'not_registered':
        return (
            False,
            'standard_citation_blocked_unregistered: the cited '
            '(standard_id, edition) is not in the registry — the citation '
            'is UNKNOWN, never an implicit current edition',
        )
    if purpose == 'production':
        if capability == 'production_eligible':
            return (
                True,
                'production citation allowed: exact edition is registered, '
                'current and validated',
            )
        reasons = {
            'draft_research_only': 'draft/review editions are research-'
            'only — never a production conformance basis',
            'source_ambiguous': 'official sources conflict on this '
            'edition — the claim stays ambiguous until resolved',
            'mapping_unvalidated': 'the exact edition is admitted but no '
            'validated HTDT mapping exists — inability to evaluate is '
            'not a physical FAIL',
            'superseded_historical_only': 'this edition is '
            'superseded/withdrawn — valid historical provenance, not a '
            'current-production citation',
            'license_profile_unavailable': 'the licensed profile content '
            'is unavailable — evaluation cannot proceed',
            'retired_for_new_projects': 'admission is retired for new '
            'projects — historical reproduction remains allowed',
            'limited': 'the document is admitted as LIMITED — check the '
            'registered limitations before citing',
        }
        return (False, f'standard_citation_blocked_{capability}: '
                + reasons[capability])
    if purpose == 'historical':
        return (
            True,
            f'historical citation allowed under capability '
            f'{capability!r}: pinned evaluations reproduce the registered '
            'edition regardless of its current lifecycle',
        )
    return (
        True,
        f'research citation allowed under capability {capability!r} '
        '(not for production conformance wording)',
    )


# ---------------------------------------------------------------------------
# Seed registry — the exact standards HTDT already cites (#599 §goal)
# ---------------------------------------------------------------------------


def seed_standard_documents(
    *, registered_at_utc: str | None = None
) -> tuple[ExternalStandardDocument, ...]:
    """The built-in registry seed: every external document REV55/56 code
    already cites, pinned to its verified edition and lifecycle as of
    2026-10-05 (sources: ISO/IEC/AVIXA official catalogs, ANSI store,
    ASA/CEDIA publications — see docs/reviews/rev56-snapstd.md)."""
    specs: tuple[dict[str, Any], ...] = (
        dict(
            standard_id='iso-3382-1',
            edition='2009',
            document_number='ISO 3382-1:2009',
            publisher='ISO',
            title='Acoustics — Measurement of room acoustic parameters — '
                  'Part 1: Performance spaces',
            lifecycle='under_revision',
            rights='public_metadata_only',
            admission='validated',
            publication_date='2009-06',
            primary_source_url='https://www.iso.org/standard/40979.html',
            primary_source_tier='standards_body_catalog',
            source_checked_at_utc='2026-10-05T00:00:00+00:00',
            notes='Edition 1, confirmed 2021-03-11 (systematic review); '
                  'to-be-revised stage 90.92 since 2025-07-17 — the '
                  'published edition remains the current evaluation basis '
                  'while edition 2 is a committee draft.',
        ),
        dict(
            standard_id='iso-3382-1',
            edition='ed2-draft',
            document_number='ISO/DIS 3382-1',
            publisher='ISO',
            title='Acoustics — Measurement of room acoustic parameters — '
                  'Part 1: Spaces for music, speech and communication '
                  '(edition 2 draft)',
            lifecycle='dis_fdis_prepublication',
            rights='public_metadata_only',
            admission='discovered',
            primary_source_url='https://www.iso.org/standard/85701.html',
            primary_source_tier='standards_body_catalog',
            source_checked_at_utc='2026-10-05T00:00:00+00:00',
            notes='Edition 2 reached DIS registration (stage 40.00, '
                  '2026-09); draft — research use only, never a '
                  'conformance citation.',
        ),
        dict(
            standard_id='iso-3382-2',
            edition='2008',
            document_number='ISO 3382-2:2008',
            publisher='ISO',
            title='Acoustics — Measurement of room acoustic parameters — '
                  'Part 2: Reverberation time in ordinary rooms',
            lifecycle='reaffirmed',
            rights='public_metadata_only',
            admission='validated',
            publication_date='2008-06',
            primary_source_url='https://www.iso.org/standard/36201.html',
            primary_source_tier='standards_body_catalog',
            source_checked_at_utc='2026-10-05T00:00:00+00:00',
            notes='Edition 1, confirmed 2022-03-16 (systematic review).',
        ),
        dict(
            standard_id='iso-354',
            edition='2003',
            document_number='ISO 354:2003',
            publisher='ISO',
            title='Acoustics — Measurement of sound absorption in a '
                  'reverberation room',
            lifecycle='published_current',
            rights='public_metadata_only',
            admission='validated',
            publication_date='2003-05',
            notes='Random-incidence absorption reference; code comments '
                  'note the weakly-damped-resonator scope exclusion.',
        ),
        dict(
            standard_id='iso-10534-2',
            edition='1998',
            document_number='ISO 10534-2:1998',
            publisher='ISO',
            title='Acoustics — Determination of sound absorption '
                  'coefficient and impedance in impedance tubes — Part 2: '
                  'Transfer-function method',
            lifecycle='withdrawn',
            replaced_by='iso-10534-2@2023',
            rights='public_metadata_only',
            admission='retired_for_new_projects',
            primary_source_url='https://www.iso.org/standard/22851.html',
            primary_source_tier='standards_body_catalog',
            source_checked_at_utc='2026-10-05T00:00:00+00:00',
            notes='Withdrawn 2023-10-06 on publication of the 2023 '
                  'edition; remains valid historical provenance for old '
                  'impedance-tube evidence.',
        ),
        dict(
            standard_id='iso-10534-2',
            edition='2023',
            document_number='ISO 10534-2:2023',
            publisher='ISO',
            title='Acoustics — Determination of acoustic properties in '
                  'impedance tubes — Part 2: Two-microphone technique for '
                  'normal sound absorption coefficient and normal surface '
                  'impedance',
            lifecycle='published_current',
            supersedes='iso-10534-2@1998',
            rights='public_metadata_only',
            admission='validated',
            publication_date='2023-10',
            primary_source_url='https://www.iso.org/standard/81294.html',
            primary_source_tier='standards_body_catalog',
            source_checked_at_utc='2026-10-05T00:00:00+00:00',
            notes='Annex E locally-reacting diffuse-field estimate — the '
                  'explicit conversion path the material-evidence gate '
                  'exposes.',
        ),
        dict(
            standard_id='iso-17497-1',
            edition='2004',
            document_number='ISO 17497-1:2004',
            publisher='ISO',
            title='Acoustics — Sound-scattering properties of surfaces — '
                  'Part 1: Measurement of the random-incidence scattering '
                  'coefficient in a reverberation room',
            lifecycle='published_current',
            rights='public_metadata_only',
            admission='validated',
        ),
        dict(
            standard_id='iso-17497-2',
            edition='2012',
            document_number='ISO 17497-2:2012',
            publisher='ISO',
            title='Acoustics — Sound-scattering properties of surfaces — '
                  'Part 2: Measurement of the directional diffusion '
                  'coefficient in a free field',
            lifecycle='published_current',
            rights='public_metadata_only',
            admission='validated',
            notes='The standard itself states the diffusion coefficient '
                  'is not suitable as a direct input to current geometric '
                  'room-acoustic algorithms — surfaced as INCOMPATIBLE by '
                  'the material-evidence gate.',
        ),
        dict(
            standard_id='iso-11654',
            edition='1997',
            document_number='ISO 11654:1997',
            publisher='ISO',
            title='Acoustics — Sound absorbers for use in buildings — '
                  'Rating of sound absorption',
            lifecycle='published_current',
            rights='public_metadata_only',
            admission='validated',
        ),
        dict(
            standard_id='iec-60268-5',
            edition='2003',
            document_number='IEC 60268-5:2003+AMD1:2007',
            publisher='IEC',
            title='Sound system equipment — Part 5: Loudspeakers',
            lifecycle='withdrawn',
            replaced_by='iec-60268-21@2018',
            rights='public_metadata_only',
            admission='retired_for_new_projects',
            amendment='AMD1:2007',
            primary_source_url='https://webstore.iec.ch/en/publication/1224',
            primary_source_tier='official_standards_store',
            source_checked_at_utc='2026-10-05T00:00:00+00:00',
            notes='Withdrawn 2026-04-17 (IEC webstore). Output-based '
                  'loudspeaker measurement moved to IEC 60268-21 / -22; '
                  'historical measurements remain attributable to this '
                  'edition.',
        ),
        dict(
            standard_id='iec-60268-16',
            edition='2011',
            document_number='IEC 60268-16:2011',
            publisher='IEC',
            title='Sound system equipment — Part 16: Objective rating of '
                  'speech intelligibility by speech transmission index',
            lifecycle='superseded',
            replaced_by='iec-60268-16@2020',
            rights='public_metadata_only',
            admission='retired_for_new_projects',
            notes='2011 speech spectrum differs from 2020 — the stimulus '
                  'registry returns WRONG_REVISION for a 2011 asset '
                  'against a 2020 requirement.',
        ),
        dict(
            standard_id='iec-60268-16',
            edition='2020',
            document_number='IEC 60268-16:2020+COR1:2025',
            publisher='IEC',
            title='Sound system equipment — Part 16: Objective rating of '
                  'speech intelligibility by speech transmission index',
            lifecycle='published_current',
            supersedes='iec-60268-16@2011',
            rights='public_metadata_only',
            admission='validated',
            amendment='COR1:2025',
            publication_date='2020-10',
            notes='Edition 5 with corrigendum COR1:2025; male speech '
                  'spectrum changed vs 2011 (125/250 Hz bands reduced).',
        ),
        dict(
            standard_id='iec-60268-21',
            edition='2018',
            document_number='IEC 60268-21:2018',
            publisher='IEC',
            title='Sound system equipment — Part 21: Acoustical '
                  '(output-based) measurements',
            lifecycle='published_current',
            rights='public_metadata_only',
            admission='validated',
            publication_date='2018-11-07',
            primary_source_url='https://webstore.iec.ch/en/publication/28687',
            primary_source_tier='official_standards_store',
            source_checked_at_utc='2026-10-05T00:00:00+00:00',
        ),
        dict(
            standard_id='iec-60268-22',
            edition='2020',
            document_number='IEC 60268-22:2020',
            publisher='IEC',
            title='Sound system equipment — Part 22: Electrical and '
                  'mechanical measurements of transducers',
            lifecycle='published_current',
            rights='public_metadata_only',
            admission='primary_source_confirmed',
            notes='Current companion to IEC 60268-21 for '
                  'electrical/mechanical transducer measurements.',
        ),
        dict(
            standard_id='iec-61672-1',
            edition='2013',
            document_number='IEC 61672-1:2013',
            publisher='IEC',
            title='Electroacoustics — Sound level meters — Part 1: '
                  'Specifications',
            lifecycle='published_current',
            rights='public_metadata_only',
            admission='primary_source_confirmed',
            notes='SLM class specification underpinning calibrated '
                  'level claims.',
        ),
        dict(
            standard_id='ansi-asa-s12-2',
            edition='2019',
            document_number='ANSI/ASA S12.2-2019',
            publisher='ANSI/ASA',
            title='Criteria for Evaluating Room Noise',
            lifecycle='superseded',
            replaced_by='ansi-asa-s12-2@2026',
            rights='public_metadata_only',
            admission='validated',
            notes='Still the edition RP22 v1.2 pins for its '
                  'background-noise (NCB-lineage) metric — superseded for '
                  'new direct citations, valid inside that pinned '
                  'dependency.',
        ),
        dict(
            standard_id='ansi-asa-s12-2',
            edition='2026',
            document_number='ANSI/ASA S12.2-2026',
            publisher='ANSI/ASA',
            title='Criteria for Evaluating Room Noise',
            lifecycle='published_current',
            supersedes='ansi-asa-s12-2@2019',
            rights='public_metadata_only',
            admission='primary_source_confirmed',
            publication_date='2026-03-23',
            primary_source_url='https://webstore.ansi.org/standards/asa/'
                               'asaansis12s3roomnoise',
            primary_source_tier='official_standards_store',
            source_checked_at_utc='2026-10-05T00:00:00+00:00',
            notes='2026 edition reverts NC curves to historic values, '
                  'states all curve values explicitly, adds 1/3-octave NC '
                  'annexes and spectral-imbalance penalties; NCB moved '
                  'out of the main lineage.',
        ),
        dict(
            standard_id='avixa-v202-01',
            edition='2016',
            document_number='ANSI/INFOCOMM V202.01:2016',
            publisher='AVIXA',
            title='Display Image Size for 2D Content in Audiovisual '
                  'Systems',
            lifecycle='superseded',
            replaced_by='avixa-v202-01@2026',
            rights='public_metadata_only',
            admission='validated',
            notes='AVIXA resource pages still present the 2016 document '
                  'number — see the conflicting lifecycle observations.',
        ),
        dict(
            standard_id='avixa-v202-01',
            edition='2026',
            document_number='ANSI/AVIXA V202.01:2026',
            publisher='AVIXA',
            title='Display Image Size for 2D Content in Audiovisual '
                  'Systems',
            lifecycle='published_current',
            supersedes='avixa-v202-01@2016',
            rights='public_metadata_only',
            admission='primary_source_confirmed',
            publication_date='2026-04',
            primary_source_url='https://store.avixa.org/CPBase__item?'
                               'id=a13f200000C2iQeAAJ',
            primary_source_tier='official_standards_store',
            source_checked_at_utc='2026-10-05T00:00:00+00:00',
        ),
        dict(
            standard_id='cedia-cta-rp22',
            edition='v1.2',
            document_number='CEDIA/CTA-RP22',
            publisher='CEDIA / Consumer Technology Association (CTA)',
            title='CEDIA/CTA-RP22 Recommended Practice for Immersive '
                  'Audio Design',
            lifecycle='published_current',
            rights='public_open_standard',
            admission='validated',
            publication_date='2023-09',
            dependency_refs=(
                StandardDependencyRef(
                    standard_id='ansi-asa-s12-2',
                    edition='2019',
                    role='background_noise_metric',
                    note='RP22 v1.2 pins the NCB lineage; the 2026 S12.2 '
                         'NC revision does not silently migrate the '
                         'pinned dependency.',
                ),
            ),
            primary_source_url='https://cedia.org/site/assets/files/6057/'
                               'cedia-cta_rp22_v1_2_sept_2023.pdf',
            primary_source_tier='official_publisher_announcement',
            source_checked_at_utc='2026-10-05T00:00:00+00:00',
        ),
        dict(
            standard_id='dolby-atmos-home-guide',
            edition='r3.1',
            document_number='Dolby Atmos Home Theater Installation '
                            'Guidelines',
            publisher='Dolby Laboratories',
            title='Dolby Atmos Home Theater Installation Guidelines',
            lifecycle='published_current',
            rights='public_open_standard',
            admission='validated',
            publication_date='2018-12-13',
            primary_source_url='https://www.dolby.com/siteassets/'
                               'technologies/dolby-atmos/'
                               'atmos-installation-guidelines-121318_r3.1.pdf',
            primary_source_tier='official_publisher_announcement',
            source_checked_at_utc='2026-10-05T00:00:00+00:00',
        ),
        dict(
            standard_id='auro3d-home-guide',
            edition='v12',
            document_number='AURO-3D Home Theater Setup Guidelines',
            publisher='NEWAURO BV',
            title='AURO-3D Home Theater Setup — Installation Guidelines',
            lifecycle='published_current',
            rights='public_open_standard',
            admission='validated',
            publication_date='2024-05-16',
            primary_source_url='https://www.auro-3d.com/wp-content/'
                               'uploads/2024/05/Auro-3D-Home-Theater-'
                               'Setup-Guidelines-v12-20240516.pdf',
            primary_source_tier='official_publisher_announcement',
            source_checked_at_utc='2026-10-05T00:00:00+00:00',
        ),
        dict(
            standard_id='aes75',
            edition='2023',
            document_number='AES75-2023',
            publisher='AES',
            title='AES standard for acoustics — Measuring loudspeaker '
                  'maximum linear sound levels using noise (Music-Noise)',
            lifecycle='published_current',
            rights='public_metadata_only',
            admission='validated',
            notes='Official 48/96 kHz Music-Noise assets carry publisher '
                  'checksums; renamed M-Noise → Music-Noise in 2023.',
        ),
        dict(
            standard_id='aes17',
            edition='2020',
            document_number='AES17-2020',
            publisher='AES',
            title='AES standard method for digital audio engineering — '
                  'Measurement of digital audio equipment',
            lifecycle='published_current',
            rights='public_metadata_only',
            admission='primary_source_confirmed',
        ),
        dict(
            standard_id='jcgm-100',
            edition='2008',
            document_number='JCGM 100:2008',
            publisher='JCGM/BIPM',
            title='Evaluation of measurement data — Guide to the '
                  'expression of uncertainty in measurement (GUM)',
            lifecycle='published_current',
            rights='public_open_standard',
            admission='validated',
        ),
        dict(
            standard_id='jcgm-101',
            edition='2008',
            document_number='JCGM 101:2008',
            publisher='JCGM/BIPM',
            title='Evaluation of measurement data — Supplement 1 to the '
                  'GUM — Propagation of distributions using a Monte Carlo '
                  'method',
            lifecycle='published_current',
            rights='public_open_standard',
            admission='validated',
        ),
        dict(
            standard_id='jcgm-106',
            edition='2012',
            document_number='JCGM 106:2012',
            publisher='JCGM/BIPM',
            title='Evaluation of measurement data — The role of '
                  'measurement uncertainty in conformity assessment',
            lifecycle='published_current',
            rights='public_open_standard',
            admission='validated',
        ),
        dict(
            standard_id='ilac-g8',
            edition='09/2019',
            document_number='ILAC-G8:09/2019',
            publisher='ILAC',
            title='Guidelines on Decision Rules and Statements of '
                  'Conformity',
            lifecycle='published_current',
            rights='public_open_standard',
            admission='validated',
            notes='Guard-band / shared-risk decision rules consumed by '
                  'the decision-rule authority.',
        ),
        dict(
            standard_id='ilac-g17',
            edition='01/2021',
            document_number='ILAC-G17:01/2021',
            publisher='ILAC',
            title='ILAC Guidelines for Measurement Uncertainty in '
                  'Testing',
            lifecycle='published_current',
            rights='public_open_standard',
            admission='primary_source_confirmed',
        ),
        dict(
            standard_id='iso-iec-17025',
            edition='2017',
            document_number='ISO/IEC 17025:2017',
            publisher='ISO/IEC',
            title='General requirements for the competence of testing '
                  'and calibration laboratories',
            lifecycle='published_current',
            rights='public_metadata_only',
            admission='primary_source_confirmed',
        ),
    )
    return tuple(
        build_standard_document(
            registered_at_utc=registered_at_utc, **spec
        )
        for spec in specs
    )


def seed_lifecycle_observations(
    *, observed_at_utc: str | None = None
) -> tuple[StandardLifecycleObservation, ...]:
    """Seed observations including the real official-source disagreement
    found during investigation (#599 §4): AVIXA's resource page still
    labels V202.01 as document number 2016 while the official store sells
    the published 2026 edition."""
    return (
        build_lifecycle_observation(
            standard_id='avixa-v202-01',
            edition='2016',
            claimed_lifecycle='published_current',
            source_tier='official_committee_or_review_page',
            source_url='https://www.avixa.org/resources/standards/'
                       'display-image-size-for-2d-content',
            observed_at_utc=observed_at_utc,
            note='AVIXA resource page still shows "Document Number '
                 '202.01:2016 — May 2016" as the current revision.',
        ),
        build_lifecycle_observation(
            standard_id='avixa-v202-01',
            edition='2016',
            claimed_lifecycle='superseded',
            source_tier='official_standards_store',
            source_url='https://store.avixa.org/CPBase__item?'
                       'id=a13f200000C2iQeAAJ',
            observed_at_utc=observed_at_utc,
            note='AVIXA store sells "ANSI/AVIXA V202.01:2026 (formerly '
                 'ANSI/InfoComm V202.01:2016)" — status Published, '
                 'April 2026.',
        ),
        build_lifecycle_observation(
            standard_id='iec-60268-5',
            edition='2003',
            claimed_lifecycle='withdrawn',
            source_tier='official_standards_store',
            source_url='https://webstore.iec.ch/en/publication/1224',
            observed_at_utc=observed_at_utc,
            note='IEC webstore withdrawal date 2026-04-17.',
        ),
    )


__all__ = [
    'ExternalStandardDocument',
    'RevisionChangeKind',
    'RevisionDiffEntry',
    'StandardDependencyRef',
    'StandardLifecycleObservation',
    'StandardLifecycleStatus',
    'StandardProfileMapping',
    'StandardReviewPolicy',
    'StandardRightsClass',
    'StandardsAdmissionState',
    'StandardsCapabilityVerdict',
    'StandardsCitationPurpose',
    'StandardsEvaluationPin',
    'StandardsEvaluationState',
    'StandardsRevisionDiff',
    'StandardsSourceTier',
    'affected_evaluation_pins',
    'build_evaluation_pin',
    'build_lifecycle_observation',
    'build_profile_mapping',
    'build_revision_diff',
    'build_standard_document',
    'detect_lifecycle_conflict',
    'effective_lifecycle',
    'seed_lifecycle_observations',
    'seed_standard_documents',
    'standard_citation_allowed',
    'standards_profile_capability',
]
