"""Delegated measurement-provider contract (#838 slice A).

Issue #838 splits responsibility: HTDT owns orchestration, exact identity,
provenance and before/after verification; specialist external tools own
measurement execution, live analysis and real-time DSP. A
**MeasurementProvider** is a delegated specialist — never copied into
HTDT core — bound by an exact identity/version/capability contract
rather than by vendor UI detail.

This module seals the provider side of that contract:

* :class:`DelegatedProviderManifest` (dpm-) — one provider declaration:
  provider class + exact provider/adapter identity, endpoint kind, and a
  *version-bound* capability manifest. Capability is a declared,
  observed-at-a-time fact, never permanent truth: a REW build change or
  adapter update is a different manifest seal. Licensing is a capability
  *condition* — ``licensed_required`` marks e.g. REW automated-sweep
  control, which needs a REW Pro upgrade; the free-tier assumption is
  structurally impossible because the gate below never maps
  ``licensed_required`` to ``provider_capable``.
* :class:`ProviderAcquisitionRecord` (dpa-) — one sealed acquisition:
  which manifest ran, which capability was exercised, the exact request
  identity, the machine-readable outcome (``observed`` / ``cancelled`` /
  ``error`` / ``capability_rejected``), the evidence refs produced, and
  the raw returned artifact's hash when one exists. Cancelled and
  errored attempts are first-class authority — a missing measurement is
  never silently dropped.

Fail-closed rules enforced by the models and the gate:

* an ``observed`` acquisition cannot be built for a capability the
  manifest does not declare ``supported`` — use
  ``capability_rejected`` and keep the gate reason;
* evidence refs must pin their sha256 (``_require_refs``) so an
  observation always binds exact returned data, not a screenshot;
* ``evidence_origin`` keeps ``provider_calculated`` /
  ``provider_imported`` / ``htdt_calculated`` / ``htdt_transformed``
  distinct — a value REW computed is never restated as an HTDT result;
* endpoint kinds stay explicit: ``localhost`` is the default binding,
  remote endpoints require operator-configured identity and are a
  different endpoint kind, never silently discovered.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


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
        **{sha_field: digest, id_field: _semantic_id(prefix, digest)},
    )


def _require_refs(*refs: AuthorityRef | None) -> None:
    for ref in refs:
        if ref is not None and ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')


def _require_iso8601(value: str, label: str) -> None:
    if not isinstance(value, str) or 'T' not in value:
        raise ValueError(f'{label} must be an ISO-8601 UTC timestamp')


MeasurementProviderClass = Literal[
    'rew_api', 'htdt_native_import', 'other_provider', 'unknown',
]
"""Provider families. ``rew_api`` is the REW localhost HTTP/OpenAPI
surface; ``htdt_native_import`` is the file-import path where HTDT itself
reads an externally produced file (the *file* is the provider output and
HTDT is its importer); ``other_provider`` keeps future delegated
providers (e.g. a qualified miniDSP/Smaart-class interface) possible
without a generic claim."""

DelegatedCapability = Literal[
    'measurement_list', 'frequency_response', 'impulse_response',
    'group_delay', 'rta_live', 'spl_leq', 'spl_logger',
    'generator_control', 'automated_sweep', 'eq_alignment',
    'trace_processing', 'subscriptions', 'file_import', 'other',
]
"""Machine-readable evidence/control classes a delegated provider can
supply, per issue #838 §1 (REW) — RTA/SPL/Leq machine-readable data maps
into #793 workflows."""

CapabilityCondition = Literal[
    'supported', 'licensed_required', 'unsupported', 'unknown',
]
"""Per-capability availability condition. ``licensed_required`` is a
capability/licensing condition, not a capability: REW automated sweep
measurement control requires a REW Pro upgrade, so HTDT must not assume
all REW automation is free-tier."""

ProviderEndpointKind = Literal[
    'localhost', 'remote_operator_configured', 'file', 'none',
]
"""How the provider is reached. ``localhost`` is the default surface;
``remote_operator_configured`` is only an explicitly configured
endpoint — HTDT never silently discovers LAN providers."""

