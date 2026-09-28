"""Reference acoustic data acquisition registry (#779).

Reference libraries only stay useful when there is a repeatable process
for discovering, licensing, ingesting, validating, versioning and
maintaining new datasets. This module is the authority for the front half
of that pipeline:

```
candidate source -> legal/source review -> raw preservation -> import
-> semantic normalization -> review -> approval -> immutable version
```

Contracts:

- **Registry entries are immutable descriptors.** A source is recorded
  once with its URL/license/attribution facts; its *status* evolves only
  through appended ``SourceReviewDecision`` records — never by rewriting
  the descriptor.
- **Raw preservation is hash-exact.** ``RawSourceRecord`` pins filename +
  sha256 + retrieval date + source version; when redistribution is not
  allowed, ``raw_preserved=False`` records that only metadata/reference
  was retained and the user must import locally.
- **Importers declare contracts.** ``ImporterDeclaration`` makes every
  parser's format/version/domain/unit/quantity semantics and known
  limitations explicit — no ambiguous generic CSV parser guesses meaning.
- **User-import-only is a first-class disposition.** Proprietary datasets
  HTDT cannot redistribute stay usable: the importer contract exists, the
  dataset is marked ``user_import``/``USER_IMPORT_ONLY``, and the local
  library entry keeps provenance without the source ever being committed.
- **Upstream changes are new versions.** ``UpstreamVersionCandidate``
  links upstream v_N to a candidate library version requiring explicit
  approval — the approved library version A is never rewritten.

Domain-specific quality policy (material coefficient bounds, polar
coverage, phase capability) stays owned by #771/#772 domain modules; this
registry only tracks provenance, legality, review and update lineage.
"""

from __future__ import annotations

import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash, canonicalize_payload


REGISTRY_SCHEMA_VERSION = 1
REGISTRY_AUTHORITY_VERSION = 'data-source-registry-1'


SourceDomain = Literal[
    'material',
    'speaker',
    'directivity',
    'output_capability',
    'other',
]
SOURCE_DOMAINS: frozenset[str] = frozenset(
    {'material', 'speaker', 'directivity', 'output_capability', 'other'}
)

# Suggested status vocabulary from the issue (#779 §1); stored lowercase.
SourceReviewStatus = Literal[
    'candidate',
    'legally_usable',
    'user_import_only',
    'rejected',
    'superseded',
    'unknown',
]
SOURCE_REVIEW_STATUSES: frozenset[str] = frozenset(
    {
        'candidate',
        'legally_usable',
        'user_import_only',
        'rejected',
        'superseded',
        'unknown',
    }
)

RedistributionStatus = Literal[
    'allowed',
    'forbidden',
    'unknown',
    'review_required',
]

AcquisitionDisposition = Literal[
    'bundle',
    'download_on_demand',
    'user_import',
    'link_only',
]

UpdateMechanism = Literal[
    'manual',
    'periodic_check',
    'upstream_version_watch',
    'none',
    'unknown',
]






