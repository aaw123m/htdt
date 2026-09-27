from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .project_identity import (
    HTDTLegacyProjectRef,
    HTDTProjectReference,
    InboundProjectRef,
    classify_project_reference,
    resolve_project_reference,
)
from .cad_schema import ensure_native_schema, require_native_tables, connect_sqlite
from .canonical_json import canonical_json as _canonical_json


class FieldReturnError(ValueError):
    """A .htdtfieldreturn artifact could not be validated or staged."""


class FieldReturnConflictError(FieldReturnError):
    """Same contribution identity with different artifact bytes."""


UUID4_PATTERN = (
    r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}'
    r'-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
)
HEX64 = r'^[0-9a-f]{64}$'
UUID4_RE = re.compile(UUID4_PATTERN)




# --- artifact-family recognition (#674 §1) ------------------------------------
#
# Classification keys on the root document's declared schema identity, never on
# the filename extension.

InboundArtifactKind = Literal[
    'capture_bundle',
    'field_return',
    'mission_package',
    'equipment_catalog_snapshot',
    'unsupported',
]

_INBOUND_SCHEMA_KINDS = {
    'htdt.capture.bundle': 'capture_bundle',
    'htdt.field-return': 'field_return',
    'htdt.capture.mission-package': 'mission_package',
    'htdt.equipment.catalog-snapshot': 'equipment_catalog_snapshot',
}


def classify_inbound_document(document: object) -> InboundArtifactKind:
    """Earliest-boundary artifact classification by declared schema."""

    if not isinstance(document, Mapping):
        return 'unsupported'
    schema = document.get('schema')
    if not isinstance(schema, str):
        return 'unsupported'
    return _INBOUND_SCHEMA_KINDS.get(schema, 'unsupported')  # type: ignore[return-value]


# --- typed manifest (#674 §2/§3) ----------------------------------------------

FieldReturnRecordKind = Literal[
    'equipment_identity',
    'installed_setting',
    'wiring_observation',
    'instrument_result',
    'evidence_asset',
    'attestation',
    'spatial_reference',
]


class FieldReturnRecord(BaseModel):
    """One typed non-spatial authority record inside a Field Return."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    record_id: str = Field(pattern=UUID4_PATTERN)
    kind: FieldReturnRecordKind
    subject_ref: str | None = Field(default=None, min_length=1)
    payload_sha256: str | None = Field(default=None, pattern=HEX64)
    evidence_refs: tuple[str, ...] = ()
    method: str | None = Field(default=None, min_length=1)

    @field_validator('evidence_refs')
    @classmethod
    def validate_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(not item for item in value):
            raise ValueError('evidence_refs must be non-empty and unique')
        return value


class FieldReturnTaskOutcome(BaseModel):
    """Declared task outcome; `fulfilled` still requires a valid ref (§6)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    task_id: str = Field(min_length=1)
    outcome: Literal['fulfilled', 'skipped', 'unavailable', 'failed']
    fulfilled_by_ref: str | None = None

    @field_validator('fulfilled_by_ref')
    @classmethod
    def validate_ref(cls, value: str | None) -> str | None:
        if value is not None and not UUID4_RE.fullmatch(value):
            raise ValueError('fulfilled_by_ref must be a lowercase UUIDv4')
        return value


class FieldReturnManifest(BaseModel):
    """Versioned .htdtfieldreturn root document — a contribution, never a
    CaptureRevision."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema: Literal['htdt.field-return'] = 'htdt.field-return'
    schema_version: int = Field(ge=1)
    contribution_id: str = Field(pattern=UUID4_PATTERN)
    mission_id: str | None = Field(default=None, pattern=UUID4_PATTERN)
    plan_sha256: str | None = Field(default=None, pattern=HEX64)
    project: HTDTProjectReference | HTDTLegacyProjectRef | None = None
    authority_binding_scope: Literal['contribution_id'] = 'contribution_id'
    finalized_at_utc: str | None = Field(default=None, min_length=1)
    records: tuple[FieldReturnRecord, ...] = ()
    task_outcomes: tuple[FieldReturnTaskOutcome, ...] = ()

    @field_validator('project', mode='before')
    @classmethod
    def resolve_inbound_project(cls, value: object) -> object:
        """Bare ``projectRef`` strings resolve to the verbatim legacy adapter;
        structured payloads decode as the versioned reference."""

        if value is None:
            return value
        if isinstance(value, (str, dict)):
            try:
                return resolve_project_reference(value)
            except ValueError as exc:
                raise ValueError(
                    f'project reference is invalid: {exc}'
                ) from exc
        return value

    @model_validator(mode='after')
    def valid_manifest(self) -> 'FieldReturnManifest':
        ids = [record.record_id for record in self.records]
        if len(set(ids)) != len(ids):
            raise ValueError('duplicate field-return record identity')
        return self

    def content_digest(self) -> str:
        """Semantic digest over the contribution's typed content."""

        projection = self.model_dump(mode='json')
        return sha256(_canonical_json(projection).encode('utf-8')).hexdigest()


