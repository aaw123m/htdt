"""Standards source registry and profile-coverage evaluation (#807).

Not a standards library — a registry + coverage evaluator answering
exactly which HTDT functionality rests on which documented sources, how
well each profile covers what it claims, and what remains ambiguous or
licensing-blocked. Sources are provenance/license/policy authority,
never acoustic truth.

- ``StandardsSourceRecord`` pins one source's identity + authoritative
  scope + usability boundaries (retention/licensing/redistribution).
- ``CriterionCoverageEntry`` records one stated criterion against the
  HTDT behavior that implements it — NOT_YET_IMPLEMENTED,
  NOT_APPLICABLE, SOURCE_AMBIGUOUS and UNSUPPORTED are all explicit
  states, never silently absorbed into a "supported" story.
- ``ProfileSourceRegistry`` pins which sources a profile depends on
  plus which it explicitly does not use; home-playback consumer sources
  stay separate from studio/mix-room production sources per the
  project's home-playback scope.
- ``StandardsCoverageReport`` explains coverage per profile:
  COMPLETE_FOR_DECLARED_SCOPE / PARTIAL / SOURCE_UNAVAILABLE /
  LICENSE_RESTRICTED / NEEDS_REVIEW / SUPERSEDED — never "100%
  certified".
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


StandardsProductDomain = Literal[
    'home_playback',
    'studio_mix_room',
    'generic_measurement',
]
StandardsSourceLicense = Literal[
    'free_technical_reference',
    'purchased_standard',
    'mixed_availability',
    'user_supplied',
    'restricted_internal',
]
StandardsReusePolicy = Literal[
    'definition_only',
    'methodological_input',
    'criteria_source',
    'operational_runtime_input',
    'UI_display_only',
    'prohibited_or_blocked',
]
CriterionCoverageStatus = Literal[
    'IMPLEMENTED',
    'NOT_YET_IMPLEMENTED',
    'NOT_APPLICABLE',
    'SOURCE_AMBIGUOUS',
    'UNSUPPORTED',
]
ProfileCoverageStatus = Literal[
    'COMPLETE_FOR_DECLARED_SCOPE',
    'PARTIAL',
    'SOURCE_UNAVAILABLE',
    'LICENSE_RESTRICTED',
    'NEEDS_REVIEW',
    'SUPERSEDED',
]

SOURCE_REGISTRY_SCHEMA_VERSION = 1


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: dict[str, Any]) -> str:
    return sha256(_canonical(payload).encode('utf-8')).hexdigest()


class StandardsSourceRecord(BaseModel):
    """Identity + authoritative scope of one standards document."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    source_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    version_or_edition: str = Field(min_length=1)
    domains: tuple[StandardsProductDomain, ...] = Field(min_length=1)
    authoritative_scope: str = Field(min_length=1)
    license_class: StandardsSourceLicense
    acceptable_use_notes: str = ''
    authoritative_reference: str | None = None
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_source(self) -> 'StandardsSourceRecord':
        if not self.source_id.startswith('standards-source:'):
            raise ValueError('source id must use standards-source: prefix')
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('standards source hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'source_id': self.source_id,
            'title': self.title,
            'publisher': self.publisher,
            'version_or_edition': self.version_or_edition,
            'domains': list(self.domains),
            'authoritative_scope': self.authoritative_scope,
            'license_class': self.license_class,
            'acceptable_use_notes': self.acceptable_use_notes,
            'authoritative_reference': self.authoritative_reference,
        }


class CriterionCoverageEntry(BaseModel):
    """One documented criterion ↔ HTDT behavior coverage row (#807 §4)."""

    model_config = ConfigDict(frozen=True)

    criterion_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    coverage: CriterionCoverageStatus
    implemented_by: tuple[str, ...] = ()
    related_artifact_ids: tuple[str, ...] = ()
    missing_behavior: str | None = None
    ambiguity_note: str | None = None
    unsupported_reason: str | None = None

    @model_validator(mode='after')
    def consistent_coverage(self) -> 'CriterionCoverageEntry':
        if self.coverage == 'IMPLEMENTED' and not self.implemented_by:
            raise ValueError('implemented criteria must name artifacts')
        if self.coverage == 'UNSUPPORTED' and not self.unsupported_reason:
            raise ValueError('unsupported criteria need a reason')
        if self.coverage == 'SOURCE_AMBIGUOUS' and not self.ambiguity_note:
            raise ValueError('ambiguous criteria need an ambiguity note')
        return self


