"""O60 applicability evidence authority.

A persisted :class:`CadApplicabilityCheck` is authoritative only when a
registered evaluator reproduces the exact check — code, canonical evaluated
subject, source authority refs and decision — from resolved repository
evidence. Automated evaluators re-derive the decision from the exact
SceneRevision/SearchSpec/Room Simulator batch/Measurement Plan authorities;
the manual evaluator re-resolves a persisted immutable attestation. Unknown
evaluator ids, unresolvable refs, foreign authority or tampered payloads fail
closed with ``ValueError``.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Literal, Mapping, NamedTuple, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_predictions import exact_rectangular_room_frame
from .cad_roomsim_results import roomsim_attempt_frequency_response
from .cad_validation_metrics import (
    CadApplicabilityCheck,
    CadApplicabilityEvidenceRef,
    build_applicability_check,
    _canonical_json,
    _canonical_sha256,
)
from .comparison import FrequencyResponse
from .cad_schema import ensure_native_schema, require_native_tables, connect_sqlite


APPLICABILITY_EVALUATOR_VERSION = '1'
APPLICABILITY_GEOMETRY_EVALUATOR_ID = 'o60-applicability-geometry'
APPLICABILITY_BAND_EVALUATOR_ID = 'o60-applicability-band'
APPLICABILITY_ROUTING_EVALUATOR_ID = 'o60-applicability-routing'
APPLICABILITY_MANUAL_EVALUATOR_ID = 'o60-applicability-manual'
APPLICABILITY_ATTESTATION_REF_KIND = 'o60_applicability_attestation'

APPLICABILITY_ATTESTATION_AUTHORITY_VERSION = 'o60-applicability-attestation-1'


class CadApplicabilityAttestation(BaseModel):
    """Immutable human attestation for one applicability code on exact scope.

    The attestation carries explicit provenance: who attested (``actor``), when
    (``attested_at_utc``), the exact subject scope (canonical ``subject_json``
    embedding the document/SearchSpec anchors) and the reviewer-supplied
    evidence payload (canonical ``evidence_json``). ``attestation_sha256``
    seals the semantic payload and ``attestation_id`` is derived from it, so a
    persisted row is self-verifying on every read.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal[
        'o60-applicability-attestation-1'
    ] = APPLICABILITY_ATTESTATION_AUTHORITY_VERSION
    attestation_id: str = Field(
        pattern=r'^o60-applicability-attestation:[0-9a-f]{64}$'
    )
    document_id: str = Field(min_length=1)
    search_spec_id: str = Field(min_length=1)
    search_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    code: str = Field(min_length=1)
    decision: Literal['pass', 'fail']
    actor: str = Field(min_length=1)
    attested_at_utc: str = Field(min_length=1)
    subject_json: str = Field(min_length=2)
    subject_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    evidence_json: str = Field(min_length=2)
    evidence_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    attestation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_attestation(self) -> 'CadApplicabilityAttestation':
        try:
            parsed_at = datetime.fromisoformat(
                self.attested_at_utc.replace('Z', '+00:00')
            )
        except ValueError as exc:
            raise ValueError(
                'applicability attestation timestamp must be ISO-8601'
            ) from exc
        if parsed_at.tzinfo is None:
            raise ValueError(
                'applicability attestation timestamp must be timezone-aware'
            )
        subject = self.subject()
        evidence = self.evidence()
        if _canonical_json(subject) != self.subject_json:
            raise ValueError('applicability attestation subject must be canonical JSON')
        if _canonical_sha256(subject) != self.subject_sha256:
            raise ValueError('applicability attestation subject hash mismatch')
        if _canonical_json(evidence) != self.evidence_json:
            raise ValueError('applicability attestation evidence must be canonical JSON')
        if _canonical_sha256(evidence) != self.evidence_sha256:
            raise ValueError('applicability attestation evidence hash mismatch')
        if not evidence:
            raise ValueError('applicability attestation requires explicit evidence')
        if subject.get('document_id') != self.document_id:
            raise ValueError('applicability attestation subject document mismatch')
        if subject.get('search_spec_id') != self.search_spec_id:
            raise ValueError('applicability attestation subject SearchSpec mismatch')
        if subject.get('search_spec_sha256') != self.search_spec_sha256:
            raise ValueError('applicability attestation subject SearchSpec hash mismatch')
        if subject.get('code') != self.code:
            raise ValueError('applicability attestation subject code mismatch')
        if _canonical_sha256(self.semantic_payload()) != self.attestation_sha256:
            raise ValueError('applicability attestation semantic hash mismatch')
        if self.attestation_id != (
            'o60-applicability-attestation:' + self.attestation_sha256
        ):
            raise ValueError('applicability attestation id does not match its hash')
        return self

    def subject(self) -> dict[str, Any]:
        subject = json.loads(self.subject_json)
        if not isinstance(subject, dict):
            raise ValueError('applicability attestation subject must be a JSON object')
        return subject

    def evidence(self) -> dict[str, Any]:
        evidence = json.loads(self.evidence_json)
        if not isinstance(evidence, dict):
            raise ValueError('applicability attestation evidence must be a JSON object')
        return evidence

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'search_spec_id': self.search_spec_id,
            'search_spec_sha256': self.search_spec_sha256,
            'code': self.code,
            'decision': self.decision,
            'actor': self.actor,
            'attested_at_utc': self.attested_at_utc,
            'subject': self.subject(),
            'evidence': self.evidence(),
        }


