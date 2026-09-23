from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .build_info import get_build_info


class CompatibilityError(ValueError):
    """A compatibility artifact could not be evaluated safely."""


class ProtocolSupportEntry(BaseModel):
    """Exact version set one product supports for one protocol kind."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: str = Field(min_length=1)
    supported_versions: tuple[int, ...] = Field(min_length=1)

    @field_validator('supported_versions')
    @classmethod
    def validate_versions(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(version < 1 for version in value):
            raise ValueError('protocol versions are >= 1')
        if len(set(value)) != len(value):
            raise ValueError('duplicate supported version')
        return tuple(sorted(value))

    @property
    def minimum_version(self) -> int:
        return min(self.supported_versions)

    @property
    def maximum_version(self) -> int:
        return max(self.supported_versions)


class ProtocolSupportRegistry(BaseModel):
    """Machine-readable registry backing capability checks and the advisor.

    This is the single support surface both the capability handshake and the
    user-facing Compatibility Advisor read, so CI, receiver negotiation, and
    UI guidance cannot drift on independent version tables.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema: Literal['htdt.protocol-support-registry'] = (
        'htdt.protocol-support-registry'
    )
    registry_version: int = Field(ge=1)
    entries: tuple[ProtocolSupportEntry, ...]

    @model_validator(mode='after')
    def validate_kinds(self) -> 'ProtocolSupportRegistry':
        kinds = [entry.kind for entry in self.entries]
        if len(set(kinds)) != len(kinds):
            raise ValueError('duplicate protocol kind in registry')
        return self

    def entry(self, kind: str) -> ProtocolSupportEntry | None:
        for item in self.entries:
            if item.kind == kind:
                return item
        return None

    def supports(self, kind: str, version: int) -> bool:
        entry = self.entry(kind)
        return entry is not None and version in entry.supported_versions


class ProductCompatibilityIdentity(BaseModel):
    """Build/protocol capability identity a product exposes for diagnosis."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema: Literal['htdt.product-compatibility'] = 'htdt.product-compatibility'
    schema_version: Literal[1] = 1
    product: str = Field(min_length=1)
    app_version: str = Field(min_length=1)
    build_identity: str | None = Field(default=None, min_length=1)
    registry: ProtocolSupportRegistry


class ArtifactProtocolRequirement(BaseModel):
    """One protocol requirement a producer artifact places on a consumer."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: str = Field(min_length=1)
    version: int = Field(ge=1)
    role: Literal['required', 'optional'] = 'required'


CompatibilityStatus = Literal[
    'supported',
    'supported_with_degraded_optional_features',
    'update_consumer_required',
    'update_producer_required',
    'unsupported_historical_format',
    'malformed_or_identity_invalid',
    'unknown',
]


class CompatibilityFinding(BaseModel):
    """Per-requirement evaluation detail."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: str
    required_version: int
    role: Literal['required', 'optional']
    state: Literal[
        'supported',
        'unknown_protocol_kind',
        'version_newer_than_consumer',
        'version_older_than_supported',
    ]
    supported_versions: tuple[int, ...] = ()


class CompatibilityVerdict(BaseModel):
    """Advisor result for one artifact vs one consumer identity."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    status: CompatibilityStatus
    findings: tuple[CompatibilityFinding, ...]
    degraded_optional_kinds: tuple[str, ...] = ()

    @property
    def consumer_update_required(self) -> bool:
        return self.status == 'update_consumer_required'

    @property
    def producer_update_required(self) -> bool:
        return self.status == 'update_producer_required'


def evaluate_compatibility(
    requirements: tuple[ArtifactProtocolRequirement, ...] | list[ArtifactProtocolRequirement],
    consumer: ProductCompatibilityIdentity | ProtocolSupportRegistry,
) -> CompatibilityVerdict:
    """Evaluate artifact protocol requirements against consumer support.

    Version comparison is exact-set, never app-version arithmetic: a consumer
    supports a requirement only when its registry lists that exact version.
    Optional requirements degrade explicitly instead of failing the artifact.
    """

    registry = (
        consumer.registry
        if isinstance(consumer, ProductCompatibilityIdentity)
        else consumer
    )
    findings: list[CompatibilityFinding] = []
    for requirement in requirements:
        entry = registry.entry(requirement.kind)
        if entry is None:
            state = 'unknown_protocol_kind'
            supported: tuple[int, ...] = ()
        elif requirement.version in entry.supported_versions:
            state = 'supported'
            supported = entry.supported_versions
        elif requirement.version > entry.maximum_version:
            state = 'version_newer_than_consumer'
            supported = entry.supported_versions
        else:
            state = 'version_older_than_supported'
            supported = entry.supported_versions
        findings.append(
            CompatibilityFinding(
                kind=requirement.kind,
                required_version=requirement.version,
                role=requirement.role,
                state=state,
                supported_versions=supported,
            )
        )

    required_newer = [
        item
        for item in findings
        if item.role == 'required'
        and item.state in {'unknown_protocol_kind', 'version_newer_than_consumer'}
    ]
    if required_newer:
        return CompatibilityVerdict(
            status='update_consumer_required',
            findings=tuple(findings),
        )
    required_older = [
        item
        for item in findings
        if item.role == 'required' and item.state == 'version_older_than_supported'
    ]
    if required_older:
        return CompatibilityVerdict(
            status='unsupported_historical_format',
            findings=tuple(findings),
        )
    degraded = tuple(
        item.kind
        for item in findings
        if item.role == 'optional' and item.state != 'supported'
    )
    if degraded:
        return CompatibilityVerdict(
            status='supported_with_degraded_optional_features',
            findings=tuple(findings),
            degraded_optional_kinds=degraded,
        )
    if all(item.state == 'supported' for item in findings):
        return CompatibilityVerdict(
            status='supported',
            findings=tuple(findings),
        )
    return CompatibilityVerdict(
        status='unknown',
        findings=tuple(findings),
    )


