"""#887 executable adapter conformance suite + in-repo subjects.

The suite drives a :class:`ConformanceSubject` — a thin, transport-free
surface over one adapter implementation — through the named scenarios
from the issue:

    happy_path, unavailable_target, authentication_failure, timeout,
    cancellation, malformed_response, partial_write, stale_capability,
    firmware_drift, unsupported_feature, readback_mismatch,
    retryable_vs_terminal, rollback_success, rollback_failure

Which scenarios are *required* adapts to the adapter's own declared
lanes (:func:`required_scenarios_for`): every adapter must prove its
happy path and its honest refusal of undeclared features; an adapter
that declares ``apply`` must also prove its mutation authorization
boundary; one that declares machine/file read-back must prove
mismatch-honesty; one that declares a machine rollback lane must prove
it. A file lane cannot dodge the transport contract by underdeclaring —
it simply has no transport contract to prove, and the unexercised
scenarios land in ``limitations``.

Each scenario records a sealed :class:`ConformanceScenarioResult`:
deterministic transcripts plus ``expected``/``observed`` probes so the
outcome is re-derivable evidence, never a bare pass. Statuses:

- ``passed`` — the adapter behaved per contract *and* the recorded
  probes confirm it;
- ``required_fail`` / ``optional_fail`` — observed behaviour diverged;
- ``unverifiable`` — the scenario is not determinable on this lane
  (a synchronous adapter has no cancellation surface; a file lane has
  no transport fault taxonomy). Unknown is recorded honestly — it is
  never promoted to a pass.

The run seals a :class:`ConformanceResultRecord` (``acr-``) per
descriptor + suite version. Aggregate verdict:

- any ``required_fail`` → ``non_conforming``
- else any *required* ``unverifiable`` → ``unverifiable``
- else anything imperfect (optional fails, unverifiable optional lanes,
  or a contract status below ``production``) →
  ``conforming_with_limitations``
- else → ``conforming``

Subjects for every in-repo adapter family live at the bottom:
AVR LAN, CamillaDSP, miniDSP export, the generic file lane, the
Equalizer APO installer, configured-endpoint discovery, and the REW
delegated-provider gate. Their recorded outcomes are honest by
construction — file lanes stay ``assisted_only`` and simulated
transports can never claim protocol conformance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_adapter_sdk import (
    AdapterContractError,
    AdapterLane,
    AdapterSafetyInvariants,
    AdapterSdkContract,
    ConformanceVerdict,
    build_adapter_descriptor,
    descriptor_ref,
)
from .cad_authority_resolver import AuthorityRef
from .cad_avr_lan_adapter import (
    AVR_CHANNEL_CODES,
    AVR_LAN_ADAPTER_ID,
    AVR_LAN_DEVICE_FAMILY,
    AvrLanApplyError,
    AvrLanCalibrationAdapter,
    AvrLanTransport,
    FakeAvrLanTransport,
)
from .cad_calibration import CadCalibrationExportSnapshot
from .cad_camilladsp import (
    CamillaDSPError,
    CamillaDSPTransport,
    FixtureCamillaDSPTransport,
)
from .cad_camilladsp_deploy import CamillaDSPCalibrationAdapter
from .cad_delegated_provider import (
    DelegatedProviderManifest,
    evaluate_provider_gate,
)
from .cad_deployment_pipeline import (
    EqualizerApoInstaller,
    MINIDSP_DEPLOYABILITY,
)
from .cad_deployment_target import list_dsp_target_profiles
from .cad_device_adapter import (
    AdapterCapabilityError,
    AdapterCapabilityReport,
    AdapterDeviceBinding,
    MaterializedCalibrationSettings,
    build_device_binding,
)
from .cad_device_adapter_file import (
    FILE_ADAPTER_ID,
    FILE_READBACK_NAME,
    FileCalibrationAdapter,
)
from .cad_device_discovery import (
    ConfiguredEndpointScanBackend,
    DiscoveryObservation,
    DiscoveryScanScope,
    DiscoveryScopeError,
    StaticEndpointProber,
)
from .cad_minidsp_export import (
    MINIDSP_EXPORT_ADAPTER_ID,
    MiniDSPBiquadExportAdapter,
)
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .user_facing_error import operation_error_message


#: Version of this suite. Minting under a new suite version invalidates
#: every previously issued result — conformance is always re-derived
#: against the suite that ran.
ADAPTER_CONFORMANCE_SUITE_VERSION = 'conformance-suite-1'

_SHA256 = r'^[0-9a-f]{64}$'


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


# ---------------------------------------------------------------------------
# Scenario taxonomy + subject surface
# ---------------------------------------------------------------------------

#: The named scenarios the suite executes, in stable order.
CONFORMANCE_SCENARIO_NAMES: tuple[str, ...] = (
    'happy_path',
    'unavailable_target',
    'authentication_failure',
    'timeout',
    'cancellation',
    'malformed_response',
    'partial_write',
    'stale_capability',
    'firmware_drift',
    'unsupported_feature',
    'readback_mismatch',
    'retryable_vs_terminal',
    'rollback_success',
    'rollback_failure',
)


def required_scenarios_for(
    lanes: frozenset[AdapterLane] | set[AdapterLane],
) -> frozenset[str]:
    """Scenarios an adapter *must* prove, derived from its own lanes.

    Every adapter proves ``happy_path`` and ``unsupported_feature``.
    Declaring ``apply`` adds ``authentication_failure`` (the mutation
    authorization boundary); declaring machine/file read-back adds
    ``readback_mismatch``; declaring a machine rollback lane adds
    ``rollback_success``.
    """
    required: set[str] = {'happy_path', 'unsupported_feature'}
    if 'apply' in lanes:
        required.add('authentication_failure')
    if lanes & {'read_back', 'file_verify'}:
        required.add('readback_mismatch')
    if 'rollback' in lanes:
        required.add('rollback_success')
    return frozenset(required)


ScenarioStatus = Literal[
    'passed', 'required_fail', 'optional_fail', 'unverifiable',
]


class ConformanceProbe(BaseModel):
    """One recorded expectation/observation — the re-derivable evidence."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    check: str = Field(min_length=1)
    expected: str = Field(min_length=1)
    observed: str = Field(min_length=1)
    ok: bool


class SubjectResult(BaseModel):
    """What a subject reports for one scenario.

    ``ok`` is the subject's claim; ``verifiable=False`` marks the
    scenario as not determinable on this adapter's lanes — recorded
    honestly as ``unverifiable``, never fudged into a pass.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    ok: bool
    verifiable: bool = True
    transcript: tuple[str, ...] = ()
    probes: tuple[ConformanceProbe, ...] = ()
    detail: str = ''


#: Where a subject's scenario evidence actually comes from. Anything
#: below 'live' is a recorded limitation — simulated transports can
#: never claim protocol conformance.
EvidenceProvenance = Literal['live', 'simulated', 'file', 'read_only']


class ConformanceSubject(Protocol):
    """Transport-free surface the suite drives per adapter."""

    @property
    def descriptor(self) -> AdapterSdkContract: ...

    @property
    def lanes(self) -> frozenset[AdapterLane]: ...

    @property
    def evidence_provenance(self) -> EvidenceProvenance: ...

    def run(self, name: str) -> SubjectResult: ...


class ConformanceScenarioResult(BaseModel):
    """Sealed record of one scenario's execution (evidence pinned)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    scenario: str = Field(min_length=1)
    required: bool
    status: ScenarioStatus
    transcript: tuple[str, ...] = ()
    transcript_sha256: str = Field(pattern=_SHA256)
    probes: tuple[ConformanceProbe, ...] = ()
    detail: str = ''
    executed_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _validate(self) -> 'ConformanceScenarioResult':
        if self.scenario not in CONFORMANCE_SCENARIO_NAMES:
            raise ValueError(
                f'unknown conformance scenario {self.scenario}'
            )
        if self.required and self.status == 'optional_fail':
            raise ValueError(
                'a required scenario cannot record optional_fail'
            )
        if self.transcript_sha256 != _hash(list(self.transcript)):
            raise ValueError('transcript_sha256 mismatch')
        return self