def build_applicability_attestation(
    *,
    document_id: str,
    search_spec_id: str,
    search_spec_sha256: str,
    code: str,
    decision: Literal['pass', 'fail'],
    actor: str,
    evidence: Mapping[str, Any],
    subject_scope: Mapping[str, Any] | None = None,
    attested_at_utc: str | None = None,
) -> CadApplicabilityAttestation:
    """Assemble an immutable attestation with canonical identity hashes.

    ``subject_scope`` carries the domain-specific subject identity the actor
    evaluated (e.g. the exact measurement/plan ids a human verified); the
    document/SearchSpec/code anchors are added automatically so the subject is
    self-describing.
    """
    if attested_at_utc is None:
        attested_at_utc = datetime.now(timezone.utc).isoformat()
    subject = {
        'code': code,
        'document_id': document_id,
        'scope': dict(subject_scope or {}),
        'search_spec_id': search_spec_id,
        'search_spec_sha256': search_spec_sha256,
    }
    provisional = CadApplicabilityAttestation.model_construct(
        authority_version=APPLICABILITY_ATTESTATION_AUTHORITY_VERSION,
        attestation_id='o60-applicability-attestation:' + '0' * 64,
        document_id=document_id,
        search_spec_id=search_spec_id,
        search_spec_sha256=search_spec_sha256,
        code=code,
        decision=decision,
        actor=actor,
        attested_at_utc=attested_at_utc,
        subject_json=_canonical_json(subject),
        subject_sha256=_canonical_sha256(subject),
        evidence_json=_canonical_json(evidence),
        evidence_sha256=_canonical_sha256(evidence),
        attestation_sha256='0' * 64,
    )
    attestation_sha256 = _canonical_sha256(provisional.semantic_payload())
    return CadApplicabilityAttestation(
        **provisional.model_dump(
            exclude={'attestation_id', 'attestation_sha256'}
        ),
        attestation_sha256=attestation_sha256,
        attestation_id='o60-applicability-attestation:' + attestation_sha256,
    )