def validate_field_return(document: object) -> FieldReturnManifest:
    """Validate the root manifest and its internal reference integrity.

    A ``fulfilled`` outcome whose ``fulfilled_by_ref`` does not resolve to a
    record carried by the same contribution is structurally invalid: the
    artifact stays parseable for staging diagnosis, so fulfillment failures
    are reported by :func:`verify_task_fulfillment` rather than raising here.
    Only schema/identity violations raise.
    """

    if not isinstance(document, Mapping):
        raise FieldReturnError('field return root must be a JSON object')
    if classify_inbound_document(document) != 'field_return':
        raise FieldReturnError(
            'document is not declared as an htdt.field-return artifact'
        )
    version = document.get('schema_version')
    if version != 1:
        raise FieldReturnError(
            f'unsupported htdt.field-return schema_version: {version}'
        )
    if isinstance(document.get('project'), (dict, str)):
        try:
            resolve_project_reference(document['project'])
        except ValueError as exc:
            raise FieldReturnError(
                f'field return project reference is invalid: {exc}'
            ) from exc
    try:
        return FieldReturnManifest.model_validate(document)
    except ValueError as exc:
        raise FieldReturnError(f'invalid field return manifest: {exc}') from exc


FulfillmentState = Literal[
    'fulfilled',
    'unresolved_insufficient',
    'skipped',
    'unavailable',
    'failed',
]


def verify_task_fulfillment(
    manifest: FieldReturnManifest,
) -> tuple[tuple[str, FulfillmentState], ...]:
    """Evidence-bound task fulfillment replay (§6).

    An operator-declared ``fulfilled`` outcome is honored only when its
    ``fulfilled_by_ref`` resolves to an authority record carried by the same
    contribution — a UI checkbox never becomes project evidence.
    """

    record_ids = {record.record_id for record in manifest.records}
    results: list[tuple[str, FulfillmentState]] = []
    for outcome in manifest.task_outcomes:
        if outcome.outcome != 'fulfilled':
            results.append((outcome.task_id, outcome.outcome))
            continue
        if (
            outcome.fulfilled_by_ref is not None
            and outcome.fulfilled_by_ref in record_ids
        ):
            results.append((outcome.task_id, 'fulfilled'))
        else:
            results.append((outcome.task_id, 'unresolved_insufficient'))
    return tuple(results)


# --- routing / staging (#674 §4/§16) ------------------------------------------

FieldReturnRouting = Literal[
    'exact_project_match',
    'known_project_lineage',
    'unknown_project_reference',
    'legacy_project_ref',
    'unrouted',
]


def classify_field_return_routing(
    manifest: FieldReturnManifest,
    known_projects: Sequence[HTDTProjectReference],
) -> FieldReturnRouting:
    """Route by exact #607 identity; never by same-name matching."""

    if manifest.project is None:
        return 'unrouted'
    return classify_project_reference(manifest.project, tuple(known_projects))


ContributionDuplicateClass = Literal[
    'exact_duplicate',
    'identity_conflict',
    'distinct',
]


def classify_contribution_duplicate(
    *,
    existing_contribution_id: str,
    existing_artifact_sha256: str,
    incoming_contribution_id: str,
    incoming_artifact_sha256: str,
) -> ContributionDuplicateClass:
    """Deterministic duplicate/conflict semantics (§16).

    Exact artifact duplicate (same id + same digest) is an idempotent known
    duplicate; same contribution id with different bytes is a hard conflict.
    Finalized timestamps never decide precedence.
    """

    if existing_contribution_id != incoming_contribution_id:
        return 'distinct'
    if existing_artifact_sha256 == incoming_artifact_sha256:
        return 'exact_duplicate'
    return 'identity_conflict'


FieldReturnValidationState = Literal[
    'validated',
    'unsupported',
    'malformed',
]