ProviderGateVerdict = Literal[
    'provider_capable', 'provider_license_required', 'provider_blocked',
]

AcquisitionOutcome = Literal[
    'observed', 'cancelled', 'error', 'capability_rejected',
]

EvidenceOrigin = Literal[
    'provider_calculated', 'provider_imported', 'htdt_calculated',
    'htdt_transformed',
]
"""Where the values in an acquisition were produced. Every result must
retain whether it was calculated by HTDT, calculated by the provider,
imported from the provider, or transformed later (#838 §1)."""


DELEGATED_PROVIDER_LABELS: dict[str, str] = {
    'rew_api': 'REW API（委託測定プロバイダ）',
    'htdt_native_import': 'HTDT ファイルインポート',
    'other_provider': 'その他プロバイダ',
    'unknown': '不明',
    'measurement_list': '測定リスト',
    'frequency_response': '周波数応答',
    'impulse_response': 'インパルス応答',
    'group_delay': '群遅延',
    'rta_live': 'ライブRTA',
    'spl_leq': 'SPL/Leq',
    'spl_logger': 'SPLロガー',
    'generator_control': 'ジェネレータ制御',
    'automated_sweep': '自動スイープ測定',
    'eq_alignment': 'EQ/アライメント',
    'trace_processing': 'トレース演算/処理',
    'subscriptions': '変更購読',
    'file_import': 'ファイルインポート',
    'other': 'その他',
    'supported': '対応',
    'licensed_required': 'ライセンス必要（例: REW Pro）',
    'unsupported': '非対応',
    'provider_capable': 'プロバイダ利用可能',
    'provider_license_required': 'プロバイダ条件付き（ライセンス必要）',
    'provider_blocked': 'プロバイダ利用不可',
    'observed': '観測済み',
    'cancelled': 'キャンセル',
    'error': 'エラー',
    'capability_rejected': '機能条件により拒否',
    'provider_calculated': 'プロバイダ算出',
    'provider_imported': 'プロバイダからインポート',
    'htdt_calculated': 'HTDT算出',
    'htdt_transformed': 'HTDT変換済み',
    'localhost': 'localhost',
    'remote_operator_configured': 'リモート（オペレータ設定済み）',
    'file': 'ファイル',
    'none': 'なし',
}


class ProviderCapabilityEntry(BaseModel):
    """One declared capability + its condition, version-bound inside a
    manifest. ``observed`` marks that the running provider actually
    answered for this surface at session open; a documented-but-unseen
    capability stays declared with ``observed=False``."""

    model_config = ConfigDict(frozen=True)

    capability: DelegatedCapability
    condition: CapabilityCondition
    condition_detail: str | None = None
    observed: bool = False

    @model_validator(mode='after')
    def _validate(self) -> 'ProviderCapabilityEntry':
        if self.condition in ('licensed_required', 'unsupported') \
                and not self.condition_detail:
            raise ValueError(
                'licensed_required/unsupported capabilities must carry '
                'a condition_detail reason')
        if self.observed and self.condition == 'unsupported':
            raise ValueError(
                'an unsupported capability cannot be marked observed')
        return self