class DataSourceRegistryEntry(BaseModel):
    """Immutable descriptor of one candidate reference-data source (#779 §1).

    Records the legally relevant facts — name, domain, reference, license,
    redistribution, attribution, automation and update mechanism — plus the
    *initial* review status. Status changes are appended as
    :class:`SourceReviewDecision` records; this row is never rewritten.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[REGISTRY_SCHEMA_VERSION] = REGISTRY_SCHEMA_VERSION
    authority_version: Literal[
        'data-source-registry-1'
    ] = REGISTRY_AUTHORITY_VERSION
    source_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    domain: SourceDomain
    url: str = Field(min_length=1)
    license_reference: str | None = None
    redistribution: RedistributionStatus = 'unknown'
    attribution_required: str | None = None
    automation_allowed: bool = False
    update_mechanism: UpdateMechanism = 'unknown'
    contact_record: str | None = None
    initial_review_status: SourceReviewStatus = 'candidate'
    disposition: AcquisitionDisposition = 'link_only'
    upstream_version: str | None = None
    upstream_date: str | None = None
    notes: str | None = None
    created_at_utc: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_entry(self) -> 'DataSourceRegistryEntry':
        if self.source_sha256 != _hash(self.semantic_payload()):
            raise ValueError('DataSourceRegistryEntry hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'source_id': self.source_id,
            'name': self.name,
            'domain': self.domain,
            'url': self.url,
            'license_reference': self.license_reference,
            'redistribution': self.redistribution,
            'attribution_required': self.attribution_required,
            'automation_allowed': self.automation_allowed,
            'update_mechanism': self.update_mechanism,
            'contact_record': self.contact_record,
            'initial_review_status': self.initial_review_status,
            'disposition': self.disposition,
            'upstream_version': self.upstream_version,
            'upstream_date': self.upstream_date,
            'notes': self.notes,
            'created_at_utc': self.created_at_utc,
        }


class RawSourceRecord(BaseModel):
    """Exact raw-artifact preservation record for one source (#779 §2).

    ``raw_preserved=False`` covers the legally-restricted case: only
    metadata/reference is retained and the user imports the artifact
    locally — the sha still pins what the user supplied.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[REGISTRY_SCHEMA_VERSION] = REGISTRY_SCHEMA_VERSION
    record_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    original_filename: str = Field(min_length=1)
    file_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    raw_preserved: bool = True
    retrieved_at_utc: str = Field(min_length=1)
    source_version: str | None = None
    source_url: str | None = None
    license_snapshot_reference: str | None = None
    created_at_utc: str = Field(min_length=1)
    record_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_record(self) -> 'RawSourceRecord':
        if self.record_sha256 != _hash(self.semantic_payload()):
            raise ValueError('RawSourceRecord hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'record_id': self.record_id,
            'source_id': self.source_id,
            'original_filename': self.original_filename,
            'file_sha256': self.file_sha256,
            'raw_preserved': self.raw_preserved,
            'retrieved_at_utc': self.retrieved_at_utc,
            'source_version': self.source_version,
            'source_url': self.source_url,
            'license_snapshot_reference': self.license_snapshot_reference,
            'created_at_utc': self.created_at_utc,
        }


class ImporterDeclaration(BaseModel):
    """Declared adapter contract for one dataset importer (#779 §3).

    Every importer states its supported format + versions, domain, required
    metadata, coordinate/unit conventions, quantity semantics and known
    limitations — normalization must be deterministic
    (``deterministic_normalization=True`` is asserted, not assumed).
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[REGISTRY_SCHEMA_VERSION] = REGISTRY_SCHEMA_VERSION
    importer_id: str = Field(min_length=1)
    supported_format: str = Field(min_length=1)
    format_versions: tuple[str, ...] = Field(min_length=1)
    domain: SourceDomain
    required_metadata: tuple[str, ...] = ()
    coordinate_convention: str | None = None
    unit_convention: str | None = None
    quantity_semantics: tuple[str, ...] = Field(min_length=1)
    known_limitations: tuple[str, ...] = ()
    parser_version: str = Field(min_length=1)
    deterministic_normalization: bool = True
    created_at_utc: str = Field(min_length=1)
    importer_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_importer(self) -> 'ImporterDeclaration':
        if len(self.format_versions) != len(set(self.format_versions)):
            raise ValueError('format_versions must be unique')
        if self.importer_sha256 != _hash(self.semantic_payload()):
            raise ValueError('ImporterDeclaration hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'importer_id': self.importer_id,
            'supported_format': self.supported_format,
            'format_versions': list(self.format_versions),
            'domain': self.domain,
            'required_metadata': list(self.required_metadata),
            'coordinate_convention': self.coordinate_convention,
            'unit_convention': self.unit_convention,
            'quantity_semantics': list(self.quantity_semantics),
            'known_limitations': list(self.known_limitations),
            'parser_version': self.parser_version,
            'deterministic_normalization': self.deterministic_normalization,
            'created_at_utc': self.created_at_utc,
        }


class SourceReviewDecision(BaseModel):
    """One appended registry-status transition (#779 §1/§5).

    Status evolves only by appending decisions — the registry's current
    status for a source is its latest decision's ``to_status`` (falling
    back to the descriptor's ``initial_review_status``).
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[REGISTRY_SCHEMA_VERSION] = REGISTRY_SCHEMA_VERSION
    decision_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    from_status: SourceReviewStatus
    to_status: SourceReviewStatus
    reviewer: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    reviewed_at_utc: str = Field(min_length=1)
    decision_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_decision(self) -> 'SourceReviewDecision':
        if self.from_status == self.to_status:
            raise ValueError('a review decision must change status')
        if self.decision_sha256 != _hash(self.semantic_payload()):
            raise ValueError('SourceReviewDecision hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'decision_id': self.decision_id,
            'source_id': self.source_id,
            'from_status': self.from_status,
            'to_status': self.to_status,
            'reviewer': self.reviewer,
            'rationale': self.rationale,
            'reviewed_at_utc': self.reviewed_at_utc,
        }


class DatasetReviewRecord(BaseModel):
    """Human review record for one bundled dataset/version (#779 §5).

    Captures the reviewed source, license state at review time, semantic
    mapping, applied transformations, known limitations, the importer used,
    and the reviewer/date — repository metadata for the provenance/legal
    approval stage before bundling.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[REGISTRY_SCHEMA_VERSION] = REGISTRY_SCHEMA_VERSION
    review_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    record_id: str | None = None
    importer_id: str | None = None
    license_status: RedistributionStatus
    semantic_mapping_json: str = Field(min_length=2)
    transformations: tuple[str, ...] = ()
    known_limitations: tuple[str, ...] = ()
    reviewer: str = Field(min_length=1)
    decision: Literal['approved', 'rejected', 'needs_changes', 'pending']
    reviewed_at_utc: str = Field(min_length=1)
    review_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_review(self) -> 'DatasetReviewRecord':
        try:
            json.loads(self.semantic_mapping_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f'malformed semantic_mapping_json: {exc}')
        if self.review_sha256 != _hash(self.semantic_payload()):
            raise ValueError('DatasetReviewRecord hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'review_id': self.review_id,
            'source_id': self.source_id,
            'record_id': self.record_id,
            'importer_id': self.importer_id,
            'license_status': self.license_status,
            'semantic_mapping_json': json.loads(self.semantic_mapping_json),
            'transformations': list(self.transformations),
            'known_limitations': list(self.known_limitations),
            'reviewer': self.reviewer,
            'decision': self.decision,
            'reviewed_at_utc': self.reviewed_at_utc,
        }


class UpstreamVersionCandidate(BaseModel):
    """Detected upstream change awaiting approval (#779 §6).

    ``upstream v_N -> candidate library version B``: the diff between the
    normalized semantic content of the current approved version and the
    candidate is recorded verbatim; approval creates a new immutable
    library version — version A is never rewritten.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[REGISTRY_SCHEMA_VERSION] = REGISTRY_SCHEMA_VERSION
    candidate_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    upstream_version: str = Field(min_length=1)
    upstream_date: str | None = None
    current_library_version: str | None = None
    candidate_content_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    diff_summary: str | None = None
    status: Literal[
        'pending_review', 'approved', 'rejected', 'superseded'
    ] = 'pending_review'
    created_at_utc: str = Field(min_length=1)
    candidate_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_candidate(self) -> 'UpstreamVersionCandidate':
        if self.candidate_sha256 != _hash(self.semantic_payload()):
            raise ValueError('UpstreamVersionCandidate hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'candidate_id': self.candidate_id,
            'source_id': self.source_id,
            'upstream_version': self.upstream_version,
            'upstream_date': self.upstream_date,
            'current_library_version': self.current_library_version,
            'candidate_content_sha256': self.candidate_content_sha256,
            'diff_summary': self.diff_summary,
            'status': self.status,
            'created_at_utc': self.created_at_utc,
        }


# -- builders ---------------------------------------------------------------


def build_registry_entry(
    *,
    name: str,
    domain: SourceDomain,
    url: str,
    created_at_utc: str,
    source_id: str | None = None,
    license_reference: str | None = None,
    redistribution: RedistributionStatus = 'unknown',
    attribution_required: str | None = None,
    automation_allowed: bool = False,
    update_mechanism: UpdateMechanism = 'unknown',
    contact_record: str | None = None,
    initial_review_status: SourceReviewStatus = 'candidate',
    disposition: AcquisitionDisposition = 'link_only',
    upstream_version: str | None = None,
    upstream_date: str | None = None,
    notes: str | None = None,
) -> DataSourceRegistryEntry:
    payload: dict[str, Any] = {
        'source_id': source_id or str(uuid4()),
        'name': name,
        'domain': domain,
        'url': url,
        'license_reference': license_reference,
        'redistribution': redistribution,
        'attribution_required': attribution_required,
        'automation_allowed': automation_allowed,
        'update_mechanism': update_mechanism,
        'contact_record': contact_record,
        'initial_review_status': initial_review_status,
        'disposition': disposition,
        'upstream_version': upstream_version,
        'upstream_date': upstream_date,
        'notes': notes,
        'created_at_utc': created_at_utc,
    }
    provisional = DataSourceRegistryEntry.model_construct(**canonicalize_payload(DataSourceRegistryEntry, dict(
        **payload, source_sha256='0' * 64
    )))
    return DataSourceRegistryEntry(
        **payload, source_sha256=_hash(provisional.semantic_payload())
    )


def build_raw_source_record(
    *,
    source_id: str,
    original_filename: str,
    file_sha256: str,
    retrieved_at_utc: str,
    created_at_utc: str,
    record_id: str | None = None,
    raw_preserved: bool = True,
    source_version: str | None = None,
    source_url: str | None = None,
    license_snapshot_reference: str | None = None,
) -> RawSourceRecord:
    payload: dict[str, Any] = {
        'record_id': record_id or str(uuid4()),
        'source_id': source_id,
        'original_filename': original_filename,
        'file_sha256': file_sha256,
        'raw_preserved': raw_preserved,
        'retrieved_at_utc': retrieved_at_utc,
        'source_version': source_version,
        'source_url': source_url,
        'license_snapshot_reference': license_snapshot_reference,
        'created_at_utc': created_at_utc,
    }
    provisional = RawSourceRecord.model_construct(**canonicalize_payload(RawSourceRecord, dict(
        **payload, record_sha256='0' * 64
    )))
    return RawSourceRecord(
        **payload, record_sha256=_hash(provisional.semantic_payload())
    )


def build_importer_declaration(
    *,
    supported_format: str,
    format_versions: tuple[str, ...],
    domain: SourceDomain,
    quantity_semantics: tuple[str, ...],
    parser_version: str,
    created_at_utc: str,
    importer_id: str | None = None,
    required_metadata: tuple[str, ...] = (),
    coordinate_convention: str | None = None,
    unit_convention: str | None = None,
    known_limitations: tuple[str, ...] = (),
    deterministic_normalization: bool = True,
) -> ImporterDeclaration:
    payload: dict[str, Any] = {
        'importer_id': importer_id or str(uuid4()),
        'supported_format': supported_format,
        'format_versions': format_versions,
        'domain': domain,
        'required_metadata': required_metadata,
        'coordinate_convention': coordinate_convention,
        'unit_convention': unit_convention,
        'quantity_semantics': quantity_semantics,
        'known_limitations': known_limitations,
        'parser_version': parser_version,
        'deterministic_normalization': deterministic_normalization,
        'created_at_utc': created_at_utc,
    }
    provisional = ImporterDeclaration.model_construct(**canonicalize_payload(ImporterDeclaration, dict(
        **payload, importer_sha256='0' * 64
    )))
    return ImporterDeclaration(
        **payload, importer_sha256=_hash(provisional.semantic_payload())
    )


def build_review_decision(
    *,
    source_id: str,
    from_status: SourceReviewStatus,
    to_status: SourceReviewStatus,
    reviewer: str,
    rationale: str,
    reviewed_at_utc: str,
    decision_id: str | None = None,
) -> SourceReviewDecision:
    payload: dict[str, Any] = {
        'decision_id': decision_id or str(uuid4()),
        'source_id': source_id,
        'from_status': from_status,
        'to_status': to_status,
        'reviewer': reviewer,
        'rationale': rationale,
        'reviewed_at_utc': reviewed_at_utc,
    }
    provisional = SourceReviewDecision.model_construct(
        **payload, decision_sha256='0' * 64
    )
    return SourceReviewDecision(
        **payload, decision_sha256=_hash(provisional.semantic_payload())
    )


def build_dataset_review(
    *,
    source_id: str,
    license_status: RedistributionStatus,
    semantic_mapping_json: str,
    reviewer: str,
    decision: Literal['approved', 'rejected', 'needs_changes', 'pending'],
    reviewed_at_utc: str,
    review_id: str | None = None,
    record_id: str | None = None,
    importer_id: str | None = None,
    transformations: tuple[str, ...] = (),
    known_limitations: tuple[str, ...] = (),
) -> DatasetReviewRecord:
    try:
        json.loads(semantic_mapping_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f'malformed semantic_mapping_json: {exc}')
    payload: dict[str, Any] = {
        'review_id': review_id or str(uuid4()),
        'source_id': source_id,
        'record_id': record_id,
        'importer_id': importer_id,
        'license_status': license_status,
        'semantic_mapping_json': semantic_mapping_json,
        'transformations': transformations,
        'known_limitations': known_limitations,
        'reviewer': reviewer,
        'decision': decision,
        'reviewed_at_utc': reviewed_at_utc,
    }
    provisional = DatasetReviewRecord.model_construct(
        **payload, review_sha256='0' * 64
    )
    return DatasetReviewRecord(
        **payload, review_sha256=_hash(provisional.semantic_payload())
    )


def build_upstream_candidate(
    *,
    source_id: str,
    upstream_version: str,
    candidate_content_sha256: str,
    created_at_utc: str,
    candidate_id: str | None = None,
    upstream_date: str | None = None,
    current_library_version: str | None = None,
    diff_summary: str | None = None,
    status: Literal[
        'pending_review', 'approved', 'rejected', 'superseded'
    ] = 'pending_review',
) -> UpstreamVersionCandidate:
    payload: dict[str, Any] = {
        'candidate_id': candidate_id or str(uuid4()),
        'source_id': source_id,
        'upstream_version': upstream_version,
        'upstream_date': upstream_date,
        'current_library_version': current_library_version,
        'candidate_content_sha256': candidate_content_sha256,
        'diff_summary': diff_summary,
        'status': status,
        'created_at_utc': created_at_utc,
    }
    provisional = UpstreamVersionCandidate.model_construct(
        **payload, candidate_sha256='0' * 64
    )
    return UpstreamVersionCandidate(
        **payload, candidate_sha256=_hash(provisional.semantic_payload())
    )


__all__ = [
    'AcquisitionDisposition',
    'DataSourceRegistryEntry',
    'DatasetReviewRecord',
    'ImporterDeclaration',
    'RawSourceRecord',
    'RedistributionStatus',
    'REGISTRY_AUTHORITY_VERSION',
    'REGISTRY_SCHEMA_VERSION',
    'SOURCE_DOMAINS',
    'SOURCE_REVIEW_STATUSES',
    'SourceDomain',
    'SourceReviewDecision',
    'SourceReviewStatus',
    'UpdateMechanism',
    'UpstreamVersionCandidate',
    'build_dataset_review',
    'build_importer_declaration',
    'build_raw_source_record',
    'build_registry_entry',
    'build_review_decision',
    'build_upstream_candidate',
]
