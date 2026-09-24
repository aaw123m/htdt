"""Saved analysis studies (#594).

Measurements, predictions, comparisons and validation results are already
persisted immutable evidence, but the *user's analysis context* — which traces
were compared, with what smoothing/range/target, and why candidate B was
rejected — was transient UI state. :class:`AnalysisStudy` makes that context
a first-class, versioned project artifact without mutating source evidence.

Contract properties:

- a study *references* exact immutable authorities (``StudyAuthorityRef``)
  and never copies source numerics into an alternate truth;
- presentation state (trace selection, range, display processing) lives in
  ``presentation_spec`` and never affects evidence hashes;
- every operation that materially changes displayed numeric data is an
  explicit versioned :class:`StudyAnalysisOperation` — no opaque screenshot
  stands in for processing provenance;
- notes are typed human engineering records (observation/decision/
  follow-up/rejected alternative/assumption) — writing a note cannot alter
  measurement or prediction authority;
- staleness is explicit: :func:`evaluate_study_state` reports whether the
  study is still reproducible, merely not-current, or has broken references;
  refreshing a study means duplicating it onto newer evidence — the original
  is never silently rewritten;
- studies are append-only; `supersedes`/`duplicated_from` chain iterations.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


STUDY_SCHEMA_VERSION = 1
STUDY_AUTHORITY_VERSION = 'analysis-study-1'

#: Authority kinds a study may bind. 'other' keeps the vocabulary open while
#: the named kinds cover every authority the issue calls out.
StudyAuthorityKind = Literal[
    'measurement_dataset',
    'prediction_result',
    'comparison_set',
    'target_curve',
    'standards_profile',
    'standards_evaluation',
    'system_variant',
    'scene_revision',
    'candidate',
    'validation_artifact',
    'robustness_result',
    'other',
]

StudyKind = Literal[
    'measurement_comparison',
    'predicted_vs_measured',
    'multi_seat',
    'optimization_review',
    'robustness_review',
    'validation_review',
    'design_comparison',
    'general',
]

#: Operations that change displayed numeric data must be explicit+versioned.
StudyOperationKind = Literal[
    'smoothing',
    'normalization',
    'aggregation',
    'envelope',
    'delta',
    'target_comparison',
    'other',
]

StudyNoteKind = Literal[
    'observation',
    'decision',
    'follow_up',
    'rejected_alternative',
    'assumption',
]

StudyRefFreshness = Literal['current', 'stale', 'missing']

#: Study-level staleness rollup.
#:
#: - ``reproducible`` — every bound authority still resolves to its pin;
#: - ``not_current`` — references resolve but the project moved on (bound
#:   scene/variant is no longer current, or a ref hash drifted);
#: - ``broken_reference`` — a required authority no longer resolves.
#: Refreshing means duplicating the study onto newer evidence, never
#: rewriting the original.
StudyState = Literal['reproducible', 'not_current', 'broken_reference']


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: Any) -> str:
    return sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


class StudySpecItem(BaseModel):
    """One key/value entry of presentation spec or operation parameters."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(min_length=1)
    value: str = Field(min_length=1)


class StudyAuthorityRef(BaseModel):
    """Exact reference to one bound source authority."""

    model_config = ConfigDict(frozen=True)

    kind: StudyAuthorityKind
    ref_id: str = Field(min_length=1)
    ref_sha256: str | None = Field(default=None, min_length=8)
    label: str | None = Field(default=None, min_length=1)


class StudyAnalysisOperation(BaseModel):
    """A versioned numeric/display operation applied inside the study."""

    model_config = ConfigDict(frozen=True)

    operation: StudyOperationKind
    version: str = Field(min_length=1)
    parameters: tuple[StudySpecItem, ...] = ()
    description: str | None = None

    @model_validator(mode='after')
    def valid_operation(self) -> 'StudyAnalysisOperation':
        keys = [item.key for item in self.parameters]
        if len(keys) != len(set(keys)):
            raise ValueError('analysis operation parameters must be unique')
        return self


class StudyNote(BaseModel):
    """A human engineering record — never a measured fact."""

    model_config = ConfigDict(frozen=True)

    note_id: str = Field(min_length=1)
    kind: StudyNoteKind
    text: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)


