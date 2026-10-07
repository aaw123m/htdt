from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
import re
import sqlite3
from typing import Literal, Mapping, Sequence
import zipfile
import zlib

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
from .content_blobs import ensure_content_blob_store, store_content_blob


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
    'channel_project_match',
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


def _channel_routing(
    channel_project_ref: str | None,
    known_projects: Sequence[HTDTProjectReference],
) -> tuple[FieldReturnRouting, str | None]:
    """Route a document-less artifact by the delivery channel's scope.

    Container-form field returns carry no project reference in the root
    document — the only destination signal is the pairing the bytes
    arrived on, whose ``project_ref`` is the issuing project's id. When
    that id resolves to a known project the contribution is honestly
    attributed ('channel_project_match' — channel-declared, never
    document-claimed); anything else stays unrouted.
    """

    if not channel_project_ref:
        return 'unrouted', None
    known_ids = {item.project_id for item in known_projects}
    if channel_project_ref in known_ids:
        return 'channel_project_match', channel_project_ref
    return 'unrouted', None


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
    recorded_at_utc: str | None = None
    artifact_retained: bool = False


# --- .htdtfieldreturn container wire form (capture-side issue #400) ------------
#
# The app ships a stored-entry classic ZIP: a `container-manifest.json`
# integrity index, the `field-return.json` root document (`htdt.field_return`,
# string schema_version ``1.0.0``/``2.0.0``), typed authority docs under
# ``authority/*.json`` and payloads under ``evidence/**``. This is a different
# wire family from the flat ``htdt.field-return`` manifest above — both stage
# into ``field_return_contributions`` under the same duplicate semantics.

FIELD_RETURN_CONTAINER_MANIFEST_PATH = 'container-manifest.json'
FIELD_RETURN_CONTAINER_ROOT_PATH = 'field-return.json'
SUPPORTED_FIELD_RETURN_CONTAINER_VERSIONS = ('1.0.0', '2.0.0')
_STORED_ZIP_MAGIC = b'PK\x03\x04'


class FieldReturnContainerDocRef(BaseModel):
    """One declared document ref on the container root document.

    ``schema`` is required on authority documents — it names the typed
    payload family the ref carries."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    path: str = Field(min_length=1)
    schema: str = Field(min_length=1)
    sha256: str = Field(pattern=HEX64)
    bytes: int = Field(ge=0)


class FieldReturnContainerAssetRef(BaseModel):
    """Evidence-asset ref: raw payload bytes, no declared schema.

    The app emits ``application/octet-stream``; containers written
    before the media-type token landed carry ``""`` — both stage.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    path: str = Field(min_length=1)
    schema: str = ''
    sha256: str = Field(pattern=HEX64)
    bytes: int = Field(ge=0)