def describe_update_guidance(
    verdict: CompatibilityVerdict,
    consumer: ProductCompatibilityIdentity,
    *,
    artifact_label: str,
) -> str:
    """Actionable user-facing guidance identifying the mismatched side.

    Never labels malformed data as 'please update': that status is produced
    only by artifact classification, not version evaluation, and this helper
    surfaces it verbatim.
    """

    product = consumer.product
    installed = consumer.app_version
    if verdict.status == 'supported':
        return f'{artifact_label} is fully supported by {product} {installed}.'
    if verdict.status == 'supported_with_degraded_optional_features':
        kinds = ', '.join(verdict.degraded_optional_kinds)
        return (
            f'{artifact_label} is supported by {product} {installed}, but '
            f'these optional capabilities are not: {kinds}. '
            'The artifact can be used with those features degraded.'
        )
    lines = [
        f'{artifact_label} cannot be fully used by {product} {installed}.',
    ]
    for finding in verdict.findings:
        if finding.state == 'supported':
            continue
        if finding.role == 'optional':
            continue
        supported = (
            ', '.join(str(v) for v in finding.supported_versions)
            if finding.supported_versions
            else 'none'
        )
        lines.append(
            f'- {finding.kind}: requires version '
            f'{finding.required_version}; {product} {installed} '
            f'supports: {supported}.'
        )
    if verdict.status == 'update_consumer_required':
        lines.append(
            f'Update {product} to a release that supports the required '
            'protocol versions. The source artifact was not changed.'
        )
    elif verdict.status == 'unsupported_historical_format':
        lines.append(
            'The artifact was produced by an older format this product no '
            'longer accepts. Regenerate it with a supported producer release.'
        )
    elif verdict.status == 'malformed_or_identity_invalid':
        lines.append(
            'The artifact is malformed or has invalid identity; this is not '
            'an update problem.'
        )
    return '\n'.join(lines)


# Canonical support surface for the protocols this repository produces and
# consumes. Both the capability handshake (#593/#374) and the Compatibility
# Advisor read this registry so neither invents its own version table.
HTDT_PROTOCOL_REGISTRY = ProtocolSupportRegistry(
    registry_version=1,
    entries=(
        ProtocolSupportEntry(
            kind='htdt.capture.bundle',
            supported_versions=(1,),
        ),
        ProtocolSupportEntry(
            kind='htdt.capture.ingestion-plan',
            supported_versions=(1,),
        ),
        ProtocolSupportEntry(
            kind='htdt.capture.task-plan',
            supported_versions=(1,),
        ),
        ProtocolSupportEntry(
            kind='htdt.capture.mission-package',
            supported_versions=(1,),
        ),
        ProtocolSupportEntry(
            kind='htdt.capture.mission-baseline',
            supported_versions=(1,),
        ),
        ProtocolSupportEntry(
            kind='htdt.capture.supplemental-document',
            supported_versions=(1,),
        ),
        ProtocolSupportEntry(
            kind='htdt.equipment.catalog-snapshot',
            supported_versions=(1,),
        ),
        ProtocolSupportEntry(
            kind='htdt.field-return',
            supported_versions=(1,),
        ),
        ProtocolSupportEntry(
            kind='htdt.project-reference',
            supported_versions=(1,),
        ),
        ProtocolSupportEntry(
            kind='htdt.project-destination',
            supported_versions=(1,),
        ),
    ),
)


def htdt_product_identity() -> ProductCompatibilityIdentity:
    """Compatibility identity for this HTDT build (diagnostics exportable)."""

    build = get_build_info()
    return ProductCompatibilityIdentity(
        product='htdt',
        app_version=build.version,
        build_identity=build.display_version,
        registry=HTDT_PROTOCOL_REGISTRY,
    )