class AnalysisStudy(BaseModel):
    """Immutable saved analysis view + rationale over exact evidence."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = STUDY_SCHEMA_VERSION
    authority_version: Literal['analysis-study-1'] = STUDY_AUTHORITY_VERSION
    study_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    study_kind: StudyKind = 'general'
    bound_refs: tuple[StudyAuthorityRef, ...] = ()
    presentation_spec: tuple[StudySpecItem, ...] = ()
    analysis_operations: tuple[StudyAnalysisOperation, ...] = ()
    notes: tuple[StudyNote, ...] = ()
    tags: tuple[str, ...] = ()
    scene_revision_id: str | None = Field(default=None, min_length=1)
    system_variant_id: str | None = Field(default=None, min_length=1)
    supersedes_study_id: str | None = Field(default=None, min_length=1)
    duplicated_from_study_id: str | None = Field(default=None, min_length=1)
    created_at_utc: str = Field(min_length=1)
    study_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_study(self) -> 'AnalysisStudy':
        ref_keys = [(ref.kind, ref.ref_id) for ref in self.bound_refs]
        if len(ref_keys) != len(set(ref_keys)):
            raise ValueError('study bound refs must be unique per kind/ref')
        spec_keys = [item.key for item in self.presentation_spec]
        if len(spec_keys) != len(set(spec_keys)):
            raise ValueError('study presentation spec keys must be unique')
        note_ids = [note.note_id for note in self.notes]
        if len(note_ids) != len(set(note_ids)):
            raise ValueError('study note ids must be unique')
        if len(self.tags) != len(set(self.tags)):
            raise ValueError('study tags must be unique')
        if self.study_sha256 != _hash(self.semantic_payload()):
            raise ValueError('AnalysisStudy hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'study_id': self.study_id,
            'document_id': self.document_id,
            'title': self.title,
            'study_kind': self.study_kind,
            'bound_refs': [ref.model_dump(mode='json') for ref in self.bound_refs],
            'presentation_spec': [
                item.model_dump(mode='json') for item in self.presentation_spec
            ],
            'analysis_operations': [
                op.model_dump(mode='json') for op in self.analysis_operations
            ],
            'notes': [note.model_dump(mode='json') for note in self.notes],
            'tags': list(self.tags),
            'scene_revision_id': self.scene_revision_id,
            'system_variant_id': self.system_variant_id,
            'supersedes_study_id': self.supersedes_study_id,
            'duplicated_from_study_id': self.duplicated_from_study_id,
            'created_at_utc': self.created_at_utc,
        }

    def bound_ref(self, kind: str, ref_id: str) -> StudyAuthorityRef | None:
        for ref in self.bound_refs:
            if ref.kind == kind and ref.ref_id == ref_id:
                return ref
        return None


class StudyRefStatus(BaseModel):
    """Freshness of one bound authority reference."""

    model_config = ConfigDict(frozen=True)

    kind: str
    ref_id: str
    state: StudyRefFreshness
    reason: str


class StudyStateReport(BaseModel):
    """Per-ref freshness plus the study-level reproducibility state."""

    model_config = ConfigDict(frozen=True)

    study_id: str
    state: StudyState
    refs: tuple[StudyRefStatus, ...]
    reasons: tuple[str, ...]


def build_analysis_study(
    *,
    document_id: str,
    title: str,
    study_kind: StudyKind = 'general',
    bound_refs: tuple[StudyAuthorityRef, ...] = (),
    presentation_spec: tuple[StudySpecItem, ...] = (),
    analysis_operations: tuple[StudyAnalysisOperation, ...] = (),
    notes: tuple[StudyNote, ...] = (),
    tags: tuple[str, ...] = (),
    scene_revision_id: str | None = None,
    system_variant_id: str | None = None,
    supersedes_study_id: str | None = None,
    duplicated_from_study_id: str | None = None,
    created_at_utc: str,
    study_id: str | None = None,
) -> AnalysisStudy:
    payload: dict[str, Any] = {
        'study_id': study_id or str(uuid4()),
        'document_id': document_id,
        'title': title,
        'study_kind': study_kind,
        'bound_refs': tuple(bound_refs),
        'presentation_spec': tuple(presentation_spec),
        'analysis_operations': tuple(analysis_operations),
        'notes': tuple(notes),
        'tags': tuple(tags),
        'scene_revision_id': scene_revision_id,
        'system_variant_id': system_variant_id,
        'supersedes_study_id': supersedes_study_id,
        'duplicated_from_study_id': duplicated_from_study_id,
        'created_at_utc': created_at_utc,
    }
    provisional = AnalysisStudy.model_construct(**payload, study_sha256='0' * 64)
    return AnalysisStudy(
        **payload,
        study_sha256=_hash(provisional.semantic_payload()),
    )


def make_study_note(
    *,
    kind: StudyNoteKind,
    text: str,
    created_at_utc: str,
    note_id: str | None = None,
) -> StudyNote:
    return StudyNote(
        note_id=note_id or str(uuid4()),
        kind=kind,
        text=text,
        created_at_utc=created_at_utc,
    )


def duplicate_analysis_study(
    study: AnalysisStudy,
    *,
    created_at_utc: str,
    title: str | None = None,
    bound_refs: tuple[StudyAuthorityRef, ...] | None = None,
    presentation_spec: tuple[StudySpecItem, ...] | None = None,
    analysis_operations: tuple[StudyAnalysisOperation, ...] | None = None,
    notes: tuple[StudyNote, ...] | None = None,
    tags: tuple[str, ...] | None = None,
    scene_revision_id: str | None = None,
    system_variant_id: str | None = None,
    study_id: str | None = None,
) -> AnalysisStudy:
    """Clone a study onto (optionally newer) evidence as a new artifact.

    This is the only "update" path: the original study stays byte-identical,
    and the clone records ``duplicated_from_study_id`` so the decision
    iteration remains auditable.
    """

    return build_analysis_study(
        document_id=study.document_id,
        title=study.title if title is None else title,
        study_kind=study.study_kind,
        bound_refs=study.bound_refs if bound_refs is None else bound_refs,
        presentation_spec=(
            study.presentation_spec
            if presentation_spec is None
            else presentation_spec
        ),
        analysis_operations=(
            study.analysis_operations
            if analysis_operations is None
            else analysis_operations
        ),
        notes=study.notes if notes is None else notes,
        tags=study.tags if tags is None else tags,
        scene_revision_id=(
            study.scene_revision_id
            if scene_revision_id is None
            else scene_revision_id
        ),
        system_variant_id=(
            study.system_variant_id
            if system_variant_id is None
            else system_variant_id
        ),
        duplicated_from_study_id=study.study_id,
        created_at_utc=created_at_utc,
        study_id=study_id,
    )


def evaluate_study_state(
    study: AnalysisStudy,
    *,
    resolve_sha256: 'Mapping[tuple[str, str], str | None]',
    current_scene_revision_id: str | None = None,
    current_system_variant_id: str | None = None,
) -> StudyStateReport:
    """Evaluate whether a study is still reproducible.

    ``resolve_sha256`` maps ``(kind, ref_id)`` to the referenced authority's
    current semantic hash, or ``None`` when the id no longer resolves. The
    study itself is never rewritten by this evaluation; pinning without a
    recorded hash counts as reproducible when the id resolves.
    """

    refs: list[StudyRefStatus] = []
    broken = False
    stale = False
    reasons: list[str] = []
    for ref in study.bound_refs:
        current = resolve_sha256.get((ref.kind, ref.ref_id))
        if current is None:
            broken = True
            refs.append(
                StudyRefStatus(
                    kind=ref.kind,
                    ref_id=ref.ref_id,
                    state='missing',
                    reason='referenced authority no longer resolves',
                )
            )
        elif ref.ref_sha256 is not None and ref.ref_sha256 != current:
            stale = True
            refs.append(
                StudyRefStatus(
                    kind=ref.kind,
                    ref_id=ref.ref_id,
                    state='stale',
                    reason='referenced authority changed since the study was saved',
                )
            )
        else:
            refs.append(
                StudyRefStatus(
                    kind=ref.kind,
                    ref_id=ref.ref_id,
                    state='current',
                    reason='reference unchanged',
                )
            )
    if (
        study.scene_revision_id is not None
        and current_scene_revision_id is not None
        and study.scene_revision_id != current_scene_revision_id
    ):
        stale = True
        reasons.append('study is bound to a historical SceneRevision')
    if (
        study.system_variant_id is not None
        and current_system_variant_id is not None
        and study.system_variant_id != current_system_variant_id
    ):
        stale = True
        reasons.append('study is bound to a non-current SystemVariant')
    if broken:
        state: StudyState = 'broken_reference'
    elif stale:
        state = 'not_current'
    else:
        state = 'reproducible'
    return StudyStateReport(
        study_id=study.study_id,
        state=state,
        refs=tuple(refs),
        reasons=tuple(reasons),
    )


__all__ = [
    'STUDY_AUTHORITY_VERSION',
    'STUDY_SCHEMA_VERSION',
    'AnalysisStudy',
    'StudyAnalysisOperation',
    'StudyAuthorityKind',
    'StudyAuthorityRef',
    'StudyKind',
    'StudyNote',
    'StudyNoteKind',
    'StudyOperationKind',
    'StudyRefFreshness',
    'StudyRefStatus',
    'StudySpecItem',
    'StudyState',
    'StudyStateReport',
    'build_analysis_study',
    'duplicate_analysis_study',
    'evaluate_study_state',
    'make_study_note',
]