class DelegatedProviderManifest(BaseModel):
    """Sealed capability/identity manifest for one delegated provider
    (dpm-). Capability is version-bound, not permanent truth — the seal
    covers provider + adapter + endpoint identity and every capability
    entry, so an engine upgrade is a different manifest."""

    model_config = ConfigDict(frozen=True)

    manifest_id: str
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    provider_class: MeasurementProviderClass = 'unknown'
    provider_id: str = Field(min_length=1)
    provider_version: str | None = None
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    endpoint_kind: ProviderEndpointKind = 'none'
    endpoint_repr: str | None = None
    capabilities: tuple[ProviderCapabilityEntry, ...] = ()
    declared_at_utc: str = Field(min_length=1)
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'DelegatedProviderManifest':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        kinds = [entry.capability for entry in self.capabilities]
        if len(kinds) != len(set(kinds)):
            raise ValueError('capabilities must be unique per capability')
        if self.endpoint_kind in ('localhost', 'remote_operator_configured') \
                and not self.endpoint_repr:
            raise ValueError(
                'a bound endpoint kind requires endpoint_repr identity')
        if self.endpoint_kind == 'file' and not self.endpoint_repr:
            raise ValueError('file providers must pin the source identity')
        if self.endpoint_kind == 'none' and self.endpoint_repr:
            raise ValueError('endpoint_repr requires a bound endpoint kind')
        if self.manifest_sha256 != _hash(self.identity_payload()):
            raise ValueError('DelegatedProviderManifest hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'manifest_id', 'manifest_sha256'})

    def capability_entry(
        self, capability: DelegatedCapability,
    ) -> ProviderCapabilityEntry | None:
        for entry in self.capabilities:
            if entry.capability == capability:
                return entry
        return None

    @classmethod
    def create(cls, **payload: Any) -> 'DelegatedProviderManifest':
        return _seal(
            cls, payload, 'manifest_id', 'manifest_sha256', 'dpm')


def manifest_ref(manifest: DelegatedProviderManifest) -> AuthorityRef:
    return AuthorityRef(
        kind='delegated_provider_manifest',
        ref_id=manifest.manifest_id,
        ref_sha256=manifest.manifest_sha256,
    )


class ProviderAcquisitionRecord(BaseModel):
    """One sealed delegated acquisition (dpa-): which manifest ran, the
    exercised capability, the exact request identity, the outcome and
    the produced evidence refs. ``raw_artifact_sha256`` pins the exact
    returned payload bytes when a raw artifact exists — HTDT binds exact
    returned data, never screenshots."""

    model_config = ConfigDict(frozen=True)

    acquisition_id: str
    acquisition_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    manifest_ref: AuthorityRef
    capability: DelegatedCapability
    request_identity_repr: str = Field(min_length=1)
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    outcome: AcquisitionOutcome
    evidence_origin: EvidenceOrigin = 'provider_calculated'
    evidence_refs: tuple[AuthorityRef, ...] = ()
    raw_artifact_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN)
    artifact_note: str | None = None
    error_detail: str | None = None
    observed_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _validate(self) -> 'ProviderAcquisitionRecord':
        _require_refs(self.manifest_ref)
        if self.manifest_ref.kind != 'delegated_provider_manifest':
            raise ValueError(
                "manifest_ref kind must be 'delegated_provider_manifest'")
        _require_refs(*self.evidence_refs)
        _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        if self.outcome == 'observed':
            if not self.evidence_refs \
                    and self.raw_artifact_sha256 is None:
                raise ValueError(
                    'an observed acquisition must bind evidence refs or a '
                    'raw artifact hash')
            if self.error_detail:
                raise ValueError(
                    'an observed acquisition cannot carry error_detail')
        if self.outcome == 'error' and not self.error_detail:
            raise ValueError('an errored acquisition requires error_detail')
        if self.outcome == 'capability_rejected' and not self.error_detail:
            raise ValueError(
                'a capability_rejected acquisition requires the gate '
                'reason in error_detail')
        if self.outcome in ('cancelled', 'error', 'capability_rejected') \
                and self.evidence_refs:
            raise ValueError(
                'a non-observed acquisition must not bind evidence refs — '
                'partial results are discarded, not sealed')
        if self.acquisition_sha256 != _hash(self.identity_payload()):
            raise ValueError('ProviderAcquisitionRecord hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'acquisition_id', 'acquisition_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'ProviderAcquisitionRecord':
        return _seal(
            cls, payload, 'acquisition_id', 'acquisition_sha256', 'dpa')


def evaluate_provider_gate(
    manifest: DelegatedProviderManifest | None,
    capability: DelegatedCapability,
) -> tuple[ProviderGateVerdict, str]:
    """Whether a capability may be exercised through this manifest.

    Fail closed: an absent manifest or an absent/``unknown``/
    ``unsupported`` entry blocks. ``licensed_required`` is returned as a
    distinct verdict — never ``provider_capable`` — so an automated REW
    sweep cannot silently run as free-tier automation; the caller must
    treat it as a licensing condition (operator confirms REW Pro) or
    record the acquisition as ``capability_rejected``.
    """
    if manifest is None:
        return ('provider_blocked', 'no_provider_manifest')
    entry = manifest.capability_entry(capability)
    if entry is None:
        return ('provider_blocked', f'capability_absent:{capability}')
    if entry.condition == 'supported':
        return ('provider_capable', f'capability:{capability}')
    if entry.condition == 'licensed_required':
        return (
            'provider_license_required',
            f'licensed:{capability}:{entry.condition_detail}')
    return (
        'provider_blocked',
        f'capability_{entry.condition}:{capability}'
        + (f':{entry.condition_detail}' if entry.condition_detail else ''))


def build_provider_acquisition(
    manifest: DelegatedProviderManifest,
    *,
    capability: DelegatedCapability,
    request_identity_repr: str,
    request_sha256: str,
    outcome: AcquisitionOutcome,
    observed_at_utc: str,
    document_id: str | None = None,
    evidence_origin: EvidenceOrigin = 'provider_calculated',
    evidence_refs: tuple[AuthorityRef, ...] = (),
    raw_artifact_sha256: str | None = None,
    artifact_note: str | None = None,
    error_detail: str | None = None,
) -> ProviderAcquisitionRecord:
    """Build a sealed acquisition record with the gate enforced.

    An ``observed`` outcome is fail-closed against the manifest: if the
    capability is absent or not ``supported`` the builder raises instead
    of sealing a claim the provider never had. ``capability_rejected``
    records the blocked attempt with the gate reason; ``cancelled`` and
    ``error`` record interrupted/failed attempts without evidence refs.
    """
    if outcome == 'observed':
        verdict, reason = evaluate_provider_gate(manifest, capability)
        if verdict != 'provider_capable':
            raise ValueError(
                f'observed acquisition is not allowed: gate verdict '
                f'{verdict} ({reason})')
    if outcome == 'capability_rejected' and error_detail is None:
        _verdict, reason = evaluate_provider_gate(manifest, capability)
        error_detail = reason
    return ProviderAcquisitionRecord.create(
        document_id=document_id or manifest.document_id,
        manifest_ref=manifest_ref(manifest),
        capability=capability,
        request_identity_repr=request_identity_repr,
        request_sha256=request_sha256,
        outcome=outcome,
        evidence_origin=evidence_origin,
        evidence_refs=evidence_refs,
        raw_artifact_sha256=raw_artifact_sha256,
        artifact_note=artifact_note,
        error_detail=error_detail,
        observed_at_utc=observed_at_utc,
    )


# ----------------------------------------------------------------------
# REW provider (issue #838 §1 — delegated specialist, never copied into
# HTDT core). The manifest merges the sealed RewEngineSession (#599)
# identity with the documented REW API capability surface.

REW_PROVIDER_ID = 'rew'
REW_PROVIDER_ADAPTER_ID = 'htdt-rew-api'

# Documented REW API capability surface with its conditions. Licensing is
# a condition on the capability row: automated sweep measurement control
# requires a REW Pro upgrade, so it is declared licensed_required rather
# than assumed by a free-tier manifest.
REW_DECLARED_CAPABILITIES: tuple[tuple[str, str, str | None], ...] = (
    ('measurement_list', 'supported', None),
    ('frequency_response', 'supported', None),
    ('impulse_response', 'supported', None),
    ('group_delay', 'supported', None),
    ('rta_live', 'supported', None),
    ('spl_leq', 'supported', None),
    ('spl_logger', 'supported', None),
    ('generator_control', 'supported', None),
    (
        'automated_sweep',
        'licensed_required',
        'automated sweep measurement control requires a REW Pro upgrade',
    ),
    ('eq_alignment', 'supported', None),
    ('trace_processing', 'supported', None),
    ('subscriptions', 'supported', None),
    ('file_import', 'supported', None),
)

# Capabilities the sealed RewEngineSession capability_snapshot proves
# live at session open — every other declared row stays observed=False.
_REW_OBSERVED_PROBES: dict[str, str] = {
    'version': 'other',
    'list_measurements': 'measurement_list',
    'frequency_response': 'frequency_response',
}


def build_rew_provider_manifest(
    session: 'Any',
    *,
    document_id: str,
    observed_capabilities: frozenset[str] | None = None,
    declared_at_utc: str | None = None,
    notes: str | None = None,
) -> DelegatedProviderManifest:
    """Seal the documented REW capability surface plus the live
    :class:`~htdt.rew_api.RewEngineSession` identity into one manifest.

    ``session`` is a ``RewEngineSession`` (engine version, adapter
    identity, endpoint, capability snapshot). ``observed_capabilities``
    overrides the observed flags (e.g. after a live RTA probe); by
    default the session's own capability snapshot marks what answered at
    session open. REW stays a delegated provider: the manifest records
    what the *provider* supplies, and automated-sweep control is a
    licensed condition, never a free-tier assumption.
    """
    snapshot = getattr(session, 'capability_snapshot', None) or {}
    session_caps = snapshot.get('capabilities') or {}
    observed: set[str] = set(observed_capabilities or ())
    if observed_capabilities is None:
        for probe, capability in _REW_OBSERVED_PROBES.items():
            if session_caps.get(probe):
                observed.add(capability)
    entries = tuple(
        ProviderCapabilityEntry(
            capability=capability,  # type: ignore[arg-type]
            condition=condition,  # type: ignore[arg-type]
            condition_detail=detail,
            observed=capability in observed,
        )
        for capability, condition, detail in REW_DECLARED_CAPABILITIES
    )
    return DelegatedProviderManifest.create(
        document_id=document_id,
        provider_class='rew_api',
        provider_id=REW_PROVIDER_ID,
        provider_version=getattr(session, 'engine_version', None),
        adapter_id=getattr(session, 'adapter_id', REW_PROVIDER_ADAPTER_ID),
        adapter_version=getattr(session, 'adapter_version', 'unknown'),
        endpoint_kind='localhost',
        endpoint_repr=getattr(session, 'endpoint', None),
        capabilities=entries,
        declared_at_utc=(
            declared_at_utc or getattr(session, 'observed_at_utc', None)
            or '1970-01-01T00:00:00Z'
        ),
        notes=notes,
    )


def build_htdt_native_import_manifest(
    *,
    document_id: str,
    source_identity: str,
    importer_id: str,
    importer_version: str,
    declared_at_utc: str,
    notes: str | None = None,
) -> DelegatedProviderManifest:
    """Manifest for the HTDT-native file-import provider path.

    File import is a provider in the contract sense: the externally
    produced file is the machine-readable observation, HTDT is the
    importer. The only capability declared is ``file_import`` — a file
    can never carry live/provider-calculated capabilities, and the
    artifact evidence stays ``provider_imported`` origin.
    """
    return DelegatedProviderManifest.create(
        document_id=document_id,
        provider_class='htdt_native_import',
        provider_id=importer_id,
        provider_version=importer_version,
        adapter_id=importer_id,
        adapter_version=importer_version,
        endpoint_kind='file',
        endpoint_repr=source_identity,
        capabilities=(
            ProviderCapabilityEntry(
                capability='file_import',
                condition='supported',
                observed=True,
            ),
        ),
        declared_at_utc=declared_at_utc,
        notes=notes,
    )


__all__ = [
    'AcquisitionOutcome',
    'CapabilityCondition',
    'DelegatedCapability',
    'DelegatedProviderManifest',
    'DELEGATED_PROVIDER_LABELS',
    'EvidenceOrigin',
    'MeasurementProviderClass',
    'ProviderAcquisitionRecord',
    'ProviderCapabilityEntry',
    'ProviderEndpointKind',
    'ProviderGateVerdict',
    'REW_DECLARED_CAPABILITIES',
    'REW_PROVIDER_ADAPTER_ID',
    'REW_PROVIDER_ID',
    'build_htdt_native_import_manifest',
    'build_provider_acquisition',
    'build_rew_provider_manifest',
    'evaluate_provider_gate',
    'manifest_ref',
]
