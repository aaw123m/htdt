"""#887 provider/device adapter SDK — the versioned contract layer.

Every external integration (measurement provider, DSP, AVR/processor,
discovery backend, file/installer lane) answers the same core questions
through one versioned contract record:

- what capabilities are available? — the adapter's
  :class:`~htdt.cad_device_adapter.AdapterCapabilityReport` is embedded
  verbatim and pinned by ``capability_sha256`` (the #878 manifest
  vocabulary is *reused*, never re-invented);
- what exact provider/device/version is bound? — ``adapter_id`` +
  ``adapter_version`` + ``device_family`` + ``compatibility_range``;
- which operations exist and which are read-only vs mutating? —
  ``supported_operations`` names every contract verb the adapter
  implements; anything absent is unreachable by contract;
- what is the mutation authorization boundary? — ``safety_invariants``
  declares the one-shot authorization rule, the operations forbidden to
  the adapter outright, and the rollback posture;
- what evidence strength can be claimed? — ``contract_status`` records
  the honest ceiling (``production`` / ``assisted_only`` /
  ``read_only`` / ``simulated``); a simulated lane can never read as a
  production authority.

The contract record is sealed: ``descriptor_sha256`` covers every field
above including the embedded capability report, so a capability or
firmware/applicability change is a *different descriptor* — stale
conformance can never be silently re-attributed.

Downstream gates use :func:`assert_conforming` — a typed, fail-closed
check that raises :class:`AdapterConformanceError` whenever the adapter
has no current conforming evidence: no result at all, a result pinned
to a different descriptor (adapter/SDK drift), a result minted under a
different suite version, or a non-conforming/unverifiable verdict.
``conformance_is_stale`` is the read-only variant for evaluators that
must rank paths without raising.

This module is the *adapter contract* layer only: it knows nothing about
vendor implementations or transports. The executable suite and the
per-family subjects live in ``cad_adapter_conformance.py``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_device_adapter import AdapterCapabilityReport, AdapterKind
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


#: Version of this SDK contract surface. Bumping it invalidates every
#: descriptor minted under an older contract — by design.
ADAPTER_SDK_CONTRACT_VERSION = 'adapter-sdk-1'

_SHA256 = r'^[0-9a-f]{64}$'


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Contract verbs an adapter may implement. ``read_only_probe`` is the
#: only lane that never mutates anything; ``apply`` is the sole device
#: mutation verb and always sits behind the authorization boundary.
AdapterOperation = Literal[
    'capability',
    'read_only_probe',
    'materialize',
    'apply',
    'read_back',
    'capture_baseline',
    'rollback',
    'observe_runtime',
    'discover',
    'install',
    'provider_gate',
]

#: Operation lanes a conformance subject drives. Kept distinct from
#: ``AdapterOperation``: lanes describe evidence reachability (e.g.
#: ``transport`` means the subject can inject transport faults).
AdapterLane = Literal[
    'capability',
    'binding',
    'transport',
    'materialize',
    'apply',
    'read_back',
    'rollback',
    'runtime',
    'file_export',
    'file_verify',
    'discovery',
    'provider_gate',
]

#: Honest ceiling of what the adapter can evidence on a real target.
AdapterContractStatus = Literal[
    'production',
    'assisted_only',
    'read_only',
    'simulated',
]

ConformanceVerdict = Literal[
    'conforming',
    'conforming_with_limitations',
    'non_conforming',
    'unverifiable',
]

#: Operations that may never appear in an adapter's surface. They exist
#: so a descriptor can make its safety boundary explicit — a declared
#: forbidden operation is contract-checked at descriptor mint.
FORBIDDEN_OPERATION_KINDS: tuple[str, ...] = (
    'ambient_lan_scan',
    'credential_storage',
    'credential_echo',
    'vendor_ui_automation',
    'unbounded_retry',
)


class AdapterContractError(RuntimeError):
    """Descriptor mint/use violation (fail-closed)."""


class AdapterConformanceError(RuntimeError):
    """An adapter may not act as conforming for the requested use.

    ``kind`` is machine-readable: ``unproven`` (no evidence),
    ``stale_descriptor`` / ``stale_suite`` (re-derivation required),
    ``non_conforming`` / ``unverifiable`` (verdict gate),
    ``insufficient_version`` (below the required contract version),
    ``limited`` (limitations present where the caller needs full).
    """

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f'{kind}: {detail}')
        self.kind = kind
        self.detail = detail


class AdapterSafetyInvariants(BaseModel):
    """The adapter's declared safety boundary — part of the seal.

    ``mutation_requires_one_shot_authorization`` states the rule every
    mutating lane obeys (#878 ``DeploymentOperatorAuthorization`` /
    ``operator_confirmed`` semantics); ``forbidden_operations`` names the
    operation classes the adapter may never perform;
    ``credential_boundary`` documents where credentials live (handles/
    vault names, never values); ``rollback_note`` records the recovery
    posture in operator-facing prose.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    mutation_requires_one_shot_authorization: bool = True
    forbidden_operations: tuple[str, ...] = ()
    credential_boundary: str = Field(min_length=1)
    rollback_note: str | None = None
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _validate(self) -> 'AdapterSafetyInvariants':
        unknown = [
            op for op in self.forbidden_operations
            if op not in FORBIDDEN_OPERATION_KINDS
        ]
        if unknown:
            raise ValueError(
                f'unknown forbidden operation kinds: {unknown} — '
                f'declared kinds are {FORBIDDEN_OPERATION_KINDS}'
            )
        if len(set(self.forbidden_operations)) != len(
            self.forbidden_operations
        ):
            raise ValueError('forbidden_operations must be unique')
        return self