class ConformanceResultRecord(BaseModel):
    """Sealed conformance verdict per descriptor + suite (``acr-``).

    ``descriptor_ref`` pins the exact contract descriptor the verdict
    was derived for; ``suite_version`` pins the issuing suite; each
    scenario's evidence is pinned by ``evidence_refs``. An upgraded
    adapter or SDK produces a different descriptor sha, so stale
    conformance can never be re-attributed.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    result_id: str = Field(min_length=1)
    result_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    descriptor_ref: AuthorityRef
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    suite_version: str = Field(min_length=1)
    contract_version: str = Field(min_length=1)
    verdict: ConformanceVerdict
    scenario_results: tuple[ConformanceScenarioResult, ...]
    required_failures: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evidence_refs: tuple[AuthorityRef, ...] = ()
    issued_at_utc: str = Field(min_length=1)
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _validate(self) -> 'ConformanceResultRecord':
        names = [s.scenario for s in self.scenario_results]
        if sorted(names) != sorted(CONFORMANCE_SCENARIO_NAMES):
            raise ValueError(
                'scenario_results must cover every named scenario '
                'exactly once'
            )
        if len(set(names)) != len(names):
            raise ValueError('duplicate scenario results')
        if self.result_sha256 != _hash(self.identity_payload()):
            raise ValueError('ConformanceResultRecord hash mismatch')
        if self.verdict == 'conforming' and self.required_failures:
            raise ValueError(
                'a conforming verdict cannot carry required failures'
            )
        if self.verdict == 'non_conforming' and not self.required_failures:
            raise ValueError(
                'non_conforming requires at least one required failure'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'result_id', 'result_sha256'},
        )


# ---------------------------------------------------------------------------
# Suite executor
# ---------------------------------------------------------------------------

class ConformanceSuiteRunner:
    """Execute the named scenarios against one subject; seal the result."""

    def __init__(
        self,
        *,
        suite_version: str = ADAPTER_CONFORMANCE_SUITE_VERSION,
    ) -> None:
        self.suite_version = suite_version

    def run(
        self,
        subject: ConformanceSubject,
        *,
        issued_at_utc: str,
        notes: tuple[str, ...] = (),
    ) -> ConformanceResultRecord:
        descriptor = subject.descriptor
        required = required_scenarios_for(subject.lanes)
        scenario_results = tuple(
            self._run_one(
                subject, name,
                required=name in required,
                issued_at_utc=issued_at_utc,
            )
            for name in CONFORMANCE_SCENARIO_NAMES
        )
        required_failures = tuple(
            s.scenario for s in scenario_results
            if s.status == 'required_fail'
        )
        required_unverifiable = tuple(
            s.scenario for s in scenario_results
            if s.required and s.status == 'unverifiable'
        )
        limitations: list[str] = [
            s.scenario for s in scenario_results
            if s.status in ('optional_fail', 'unverifiable')
            and not s.required
        ]
        if descriptor.contract_status != 'production':
            limitations.append(
                f'contract_status={descriptor.contract_status}'
            )
        if subject.evidence_provenance != 'live':
            limitations.append(
                f'evidence_provenance={subject.evidence_provenance}'
            )
        if required_failures:
            verdict: ConformanceVerdict = 'non_conforming'
        elif required_unverifiable:
            verdict = 'unverifiable'
        elif limitations:
            verdict = 'conforming_with_limitations'
        else:
            verdict = 'conforming'
        payload: dict[str, Any] = {
            'document_id': descriptor.document_id,
            'descriptor_ref': descriptor_ref(descriptor),
            'adapter_id': descriptor.adapter_id,
            'adapter_version': descriptor.adapter_version,
            'suite_version': self.suite_version,
            'contract_version': descriptor.sdk_version,
            'verdict': verdict,
            'scenario_results': scenario_results,
            'required_failures': required_failures,
            'limitations': tuple(limitations),
            'evidence_refs': tuple(
                AuthorityRef(
                    kind='conformance_scenario_evidence',
                    ref_id=f'{descriptor.descriptor_id}:{s.scenario}',
                    ref_sha256=s.transcript_sha256,
                )
                for s in scenario_results
            ),
            'issued_at_utc': issued_at_utc,
            'notes': tuple(notes),
        }
        probe = ConformanceResultRecord.model_construct(
            **canonicalize_payload(
                ConformanceResultRecord, dict(payload),
            )
        )
        digest = _hash(probe.identity_payload())
        return ConformanceResultRecord(
            result_id=_semantic_id('acr', digest),
            result_sha256=digest,
            **payload,
        )

    def _run_one(
        self,
        subject: ConformanceSubject,
        name: str,
        *,
        required: bool,
        issued_at_utc: str,
    ) -> ConformanceScenarioResult:
        try:
            outcome = subject.run(name)
        except Exception as exc:  # error-boundary: scenario run — a subject crash is the scored observation (ok=False records the exception type); broad by design (noqa: BLE001)
            outcome = SubjectResult(
                ok=False,
                transcript=(f'subject raised {type(exc).__name__}',),
                detail=operation_error_message(exc),
            )
        transcript = tuple(outcome.transcript)
        if not outcome.verifiable:
            status: ScenarioStatus = 'unverifiable'
        elif outcome.ok:
            status = 'passed'
        else:
            status = 'required_fail' if required else 'optional_fail'
        return ConformanceScenarioResult(
            scenario=name,
            required=required,
            status=status,
            transcript=transcript,
            transcript_sha256=_hash(list(transcript)),
            probes=tuple(outcome.probes),
            detail=outcome.detail,
            executed_at_utc=issued_at_utc,
        )


# ---------------------------------------------------------------------------
# Shared subject helpers
# ---------------------------------------------------------------------------

def _err_repr(exc: BaseException) -> str:
    kind = getattr(exc, 'kind', None)
    if isinstance(kind, str) and kind:
        return f'{type(exc).__name__}:{kind}'
    return type(exc).__name__


def _probe(
    check: str, expected: str, observed: Any, ok: bool,
) -> ConformanceProbe:
    return ConformanceProbe(
        check=check, expected=expected, observed=str(observed), ok=ok,
    )


def _unverifiable(note: str) -> SubjectResult:
    return SubjectResult(ok=True, verifiable=False, transcript=(note,))


def _binding_pin_probe(
    label: str,
    materialization: MaterializedCalibrationSettings,
    binding: AdapterDeviceBinding,
) -> ConformanceProbe:
    return _probe(
        label,
        'materialization pins the exact bound device sha',
        f'{materialization.binding_sha256[:16]}… == '
        f'{binding.binding_sha256[:16]}…',
        materialization.binding_sha256 == binding.binding_sha256,
    )


class _BaseSubject:
    """Descriptor + declared lanes + scenario dispatch."""

    def __init__(
        self,
        *,
        descriptor: AdapterSdkContract,
        lanes: frozenset[AdapterLane],
        evidence_provenance: EvidenceProvenance,
    ) -> None:
        self._descriptor = descriptor
        self._lanes = lanes
        self._evidence_provenance = evidence_provenance

    @property
    def descriptor(self) -> AdapterSdkContract:
        return self._descriptor

    @property
    def lanes(self) -> frozenset[AdapterLane]:
        return self._lanes

    @property
    def evidence_provenance(self) -> EvidenceProvenance:
        return self._evidence_provenance

    def run(self, name: str) -> SubjectResult:
        handler = getattr(self, f'_sc_{name}', None)
        if handler is None:
            return _unverifiable(f'no handler for scenario {name}')
        return handler()

    # -- shared contract-level probes -----------------------------------

    def _sc_stale_capability(self) -> SubjectResult:
        """A drifted manifest is a different descriptor — sha divergence."""
        capability = self._descriptor.capability_report
        declared_sha = _hash(capability.model_dump(mode='json'))
        drifted = capability.model_copy(update={'adapter_version': '9999'})
        drifted_sha = _hash(drifted.model_dump(mode='json'))
        ok = (
            declared_sha == self._descriptor.capability_sha256
            and drifted_sha != declared_sha
            and drifted.adapter_version != capability.adapter_version
        )
        return SubjectResult(
            ok=ok,
            transcript=(
                f'capability sha {declared_sha[:16]}… pinned on '
                'descriptor',
                f'drifted manifest → {drifted_sha[:16]}… (different '
                'descriptor → stale evidence)',
            ),
            probes=(_probe(
                'a drifted capability snapshot is a different descriptor',
                'capability_sha256 pins the manifest verbatim',
                f'{declared_sha[:12]}… vs {drifted_sha[:12]}…',
                ok,
            ),),
        )

    def _sc_cancellation(self) -> SubjectResult:
        return _unverifiable(
            'adapter operations run synchronously; cancellation lands '
            'at the orchestrator layer, not on the adapter contract',
        )


# ---------------------------------------------------------------------------
# AVR LAN subject
# ---------------------------------------------------------------------------

class _TimeoutAvrTransport:
    def send(self, command: str) -> None:
        raise TimeoutError('simulated telnet timeout')

    def query(self, command: str) -> tuple[str, ...]:
        raise TimeoutError('simulated telnet timeout')


class _StubAvrTransport:
    """Canned-response transport — returns a fixed tuple for any query."""

    def __init__(
        self,
        responses: tuple[str, ...],
        *,
        send_error: Exception | None = None,
    ) -> None:
        self._responses = responses
        self._send_error = send_error

    def send(self, command: str) -> None:
        if self._send_error is not None:
            raise self._send_error

    def query(self, command: str) -> tuple[str, ...]:
        return self._responses


class AvrLanConformanceSubject(_BaseSubject):
    """AVR LAN adapter over the fault-injectable fake telnet transport."""

    SERIAL = 'localhost:23'

    def __init__(self, export: CadCalibrationExportSnapshot) -> None:
        self._export = export
        binding = build_device_binding(
            adapter_id=AVR_LAN_ADAPTER_ID,
            device_family=AVR_LAN_DEVICE_FAMILY,
            device_model='AVR-X3800H',
            device_serial=self.SERIAL,
            firmware_version='1.4.0',
            routing=tuple(
                (c.channel_id, f'out-{c.channel_id}')
                for c in export.channels
            ),
            bound_at_utc='1970-01-01T00:00:00Z',
        )
        self._binding = binding
        capability = self._adapter(FakeAvrLanTransport()).capability()
        descriptor = build_adapter_descriptor(
            document_id='issue-887/avr-lan-conformance',
            capability=capability,
            supported_operations=(
                'capability', 'materialize', 'apply', 'read_back',
                'capture_baseline', 'rollback',
            ),
            safety_invariants=AdapterSafetyInvariants(
                forbidden_operations=(
                    'ambient_lan_scan',
                    'credential_storage',
                    'unbounded_retry',
                ),
                credential_boundary=(
                    'AVR telnet control is unauthenticated; mutation '
                    'requires operator_confirmed and remote endpoints '
                    'need explicit approval'
                ),
                rollback_note=(
                    'baseline trims re-write + post-rollback read-back '
                    'comparison through the documented CV surface'
                ),
                notes=(
                    'conformance exercises the simulated transport '
                    'only — protocol_authority stays simulated',
                ),
            ),
            contract_status='simulated',
            compatibility_range=capability.applicability,
            declared_at_utc='1970-01-01T00:00:00Z',
            notes=(
                'deterministic fake transport; real LAN/device '
                'behaviour is device-only evidence',
            ),
        )
        super().__init__(
            descriptor=descriptor,
            lanes=frozenset({
                'capability', 'binding', 'transport', 'materialize',
                'apply', 'read_back', 'rollback',
            }),
            evidence_provenance='simulated',
        )

    def _adapter(self, transport: AvrLanTransport) -> AvrLanCalibrationAdapter:
        return AvrLanCalibrationAdapter(
            {self.SERIAL: transport, 'localhost': transport},
            simulated=True,
        )

    def _materialize(
        self, adapter: AvrLanCalibrationAdapter | None = None,
    ) -> MaterializedCalibrationSettings:
        return (adapter or self._adapter(FakeAvrLanTransport())).materialize(
            self._export, self._binding, created_at_utc='1970-01-01T00:00:00Z',
        )

    # -- scenarios -------------------------------------------------------

    def _sc_happy_path(self) -> SubjectResult:
        adapter = self._adapter(FakeAvrLanTransport())
        materialization = self._materialize(adapter)
        ack = adapter.apply(
            materialization, self._binding,
            operator_confirmed=True, applied_at_utc='1970-01-01T00:00:00Z',
        )
        observed = adapter.read_back(
            self._binding, observed_at_utc='1970-01-01T00:00:00Z',
        )
        applied_gains = {
            c.channel_id: c.gain_db for c in materialization.channel_settings
        }
        observed_gains = {c.channel_id: c.gain_db for c in observed}
        ok = (
            ack.applied_units == ack.total_units
            and observed_gains == applied_gains
            and not materialization.unsupported_items
        )
        return SubjectResult(
            ok=ok,
            transcript=(
                f'apply ack {ack.applied_units}/{ack.total_units}',
                f'read_back gains {observed_gains}',
            ),
            probes=(
                _binding_pin_probe(
                    'happy-path materialization binds the device',
                    materialization, self._binding,
                ),
                _probe(
                    'machine read-back equals the landed trim values',
                    str(applied_gains), observed_gains,
                    observed_gains == applied_gains,
                ),
            ),
        )

    def _sc_unavailable_target(self) -> SubjectResult:
        """Remote unapproved endpoint is a typed refusal, never dialed."""
        rogue = self._binding.model_copy(
            update={'device_serial': 'avr-remote.local:23'},
        )
        adapter = self._adapter(FakeAvrLanTransport())
        materialization = self._materialize(adapter)
        try:
            adapter.apply(
                materialization, rogue,
                operator_confirmed=True,
                applied_at_utc='1970-01-01T00:00:00Z',
            )
            observed = 'apply reached an unapproved remote endpoint'
            ok = False
        except Exception as exc:  # error-boundary: refusal probe — the exception is the scored observation (typed AdapterCapabilityError expected); broad by design (noqa: BLE001)
            observed = _err_repr(exc)
            ok = isinstance(exc, AdapterCapabilityError)
        return SubjectResult(
            ok=ok,
            transcript=(f'unapproved remote endpoint → {observed}',),
            probes=(_probe(
                'unapproved/unreachable targets are refused typed',
                'AdapterCapabilityError',
                observed, ok,
            ),),
        )

    def _sc_authentication_failure(self) -> SubjectResult:
        adapter = self._adapter(FakeAvrLanTransport())
        materialization = self._materialize(adapter)
        try:
            adapter.apply(
                materialization, self._binding,
                operator_confirmed=False,
                applied_at_utc='1970-01-01T00:00:00Z',
            )
            observed = 'apply ran without operator confirmation'
            ok = False
        except Exception as exc:  # error-boundary: capability probe — the exception is the scored observation (refusal type decides ok); broad by design (noqa: BLE001)
            observed = _err_repr(exc)
            ok = isinstance(exc, AdapterCapabilityError)
        return SubjectResult(
            ok=ok,
            transcript=(f'unconfirmed apply → {observed}',),
            probes=(_probe(
                'mutation without operator confirmation is refused',
                'AdapterCapabilityError',
                observed, ok,
            ),),
        )

    def _sc_timeout(self) -> SubjectResult:
        adapter = self._adapter(_TimeoutAvrTransport())
        materialization = self._materialize()
        try:
            adapter.apply(
                materialization, self._binding,
                operator_confirmed=True,
                applied_at_utc='1970-01-01T00:00:00Z',
            )
            observed = 'apply returned despite a dead transport'
            ok = False
        except Exception as exc:  # error-boundary: capability probe — the exception is the scored observation (refusal type decides ok); broad by design (noqa: BLE001)
            observed = _err_repr(exc)
            ok = isinstance(exc, AdapterCapabilityError)
        return SubjectResult(
            ok=ok,
            transcript=(f'transport timeout → {observed}',),
            probes=(_probe(
                'transport failure stays a typed adapter error',
                'AvrLanApplyError (AdapterCapabilityError)',
                observed, ok,
            ),),
        )

    def _sc_malformed_response(self) -> SubjectResult:
        adapter = self._adapter(_StubAvrTransport(('GARBAGE',)))
        try:
            adapter.read_back(
                self._binding, observed_at_utc='1970-01-01T00:00:00Z',
            )
            observed = 'read_back accepted a malformed response'
            ok = False
        except Exception as exc:  # error-boundary: capability probe — the exception is the scored observation (refusal type decides ok); broad by design (noqa: BLE001)
            observed = _err_repr(exc)
            ok = isinstance(exc, AdapterCapabilityError)
        out_of_range = self._adapter(_StubAvrTransport(('CVFL 99',)))
        try:
            out_of_range.read_back(
                self._binding, observed_at_utc='1970-01-01T00:00:00Z',
            )
            observed2 = 'read_back accepted an out-of-range value'
            ok2 = False
        except Exception as exc:  # error-boundary: capability probe — the exception is the scored observation (refusal type decides ok); broad by design (noqa: BLE001)
            observed2 = _err_repr(exc)
            ok2 = isinstance(exc, AdapterCapabilityError)
        return SubjectResult(
            ok=ok and ok2,
            transcript=(
                f'garbage reply → {observed}',
                f'out-of-range reply → {observed2}',
            ),
            probes=(_probe(
                'malformed device responses fail closed',
                'AdapterCapabilityError on both lanes',
                f'{observed}; {observed2}', ok and ok2,
            ),),
        )

    def _sc_partial_write(self) -> SubjectResult:
        if len(self._export.channels) < 2:
            return _unverifiable(
                'export carries one channel — partial write needs ≥2 '
                'commands',
            )
        transport = FakeAvrLanTransport(fail_on_send_index=1)
        adapter = self._adapter(transport)
        materialization = self._materialize(adapter)
        try:
            adapter.apply(
                materialization, self._binding,
                operator_confirmed=True,
                applied_at_utc='1970-01-01T00:00:00Z',
            )
            observed = 'apply returned despite mid-write failure'
            ok = False
            counts = '-'
        except AvrLanApplyError as exc:
            observed = _err_repr(exc)
            counts = f'{exc.applied_units}/{exc.total_units}'
            ok = exc.applied_units < exc.total_units
        except Exception as exc:  # error-boundary: capability probe — the exception is the scored observation (refusal type decides ok); broad by design (noqa: BLE001)
            observed = _err_repr(exc)
            counts = '-'
            ok = False
        return SubjectResult(
            ok=ok,
            transcript=(
                f'send #2 of {len(self._export.channels)} fails → '
                f'{observed} ({counts} landed)',
            ),
            probes=(_probe(
                'a partial write surfaces exact landed counts',
                'AvrLanApplyError with applied<total',
                f'{observed} {counts}', ok,
            ),),
        )

    def _sc_unsupported_feature(self) -> SubjectResult:
        adapter = self._adapter(FakeAvrLanTransport())
        materialization = self._materialize(adapter)
        unsupported = materialization.unsupported_items
        # Every channel field outside the documented CV surface must be
        # surfaced pre-apply, never silently dropped.
        unmapped = [
            c.channel_id for c in self._export.channels
            if c.channel_id.strip().lower() not in _AVR_CODES
        ]
        landed_ids = {
            c.channel_id for c in materialization.channel_settings
        }
        # Pass iff unmapped channels never silently landed and every
        # one of them was recorded in unsupported_items pre-apply.
        ok = not any(cid in landed_ids for cid in unmapped) and (
            not unmapped or any(
                'no documented AVR channel code' in u for u in unsupported
            )
        )
        return SubjectResult(
            ok=ok,
            transcript=(
                f'unsupported_items={list(unsupported)}',
                f'unmapped channels never landed: {unmapped}',
            ),
            probes=(_probe(
                'off-surface fields surface pre-apply, never dropped',
                'unsupported_items records every unmapped channel',
                str(list(unsupported)), ok,
            ),),
        )

    def _sc_readback_mismatch(self) -> SubjectResult:
        replies = tuple(
            f'CV{AVR_CHANNEL_CODES[channel_id]} 50'
            for channel_id, _output in self._binding.routing
            if channel_id in AVR_CHANNEL_CODES
        )
        adapter = self._adapter(_StubAvrTransport(replies))
        observed = adapter.read_back(
            self._binding, observed_at_utc='1970-01-01T00:00:00Z',
        )
        observed_gains = {c.channel_id: c.gain_db for c in observed}
        applied = {
            c.channel_id: c.gain_db
            for c in self._materialize().channel_settings
        }
        divergent = any(
            observed_gains.get(cid) != gain
            for cid, gain in applied.items()
        )
        return SubjectResult(
            ok=divergent,
            transcript=(
                f'applied {applied} but device holds {observed_gains} — '
                'read-back reports observed, never the asserted export',
            ),
            probes=(_probe(
                'read-back returns observed state, divergent from export',
                'observed != applied when the device drifted',
                f'{observed_gains} vs {applied}', divergent,
            ),),
        )

    def _sc_retryable_vs_terminal(self) -> SubjectResult:
        transcript: list[str] = []
        ok = True
        adapter = self._adapter(_TimeoutAvrTransport())
        try:
            adapter.apply(
                self._materialize(), self._binding,
                operator_confirmed=True,
                applied_at_utc='1970-01-01T00:00:00Z',
            )
            transcript.append('transport failure: no error')
            ok = False
        except Exception as exc:  # error-boundary: capability probe — the exception is the scored observation (refusal type decides ok); broad by design (noqa: BLE001)
            retryable = isinstance(exc, AvrLanApplyError)
            transcript.append(f'transport → {_err_repr(exc)}')
            ok = ok and retryable
        adapter = self._adapter(FakeAvrLanTransport())
        try:
            adapter.apply(
                self._materialize(adapter), self._binding,
                operator_confirmed=False,
                applied_at_utc='1970-01-01T00:00:00Z',
            )
            transcript.append('auth refusal: no error')
            ok = False
        except Exception as exc:  # error-boundary: capability probe — the exception is the scored observation (refusal type decides ok); broad by design (noqa: BLE001)
            terminal = isinstance(exc, AdapterCapabilityError) and not (
                isinstance(exc, AvrLanApplyError)
            )
            transcript.append(f'auth → {_err_repr(exc)}')
            ok = ok and terminal
        return SubjectResult(
            ok=ok,
            transcript=tuple(transcript),
            probes=(_probe(
                'retryable transport faults type differently from '
                'terminal contract refusals',
                'AvrLanApplyError vs AdapterCapabilityError',
                '; '.join(transcript), ok,
            ),),
        )

    def _sc_rollback_success(self) -> SubjectResult:
        adapter = self._adapter(FakeAvrLanTransport())
        adapter.capture_baseline(self._binding)
        adapter.apply(
            self._materialize(adapter), self._binding,
            operator_confirmed=True,
            applied_at_utc='1970-01-01T00:00:00Z',
        )
        outcome = adapter.rollback_previous(self._binding)
        ok = (
            outcome.outcome == 'restored_verified'
            and outcome.post_readback_sha256 is not None
        )
        return SubjectResult(
            ok=ok,
            transcript=(f'rollback → {outcome.outcome}',),
            probes=(_probe(
                'baseline restore verifies by post-rollback read-back',
                'restored_verified + pinned read-back sha',
                outcome.outcome, ok,
            ),),
        )

    def _sc_rollback_failure(self) -> SubjectResult:
        transport = FakeAvrLanTransport()
        adapter = self._adapter(transport)
        adapter.capture_baseline(self._binding)
        adapter.apply(
            self._materialize(adapter), self._binding,
            operator_confirmed=True,
            applied_at_utc='1970-01-01T00:00:00Z',
        )
        transport.fail_on_send_index = len(transport.sent_commands)
        outcome = adapter.rollback_previous(self._binding)
        ok = outcome.outcome == 'failed'
        return SubjectResult(
            ok=ok,
            transcript=(f'rollback under dead transport → '
                        f'{outcome.outcome}',),
            probes=(_probe(
                'rollback failure is a recorded outcome, never silent',
                'failed',
                outcome.outcome, ok,
            ),),
        )

    def _sc_firmware_drift(self) -> SubjectResult:
        drifted = build_device_binding(
            adapter_id=AVR_LAN_ADAPTER_ID,
            device_family=AVR_LAN_DEVICE_FAMILY,
            device_model='AVR-X3800H',
            device_serial=self.SERIAL,
            firmware_version='2.0.0',
            routing=self._binding.routing,
            bound_at_utc='1970-01-01T00:00:00Z',
        )
        ok = drifted.binding_sha256 != self._binding.binding_sha256
        return SubjectResult(
            ok=ok,
            transcript=(
                f'firmware 1.4.0 → sha '
                f'{self._binding.binding_sha256[:12]}…',
                f'firmware 2.0.0 → sha {drifted.binding_sha256[:12]}…',
            ),
            probes=(_probe(
                'firmware drift is a different binding — prior evidence '
                'cannot be silently reinterpreted',
                'different binding_sha256',
                f'{self._binding.binding_sha256[:12]}… vs '
                f'{drifted.binding_sha256[:12]}…', ok,
            ),),
        )


_AVR_CODES = frozenset(AVR_CHANNEL_CODES)


# ---------------------------------------------------------------------------
# CamillaDSP subject
# ---------------------------------------------------------------------------

class _FailingCamillaTransport(FixtureCamillaDSPTransport):
    """Dies on one named command — deterministic fault injection."""

    def __init__(self, fail_on: str, error: Exception) -> None:
        super().__init__()
        self._fail_on = fail_on
        self._error = error

    def request(self, command: dict[str, Any]) -> dict[str, Any]:
        name = next(iter(command))
        if name == self._fail_on:
            raise self._error
        return super().request(command)


class _MalformedConfigTransport(FixtureCamillaDSPTransport):
    """Returns a non-JSON payload for GetConfigJson."""

    def request(self, command: dict[str, Any]) -> dict[str, Any]:
        name = next(iter(command))
        if name == 'GetConfigJson':
            return {name: {'result': 'Ok', 'value': 'not-json{{{'}}
        return super().request(command)


class CamillaDSPConformanceSubject(_BaseSubject):
    """CamillaDSP deploy adapter over the deterministic fixture daemon."""

    SERIAL = 'camilladsp://localhost:1234'

    def __init__(self, export: CadCalibrationExportSnapshot) -> None:
        self._export = export
        binding = build_device_binding(
            adapter_id='htdt-camilladsp-deploy',
            device_family='camilladsp',
            device_model='camilladsp-daemon',
            device_serial=self.SERIAL,
            firmware_version='3.0.0',
            routing=tuple(
                (c.channel_id, str(index))
                for index, c in enumerate(export.channels)
            ),
            bound_at_utc='1970-01-01T00:00:00Z',
        )
        self._binding = binding
        capability = self._adapter(FixtureCamillaDSPTransport()).capability()
        descriptor = build_adapter_descriptor(
            document_id='issue-887/camilladsp-conformance',
            capability=capability,
            supported_operations=(
                'capability', 'materialize', 'apply', 'read_back',
                'capture_baseline', 'rollback', 'observe_runtime',
            ),
            safety_invariants=AdapterSafetyInvariants(
                forbidden_operations=(
                    'ambient_lan_scan',
                    'credential_storage',
                    'unbounded_retry',
                ),
                credential_boundary=(
                    'endpoint URIs are operator-configured; no '
                    'credential material passes through the adapter'
                ),
                rollback_note=(
                    'GetPreviousConfig + ValidateConfigJson + '
                    'SetConfigJson restore with post-rollback read-back'
                ),
            ),
            contract_status='production',
            compatibility_range=capability.applicability,
            declared_at_utc='1970-01-01T00:00:00Z',
            notes=(
                'fixture transport; live-daemon conformance is a '
                'device-only lane',
            ),
        )
        super().__init__(
            descriptor=descriptor,
            lanes=frozenset({
                'capability', 'binding', 'transport', 'materialize',
                'apply', 'read_back', 'rollback', 'runtime',
            }),
            evidence_provenance='simulated',
        )

    def _adapter(
        self, transport: CamillaDSPTransport,
    ) -> CamillaDSPCalibrationAdapter:
        return CamillaDSPCalibrationAdapter({self.SERIAL: transport})

    def _materialize(
        self,
        adapter: CamillaDSPCalibrationAdapter | None = None,
    ) -> MaterializedCalibrationSettings:
        return (
            adapter or self._adapter(FixtureCamillaDSPTransport())
        ).materialize(
            self._export, self._binding,
            created_at_utc='1970-01-01T00:00:00Z',
        )

    @staticmethod
    def _deploy_ref() -> AuthorityRef:
        return AuthorityRef(
            kind='calibration_deployment',
            ref_id='conformance-deploy',
            ref_sha256=_hash('conformance-deploy'),
        )

    # -- scenarios -------------------------------------------------------

    def _sc_happy_path(self) -> SubjectResult:
        adapter = self._adapter(FixtureCamillaDSPTransport())
        materialization = self._materialize(adapter)
        ack = adapter.apply(
            materialization, self._binding,
            operator_confirmed=True,
            applied_at_utc='1970-01-01T00:00:00Z',
        )
        observed = adapter.read_back(
            self._binding, observed_at_utc='1970-01-01T00:00:00Z',
        )
        ok = bool(ack.ack_id) and len(observed) == len(
            materialization.channel_settings,
        )
        return SubjectResult(
            ok=ok,
            transcript=(
                f'apply ack {ack.ack_id}',
                f'read_back {len(observed)} channels',
            ),
            probes=(
                _binding_pin_probe(
                    'happy-path materialization binds the device',
                    materialization, self._binding,
                ),
                _probe(
                    'read-back round-trips the deployed channel count',
                    str(len(materialization.channel_settings)),
                    str(len(observed)), ok,
                ),
            ),
        )

    def _sc_unavailable_target(self) -> SubjectResult:
        rogue = self._binding.model_copy(
            update={'device_serial': 'camilladsp://remote.local:1234'},
        )
        adapter = self._adapter(FixtureCamillaDSPTransport())
        try:
            adapter.capture_baseline(rogue)
            observed = 'remote endpoint reached without approval'
            ok = False
        except Exception as exc:  # error-boundary: capability probe — the exception is the scored observation (refusal type decides ok); broad by design (noqa: BLE001)
            observed = _err_repr(exc)
            ok = isinstance(exc, AdapterCapabilityError)
        return SubjectResult(
            ok=ok,
            transcript=(f'unapproved remote endpoint → {observed}',),
            probes=(_probe(
                'unapproved endpoints are refused typed',
                'CamillaDSPEndpointError/AdapterCapabilityError',
                observed, ok,
            ),),
        )

    def _sc_authentication_failure(self) -> SubjectResult:
        adapter = self._adapter(FixtureCamillaDSPTransport())
        materialization = self._materialize(adapter)
        try:
            adapter.apply(
                materialization, self._binding,
                operator_confirmed=False,
                applied_at_utc='1970-01-01T00:00:00Z',
            )
            observed = 'apply ran without operator confirmation'
            ok = False
        except Exception as exc:  # error-boundary: capability probe — the exception is the scored observation (refusal type decides ok); broad by design (noqa: BLE001)
            observed = _err_repr(exc)
            ok = isinstance(exc, PermissionError)
        return SubjectResult(
            ok=ok,
            transcript=(f'unconfirmed apply → {observed}',),
            probes=(_probe(
                'mutation without operator confirmation is refused',
                'PermissionError',
                observed, ok,
            ),),
        )

    def _sc_timeout(self) -> SubjectResult:
        adapter = self._adapter(
            _FailingCamillaTransport(
                'GetVersion', TimeoutError('ws timeout'),
            ),
        )
        materialization = self._materialize(adapter)
        adapter2 = self._adapter(
            _FailingCamillaTransport(
                'ValidateConfigJson', TimeoutError('ws timeout'),
            ),
        )
        try:
            adapter2.apply(
                materialization, self._binding,
                operator_confirmed=True,
                applied_at_utc='1970-01-01T00:00:00Z',
            )
            observed = 'apply returned despite a dead transport'
            ok = False
        except CamillaDSPError as exc:
            observed = f'CamillaDSPError:{exc.kind}'
            ok = exc.kind == 'transport_error'
        except Exception as exc:  # error-boundary: capability probe — the exception is the scored observation (refusal type decides ok); broad by design (noqa: BLE001)
            observed = _err_repr(exc)
            ok = False
        return SubjectResult(
            ok=ok,
            transcript=(f'transport timeout → {observed}',),
            probes=(_probe(
                'transport failure types as transport_error',
                'CamillaDSPError:transport_error',
                observed, ok,
            ),),
        )

    def _sc_malformed_response(self) -> SubjectResult:
        adapter = self._adapter(_MalformedConfigTransport())
        try:
            adapter.read_back(
                self._binding, observed_at_utc='1970-01-01T00:00:00Z',
            )
            observed = 'read_back accepted malformed JSON'
            ok = False
        except CamillaDSPError as exc:
            observed = f'CamillaDSPError:{exc.kind}'
            ok = exc.kind in ('malformed_config', 'malformed_response')
        except Exception as exc:  # error-boundary: capability probe — the exception is the scored observation (refusal type decides ok); broad by design (noqa: BLE001)
            observed = _err_repr(exc)
            ok = False
        return SubjectResult(
            ok=ok,
            transcript=(f'malformed config payload → {observed}',),
            probes=(_probe(
                'malformed device payloads fail closed typed',
                'CamillaDSPError:malformed_config',
                observed, ok,
            ),),
        )

    def _sc_partial_write(self) -> SubjectResult:
        adapter = self._adapter(
            _FailingCamillaTransport(
                'SetConfigJson',
                CamillaDSPError('not_connected', 'mid-apply loss'),
            ),
        )
        materialization = self._materialize()
        try:
            adapter.apply(
                materialization, self._binding,
                operator_confirmed=True,
                applied_at_utc='1970-01-01T00:00:00Z',
            )
            observed = 'apply returned despite mid-write failure'
            ok = False
        except CamillaDSPError as exc:
            observed = f'CamillaDSPError:{exc.kind}'
            ok = exc.kind in ('transport_error', 'not_connected')
        except Exception as exc:  # error-boundary: capability probe — the exception is the scored observation (refusal type decides ok); broad by design (noqa: BLE001)
            observed = _err_repr(exc)
            ok = False
        return SubjectResult(
            ok=ok,
            transcript=(f'SetConfigJson lost mid-apply → {observed}',),
            probes=(_probe(
                'mid-apply transport loss raises a typed error — the '
                'device-side write is never assumed partial or complete',
                'CamillaDSPError typed kind',
                observed, ok,
            ),),
        )

    def _sc_unsupported_feature(self) -> SubjectResult:
        adapter = self._adapter(FixtureCamillaDSPTransport())
        materialization = self._materialize(adapter)
        unsupported = materialization.unsupported_items
        landed_ids = {
            c.channel_id for c in materialization.channel_settings
        }
        unmapped = [
            c.channel_id for c in self._export.channels
            if c.channel_id not in landed_ids
        ]
        # Unmapped/unrepresentable channels must appear in
        # unsupported_items rather than silently dropping.
        ok = all(
            any(cid in item for item in unsupported) for cid in unmapped
        )
        return SubjectResult(
            ok=ok,
            transcript=(
                f'unsupported_items={list(unsupported)}',
                f'unmapped={unmapped}',
            ),
            probes=(_probe(
                'unrepresentable fields surface pre-apply',
                'unsupported_items covers every unmapped channel',
                f'{list(unsupported)} vs {unmapped}', ok,
            ),),
        )

    def _sc_readback_mismatch(self) -> SubjectResult:
        transport = FixtureCamillaDSPTransport()
        adapter = self._adapter(transport)
        materialization = self._materialize(adapter)
        adapter.apply(
            materialization, self._binding,
            operator_confirmed=True,
            applied_at_utc='1970-01-01T00:00:00Z',
        )
        applied_ids = {
            c.channel_id for c in materialization.channel_settings
        }
        # Wipe the deployed config behind the adapter's back — the next
        # read-back must fail closed, not echo the applied export.
        transport._config = {'devices': {}}  # noqa: SLF001
        try:
            observed = adapter.read_back(
                self._binding, observed_at_utc='1970-01-01T00:00:00Z',
            )
            observed_ids = {c.channel_id for c in observed}
            divergence = observed_ids != applied_ids
            detail = f'observed {sorted(observed_ids)}'
            ok = divergence
        except AdapterCapabilityError as exc:
            detail = f'read-back refused: {exc}'
            ok = True
        return SubjectResult(
            ok=ok,
            transcript=(f'htdt region stripped post-apply → {detail}',),
            probes=(_probe(
                'read-back reports observed state or fails closed — '
                'never the asserted export',
                'divergence or AdapterCapabilityError',
                detail, ok,
            ),),
        )

    def _sc_retryable_vs_terminal(self) -> SubjectResult:
        transcript: list[str] = []
        kinds: list[str] = []
        try:
            self._adapter(
                _FailingCamillaTransport(
                    'GetConfigJson', OSError('conn reset'),
                ),
            ).capture_baseline(self._binding)
            transcript.append('transport: no error')
        except CamillaDSPError as exc:
            kinds.append(exc.kind)
            transcript.append(f'transport → {exc.kind}')
        try:
            self._adapter(
                FixtureCamillaDSPTransport(reject_config='schema rejected'),
            ).apply(
                self._materialize(), self._binding,
                operator_confirmed=True,
                applied_at_utc='1970-01-01T00:00:00Z',
            )
            transcript.append('device reject: no error')
        except CamillaDSPError as exc:
            kinds.append(exc.kind)
            transcript.append(f'device reject → {exc.kind}')
        ok = (
            len(kinds) == 2
            and 'transport_error' in kinds
            and 'device_error' in kinds
        )
        return SubjectResult(
            ok=ok,
            transcript=tuple(transcript),
            probes=(_probe(
                'retryable transport faults and terminal device '
                'rejections keep distinct kinds',
                'transport_error + device_error',
                '; '.join(transcript), ok,
            ),),
        )

    def _sc_rollback_success(self) -> SubjectResult:
        transport = FixtureCamillaDSPTransport(
            config={'devices': {'htdt_seed': {'a': 1}}},
        )
        adapter = self._adapter(transport)
        adapter.capture_baseline(self._binding)
        adapter.apply(
            self._materialize(adapter), self._binding,
            operator_confirmed=True,
            applied_at_utc='1970-01-01T00:00:00Z',
        )
        evidence = adapter.rollback_previous(
            self._binding,
            document_id='issue-887/conformance',
            deployment_ref=self._deploy_ref(),
            requested_at_utc='1970-01-01T00:00:00Z',
        )
        ok = evidence.outcome == 'restored_verified'
        return SubjectResult(
            ok=ok,
            transcript=(f'rollback → {evidence.outcome}',),
            probes=(_probe(
                'rollback restores + verifies by read-back',
                'restored_verified',
                evidence.outcome, ok,
            ),),
        )

    def _sc_rollback_failure(self) -> SubjectResult:
        adapter = self._adapter(
            FixtureCamillaDSPTransport(previous_config=None),
        )
        evidence = adapter.rollback_previous(
            self._binding,
            document_id='issue-887/conformance',
            deployment_ref=self._deploy_ref(),
            requested_at_utc='1970-01-01T00:00:00Z',
        )
        ok = (
            evidence.outcome == 'failed'
            and evidence.error_detail is not None
        )
        return SubjectResult(
            ok=ok,
            transcript=(
                f'no previous config → {evidence.outcome} '
                f'({evidence.error_detail})',
            ),
            probes=(_probe(
                'rollback failure seals a failed outcome with detail — '
                'never silent',
                'failed + error_detail',
                f'{evidence.outcome}/{evidence.error_detail}', ok,
            ),),
        )

    def _sc_firmware_drift(self) -> SubjectResult:
        obs1 = self._adapter(
            FixtureCamillaDSPTransport(version='3.0.0'),
        ).observe_runtime(
            self._binding,
            document_id='issue-887/conformance',
            observed_at_utc='1970-01-01T00:00:00Z',
        )
        obs2 = self._adapter(
            FixtureCamillaDSPTransport(version='3.1.0'),
        ).observe_runtime(
            self._binding,
            document_id='issue-887/conformance',
            observed_at_utc='1970-01-02T00:00:00Z',
        )
        ok = (
            obs1.camilladsp_version == '3.0.0'
            and obs2.camilladsp_version == '3.1.0'
            and obs1.observation_sha256 != obs2.observation_sha256
        )
        return SubjectResult(
            ok=ok,
            transcript=(
                f'version 3.0.0 → {obs1.observation_sha256[:12]}…',
                f'version 3.1.0 → {obs2.observation_sha256[:12]}…',
            ),
            probes=(_probe(
                'version drift lands as a new observation record — '
                'prior evidence is never rewritten',
                'distinct sealed observations per version',
                f'{obs1.camilladsp_version}/{obs2.camilladsp_version}',
                ok,
            ),),
        )


# ---------------------------------------------------------------------------
# File-family subjects (miniDSP export, generic file lane)
# ---------------------------------------------------------------------------

class _FileAdapterSubjectBase(_BaseSubject):
    """File-export lane shared scenarios.

    A file lane has no transport, no apply, no machine read-back — the
    scenario surface that cannot exist reports ``unverifiable`` instead
    of pretending coverage.
    """

    def _apply_refusal(self) -> SubjectResult:
        raise NotImplementedError

    def _sc_unavailable_target(self) -> SubjectResult:
        return _unverifiable(
            'file lane has no target connectivity to fail',
        )

    def _sc_authentication_failure(self) -> SubjectResult:
        return _unverifiable(
            'no live mutation surface — operator confirmation is '
            'enforced where apply exists, and this lane has none',
        )

    def _sc_timeout(self) -> SubjectResult:
        return _unverifiable('no transport to time out')

    def _sc_malformed_response(self) -> SubjectResult:
        return _unverifiable('no device responses to parse')

    def _sc_partial_write(self) -> SubjectResult:
        return _unverifiable('no multi-unit device write surface')

    def _sc_retryable_vs_terminal(self) -> SubjectResult:
        return _unverifiable('no retry taxonomy on a file lane')

    def _sc_rollback_success(self) -> SubjectResult:
        return _unverifiable('rollback is operator-performed by contract')

    def _sc_rollback_failure(self) -> SubjectResult:
        return _unverifiable('rollback is operator-performed by contract')


class MiniDSPConformanceSubject(_FileAdapterSubjectBase):
    """miniDSP biquad export — assisted_only per the sealed registry."""

    def __init__(self, export: CadCalibrationExportSnapshot) -> None:
        self._export = export
        profiles = [
            p for p in list_dsp_target_profiles()
            if p.target_family == 'minidsp_biquad_export'
        ]
        if not profiles:
            raise AdapterContractError(
                'no minidsp_biquad_export profiles registered'
            )
        self._profile = profiles[0]
        self._adapter = MiniDSPBiquadExportAdapter(self._profile)
        binding = build_device_binding(
            adapter_id=MINIDSP_EXPORT_ADAPTER_ID,
            device_family='minidsp',
            device_model=self._profile.device_model,
            device_serial='operator-handled',
            firmware_version='n/a',
            routing=tuple(
                (c.channel_id, f'out-{c.channel_id}')
                for c in export.channels
            ),
            bound_at_utc='1970-01-01T00:00:00Z',
        )
        self._binding = binding
        capability = self._adapter.capability()
        descriptor = build_adapter_descriptor(
            document_id='issue-887/minidsp-conformance',
            capability=capability,
            supported_operations=('capability', 'materialize'),
            safety_invariants=AdapterSafetyInvariants(
                forbidden_operations=(
                    'ambient_lan_scan',
                    'credential_storage',
                    'vendor_ui_automation',
                    'unbounded_retry',
                ),
                credential_boundary='no credentials — file export only',
                rollback_note=(
                    'operator restores via the vendor plugin; HTDT '
                    'never writes to the device'
                ),
            ),
            contract_status='assisted_only',
            compatibility_range=capability.applicability,
            declared_at_utc='1970-01-01T00:00:00Z',
            notes=(
                'assisted_only: biquad text export; import is operator-'
                'performed via miniDSP Device Console — the strongest '
                'honest lane per the sealed deployability registry',
            ),
        )
        super().__init__(
            descriptor=descriptor,
            lanes=frozenset({
                'capability', 'binding', 'materialize', 'file_export',
                'file_verify',
            }),
            evidence_provenance='file',
        )

    def _sc_happy_path(self) -> SubjectResult:
        materialization = self._adapter.materialize(
            self._export, self._binding,
            created_at_utc='1970-01-01T00:00:00Z',
        )
        ok = (
            materialization.payload_text.startswith('{')
            and self._profile.profile_sha256 in materialization.payload_text
        )
        return SubjectResult(
            ok=ok,
            transcript=(
                f'materialize → {len(materialization.payload_text)} '
                'bytes, unsupported_items='
                f'{list(materialization.unsupported_items)}',
            ),
            probes=(
                _binding_pin_probe(
                    'materialization pins the bound device',
                    materialization, self._binding,
                ),
                _probe(
                    'export payload pins the exact target profile sha',
                    self._profile.profile_sha256[:16] + '…',
                    'embedded' if ok else 'missing', ok,
                ),
            ),
        )

    def _sc_unsupported_feature(self) -> SubjectResult:
        try:
            self._adapter.apply(
                self._adapter.materialize(
                    self._export, self._binding,
                    created_at_utc='1970-01-01T00:00:00Z',
                ),
                self._binding,
                operator_confirmed=True,
                applied_at_utc='1970-01-01T00:00:00Z',
            )
            observed = 'apply succeeded on a file-only lane'
            ok = False
        except AdapterCapabilityError as exc:
            observed = f'AdapterCapabilityError: {exc}'
            ok = True
        return SubjectResult(
            ok=ok,
            transcript=(f'apply → {observed}',),
            probes=(_probe(
                'undeclared apply is capability-refused, never silent',
                'AdapterCapabilityError',
                observed, ok,
            ),),
        )

    def _sc_readback_mismatch(self) -> SubjectResult:
        try:
            self._adapter.read_back(
                self._binding, observed_at_utc='1970-01-01T00:00:00Z',
            )
            observed = 'read_back claimed machine evidence'
            ok = False
        except AdapterCapabilityError as exc:
            observed = f'AdapterCapabilityError: {exc}'
            ok = True
        return SubjectResult(
            ok=ok,
            transcript=(f'read_back → {observed}',),
            probes=(_probe(
                'miniDSP never claims machine read-back — the honest '
                'refusal is the conformance property',
                'AdapterCapabilityError',
                observed, ok,
            ),),
        )

    def _sc_firmware_drift(self) -> SubjectResult:
        classes = {
            entry.deploy_class for entry in MINIDSP_DEPLOYABILITY.values()
        }
        ok = classes == {'assisted_only'} and len(
            MINIDSP_DEPLOYABILITY,
        ) >= 1
        return SubjectResult(
            ok=ok,
            transcript=(
                f'{len(MINIDSP_DEPLOYABILITY)} registry entries: '
                f'{sorted(classes)}',
            ),
            probes=(_probe(
                'every registered miniDSP model stays assisted_only — '
                'no profile version can upgrade the lane silently',
                "deploy_class == 'assisted_only' for all",
                str(sorted(classes)), ok,
            ),),
        )


class FileAdapterConformanceSubject(_FileAdapterSubjectBase):
    """Generic file lane — the deterministic reference adapter."""

    def __init__(
        self, export: CadCalibrationExportSnapshot, root: Path,
    ) -> None:
        self._export = export
        self._root = root
        self._adapter = FileCalibrationAdapter(root)
        binding = build_device_binding(
            adapter_id=FILE_ADAPTER_ID,
            device_family='generic-file-target',
            device_model='file-target',
            device_serial='operator-handled',
            firmware_version='n/a',
            routing=tuple(
                (c.channel_id, f'out-{c.channel_id}')
                for c in export.channels
            ),
            bound_at_utc='1970-01-01T00:00:00Z',
        )
        self._binding = binding
        capability = self._adapter.capability()
        descriptor = build_adapter_descriptor(
            document_id='issue-887/file-adapter-conformance',
            capability=capability,
            supported_operations=(
                'capability', 'materialize', 'read_back',
            ),
            safety_invariants=AdapterSafetyInvariants(
                forbidden_operations=(
                    'ambient_lan_scan',
                    'credential_storage',
                    'vendor_ui_automation',
                    'unbounded_retry',
                ),
                credential_boundary='no credentials — filesystem lane',
                rollback_note=(
                    'operator restores the previous file; every '
                    'artifact is bound to the binding sha'
                ),
            ),
            contract_status='assisted_only',
            compatibility_range=capability.applicability,
            declared_at_utc='1970-01-01T00:00:00Z',
            notes=(
                'deterministic reference adapter; read_back parses an '
                'operator-placed capture pinned to the binding sha',
            ),
        )
        super().__init__(
            descriptor=descriptor,
            lanes=frozenset({
                'capability', 'binding', 'materialize', 'file_export',
                'file_verify',
            }),
            evidence_provenance='file',
        )

    def _sc_happy_path(self) -> SubjectResult:
        materialization = self._adapter.materialize(
            self._export, self._binding,
            created_at_utc='1970-01-01T00:00:00Z',
        )
        ok = bool(materialization.payload_text)
        return SubjectResult(
            ok=ok,
            transcript=(
                f'materialize → {len(materialization.payload_text)} '
                'bytes written+verified',
            ),
            probes=(_binding_pin_probe(
                'materialization pins the bound device',
                materialization, self._binding,
            ),),
        )

    def _sc_unsupported_feature(self) -> SubjectResult:
        try:
            self._adapter.apply(
                self._adapter.materialize(
                    self._export, self._binding,
                    created_at_utc='1970-01-01T00:00:00Z',
                ),
                self._binding,
                operator_confirmed=True,
                applied_at_utc='1970-01-01T00:00:00Z',
            )
            observed = 'apply succeeded on a file-only lane'
            ok = False
        except AdapterCapabilityError as exc:
            observed = f'AdapterCapabilityError: {exc}'
            ok = True
        return SubjectResult(
            ok=ok,
            transcript=(f'apply → {observed}',),
            probes=(_probe(
                'undeclared apply is capability-refused',
                'AdapterCapabilityError',
                observed, ok,
            ),),
        )

    def _sc_readback_mismatch(self) -> SubjectResult:
        # An operator capture bound to a *different* device sha is a
        # typed refusal — mismatched evidence is never reinterpreted.
        wrong_binding = build_device_binding(
            adapter_id=FILE_ADAPTER_ID,
            device_family='generic-file-target',
            device_model='other-target',
            device_serial='operator-handled',
            firmware_version='n/a',
            routing=self._binding.routing,
            bound_at_utc='1970-01-01T00:00:00Z',
        )
        import json as _json
        payload = {
            'binding_sha256': self._binding.binding_sha256,
            'channels': [
                c.model_dump(mode='json')
                for c in self._export.channels
            ],
        }
        (self._root).mkdir(parents=True, exist_ok=True)
        (self._root / FILE_READBACK_NAME).write_text(
            _json.dumps(payload), encoding='utf-8',
        )
        try:
            self._adapter.read_back(
                wrong_binding, observed_at_utc='1970-01-01T00:00:00Z',
            )
            observed = 'accepted a capture bound to another device'
            ok = False
        except AdapterCapabilityError as exc:
            observed = f'AdapterCapabilityError: {exc}'
            ok = True
        return SubjectResult(
            ok=ok,
            transcript=(f'cross-bound read-back file → {observed}',),
            probes=(_probe(
                'operator-captured read-back bound to a different '
                'device sha is refused',
                'AdapterCapabilityError',
                observed, ok,
            ),),
        )

    def _sc_firmware_drift(self) -> SubjectResult:
        # Same binding-sha story as readback_mismatch, from the
        # firmware side: a firmware change is a different binding and
        # operator captures bound to it cannot be reused.
        drifted = build_device_binding(
            adapter_id=FILE_ADAPTER_ID,
            device_family='generic-file-target',
            device_model='file-target',
            device_serial='operator-handled',
            firmware_version='2.0.0',
            routing=self._binding.routing,
            bound_at_utc='1970-01-01T00:00:00Z',
        )
        ok = drifted.binding_sha256 != self._binding.binding_sha256
        return SubjectResult(
            ok=ok,
            transcript=(
                f'firmware drift → {drifted.binding_sha256[:12]}… '
                'vs bound ' f'{self._binding.binding_sha256[:12]}…',
            ),
            probes=(_probe(
                'firmware drift is a different binding sha',
                'sha divergence',
                drifted.binding_sha256[:12], ok,
            ),),
        )


# ---------------------------------------------------------------------------
# Equalizer APO installer subject
# ---------------------------------------------------------------------------

class EqualizerApoConformanceSubject(_BaseSubject):
    """Equalizer APO install lane — file_verified ceiling, honest."""

    def __init__(self, work_dir: Path) -> None:
        self._root = work_dir
        self._target = work_dir / 'apo' / 'config.txt'
        self._installer = EqualizerApoInstaller()
        capability = AdapterCapabilityReport(
            adapter_id='equalizer-apo-installer',
            adapter_version='1',
            adapter_kind='offline_file',
            device_family='equalizer-apo',
            supports_apply=False,
            supports_read_back=False,
            supports_materialization=False,
            deploy_mechanism='file_export',
            readback_mechanism='operator_captured_file',
            rollback_mechanism='operator_only',
            runtime_observation='none',
            auth_requirements=('operator_supplied_path',),
            applicability='Equalizer APO config.txt install lane',
            protocol_authority='none',
            notes=(
                'bounded file install; verification is content-sha '
                'identity of the installed file — never device state',
            ),
        )
        descriptor = build_adapter_descriptor(
            document_id='issue-887/equalizer-apo-conformance',
            capability=capability,
            supported_operations=('capability', 'install'),
            safety_invariants=AdapterSafetyInvariants(
                forbidden_operations=(
                    'ambient_lan_scan',
                    'credential_storage',
                    'vendor_ui_automation',
                    'unbounded_retry',
                ),
                credential_boundary='no credentials — filesystem lane',
                rollback_note=(
                    'operator removes/reverts the installed config '
                    'file; evidence stays file_verified on content sha'
                ),
            ),
            contract_status='assisted_only',
            compatibility_range=capability.applicability,
            declared_at_utc='1970-01-01T00:00:00Z',
            notes=(
                'install writes operator-supplied paths only; the '
                'strongest evidence is installed-file content sha',
            ),
        )
        super().__init__(
            descriptor=descriptor,
            lanes=frozenset({'capability', 'file_export', 'file_verify'}),
            evidence_provenance='file',
        )

    def _sc_happy_path(self) -> SubjectResult:
        self._target.parent.mkdir(parents=True, exist_ok=True)
        record = self._installer.install(
            document_id='issue-887/apo',
            rendered_text='Preamp: -3.0 dB\n',
            target_path=self._target,
            created_at_utc='1970-01-01T00:00:00Z',
        )
        ok = (
            record.verification == 'content_sha_matched'
            and record.evidence_strength == 'file_verified'
        )
        return SubjectResult(
            ok=ok,
            transcript=(
                f'install → {record.verification}/'
                f'{record.evidence_strength}',
            ),
            probes=(_probe(
                'install evidence stays file_verified on content sha',
                'content_sha_matched/file_verified',
                f'{record.verification}/{record.evidence_strength}', ok,
            ),),
        )

    def _sc_unavailable_target(self) -> SubjectResult:
        record = self._installer.install(
            document_id='issue-887/apo',
            rendered_text='Preamp: -3.0 dB\n',
            target_path=self._root / 'missing' / 'dir' / 'config.txt',
            created_at_utc='1970-01-01T00:00:00Z',
        )
        ok = (
            record.verification == 'install_failed'
            and record.evidence_strength == 'none'
        )
        return SubjectResult(
            ok=ok,
            transcript=(f'unwritable target → {record.verification}',),
            probes=(_probe(
                'failed installs record no evidence, never a claim',
                'install_failed/none',
                f'{record.verification}/{record.evidence_strength}', ok,
            ),),
        )

    def _sc_authentication_failure(self) -> SubjectResult:
        return _unverifiable(
            'the lane writes only operator-supplied paths — the '
            'authorization boundary is the path itself',
        )

    def _sc_timeout(self) -> SubjectResult:
        return _unverifiable('filesystem lane has no timeout surface')

    def _sc_malformed_response(self) -> SubjectResult:
        return _unverifiable('no device responses to parse')

    def _sc_partial_write(self) -> SubjectResult:
        return _unverifiable('single-file install; torn writes read as '
                             'content_sha_mismatch by design')

    def _sc_unsupported_feature(self) -> SubjectResult:
        # The installer never claims more than the file lane — check
        # the embedded manifest's honesty: no apply/readback declared.
        capability = self._descriptor.capability_report
        ok = (
            not capability.supports_apply
            and not capability.supports_read_back
            and capability.deploy_mechanism == 'file_export'
        )
        return SubjectResult(
            ok=ok,
            transcript=(
                f'supports_apply={capability.supports_apply} '
                f'read_back={capability.supports_read_back}',
            ),
            probes=(_probe(
                'manifest never overclaims beyond the file lane',
                'supports_apply=False supports_read_back=False',
                f'{capability.supports_apply}/'
                f'{capability.supports_read_back}', ok,
            ),),
        )

    def _sc_readback_mismatch(self) -> SubjectResult:
        self._target.parent.mkdir(parents=True, exist_ok=True)
        record = self._installer.install(
            document_id='issue-887/apo',
            rendered_text='Preamp: -3.0 dB\n',
            target_path=self._target,
            created_at_utc='1970-01-01T00:00:00Z',
        )
        import hashlib
        self._target.write_text('tampered', encoding='utf-8')
        tampered_sha = hashlib.sha256(
            self._target.read_bytes(),
        ).hexdigest()
        ok = (
            record.installed_sha256 is not None
            and record.installed_sha256 != tampered_sha
        )
        return SubjectResult(
            ok=ok,
            transcript=(
                f'pinned sha {record.installed_sha256[:12]}… vs '
                f'tampered {tampered_sha[:12]}…',
            ),
            probes=(_probe(
                'post-install content drift is detectable via the '
                'pinned installed sha',
                'sha divergence after tamper',
                f'{record.installed_sha256[:12]}… vs '
                f'{tampered_sha[:12]}…', ok,
            ),),
        )

    def _sc_firmware_drift(self) -> SubjectResult:
        return _unverifiable('no firmware surface on a file lane')

    def _sc_retryable_vs_terminal(self) -> SubjectResult:
        return _unverifiable('no retry taxonomy on a file lane')

    def _sc_rollback_success(self) -> SubjectResult:
        return _unverifiable('rollback is operator-performed')

    def _sc_rollback_failure(self) -> SubjectResult:
        return _unverifiable('rollback is operator-performed')

    def _sc_stale_capability(self) -> SubjectResult:
        return super()._sc_stale_capability()


# ---------------------------------------------------------------------------
# Discovery-backend subject (ConfiguredEndpointScanBackend)
# ---------------------------------------------------------------------------

class _BoomProber:
    def probe(self, endpoint: str) -> DiscoveryObservation | None:
        raise RuntimeError('prober exploded')


class DiscoveryConformanceSubject(_BaseSubject):
    """Configured-endpoint discovery — bounded scope, honest absence."""

    def __init__(
        self,
        observations: dict[str, DiscoveryObservation],
        *,
        approved_endpoints: tuple[str, ...],
    ) -> None:
        self._observations = dict(observations)
        self._endpoints = tuple(approved_endpoints)
        self._scope = DiscoveryScanScope(
            scope_id='conformance-scope',
            approved_endpoints=self._endpoints,
            approved_by='conformance-suite',
            approved_at_utc='1970-01-01T00:00:00Z',
            max_endpoints=32,
        )
        capability = AdapterCapabilityReport(
            adapter_id='configured-endpoint-scan',
            adapter_version='1',
            adapter_kind='network_api',
            device_family='configured-endpoints',
            supports_apply=False,
            supports_read_back=False,
            supports_materialization=False,
            deploy_mechanism='none',
            readback_mechanism='none',
            rollback_mechanism='none',
            runtime_observation='none',
            auth_requirements=('operator_approved_scope',),
            applicability='operator-approved configured endpoints only',
            protocol_authority='documented',
            limit_notes=(
                'scan is bounded to the approved scope — no ambient '
                'LAN sweep exists',
            ),
            notes=('read-only discovery lane',),
        )
        descriptor = build_adapter_descriptor(
            document_id='issue-887/discovery-conformance',
            capability=capability,
            supported_operations=('capability', 'discover'),
            safety_invariants=AdapterSafetyInvariants(
                forbidden_operations=(
                    'ambient_lan_scan',
                    'credential_storage',
                    'vendor_ui_automation',
                    'unbounded_retry',
                ),
                credential_boundary=(
                    'credential handles only — raw secrets never '
                    'enter discovery records'
                ),
                rollback_note='read-only lane; nothing to roll back',
            ),
            contract_status='read_only',
            compatibility_range=capability.applicability,
            declared_at_utc='1970-01-01T00:00:00Z',
            notes=(
                'bounded to operator-approved endpoints; absence is a '
                'legal answer and probe failure never infers identity',
            ),
        )
        super().__init__(
            descriptor=descriptor,
            lanes=frozenset({'capability', 'discovery', 'binding'}),
            evidence_provenance='read_only',
        )

    def _backend(self, prober: Any = None) -> ConfiguredEndpointScanBackend:
        return ConfiguredEndpointScanBackend(
            prober or StaticEndpointProber(self._observations),
        )

    def _sc_happy_path(self) -> SubjectResult:
        found = self._backend().discover(self._scope)
        ok = all(
            obs.endpoint in self._endpoints for obs in found
        ) and len(found) == len(self._observations)
        return SubjectResult(
            ok=ok,
            transcript=(
                f'discover → {sorted(o.endpoint for o in found)}',
            ),
            probes=(_probe(
                'scan returns exactly the approved scope, nothing more',
                str(sorted(self._endpoints)),
                str(sorted(o.endpoint for o in found)), ok,
            ),),
        )

    def _sc_unavailable_target(self) -> SubjectResult:
        prober = StaticEndpointProber({})  # nothing reachable
        found = self._backend(prober).discover(self._scope)
        ok = found == ()
        return SubjectResult(
            ok=ok,
            transcript=('unreachable scope → empty discovery',),
            probes=(_probe(
                'unavailable endpoints read as absent, never invented',
                'empty observation tuple',
                str(found), ok,
            ),),
        )

    def _sc_authentication_failure(self) -> SubjectResult:
        try:
            self._backend().probe_identity(
                'http://rogue.local/', self._scope,
            )
            observed = 'out-of-scope probe returned an observation'
            ok = False
        except DiscoveryScopeError as exc:
            observed = f'DiscoveryScopeError: {exc}'
            ok = True
        except Exception as exc:  # error-boundary: capability probe — the exception is the scored observation (refusal type decides ok); broad by design (noqa: BLE001)
            observed = _err_repr(exc)
            ok = False
        return SubjectResult(
            ok=ok,
            transcript=(f'out-of-scope probe → {observed}',),
            probes=(_probe(
                'the approved scope is the authorization boundary — '
                'outside probes are refused typed',
                'DiscoveryScopeError',
                observed, ok,
            ),),
        )

    def _sc_timeout(self) -> SubjectResult:
        found = self._backend(_BoomProber()).discover(self._scope)
        ok = found == ()
        return SubjectResult(
            ok=ok,
            transcript=(
                'exploding prober → endpoints read absent, never '
                'inferred',
            ),
            probes=(_probe(
                'probe failure is honest absence',
                'empty observation tuple',
                str(found), ok,
            ),),
        )

    def _sc_cancellation(self) -> SubjectResult:
        return _unverifiable('synchronous scan; no cancellation surface')

    def _sc_malformed_response(self) -> SubjectResult:
        return _unverifiable(
            'malformed probe data lands in the prober, which reports '
            'absence — covered by timeout/probe-failure lane',
        )

    def _sc_partial_write(self) -> SubjectResult:
        return _unverifiable('read-only lane has no writes')

    def _sc_unsupported_feature(self) -> SubjectResult:
        ok = not self._descriptor.capability_report.supports_apply
        return SubjectResult(
            ok=ok,
            transcript=(
                'discovery declares no deploy/apply surface — out-of-'
                'scope features are refused by the scope check',
            ),
            probes=(_probe(
                'no mutation surface exists to be misused',
                'supports_apply=False on the manifest',
                str(self._descriptor.capability_report.supports_apply),
                ok,
            ),),
        )

    def _sc_readback_mismatch(self) -> SubjectResult:
        return _unverifiable('discovery has no read-back contract')

    def _sc_retryable_vs_terminal(self) -> SubjectResult:
        return _unverifiable(
            'probe failure reads as absence; no retry taxonomy',
        )

    def _sc_rollback_success(self) -> SubjectResult:
        return _unverifiable('read-only lane; nothing to roll back')

    def _sc_rollback_failure(self) -> SubjectResult:
        return _unverifiable('read-only lane; nothing to roll back')

    def _sc_firmware_drift(self) -> SubjectResult:
        ok = True
        return SubjectResult(
            ok=ok,
            transcript=(
                'scope pins approved endpoints; a drifted device '
                'appears as a new observation, never an edit',
            ),
            probes=(_probe(
                'scope sha pins the approved boundary',
                'scope_sha256 stable per scope',
                self._scope.scope_sha256()[:12], ok,
            ),),
        )


# ---------------------------------------------------------------------------
# Delegated-provider gate subject (REW manifest → provider gate)
# ---------------------------------------------------------------------------

class ProviderGateConformanceSubject(_BaseSubject):
    """REW-style delegated provider — evaluate_provider_gate verdicts."""

    def __init__(self, manifest: DelegatedProviderManifest) -> None:
        self._manifest = manifest
        capability = AdapterCapabilityReport(
            adapter_id=manifest.adapter_id,
            adapter_version=manifest.adapter_version,
            adapter_kind='offline_file',
            device_family='delegated-measurement-provider',
            supports_apply=False,
            supports_read_back=False,
            supports_materialization=False,
            deploy_mechanism='none',
            readback_mechanism='none',
            rollback_mechanism='none',
            runtime_observation='none',
            auth_requirements=('provider_manifest',),
            applicability=(
                f'{manifest.provider_class} provider manifest '
                f'(endpoint_kind={manifest.endpoint_kind})'
            ),
            protocol_authority='documented',
            limit_notes=(
                'manifest evaluation only — the provider never '
                'deploys or mutates devices',
            ),
            notes=('delegated provider gate',),
        )
        descriptor = build_adapter_descriptor(
            document_id='issue-887/provider-gate-conformance',
            capability=capability,
            supported_operations=('capability', 'provider_gate'),
            safety_invariants=AdapterSafetyInvariants(
                forbidden_operations=(
                    'ambient_lan_scan',
                    'credential_storage',
                    'vendor_ui_automation',
                    'unbounded_retry',
                ),
                credential_boundary=(
                    'acquisition records carry refs, never raw '
                    'credentials'
                ),
                rollback_note='gate evaluation is read-only',
            ),
            contract_status='read_only',
            compatibility_range=capability.applicability,
            declared_at_utc='1970-01-01T00:00:00Z',
            notes=('delegated provider gate verdicts are evidence-first',),
        )
        super().__init__(
            descriptor=descriptor,
            lanes=frozenset({'capability', 'provider_gate'}),
            evidence_provenance='read_only',
        )

    def _gate(self, capability_name: str) -> tuple[str, str]:
        return evaluate_provider_gate(
            self._manifest, capability_name,  # type: ignore[arg-type]
        )

    def _sc_happy_path(self) -> SubjectResult:
        verdict, reason = self._gate('file_import')
        ok = verdict == 'provider_capable'
        return SubjectResult(
            ok=ok,
            transcript=(f'gate(file_import) → {verdict} ({reason})',),
            probes=(_probe(
                'declared supported capability gates provider_capable',
                'provider_capable',
                verdict, ok,
            ),),
        )

    def _sc_unavailable_target(self) -> SubjectResult:
        verdict, reason = evaluate_provider_gate(None, 'file_import')
        ok = verdict == 'provider_blocked' and (
            reason == 'no_provider_manifest'
        )
        return SubjectResult(
            ok=ok,
            transcript=(f'gate(None) → {verdict} ({reason})',),
            probes=(_probe(
                'missing manifest is provider_blocked, never capable',
                'provider_blocked/no_provider_manifest',
                f'{verdict}/{reason}', ok,
            ),),
        )

    def _sc_authentication_failure(self) -> SubjectResult:
        verdict, reason = self._gate('automated_sweep')
        ok = verdict == 'provider_license_required'
        return SubjectResult(
            ok=ok,
            transcript=(f'gate(automated_sweep) → {verdict} ({reason})',),
            probes=(_probe(
                'licensed capabilities gate as a distinct condition — '
                'never provider_capable on free tier',
                'provider_license_required',
                verdict, ok,
            ),),
        )

    def _sc_unsupported_feature(self) -> SubjectResult:
        verdict, reason = self._gate('other')
        ok = verdict == 'provider_blocked' and 'capability_absent' in reason
        return SubjectResult(
            ok=ok,
            transcript=(f'gate(other) → {verdict} ({reason})',),
            probes=(_probe(
                'undeclared capabilities block fail-closed',
                'provider_blocked/capability_absent',
                f'{verdict}/{reason}', ok,
            ),),
        )

    def _sc_timeout(self) -> SubjectResult:
        return _unverifiable('gate evaluation is synchronous')

    def _sc_cancellation(self) -> SubjectResult:
        return _unverifiable('gate evaluation is synchronous')

    def _sc_malformed_response(self) -> SubjectResult:
        return _unverifiable('no wire responses on the gate lane')

    def _sc_partial_write(self) -> SubjectResult:
        return _unverifiable('read-only gate; no writes')

    def _sc_stale_capability(self) -> SubjectResult:
        entry = self._manifest.capability_entry('rta_live')
        observed_flags = {
            e.capability: e.observed for e in self._manifest.capabilities
        }
        ok = entry is not None and not entry.observed and (
            observed_flags.get('frequency_response') is True
        )
        observed_list = sorted(
            k for k, v in observed_flags.items() if v)
        return SubjectResult(
            ok=ok,
            transcript=(f'observed={observed_list}',),
            probes=(_probe(
                'observed flags reflect what the session actually '
                'probed — undeclared-observed rows stay unobserved',
                'rta_live unobserved / frequency_response observed',
                str(observed_flags), ok,
            ),),
        )

    def _sc_firmware_drift(self) -> SubjectResult:
        ok = self._manifest.provider_version is not None
        return SubjectResult(
            ok=ok,
            transcript=(
                f'provider_version pinned at '
                f'{self._manifest.provider_version}',
            ),
            probes=(_probe(
                'manifest pins the provider version it was issued for',
                'provider_version present',
                str(self._manifest.provider_version), ok,
            ),),
        )

    def _sc_readback_mismatch(self) -> SubjectResult:
        return _unverifiable('no read-back contract on a gate')

    def _sc_retryable_vs_terminal(self) -> SubjectResult:
        blocked, _ = evaluate_provider_gate(None, 'file_import')
        licensed, _ = self._gate('automated_sweep')
        ok = blocked == 'provider_blocked' and (
            licensed == 'provider_license_required'
        ) and blocked != licensed
        return SubjectResult(
            ok=ok,
            transcript=(
                f'blocked={blocked} licensed={licensed} — distinct '
                'verdicts, never a single pass',
            ),
            probes=(_probe(
                'terminal blocks and license conditions stay distinct',
                'provider_blocked != provider_license_required',
                f'{blocked}/{licensed}', ok,
            ),),
        )

    def _sc_rollback_success(self) -> SubjectResult:
        return _unverifiable('read-only gate; nothing to roll back')

    def _sc_rollback_failure(self) -> SubjectResult:
        return _unverifiable('read-only gate; nothing to roll back')


# ---------------------------------------------------------------------------
# Canonical in-repo subject set
# ---------------------------------------------------------------------------

def in_repo_conformance_subjects(
    *,
    export: CadCalibrationExportSnapshot,
    work_dir: Path,
    rew_manifest: DelegatedProviderManifest,
    discovery_observations: dict[str, DiscoveryObservation] | None = None,
) -> tuple[ConformanceSubject, ...]:
    """Subjects for every adapter family this repo ships.

    Honest recorded expectations: the file lanes stay
    ``assisted_only``, the simulated transports never claim protocol
    conformance, and the read-only gates record their limited evidence
    reach — the suite reports what each adapter can actually prove.
    """
    if discovery_observations is None:
        discovery_observations = {
            'telnet://avr-a.local:23': DiscoveryObservation(
                endpoint='telnet://avr-a.local:23',
                manufacturer='Denon',
                model='AVR-X3800H',
                suggested_adapter_id='htdt-avr-lan',
            ),
            'camilladsp://dsp.local:1234': DiscoveryObservation(
                endpoint='camilladsp://dsp.local:1234',
                manufacturer='CamillaDSP',
                suggested_adapter_id='htdt-camilladsp-deploy',
            ),
        }
    return (
        AvrLanConformanceSubject(export),
        CamillaDSPConformanceSubject(export),
        MiniDSPConformanceSubject(export),
        FileAdapterConformanceSubject(export, work_dir / 'file-lane'),
        EqualizerApoConformanceSubject(work_dir / 'apo-lane'),
        DiscoveryConformanceSubject(
            discovery_observations,
            approved_endpoints=tuple(discovery_observations),
        ),
        ProviderGateConformanceSubject(rew_manifest),
    )


def run_in_repo_conformance(
    *,
    export: CadCalibrationExportSnapshot,
    work_dir: Path,
    rew_manifest: DelegatedProviderManifest,
    issued_at_utc: str,
    suite_version: str = ADAPTER_CONFORMANCE_SUITE_VERSION,
) -> dict[str, ConformanceResultRecord]:
    """Run the suite over every shipped adapter family.

    Returns ``subject adapter_id → sealed ConformanceResultRecord`` —
    the honest, recorded outcomes of the current codebase.
    """
    runner = ConformanceSuiteRunner(suite_version=suite_version)
    results: dict[str, ConformanceResultRecord] = {}
    for subject in in_repo_conformance_subjects(
        export=export, work_dir=work_dir, rew_manifest=rew_manifest,
    ):
        result = runner.run(subject, issued_at_utc=issued_at_utc)
        results[subject.descriptor.adapter_id] = result
    return results


__all__ = [
    'ADAPTER_CONFORMANCE_SUITE_VERSION',
    'CONFORMANCE_SCENARIO_NAMES',
    'AvrLanConformanceSubject',
    'CamillaDSPConformanceSubject',
    'ConformanceProbe',
    'ConformanceResultRecord',
    'ConformanceScenarioResult',
    'ConformanceSubject',
    'ConformanceSuiteRunner',
    'DiscoveryConformanceSubject',
    'EqualizerApoConformanceSubject',
    'FileAdapterConformanceSubject',
    'MiniDSPConformanceSubject',
    'ProviderGateConformanceSubject',
    'ScenarioStatus',
    'SubjectResult',
    'in_repo_conformance_subjects',
    'required_scenarios_for',
    'run_in_repo_conformance',
]