class FieldReturnContainerDocument(BaseModel):
    """Envelope validation for the container's ``field-return.json`` root.

    Strict on the identity and reference fields the receiver stages on; the
    nested ``contribution_ref``/``provenance``/ledger entry shapes stay
    app-owned — fulfillment replay belongs to the contribution's consumer,
    not the staging boundary.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema: Literal['htdt.field_return']
    schema_version: str = Field(min_length=1)
    authority_binding_scope: str = Field(min_length=1)
    contribution_id: str = Field(pattern=UUID4_PATTERN)
    contribution_ref: dict | None = None
    supersedes_contribution_ref: dict | None = None
    mission_id: str | None = None
    plan_id: str | None = None
    plan_version: str | None = None
    plan_sha256: str | None = Field(default=None, pattern=HEX64)
    created_at: str = Field(min_length=1)
    finalized_at: str = Field(min_length=1)
    provenance: dict
    related_capture_revision_ids: list[str] = []
    task_fulfillment_ledger: list[dict] = []
    authority_documents: list[FieldReturnContainerDocRef] = []
    evidence_assets: list[FieldReturnContainerAssetRef] = []
    content_digest: str = Field(pattern=HEX64)


def read_field_return_container(
    artifact: bytes,
) -> tuple[dict[str, bytes], FieldReturnContainerDocument, dict]:
    """Extract + integrity-verify a ``.htdtfieldreturn`` container.

    Mirrors ``HTDTFieldReturnArchiveReader``: stored entries only, every
    declared entry's crc32/sha256/bytes verified against
    ``container-manifest.json``, the declared set must equal the entry set,
    and every ``authority_documents``/``evidence_assets`` ref must resolve
    to its declared bytes. Returns (payload entries minus the manifest,
    parsed envelope, raw root document).
    """

    if not artifact[:4] == _STORED_ZIP_MAGIC:
        raise FieldReturnError(
            'field return artifact is not a stored-ZIP container'
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(artifact))
    except zipfile.BadZipFile as exc:
        raise FieldReturnError(f'invalid field-return container: {exc}')
    entries: dict[str, bytes] = {}
    for info in archive.infolist():
        if info.is_dir():
            continue
        if info.compress_type != zipfile.ZIP_STORED:
            raise FieldReturnError(
                'container entry is compressed; the wire family stores '
                f'entries verbatim: {info.filename}'
            )
        if info.flag_bits & 0x01:
            raise FieldReturnError(
                f'container entry is encrypted: {info.filename}'
            )
        entries[info.filename] = archive.read(info)
    manifest_data = entries.pop(FIELD_RETURN_CONTAINER_MANIFEST_PATH, None)
    if manifest_data is None:
        raise FieldReturnError('container-manifest.json is missing')
    try:
        manifest = json.loads(manifest_data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FieldReturnError(
            f'container-manifest.json is not valid JSON: {exc}'
        )
    declared: set[str] = set()
    for record in manifest.get('entries', ()):
        path = record.get('path')
        declared.add(path)
        payload = entries.get(path)
        if (
            payload is None
            or len(payload) != record.get('bytes')
            or sha256(payload).hexdigest() != record.get('sha256')
            or zlib.crc32(payload) & 0xFFFFFFFF != record.get('crc32')
        ):
            raise FieldReturnError(
                f'container manifest mismatch: {path}'
            )
    if declared != set(entries):
        raise FieldReturnError(
            'container manifest does not cover the entry set'
        )
    root_data = entries.get(FIELD_RETURN_CONTAINER_ROOT_PATH)
    if root_data is None:
        raise FieldReturnError('field-return.json is missing')
    try:
        document = json.loads(root_data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FieldReturnError(
            f'field-return.json is not valid JSON: {exc}'
        )
    try:
        envelope = FieldReturnContainerDocument.model_validate(document)
    except ValueError as exc:
        raise FieldReturnError(
            f'invalid field-return container document: {exc}'
        ) from exc
    for ref in (*envelope.authority_documents, *envelope.evidence_assets):
        payload = entries.get(ref.path)
        if (
            payload is None
            or len(payload) != ref.bytes
            or sha256(payload).hexdigest() != ref.sha256
        ):
            raise FieldReturnError(
                'declared document ref does not resolve to verified '
                f'container bytes: {ref.path}'
            )
    return entries, envelope, document


_RETURN_DOC_BINDINGS: Mapping[str, tuple[str, str, str]] = {
    # container doc schema → (records key, identity field, ref namespace)
    'htdt.field_return.field-evidence': (
        'records',
        'evidence_id',
        'field_evidence',
    ),
    'htdt.field_return.settings-observations': (
        'observations',
        'observation_id',
        'settings_observation',
    ),
    'htdt.field_return.room-state-observations': (
        'observations',
        'observation_id',
        'room_state',
    ),
    'htdt.field_return.wiring-routes': (
        'routes',
        'route_id',
        'wiring_route',
    ),
    'htdt.field_return.inventory-items': (
        'inventory_items',
        'item_id',
        'inventory_item',
    ),
    'htdt.field_return.operator-profiles': (
        'operators',
        'operator_id',
        'operator',
    ),
    'htdt.field_return.instrument-profiles': (
        'instruments',
        'instrument_id',
        'instrument',
    ),
}

_CONTAINER_DOC_NAMESPACES = frozenset(
    namespace for _, _, namespace in _RETURN_DOC_BINDINGS.values()
)

# Binding-token namespaces that name authorities outside a field-return
# container (capture bundles, mission plans, calibration records) — they
# resolve elsewhere, never against `authority/`.
_EXTERNAL_BINDING_NAMESPACES = frozenset({
    'entity',
    'measurement',
    'capture_revision',
    'capture_session',
    'field_return',
    'task_item',
    'commissioning_check',
    'equipment',
    'calibration_plan_item',
    'surface',
    'mesh_anchor',
    'path',
    'frame',
})


class ResolvedReturnRef(BaseModel):
    """One `fulfilled_by` ref resolved against the retained artifact.

    ``resolved`` carries the typed record the ref names; ``evidence_asset``
    resolves a ``sha256:`` ref to a declared container payload;
    ``external`` honestly marks refs whose authority lives outside the
    artifact (capture-bundle records, plan items); ``unresolved`` means a
    container-bound namespace did not match any carried record — a real
    gap, never silently guessed.
    """

    model_config = ConfigDict(frozen=True)

    ref: str = Field(min_length=1)
    state: Literal['resolved', 'evidence_asset', 'external', 'unresolved']
    document_schema: str | None = None
    document_path: str | None = None
    record: dict | None = None
    asset_path: str | None = None
    detail: str | None = None


class ResolvedReturnTask(BaseModel):
    """One plan task's fulfillment refs resolved."""

    model_config = ConfigDict(frozen=True)

    task_id: str = Field(min_length=1)
    outcome: str = Field(min_length=1)
    refs: tuple[ResolvedReturnRef, ...] = ()