class ProfileSourceRegistry(BaseModel):
    """Which sources a calibration/feature profile depends on — plus
    which it explicitly does not use (#807 §5). Home-playback sources
    stay separate from mix-room/production sources."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    registry_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    product_domain: StandardsProductDomain
    declared_scope: str = Field(min_length=1)
    required_source_ids: tuple[str, ...] = ()
    optional_source_ids: tuple[str, ...] = ()
    excluded_source_ids: tuple[str, ...] = ()
    criteria: tuple[CriterionCoverageEntry, ...] = ()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_registry(self) -> 'ProfileSourceRegistry':
        if not self.registry_id.startswith('standards-registry:'):
            raise ValueError('registry id must use standards-registry: prefix')
        overlap = set(self.required_source_ids) & set(self.excluded_source_ids)
        if overlap:
            raise ValueError(f'sources both required and excluded: {overlap}')
        for entry in self.criteria:
            known = (
                set(self.required_source_ids)
                | set(self.optional_source_ids)
                | set(self.excluded_source_ids)
            )
            if entry.source_id not in known:
                raise ValueError(
                    f'criterion {entry.criterion_id} cites unlisted source'
                )
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('profile source registry hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'registry_id': self.registry_id,
            'profile_id': self.profile_id,
            'profile_version': self.profile_version,
            'product_domain': self.product_domain,
            'declared_scope': self.declared_scope,
            'required_source_ids': list(self.required_source_ids),
            'optional_source_ids': list(self.optional_source_ids),
            'excluded_source_ids': list(self.excluded_source_ids),
            'criteria': [
                entry.model_dump(mode='json') for entry in self.criteria
            ],
        }


class ProfileCoverageRow(BaseModel):
    """Coverage verdict for one profile with gaps made explicit."""

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    status: ProfileCoverageStatus
    missing_sources: tuple[str, ...] = ()
    license_blocked_sources: tuple[str, ...] = ()
    unimplemented_criteria: tuple[str, ...] = ()
    ambiguous_criteria: tuple[str, ...] = ()
    unsupported_criteria: tuple[str, ...] = ()
    domain_violations: tuple[str, ...] = ()
    detail: str = ''


class StandardsCoverageReport(BaseModel):
    """Derived coverage evaluation — deterministic over sources +
    registries, honest about PARTIAL/UNAVAILABLE/RESTRICTED states."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    report_id: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    rows: tuple[ProfileCoverageRow, ...]
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_report(self) -> 'StandardsCoverageReport':
        if not self.report_id.startswith('standards-report:'):
            raise ValueError('report id must use standards-report: prefix')
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('coverage report hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'report_id': self.report_id,
            'created_at_utc': self.created_at_utc,
            'rows': [row.model_dump(mode='json') for row in self.rows],
        }


_LICENSE_BLOCKED = frozenset({'purchased_standard', 'restricted_internal'})


def evaluate_coverage(
    sources: tuple[StandardsSourceRecord, ...],
    registries: tuple[ProfileSourceRegistry, ...],
    *,
    report_id: str,
    created_at_utc: str,
) -> StandardsCoverageReport:
    """Evaluate each profile's coverage against the source registry."""
    by_id = {source.source_id: source for source in sources}
    rows: list[ProfileCoverageRow] = []
    for registry in registries:
        missing: list[str] = []
        license_blocked: list[str] = []
        domain_violations: list[str] = []
        for sid in registry.required_source_ids:
            source = by_id.get(sid)
            if source is None:
                missing.append(sid)
                continue
            if source.license_class in _LICENSE_BLOCKED:
                license_blocked.append(sid)
            if (
                registry.product_domain != 'generic_measurement'
                and source.domains
                and registry.product_domain not in source.domains
            ):
                domain_violations.append(sid)
        unimplemented = [
            e.criterion_id
            for e in registry.criteria
            if e.coverage == 'NOT_YET_IMPLEMENTED'
        ]
        ambiguous = [
            e.criterion_id
            for e in registry.criteria
            if e.coverage == 'SOURCE_AMBIGUOUS'
        ]
        unsupported = [
            e.criterion_id
            for e in registry.criteria
            if e.coverage == 'UNSUPPORTED'
        ]
        if missing:
            status: ProfileCoverageStatus = 'SOURCE_UNAVAILABLE'
        elif license_blocked:
            status = 'LICENSE_RESTRICTED'
        elif domain_violations:
            status = 'NEEDS_REVIEW'
        elif unimplemented or ambiguous or unsupported:
            status = 'PARTIAL'
        else:
            status = 'COMPLETE_FOR_DECLARED_SCOPE'
        rows.append(
            ProfileCoverageRow(
                profile_id=registry.profile_id,
                status=status,
                missing_sources=tuple(missing),
                license_blocked_sources=tuple(license_blocked),
                unimplemented_criteria=tuple(unimplemented),
                ambiguous_criteria=tuple(ambiguous),
                unsupported_criteria=tuple(unsupported),
                domain_violations=tuple(domain_violations),
                detail=(
                    'coverage evaluated against declared scope only; '
                    'this is not a certification claim'
                ),
            )
        )
    payload: dict[str, Any] = {
        'report_id': report_id,
        'created_at_utc': created_at_utc,
        'rows': tuple(rows),
    }
    provisional = StandardsCoverageReport.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return StandardsCoverageReport.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


__all__ = [
    'CriterionCoverageEntry',
    'CriterionCoverageStatus',
    'ProfileCoverageRow',
    'ProfileCoverageStatus',
    'ProfileSourceRegistry',
    'SOURCE_REGISTRY_SCHEMA_VERSION',
    'StandardsCoverageReport',
    'StandardsProductDomain',
    'StandardsSourceLicense',
    'StandardsSourceRecord',
    'StandardsReusePolicy',
    'evaluate_coverage',
]