class AdapterSdkContract(BaseModel):
    """Versioned SDK contract record for one adapter (``asd-``).

    The seal covers the adapter identity/version, the embedded
    capability manifest, the operation surface, the safety invariants
    and the compatibility pin — any of those changing is a different
    descriptor, so conformance bound to this sha is re-derivation-safe.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    descriptor_id: str = Field(min_length=1)
    descriptor_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    adapter_kind: AdapterKind
    device_family: str = Field(min_length=1)
    sdk_version: str = Field(min_length=1)
    #: The adapter's capability manifest embedded verbatim — the #878
    #: vocabulary, sealed inside the descriptor.
    capability_report: AdapterCapabilityReport
    capability_sha256: str = Field(pattern=_SHA256)
    #: Contract verbs the adapter implements. Absent verbs are
    #: unreachable by contract; the suite checks refused calls stay
    #: refused.
    supported_operations: tuple[AdapterOperation, ...]
    safety_invariants: AdapterSafetyInvariants
    contract_status: AdapterContractStatus
    #: Firmware/software compatibility pin this descriptor is valid for.
    compatibility_range: str | None = None
    declared_at_utc: str = Field(min_length=1)
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _validate(self) -> 'AdapterSdkContract':
        if len(set(self.supported_operations)) != len(
            self.supported_operations
        ):
            raise ValueError('supported_operations must be unique')
        if 'capability' not in self.supported_operations:
            raise ValueError(
                "every adapter contract must implement 'capability'"
            )
        declared_sha = _hash(
            self.capability_report.model_dump(mode='json')
        )
        if self.capability_sha256 != declared_sha:
            raise ValueError(
                'capability_sha256 does not cover the embedded manifest'
            )
        if (
            self.capability_report.adapter_id != self.adapter_id
            or self.capability_report.adapter_version != self.adapter_version
            or self.capability_report.adapter_kind != self.adapter_kind
            or self.capability_report.device_family != self.device_family
        ):
            raise ValueError(
                'embedded capability manifest disagrees with the '
                'descriptor identity fields'
            )
        if self.contract_status == 'simulated' and not (
            self.adapter_kind == 'simulated'
            or self.capability_report.protocol_authority == 'simulated'
        ):
            raise ValueError(
                "contract_status 'simulated' requires a simulated "
                'adapter kind or protocol authority'
            )
        if self.descriptor_sha256 != _hash(self.identity_payload()):
            raise ValueError('AdapterSdkContract hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'descriptor_id', 'descriptor_sha256'},
        )


def build_adapter_descriptor(
    *,
    document_id: str,
    capability: AdapterCapabilityReport,
    supported_operations: tuple[AdapterOperation, ...],
    safety_invariants: AdapterSafetyInvariants,
    contract_status: AdapterContractStatus,
    declared_at_utc: str,
    sdk_version: str = ADAPTER_SDK_CONTRACT_VERSION,
    compatibility_range: str | None = None,
    notes: tuple[str, ...] = (),
) -> AdapterSdkContract:
    """Seal one adapter's SDK contract record from its live manifest."""
    payload: dict[str, Any] = {
        'document_id': document_id,
        'adapter_id': capability.adapter_id,
        'adapter_version': capability.adapter_version,
        'adapter_kind': capability.adapter_kind,
        'device_family': capability.device_family,
        'sdk_version': sdk_version,
        'capability_report': capability,
        'capability_sha256': _hash(capability.model_dump(mode='json')),
        'supported_operations': tuple(supported_operations),
        'safety_invariants': safety_invariants,
        'contract_status': contract_status,
        'compatibility_range': compatibility_range,
        'declared_at_utc': declared_at_utc,
        'notes': tuple(notes),
    }
    probe = AdapterSdkContract.model_construct(
        **canonicalize_payload(AdapterSdkContract, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return AdapterSdkContract(
        descriptor_id=_semantic_id('asd', digest),
        descriptor_sha256=digest,
        **payload,
    )


def descriptor_ref(descriptor: AdapterSdkContract) -> AuthorityRef:
    """The pinnable reference downstream records carry."""
    return AuthorityRef(
        kind='adapter_sdk_contract',
        ref_id=descriptor.descriptor_id,
        ref_sha256=descriptor.descriptor_sha256,
    )


# ---------------------------------------------------------------------------
# Fail-closed conformance gate — what deployment/discovery consult.
# ---------------------------------------------------------------------------

def _version_tuple(version: str) -> tuple[int, ...]:
    """Parse 'adapter-sdk-1' / 'conformance-suite-2' into (1,) / (2,)."""
    tail = version.rsplit('-', 1)[-1]
    try:
        return (int(tail),)
    except (TypeError, ValueError):
        return (0,)


def conformance_is_stale(
    result: Any,
    descriptor: AdapterSdkContract,
    *,
    suite_version: str,
) -> bool:
    """True when the result can no longer certify this adapter.

    Stale = pinned to a different descriptor sha (adapter/contract
    drift) or minted under a different suite version. Anything the
    caller cannot identify is stale — the check is intentionally
    duck-typed so it works on persisted and freshly built results.
    """
    ref = getattr(result, 'descriptor_ref', None)
    if not isinstance(ref, AuthorityRef):
        return True
    if ref.ref_sha256 != descriptor.descriptor_sha256:
        return True
    return getattr(result, 'suite_version', None) != suite_version


def assert_conforming(
    descriptor: AdapterSdkContract,
    result: Any,
    *,
    minimum_version: str = ADAPTER_SDK_CONTRACT_VERSION,
    suite_version: str,
    allow_limitations: bool = True,
) -> None:
    """Gate an adapter's use on current conforming evidence.

    Fail closed in every direction: no result, a result bound to an
    older descriptor or suite, a verdict that is not positive, or a
    contract version below ``minimum_version`` all raise
    :class:`AdapterConformanceError`. ``allow_limitations=False`` turns
    ``conforming_with_limitations`` into a refusal for callers that
    require the unqualified verdict.
    """
    if _version_tuple(descriptor.sdk_version) < _version_tuple(
        minimum_version
    ):
        raise AdapterConformanceError(
            'insufficient_version',
            f'descriptor sdk_version {descriptor.sdk_version} is below '
            f'the required {minimum_version}',
        )
    if result is None:
        raise AdapterConformanceError(
            'unproven',
            f'adapter {descriptor.adapter_id} has no conformance '
            'evidence — unproven is never conforming',
        )
    if conformance_is_stale(
        result, descriptor, suite_version=suite_version,
    ):
        ref = getattr(result, 'descriptor_ref', None)
        if not isinstance(ref, AuthorityRef) or (
            ref.ref_sha256 != descriptor.descriptor_sha256
        ):
            raise AdapterConformanceError(
                'stale_descriptor',
                'conformance result pins a different descriptor sha — '
                'the adapter or its contract drifted; re-run the suite',
            )
        raise AdapterConformanceError(
            'stale_suite',
            f'conformance result was minted under suite '
            f'{getattr(result, "suite_version", "?")} — current suite '
            f'is {suite_version}; re-run for fresh evidence',
        )
    verdict = getattr(result, 'verdict', None)
    if verdict == 'non_conforming':
        raise AdapterConformanceError(
            'non_conforming',
            f'adapter {descriptor.adapter_id} failed required '
            'conformance scenarios',
        )
    if verdict == 'unverifiable':
        raise AdapterConformanceError(
            'unverifiable',
            f'adapter {descriptor.adapter_id} conformance could not be '
            'determined — unknown is never conforming',
        )
    if verdict == 'conforming_with_limitations' and not allow_limitations:
        raise AdapterConformanceError(
            'limited',
            f'adapter {descriptor.adapter_id} conforms only with '
            f'limitations: {getattr(result, "limitations", ())}',
        )
    if verdict not in ('conforming', 'conforming_with_limitations'):
        raise AdapterConformanceError(
            'unverifiable',
            f'unrecognized conformance verdict {verdict!r}',
        )


__all__ = [
    'ADAPTER_SDK_CONTRACT_VERSION',
    'FORBIDDEN_OPERATION_KINDS',
    'AdapterConformanceError',
    'AdapterContractError',
    'AdapterContractStatus',
    'AdapterLane',
    'AdapterOperation',
    'AdapterSafetyInvariants',
    'AdapterSdkContract',
    'ConformanceVerdict',
    'assert_conforming',
    'build_adapter_descriptor',
    'conformance_is_stale',
    'descriptor_ref',
]