def _return_container_ref_index(
    entries: dict[str, bytes],
    envelope: FieldReturnContainerDocument,
) -> dict[str, tuple[str, str, dict]]:
    """Index every typed record a container carries by its binding ref.

    Documents are byte-verified by ``read_field_return_container`` before
    indexing — a record that parses here is integrity-pinned.
    """

    index: dict[str, tuple[str, str, dict]] = {}
    for declared in envelope.authority_documents:
        binding = _RETURN_DOC_BINDINGS.get(declared.schema)
        if binding is None:
            continue
        records_key, identity_field, namespace = binding
        payload = entries.get(declared.path)
        if payload is None:
            continue
        try:
            document = json.loads(payload.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        records = document.get(records_key)
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            identity = record.get(identity_field)
            if not isinstance(identity, str) or not identity:
                continue
            index[f'{namespace}:{identity}'] = (
                declared.schema,
                declared.path,
                record,
            )
    return index


def _resolve_return_ref(
    ref: str,
    index: Mapping[str, tuple[str, str, dict]],
    assets: Mapping[str, FieldReturnContainerAssetRef],
) -> ResolvedReturnRef:
    if ref.startswith('sha256:'):
        asset = assets.get(ref.split(':', 1)[1])
        if asset is not None:
            return ResolvedReturnRef(
                ref=ref,
                state='evidence_asset',
                asset_path=asset.path,
            )
        return ResolvedReturnRef(ref=ref, state='external')
    hit = index.get(ref)
    if hit is not None:
        doc_schema, doc_path, record = hit
        return ResolvedReturnRef(
            ref=ref,
            state='resolved',
            document_schema=doc_schema,
            document_path=doc_path,
            record=record,
        )
    namespace = ref.split(':', 1)[0]
    if namespace in _CONTAINER_DOC_NAMESPACES:
        return ResolvedReturnRef(
            ref=ref,
            state='unresolved',
            detail='container does not carry the named record',
        )
    if namespace in _EXTERNAL_BINDING_NAMESPACES:
        return ResolvedReturnRef(ref=ref, state='external')
    return ResolvedReturnRef(
        ref=ref,
        state='unresolved',
        detail='unknown ref namespace',
    )


def _stage_field_return_container(
    artifact: bytes,
    channel_project_ref: str | None = None,
    known_projects: Sequence[HTDTProjectReference] = (),
    *,
    declared_content_digest: str | None = None,
) -> StagedFieldReturn:
    digest = sha256(artifact).hexdigest()
    try:
        _entries, envelope, document = read_field_return_container(artifact)
    except (FieldReturnError, ValueError) as exc:
        return StagedFieldReturn(
            contribution_id=None,
            artifact_sha256=digest,
            validation_state='malformed',
            routing='unrouted',
            detail=str(exc),
        )
    # Wire-level cross-check: the sender's declared semantic digest must
    # equal the document's own content_digest — a divergent claim stages
    # silently otherwise and the receipt echoes the wrong value back.
    if (
        declared_content_digest is not None
        and declared_content_digest != envelope.content_digest
    ):
        return StagedFieldReturn(
            contribution_id=envelope.contribution_id,
            artifact_sha256=digest,
            validation_state='malformed',
            routing='unrouted',
            detail=(
                'artifact digest header disagrees with the container '
                'content digest'
            ),
        )
    manifest_json = _canonical_json(document)
    routing, matched = _channel_routing(channel_project_ref, known_projects)
    if envelope.schema_version not in SUPPORTED_FIELD_RETURN_CONTAINER_VERSIONS:
        return StagedFieldReturn(
            contribution_id=envelope.contribution_id,
            artifact_sha256=digest,
            validation_state='unsupported',
            routing='unrouted',
            mission_id=envelope.mission_id,
            plan_sha256=envelope.plan_sha256,
            manifest_json=manifest_json,
            detail=(
                'unsupported htdt.field_return schema_version: '
                f'{envelope.schema_version}; artifact preserved for a '
                'compatible HTDT release'
            ),
        )
    # Container root docs carry mission/plan identity but no project
    # reference — the pairing's declared scope is the routing signal.
    detail = (
        f'container {envelope.schema_version}: '
        f'{len(envelope.authority_documents)} authority documents, '
        f'{len(envelope.evidence_assets)} evidence assets'
    )
    if routing == 'channel_project_match':
        detail += '; scoped by pairing'
    return StagedFieldReturn(
        contribution_id=envelope.contribution_id,
        artifact_sha256=digest,
        validation_state='validated',
        routing=routing,
        matched_project_id=matched,
        mission_id=envelope.mission_id,
        plan_sha256=envelope.plan_sha256,
        manifest_json=manifest_json,
        detail=detail,
    )


def stage_field_return_artifact(
    artifact: bytes,
    known_projects: Sequence[HTDTProjectReference] = (),
    *,
    channel_project_ref: str | None = None,
    declared_content_digest: str | None = None,
) -> StagedFieldReturn:
    """Stage whichever wire form was handed in: the emitted
    ``.htdtfieldreturn`` container (stored ZIP), or a bare
    ``htdt.field-return`` JSON manifest. ``channel_project_ref`` is the
    delivery pairing's declared project scope — the routing signal for
    artifacts that carry no project reference of their own.
    ``declared_content_digest`` is the sender's claimed semantic digest
    (the wire ``X-HTDT-Artifact-Digest`` header): container-form
    artifacts must carry a matching document ``content_digest``."""

    if artifact[:4] == _STORED_ZIP_MAGIC:
        return _stage_field_return_container(
            artifact,
            channel_project_ref,
            known_projects,
            declared_content_digest=declared_content_digest,
        )
    return stage_field_return(
        artifact, known_projects, channel_project_ref=channel_project_ref
    )


def stage_field_return(
    artifact: bytes,
    known_projects: Sequence[HTDTProjectReference] = (),
    *,
    channel_project_ref: str | None = None,
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
    elif manifest.project is None:
        routing, matched = _channel_routing(
            channel_project_ref, known_projects
        )
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
        *,
        channel_project_ref: str | None = None,
    ) -> StagedFieldReturn:
        """Persist staging state with deterministic duplicate semantics."""

        staged, _created = self.stage_artifact(
            artifact, known_projects, channel_project_ref=channel_project_ref
        )
        return staged

    def stage_artifact(
        self,
        artifact: bytes,
        known_projects: Sequence[HTDTProjectReference] = (),
        *,
        channel_project_ref: str | None = None,
        declared_content_digest: str | None = None,
    ) -> tuple[StagedFieldReturn, bool]:
        """Stage the emitted wire form and report whether a new row landed.

        Returns ``(staged, created)`` — ``created`` is False for an exact
        duplicate already on the ledger, for a contribution-less malformed
        artifact, or for a staged-but-not-inserted diagnostic.
        """

        return self._stage_entry(
            stage_field_return_artifact(
                artifact,
                known_projects,
                channel_project_ref=channel_project_ref,
                declared_content_digest=declared_content_digest,
            ),
            artifact,
        )

    def stage_prepared(
        self,
        staged: StagedFieldReturn,
        artifact: bytes,
    ) -> tuple[StagedFieldReturn, bool]:
        """Persist a ``StagedFieldReturn`` built by the caller.

        Delivery lanes that must gate persistence on checks that only
        resolve after parsing (e.g. a header artifact id that must equal
        the contribution identity) build the staged record first with
        ``stage_field_return_artifact`` and only persist through here once
        every gate has passed.
        """

        return self._stage_entry(staged, artifact)

    def _stage_entry(
        self,
        staged: StagedFieldReturn,
        artifact: bytes,
    ) -> tuple[StagedFieldReturn, bool]:
        if staged.contribution_id is None:
            return staged, False
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                '''
                SELECT contribution_id, artifact_sha256, validation_state
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
                    # Self-heal a malformed row: the malformation may be
                    # declaration-driven (e.g. a wrong declared digest
                    # header), not byte-driven — identical bytes that now
                    # pass validation upgrade the parked row instead of
                    # reporting already_staged over a dead record.
                    if (
                        existing['validation_state'] == 'malformed'
                        and staged.validation_state != 'malformed'
                    ):
                        connection.execute(
                            '''
                            UPDATE field_return_contributions
                            SET validation_state=?, routing=?,
                                matched_project_id=?, mission_id=?,
                                plan_sha256=?, manifest_json=?,
                                detail=?
                            WHERE contribution_id=?
                            ''',
                            (
                                staged.validation_state,
                                staged.routing,
                                staged.matched_project_id,
                                staged.mission_id,
                                staged.plan_sha256,
                                staged.manifest_json,
                                staged.detail,
                                staged.contribution_id,
                            ),
                        )
                        store_content_blob(
                            connection,
                            artifact,
                            expected_sha256=staged.artifact_sha256,
                        )
                        return staged, True
                    # Rows staged before byte retention get their blob
                    # healed on redelivery (INSERT OR IGNORE — idempotent).
                    store_content_blob(
                        connection,
                        artifact,
                        expected_sha256=staged.artifact_sha256,
                    )
                    return staged, False
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
            # Keep the artifact bytes: applied record_refs resolve into
            # this container — discarding them left the binding dead.
            store_content_blob(
                connection,
                artifact,
                expected_sha256=staged.artifact_sha256,
            )
        return staged, True

    def artifact_bytes(
        self, contribution_id: str
    ) -> bytes | None:
        """The staged artifact's bytes, or None when never retained.

        Contributions staged before byte retention existed report
        ``None`` — the row's sha pins the identity either way.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                '''
                SELECT artifact_sha256 FROM field_return_contributions
                WHERE contribution_id=?
                ''',
                (contribution_id,),
            ).fetchone()
            if row is None:
                return None
            ensure_content_blob_store(connection)
            blob_row = connection.execute(
                'SELECT payload_blob, payload_sha256, byte_count '
                'FROM htdt_content_blobs WHERE payload_sha256=?',
                (row['artifact_sha256'],),
            ).fetchone()
        if blob_row is None:
            return None
        payload = bytes(blob_row['payload_blob'])
        if (
            len(payload) != blob_row['byte_count']
            or sha256(payload).hexdigest()
            != blob_row['payload_sha256']
        ):
            raise FieldReturnError(
                'stored field-return artifact fails integrity check'
            )
        return payload

    def resolve_return_refs(
        self, contribution_id: str
    ) -> tuple[ResolvedReturnTask, ...] | None:
        """Resolve every task's `fulfilled_by` refs to what they name.

        ``None`` when the row is unknown, its bytes were never retained,
        or the artifact is neither wire family — the caller surfaces that
        honestly rather than fabricating resolution.
        """

        artifact = self.artifact_bytes(contribution_id)
        if artifact is None:
            return None
        if artifact[:4] == _STORED_ZIP_MAGIC:
            entries, envelope, _root = read_field_return_container(
                artifact
            )
            index = _return_container_ref_index(entries, envelope)
            assets = {
                asset.sha256: asset
                for asset in envelope.evidence_assets
            }
            tasks: list[ResolvedReturnTask] = []
            for ledger_entry in envelope.task_fulfillment_ledger:
                item_ref = str(ledger_entry.get('item_ref') or '')
                task_id = (
                    item_ref.split(':', 1)[1]
                    if item_ref.startswith('task_item:')
                    else item_ref
                )
                refs = tuple(
                    _resolve_return_ref(str(ref), index, assets)
                    for ref in (
                        ledger_entry.get('fulfilled_by_refs') or ()
                    )
                )
                tasks.append(
                    ResolvedReturnTask(
                        task_id=task_id or item_ref or 'task',
                        outcome=str(
                            ledger_entry.get('outcome') or 'unknown'
                        ),
                        refs=refs,
                    )
                )
            return tuple(tasks)
        try:
            document = json.loads(artifact.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            return None
        if not (
            isinstance(document, dict)
            and document.get('schema') == 'htdt.field-return'
        ):
            return None
        manifest = validate_field_return(document)
        records = {record.record_id: record for record in manifest.records}
        tasks = []
        for outcome in manifest.task_outcomes:
            refs: tuple[ResolvedReturnRef, ...] = ()
            if outcome.fulfilled_by_ref:
                record = records.get(outcome.fulfilled_by_ref)
                if record is None:
                    refs = (
                        ResolvedReturnRef(
                            ref=outcome.fulfilled_by_ref,
                            state='unresolved',
                            detail='manifest carries no such record',
                        ),
                    )
                else:
                    refs = (
                        ResolvedReturnRef(
                            ref=outcome.fulfilled_by_ref,
                            state='resolved',
                            record=record.model_dump(mode='json'),
                        ),
                    )
            tasks.append(
                ResolvedReturnTask(
                    task_id=outcome.task_id,
                    outcome=outcome.outcome,
                    refs=refs,
                )
            )
        return tuple(tasks)

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
            ensure_content_blob_store(connection)
            return self._from_row(connection, row)

    def _from_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> StagedFieldReturn:
        retained = connection.execute(
            'SELECT 1 FROM htdt_content_blobs WHERE payload_sha256=?',
            (row['artifact_sha256'],),
        ).fetchone() is not None
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
            recorded_at_utc=row['recorded_at_utc'],
            artifact_retained=retained,
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
            ensure_content_blob_store(connection)
            return tuple(
                self._from_row(connection, row) for row in rows
            )