class StagedFieldReturn(BaseModel):
    """Durable staging state for one received contribution (§15)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    contribution_id: str | None
    artifact_sha256: str = Field(pattern=HEX64)
    validation_state: FieldReturnValidationState
    routing: FieldReturnRouting
    matched_project_id: str | None = None
    mission_id: str | None = None
    plan_sha256: str | None = None
    manifest_json: str | None = None
    detail: str | None = None


def stage_field_return(
    artifact: bytes,
    known_projects: Sequence[HTDTProjectReference] = (),
) -> StagedFieldReturn:
    """Validate + classify one .htdtfieldreturn for staging (§2/§4/§17).

    - structurally valid v1 -> ``validated`` typed staging;
    - valid JSON declaring an unsupported version -> ``unsupported``: the
      exact bytes/digest are preserved and surfaced, never silently dropped;
    - malformed bytes/root -> ``malformed`` staging diagnostic; the artifact
      is never converted into a fake Capture to force it through ingestion.
    """

    digest = sha256(artifact).hexdigest()
    try:
        document = json.loads(artifact.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        return StagedFieldReturn(
            contribution_id=None,
            artifact_sha256=digest,
            validation_state='malformed',
            routing='unrouted',
            detail=f'field return artifact is not valid JSON: {exc}',
        )
    if classify_inbound_document(document) != 'field_return':
        return StagedFieldReturn(
            contribution_id=None,
            artifact_sha256=digest,
            validation_state='malformed',
            routing='unrouted',
            detail='document is not declared as an htdt.field-return artifact',
        )
    version = document.get('schema_version')
    if version != 1:
        contribution_id = document.get('contribution_id')
        return StagedFieldReturn(
            contribution_id=(
                contribution_id if isinstance(contribution_id, str) else None
            ),
            artifact_sha256=digest,
            validation_state='unsupported',
            routing='unrouted',
            manifest_json=_canonical_json(document),
            detail=(
                f'unsupported htdt.field-return schema_version: {version}; '
                'artifact preserved for a compatible HTDT release'
            ),
        )
    try:
        manifest = validate_field_return(document)
    except FieldReturnError as exc:
        contribution_id = document.get('contribution_id')
        return StagedFieldReturn(
            contribution_id=(
                contribution_id if isinstance(contribution_id, str) else None
            ),
            artifact_sha256=digest,
            validation_state='malformed',
            routing='unrouted',
            detail=str(exc),
        )
    routing = classify_field_return_routing(manifest, known_projects)
    matched: str | None = None
    if (
        isinstance(manifest.project, HTDTProjectReference)
        and routing in {'exact_project_match', 'known_project_lineage'}
    ):
        matched = manifest.project.project_id
    return StagedFieldReturn(
        contribution_id=manifest.contribution_id,
        artifact_sha256=digest,
        validation_state='validated',
        routing=routing,
        matched_project_id=matched,
        mission_id=manifest.mission_id,
        plan_sha256=manifest.plan_sha256,
        manifest_json=_canonical_json(manifest.model_dump(mode='json')),
    )


class FieldReturnRepository:
    """Durable staging store for received field-return contributions.

    Receiver-side state lives in the native store; the original artifact file
    is never mutated to record disposition.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'field_return_contributions')

    def stage(
        self,
        artifact: bytes,
        known_projects: Sequence[HTDTProjectReference] = (),
    ) -> StagedFieldReturn:
        """Persist staging state with deterministic duplicate semantics."""

        staged = stage_field_return(artifact, known_projects)
        if staged.contribution_id is None:
            return staged
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                '''
                SELECT contribution_id, artifact_sha256
                FROM field_return_contributions
                WHERE contribution_id=?
                ''',
                (staged.contribution_id,),
            ).fetchone()
            if existing is not None:
                duplicate = classify_contribution_duplicate(
                    existing_contribution_id=existing['contribution_id'],
                    existing_artifact_sha256=existing['artifact_sha256'],
                    incoming_contribution_id=staged.contribution_id,
                    incoming_artifact_sha256=staged.artifact_sha256,
                )
                if duplicate == 'exact_duplicate':
                    return staged
                raise FieldReturnConflictError(
                    'field return contribution '
                    f'{staged.contribution_id} already exists with different '
                    'artifact bytes'
                )
            connection.execute(
                '''
                INSERT INTO field_return_contributions(
                    contribution_id, artifact_sha256, validation_state,
                    routing, matched_project_id, mission_id, plan_sha256,
                    manifest_json, detail, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    staged.contribution_id,
                    staged.artifact_sha256,
                    staged.validation_state,
                    staged.routing,
                    staged.matched_project_id,
                    staged.mission_id,
                    staged.plan_sha256,
                    staged.manifest_json,
                    staged.detail,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        return staged

    def get(self, contribution_id: str) -> StagedFieldReturn | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                '''
                SELECT *
                FROM field_return_contributions
                WHERE contribution_id=?
                ''',
                (contribution_id,),
            ).fetchone()
        if row is None:
            return None
        return StagedFieldReturn(
            contribution_id=row['contribution_id'],
            artifact_sha256=row['artifact_sha256'],
            validation_state=row['validation_state'],
            routing=row['routing'],
            matched_project_id=row['matched_project_id'],
            mission_id=row['mission_id'],
            plan_sha256=row['plan_sha256'],
            manifest_json=row['manifest_json'],
            detail=row['detail'],
        )

    def list_staged(self) -> tuple[StagedFieldReturn, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                '''
                SELECT *
                FROM field_return_contributions
                ORDER BY recorded_at_utc ASC, contribution_id ASC
                '''
            ).fetchall()
        return tuple(
            StagedFieldReturn(
                contribution_id=row['contribution_id'],
                artifact_sha256=row['artifact_sha256'],
                validation_state=row['validation_state'],
                routing=row['routing'],
                matched_project_id=row['matched_project_id'],
                mission_id=row['mission_id'],
                plan_sha256=row['plan_sha256'],
                manifest_json=row['manifest_json'],
                detail=row['detail'],
            )
            for row in rows
        )