class CadApplicabilityAttestationRepository:
    """Immutable O60 applicability attestation storage in the shared native DB."""

    def __init__(self, path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_applicability_attestations')

    def save(
        self,
        attestation: CadApplicabilityAttestation,
    ) -> CadApplicabilityAttestation:
        if not isinstance(attestation, CadApplicabilityAttestation):
            raise TypeError('attestation must be CadApplicabilityAttestation')
        attestation = CadApplicabilityAttestation.model_validate(
            attestation.model_dump(mode='python')
        )
        existing = self.get(attestation.attestation_id)
        if existing is not None:
            if existing != attestation:
                raise ValueError(
                    'applicability attestation id has different semantics'
                )
            return existing
        with closing(self._connect()) as connection, connection:
            connection.execute(
                '''INSERT INTO cad_applicability_attestations(
                    attestation_id, attestation_sha256, document_id,
                    search_spec_id, code, payload_json, attested_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (
                    attestation.attestation_id,
                    attestation.attestation_sha256,
                    attestation.document_id,
                    attestation.search_spec_id,
                    attestation.code,
                    attestation.model_dump_json(),
                    attestation.attested_at_utc,
                ),
            )
        return attestation

    def get(self, attestation_id: str) -> CadApplicabilityAttestation | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_applicability_attestations '
                'WHERE attestation_id=?',
                (attestation_id,),
            ).fetchone()
        return None if row is None else CadApplicabilityAttestation.model_validate_json(
            row['payload_json']
        )

    def list_for_search_spec(
        self,
        search_spec_id: str,
    ) -> tuple[CadApplicabilityAttestation, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_applicability_attestations '
                'WHERE search_spec_id=? ORDER BY seq ASC',
                (search_spec_id,),
            ).fetchall()
        return tuple(
            CadApplicabilityAttestation.model_validate_json(row['payload_json'])
            for row in rows
        )


class ApplicabilityAuthorityContext(NamedTuple):
    """Resolved evidence authority one validation record is evaluated against."""

    document_id: str
    search_spec_id: str
    search_spec_sha256: str
    candidate_set_sha256: str
    model_id: str
    model_version: str
    evidence_scope: str
    campaign_id: str | None
    requested_band_hz: tuple[float, float]
    search_spec: Any
    scene_revision: Any
    prediction_batches: tuple[tuple[str, Any], ...]
    measurement_plans: tuple[Any, ...]
    measurements: tuple[Any, ...]
    pair_responses: tuple[tuple[FrequencyResponse, FrequencyResponse], ...]
    attestation_repository: CadApplicabilityAttestationRepository | None


def resolve_applicability_context(
    *,
    document_id: str,
    search_spec_id: str,
    search_spec_sha256: str,
    candidate_set_sha256: str,
    model_id: str,
    model_version: str,
    evidence_scope: str,
    campaign_id: str | None,
    requested_band_hz: tuple[float, float],
    pair_attempt_ids: Sequence[str],
    pair_measurement_ids: Sequence[str],
    scoped_measurement_ids: Sequence[str],
    search_repository,
    roomsim_repository,
    measurement_repository,
    attestation_repository: CadApplicabilityAttestationRepository | None = None,
) -> ApplicabilityAuthorityContext:
    """Resolve every authority applicability evaluators may bind to.

    Resolution is intentionally fail-closed: an applicability claim over a
    record whose SearchSpec, SceneRevision, prediction batch, measurement or
    plan authority does not resolve raises rather than guessing.
    """
    spec = search_repository.get(search_spec_id)
    if (
        spec is None
        or spec.document_id != document_id
        or spec.search_spec_sha256 != search_spec_sha256
    ):
        raise ValueError('applicability SearchSpec authority does not resolve')
    scene_revision = search_repository.scene_repository.get(spec.scene_revision_id)
    if (
        scene_revision is None
        or scene_revision.document_id != document_id
        or scene_revision.content_hash != spec.scene_content_hash
    ):
        raise ValueError('applicability SceneRevision authority does not resolve')

    if len(pair_attempt_ids) != len(pair_measurement_ids):
        raise ValueError('applicability pair authority is inconsistent')
    batches: dict[str, Any] = {}
    pair_responses: list[tuple[FrequencyResponse, FrequencyResponse]] = []
    for attempt_id, measurement_id in zip(pair_attempt_ids, pair_measurement_ids):
        attempt = roomsim_repository.get_attempt(attempt_id)
        if attempt is None or attempt.status != 'completed':
            raise ValueError(
                'applicability prediction attempt does not resolve'
            )
        batch = roomsim_repository.get_batch_spec(attempt.batch_run_id)
        if batch is None:
            raise ValueError(
                'applicability prediction batch does not resolve'
            )
        if getattr(batch, 'batch_run_id', attempt.batch_run_id) != attempt.batch_run_id:
            raise ValueError(
                'applicability prediction batch identity does not resolve'
            )
        batches[attempt.batch_run_id] = batch
        dataset = measurement_repository.dataset_for_measurement(measurement_id)
        if dataset is None:
            raise ValueError(
                'applicability measurement response does not resolve'
            )
        pair_responses.append((
            roomsim_attempt_frequency_response(attempt),
            FrequencyResponse(
                frequency_hz=tuple(float(value) for value in dataset.frequency_hz),
                level_db=tuple(float(value) for value in dataset.level_db),
            ),
        ))

    scoped_ids = tuple(sorted(set(scoped_measurement_ids)))
    measurements = []
    for measurement_id in scoped_ids:
        measurement = measurement_repository.get_measurement(measurement_id)
        if measurement is None:
            raise ValueError(
                'applicability measurement evidence does not resolve'
            )
        measurements.append(measurement)

    scoped_set = set(scoped_ids)
    plans = tuple(
        plan
        for plan in measurement_repository.list_measurement_plans(search_spec_id)
        if plan.status == 'measured'
        and plan.candidate_set_sha256 == candidate_set_sha256
        and any(measurement_id in plan.measurement_ids for measurement_id in scoped_set)
    )
    covered = {
        measurement_id
        for plan in plans
        for measurement_id in plan.measurement_ids
    }
    if not scoped_set <= covered:
        raise ValueError(
            'applicability measurement is not linked to a measured '
            'Measurement Plan for this candidate set'
        )

    return ApplicabilityAuthorityContext(
        document_id=document_id,
        search_spec_id=search_spec_id,
        search_spec_sha256=search_spec_sha256,
        candidate_set_sha256=candidate_set_sha256,
        model_id=model_id,
        model_version=model_version,
        evidence_scope=evidence_scope,
        campaign_id=campaign_id,
        requested_band_hz=(
            float(requested_band_hz[0]),
            float(requested_band_hz[1]),
        ),
        search_spec=spec,
        scene_revision=scene_revision,
        prediction_batches=tuple(
            (batch_run_id, batches[batch_run_id])
            for batch_run_id in sorted(batches)
        ),
        measurement_plans=plans,
        measurements=tuple(measurements),
        pair_responses=tuple(pair_responses),
        attestation_repository=attestation_repository,
    )


def _require_batch_authority(
    batch,
    context: ApplicabilityAuthorityContext,
) -> None:
    expected = {
        'document_id': context.document_id,
        'scene_revision_id': context.search_spec.scene_revision_id,
        'scene_content_hash': context.search_spec.scene_content_hash,
        'search_spec_id': context.search_spec_id,
        'search_spec_sha256': context.search_spec_sha256,
        'candidate_set_sha256': context.candidate_set_sha256,
        'model_id': context.model_id,
    }
    for field, value in expected.items():
        if getattr(batch, field, None) != value:
            raise ValueError(
                'applicability prediction batch authority does not match '
                'the validation scope'
            )
    batch_sha256 = getattr(batch, 'batch_spec_sha256', None)
    if not isinstance(batch_sha256, str) or len(batch_sha256) != 64:
        raise ValueError(
            'applicability prediction batch has no semantic hash authority'
        )


def _batch_binding(batch) -> dict[str, Any]:
    raw = getattr(batch, 'binding_json', None)
    if not isinstance(raw, str):
        return {}
    try:
        binding = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return binding if isinstance(binding, dict) else {}


def _batch_ref(batch_run_id: str, batch) -> CadApplicabilityEvidenceRef:
    return CadApplicabilityEvidenceRef(
        source_kind='cad_roomsim_batch_spec',
        source_id=batch_run_id,
        source_sha256=batch.batch_spec_sha256,
    )


def _scene_revision_ref(context: ApplicabilityAuthorityContext) -> CadApplicabilityEvidenceRef:
    return CadApplicabilityEvidenceRef(
        source_kind='cad_scene_revision',
        source_id=context.search_spec.scene_revision_id,
        source_sha256=context.search_spec.scene_content_hash,
    )


def _evaluate_geometry(
    context: ApplicabilityAuthorityContext,
    *,
    code: str,
    detail: str | None,
    attestation_id: str | None,
) -> CadApplicabilityCheck:
    if code != 'geometry':
        raise ValueError('geometry evaluator cannot decide this applicability code')
    if attestation_id is not None:
        raise ValueError('automated geometry evaluation does not accept attestations')
    room = getattr(getattr(context.scene_revision, 'document', None), 'room', None)
    rectangular = room is not None and exact_rectangular_room_frame(room) is not None
    refs = [_scene_revision_ref(context)]
    passed = rectangular
    for batch_run_id, batch in context.prediction_batches:
        _require_batch_authority(batch, context)
        refs.append(_batch_ref(batch_run_id, batch))
        if _batch_binding(batch).get('geometry_mode') != 'exact_rectangular':
            passed = False
    subject = {
        'document_id': context.document_id,
        'model_id': context.model_id,
        'model_version': context.model_version,
        'scene_content_hash': context.search_spec.scene_content_hash,
        'scene_revision_id': context.search_spec.scene_revision_id,
        'search_spec_id': context.search_spec_id,
        'search_spec_sha256': context.search_spec_sha256,
    }
    return build_applicability_check(
        code='geometry',
        passed=passed,
        evaluator_id=APPLICABILITY_GEOMETRY_EVALUATOR_ID,
        evaluator_version=APPLICABILITY_EVALUATOR_VERSION,
        subject=subject,
        evidence_refs=refs,
        detail=detail,
    )


def _evaluate_band(
    context: ApplicabilityAuthorityContext,
    *,
    code: str,
    detail: str | None,
    attestation_id: str | None,
) -> CadApplicabilityCheck:
    if code != 'band':
        raise ValueError('band evaluator cannot decide this applicability code')
    if attestation_id is not None:
        raise ValueError('automated band evaluation does not accept attestations')
    low_hz, high_hz = context.requested_band_hz
    passed = True
    for predicted, measured in context.pair_responses:
        for response in (predicted, measured):
            frequencies = response.frequency_hz
            if (
                not frequencies
                or float(frequencies[0]) > low_hz
                or float(frequencies[-1]) < high_hz
            ):
                passed = False
    refs = [
        CadApplicabilityEvidenceRef(
            source_kind='cad_search_spec',
            source_id=context.search_spec_id,
            source_sha256=context.search_spec_sha256,
        )
    ]
    for batch_run_id, batch in context.prediction_batches:
        _require_batch_authority(batch, context)
        refs.append(_batch_ref(batch_run_id, batch))
    subject = {
        'campaign_id': context.campaign_id,
        'candidate_set_sha256': context.candidate_set_sha256,
        'document_id': context.document_id,
        'model_id': context.model_id,
        'model_version': context.model_version,
        'requested_band_hz': [low_hz, high_hz],
        'search_spec_id': context.search_spec_id,
        'search_spec_sha256': context.search_spec_sha256,
    }
    return build_applicability_check(
        code='band',
        passed=passed,
        evaluator_id=APPLICABILITY_BAND_EVALUATOR_ID,
        evaluator_version=APPLICABILITY_EVALUATOR_VERSION,
        subject=subject,
        evidence_refs=refs,
        detail=detail,
    )


def _evaluate_routing(
    context: ApplicabilityAuthorityContext,
    *,
    code: str,
    detail: str | None,
    attestation_id: str | None,
) -> CadApplicabilityCheck:
    if code != 'routing':
        raise ValueError('routing evaluator cannot decide this applicability code')
    if attestation_id is not None:
        raise ValueError('automated routing evaluation does not accept attestations')
    refs = []
    plan_ids = []
    for plan in context.measurement_plans:
        plan_sha256 = getattr(plan, 'plan_sha256', None)
        if not isinstance(plan_sha256, str) or len(plan_sha256) != 64:
            raise ValueError(
                'applicability measurement plan has no semantic hash authority'
            )
        plan_ids.append(plan.plan_id)
        refs.append(CadApplicabilityEvidenceRef(
            source_kind='cad_measurement_plan',
            source_id=plan.plan_id,
            source_sha256=plan_sha256,
        ))
    def _strong_routing(measurement: Any) -> bool:
        """Verified routing requires an exact #473 profile pin (#848).

        The coarse ``routing_evidence`` enum alone never satisfies the
        strong-routing capability: the record must also pin a persisted
        RoutingProfile by id + semantic hash in its provenance, so a bare
        'verified' label cannot masquerade as a resolved authority.
        """
        if (
            getattr(measurement, 'evidence_type', None) != 'measured'
            or getattr(measurement, 'routing_evidence', None) != 'verified'
        ):
            return False
        try:
            provenance = json.loads(
                getattr(measurement, 'provenance_json', '') or '{}'
            )
        except (ValueError, TypeError):
            return False
        profile = provenance.get('routing_profile')
        if not isinstance(profile, dict):
            return False
        profile_id = profile.get('routing_profile_id')
        profile_sha256 = profile.get('routing_profile_sha256')
        return (
            isinstance(profile_id, str)
            and isinstance(profile_sha256, str)
            and len(profile_sha256) == 64
        )

    passed = all(
        _strong_routing(measurement)
        for measurement in context.measurements
    )
    subject = {
        'candidate_set_sha256': context.candidate_set_sha256,
        'document_id': context.document_id,
        'measurement_ids': [
            measurement.measurement_id for measurement in context.measurements
        ],
        'measurement_plan_ids': sorted(plan_ids),
        'search_spec_id': context.search_spec_id,
        'search_spec_sha256': context.search_spec_sha256,
    }
    return build_applicability_check(
        code='routing',
        passed=passed,
        evaluator_id=APPLICABILITY_ROUTING_EVALUATOR_ID,
        evaluator_version=APPLICABILITY_EVALUATOR_VERSION,
        subject=subject,
        evidence_refs=refs,
        detail=detail,
    )


def _evaluate_manual(
    context: ApplicabilityAuthorityContext,
    *,
    code: str,
    detail: str | None,
    attestation_id: str | None,
) -> CadApplicabilityCheck:
    if attestation_id is None:
        raise ValueError('manual applicability requires an attestation reference')
    repository = context.attestation_repository
    if repository is None:
        raise ValueError('manual applicability attestation store is unavailable')
    attestation = repository.get(attestation_id)
    if attestation is None:
        raise ValueError('applicability attestation does not exist')
    if attestation.code != code:
        raise ValueError('applicability attestation code mismatch')
    if (
        attestation.document_id != context.document_id
        or attestation.search_spec_id != context.search_spec_id
        or attestation.search_spec_sha256 != context.search_spec_sha256
    ):
        raise ValueError('applicability attestation belongs to another scope')
    return build_applicability_check(
        code=code,
        passed=attestation.decision == 'pass',
        evaluator_id=APPLICABILITY_MANUAL_EVALUATOR_ID,
        evaluator_version=APPLICABILITY_EVALUATOR_VERSION,
        subject=attestation.subject(),
        evidence_refs=(
            CadApplicabilityEvidenceRef(
                source_kind=APPLICABILITY_ATTESTATION_REF_KIND,
                source_id=attestation.attestation_id,
                source_sha256=attestation.attestation_sha256,
            ),
        ),
        detail=detail,
    )


APPLICABILITY_EVALUATORS = {
    APPLICABILITY_GEOMETRY_EVALUATOR_ID: _evaluate_geometry,
    APPLICABILITY_BAND_EVALUATOR_ID: _evaluate_band,
    APPLICABILITY_ROUTING_EVALUATOR_ID: _evaluate_routing,
    APPLICABILITY_MANUAL_EVALUATOR_ID: _evaluate_manual,
}

AUTOMATED_EVALUATOR_BY_CODE = {
    'geometry': APPLICABILITY_GEOMETRY_EVALUATOR_ID,
    'band': APPLICABILITY_BAND_EVALUATOR_ID,
    'routing': APPLICABILITY_ROUTING_EVALUATOR_ID,
}


def evaluate_applicability(
    context: ApplicabilityAuthorityContext,
    *,
    code: str,
    evaluator_id: str,
    detail: str | None = None,
    attestation_id: str | None = None,
) -> CadApplicabilityCheck:
    """Derive the canonical check for one applicability code.

    Only registered evaluator identities produce a check; anything else fails
    closed with ``ValueError``.
    """
    evaluator = APPLICABILITY_EVALUATORS.get(evaluator_id)
    if evaluator is None:
        raise ValueError(
            f'applicability evaluator is not registered: {evaluator_id}'
        )
    return evaluator(
        context,
        code=code,
        detail=detail,
        attestation_id=attestation_id,
    )


def rederive_applicability_check(
    context: ApplicabilityAuthorityContext,
    check: CadApplicabilityCheck,
) -> CadApplicabilityCheck:
    """Replay a persisted check through its registered evaluator."""
    attestation_id = next(
        (
            ref.source_id
            for ref in check.evidence_refs
            if ref.source_kind == APPLICABILITY_ATTESTATION_REF_KIND
        ),
        None,
    )
    return evaluate_applicability(
        context,
        code=check.code,
        evaluator_id=check.evaluator_id,
        detail=check.detail,
        attestation_id=attestation_id,
    )


def applicability_authority_summary(check: CadApplicabilityCheck) -> str:
    """Compact audit line naming the exact authority behind one decision."""
    refs = ' '.join(
        f'{ref.source_kind}:{ref.source_id}[{ref.source_sha256}]'
        for ref in check.evidence_refs
    )
    return (
        f'{check.evaluator_id}@{check.evaluator_version} '
        f'decision={check.decision_sha256} {refs}'
    )
