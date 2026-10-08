"""Sealed headless-run evidence for the ``htdt`` command line (#888).

Every mutating CLI verb seals a :class:`CadHeadlessRunRecord` — even when
the workflow failed, was blocked, or was refused for missing
authorization — so the batch automation lane carries the same sealed,
append-only evidence discipline as the GUI lanes. The record pins:

* the resolved input spec (canonical JSON + sha256),
* tool + environment identity (tool version/commit/dirty, Python and
  platform fingerprints, argv sha),
* backend identity (with the simulated-honesty flag),
* the workflow's own verdict token and the produced ``AuthorityRef``s,
* run timing (start/finish UTC, elapsed, timeout, cancelled flag).

Records are sealed with the repo's ``_seal`` convention and persisted by
``CadHeadlessRunRepository`` through ``_SealedStore`` — save-time seal
re-verification and read-time column-vs-payload checks apply unchanged.

The spec models below are the *machine-readable input contract*: each
verb resolves exactly one spec document (``--spec``), validates it with
``extra='forbid'``, and stamps its sha into the run record. Operator
authorization (``--arm``, ``--authorize-apply``, ...) is *never* part of
a spec — it is a session flag so a configuration file can never silently
authorize device mutation.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .cad_authority_resolver import AuthorityRef
from .cad_sweep_acquisition import (
    CalibrationBindingState,
    ChannelRouting,
    LevelSafetyPolicy,
    QualityGateThresholds,
    SweepStimulusSpec,
)
from .cad_campaign_execution import (
    CampaignAutomationPolicy,
    CampaignChannelBinding,
    CampaignPositionSpec,
)
from .cad_channel_verification import VerificationThresholds
from .cad_interface_loopback import CadInterfaceIoPath
from .cad_calibration_lifecycle import CadInstrumentInstance
from .cad_calibration_wizard import (
    CadCampaignCheckPlan,
    CadSplCheckAcceptanceProfile,
)
from .cad_calibration import CadCalibrationExportSnapshot
from .cad_channel_verification import OperatorChannelAttestation
from .canonical_json import canonical_json, canonical_sha256
from .cad_delegated_provider import _require_iso8601, _seal


# ---------------------------------------------------------------------------
# Contract vocabulary
# ---------------------------------------------------------------------------

HEADLESS_CLI_AUTHORITY_VERSION = 'headless-cli-1'

#: Envelope format token for ``--json`` output.
HEADLESS_RESULT_FORMAT = 'htdt-headless-result-1'

#: Verbs that seal a CadHeadlessRunRecord. Read-only verbs do not mutate.
SEALED_VERBS: frozenset[str] = frozenset({
    'project.create',
    'sweep.run',
    'campaign.plan',
    'campaign.run',
    'channel_verify.plan',
    'channel_verify.run',
    'calibration.run',
    'deployment.run',
    'diagnostic.run',
    'drill.run',
})

HeadlessOutcome = Literal[
    'succeeded',      # the workflow's own verdict is a success
    'failed',         # the workflow completed and verdicted negatively
    'blocked',        # the workflow could not reach its verdict (precheck
                      # failures, parked await stages, ambiguous evidence)
    'unauthorized',   # device-mutating step reached without the
                      # authorization flag for it
    'missing_evidence',  # referenced sealed evidence does not exist
    'cancelled',      # operator cancel or --timeout-seconds deadline
    'dry_run',        # --dry-run: plan emitted, nothing mutated
]

#: Fail-closed exit-code lattice: 0 only on ``succeeded`` (and on a
#: ``dry_run`` plan, which is by definition a successful *plan*).
OUTCOME_EXIT_CODES: dict[str, int] = {
    'succeeded': 0,
    'dry_run': 0,
    'failed': 2,
    'blocked': 3,
    'unauthorized': 4,
    'missing_evidence': 5,
    'cancelled': 7,
}

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_INTERNAL = 6


class HeadlessCliError(RuntimeError):
    """Typed CLI failure carrying the outcome the run record should seal."""

    def __init__(
        self,
        outcome: HeadlessOutcome,
        reason: str,
        *,
        verdict: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.outcome: HeadlessOutcome = outcome
        self.reason = reason
        self.verdict = verdict


# ---------------------------------------------------------------------------
# Spec models — the machine-readable input contract (extra='forbid')
# ---------------------------------------------------------------------------

_BACKEND_CHOICES = Literal['fake', 'wasapi']
_ADAPTER_KINDS = Literal['avr-lan', 'file']


class HeadlessBackendSpec(BaseModel):
    """Backend selection shared by every acquisition-driving verb.

    ``fake`` selects the deterministic ``FakeAudioBackend``; its knobs go
    in ``fake_scenario`` (forwarded to ``default_fake_scenario(**kw)``) or
    per-channel in ``fake_scenarios_by_channel`` for multi-target verbs.
    ``wasapi`` selects the real device lane (fail-closed stub until the
    WASAPI backend lands).
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    backend: _BACKEND_CHOICES = 'fake'
    fake_scenario: dict[str, Any] | None = None
    fake_scenarios_by_channel: dict[str, dict[str, Any]] | None = None


class HeadlessSweepSpec(HeadlessBackendSpec):
    """Spec for ``htdt sweep run``."""

    document_id: str | None = None
    stimulus: SweepStimulusSpec
    routing: ChannelRouting
    level_policy: LevelSafetyPolicy = Field(
        default_factory=LevelSafetyPolicy)
    requires_absolute_level: bool = False
    calibration_state: CalibrationBindingState = 'unknown'
    declared_synchronized: bool = False
    quality_thresholds: QualityGateThresholds = Field(
        default_factory=QualityGateThresholds)


class HeadlessCampaignPlanSpec(BaseModel):
    """Spec for ``htdt campaign plan``."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    document_id: str | None = None
    campaign_ref: AuthorityRef
    scene_ref: AuthorityRef | None = None
    stimulus_template: SweepStimulusSpec
    channel_bindings: tuple[CampaignChannelBinding, ...]
    positions: tuple[CampaignPositionSpec, ...]
    repetitions_per_entry: int = 1
    policy: CampaignAutomationPolicy | None = None
    level_policy: LevelSafetyPolicy | None = None
    requires_absolute_level: bool = False
    declared_synchronized: bool = False
    notes: str | None = None


class HeadlessChainSpec(BaseModel):
    """One logical channel for a channel-verification plan."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    logical_channel: str
    channel_class: str = 'bed_channel'
    expected_speaker_entity_ids: tuple[str, ...] = ()
    hops: tuple[str, ...] = ()


class HeadlessChannelVerifyPlanSpec(BaseModel):
    """Spec for ``htdt channel-verify plan``."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    document_id: str | None = None
    chains: tuple[HeadlessChainSpec, ...]
    routings: dict[str, ChannelRouting]
    sample_rate_hz: int = 48000
    stimulus_start_hz: float = 100.0
    stimulus_end_hz: float = 8000.0
    stimulus_duration_s: float = 0.25
    stimulus_level_dbfs: float = -12.0
    repetitions: int = 2
    thresholds: VerificationThresholds | None = None
    reference_channel: str | None = None
    polarity_expectations: dict[str, str] | None = None


class HeadlessCalibrationSpec(HeadlessBackendSpec):
    """Spec for ``htdt calibration run`` — one wizard lane."""

    document_id: str | None = None
    lane: Literal[
        'interface_loopback', 'spl_reference_check', 'campaign_checks'] = (
            'interface_loopback')
    routing: ChannelRouting
    stimulus_spec: SweepStimulusSpec
    io_path: CadInterfaceIoPath | None = None
    instrument: CadInstrumentInstance | None = None
    calibrator: CadInstrumentInstance | None = None
    acceptance_profile: CadSplCheckAcceptanceProfile | None = None
    check_plan: CadCampaignCheckPlan | None = None
    campaign_ref: AuthorityRef | None = None
    max_steps: int = 64
    notes: str | None = None


class HeadlessAdapterSpec(BaseModel):
    """Adapter selection for deployment/diagnostic verbs.

    ``avr-lan`` + ``simulated=True`` (or loopback-only endpoints) selects
    ``FakeAvrLanTransport``; ``file`` selects ``FileCalibrationAdapter``
    rooted at ``file_root``. Non-loopback remote endpoints must be listed
    in ``approved_remote_endpoints`` — the adapter refuses otherwise.
    Credentials are secret-store *handles* only; raw secrets are never
    accepted anywhere on this CLI.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: _ADAPTER_KINDS = 'avr-lan'
    simulated: bool = True
    initial_gains: dict[str, float] | None = None
    approved_remote_endpoints: tuple[str, ...] = ()
    file_root: str | None = None


class HeadlessBindingSpec(BaseModel):
    """Device binding for deployment/diagnostic verbs."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    device_family: str = 'avr-denon-marantz-telnet'
    device_model: str
    device_serial: str
    firmware_version: str = 'unknown'
    routing: tuple[tuple[str, str], ...] = ()


class HeadlessDeploymentSpec(BaseModel):
    """Spec for ``htdt deploy run``."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    document_id: str | None = None
    adapter: HeadlessAdapterSpec = Field(
        default_factory=HeadlessAdapterSpec)
    binding: HeadlessBindingSpec
    target_ref: str
    export: CadCalibrationExportSnapshot


class HeadlessDiagnosticSpec(HeadlessBackendSpec):
    """Spec for ``htdt diagnose run``."""

    document_id: str | None = None
    fault_tree_id: str
    symptom_summary: str
    symptom_ref: AuthorityRef | None = None
    reference_channel: str = 'main'
    routings: dict[str, ChannelRouting] = Field(default_factory=dict)
    operator_facts: dict[str, str] | None = None
    attestations: dict[str, OperatorChannelAttestation] | None = None
    adapter: HeadlessAdapterSpec | None = None
    binding: HeadlessBindingSpec | None = None
    expected_deployed_config_sha256: str | None = None
    max_stimulus_level_dbfs: float = -12.0
    max_steps: int = 32


_HEADLESS_SPEC_MODELS: dict[str, type[BaseModel]] = {
    'sweep.run': HeadlessSweepSpec,
    'campaign.plan': HeadlessCampaignPlanSpec,
    'channel_verify.plan': HeadlessChannelVerifyPlanSpec,
    'calibration.run': HeadlessCalibrationSpec,
    'deployment.run': HeadlessDeploymentSpec,
    'diagnostic.run': HeadlessDiagnosticSpec,
    'channel_verify.run': HeadlessBackendSpec,
    'campaign.run': HeadlessBackendSpec,
}

#: Aliases users may type for the canonical verb tokens above.
VERB_ALIASES: dict[str, str] = {
    'project.create': 'project.create',
    'project.inspect': 'project.inspect',
    'sweep.run': 'sweep.run',
    'campaign.plan': 'campaign.plan',
    'campaign.run': 'campaign.run',
    'channel_verify.plan': 'channel_verify.plan',
    'channel_verify.run': 'channel_verify.run',
    'calibration.run': 'calibration.run',
    'deployment.run': 'deployment.run',
    'diagnostic.run': 'diagnostic.run',
    'records.list': 'records.list',
    'records.export': 'records.export',
    'status': 'status',
}


def canonical_spec_payload(spec: BaseModel) -> dict[str, Any]:
    """Canonical JSON-able payload for a spec (used for sha + evidence)."""

    return spec.model_dump(mode='json')


def spec_sha256(spec: BaseModel) -> str:
    return canonical_sha256(canonical_spec_payload(spec))


def spec_model_for(verb: str) -> type[BaseModel] | None:
    return _HEADLESS_SPEC_MODELS.get(verb)


# ---------------------------------------------------------------------------
# CadHeadlessRunRecord — the sealed evidence record
# ---------------------------------------------------------------------------

_TS_FIELDS = ('started_at_utc', 'finished_at_utc')


class CadHeadlessRunRecord(BaseModel):
    """Sealed evidence for one headless CLI invocation.

    Sealed for every mutating verb outcome — ``failed``, ``blocked``,
    ``unauthorized`` and ``cancelled`` runs are evidence too, so a batch
    job can never leave an unrecorded mutation attempt.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    run_record_id: str
    run_sha256: str
    document_id: str
    verb: str
    outcome: HeadlessOutcome
    verdict: str | None = None
    reason: str | None = None
    spec_sha256: str
    spec_json: str
    tool_version: str
    tool_commit_sha: str | None = None
    tool_commit_dirty: bool = False
    python_version: str
    platform: str
    env_fingerprint: str
    argv_sha256: str
    backend_id: str | None = None
    backend_is_simulated: bool = False
    record_refs: tuple[AuthorityRef, ...] = ()
    dry_run: bool = False
    cancelled: bool = False
    timeout_seconds: float | None = None
    started_at_utc: str
    finished_at_utc: str
    elapsed_ms: int = 0
    authority_version: str = HEADLESS_CLI_AUTHORITY_VERSION

    @field_validator(*_TS_FIELDS)
    @classmethod
    def _check_ts(cls, value: str, info: Any) -> str:
        _require_iso8601(value, info.field_name)
        return value

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('run_record_id', None)
        payload.pop('run_sha256', None)
        return payload


def build_run_record(
    *,
    document_id: str,
    verb: str,
    outcome: HeadlessOutcome,
    spec: BaseModel | None,
    spec_json: str | None,
    spec_sha: str | None,
    tool_version: str,
    tool_commit_sha: str | None,
    tool_commit_dirty: bool,
    python_version: str,
    platform: str,
    env_fingerprint: str,
    argv_sha256: str,
    backend_id: str | None = None,
    backend_is_simulated: bool = False,
    record_refs: tuple[AuthorityRef, ...] = (),
    verdict: str | None = None,
    reason: str | None = None,
    dry_run: bool = False,
    cancelled: bool = False,
    timeout_seconds: float | None = None,
    started_at_utc: str,
    finished_at_utc: str,
    elapsed_ms: int = 0,
) -> CadHeadlessRunRecord:
    """Seal a headless-run record. ``spec_json``/``spec_sha`` are required
    whenever a spec was resolved; both may be the empty-spec sha for verbs
    without an input spec (e.g. ``project.create`` seals its flags)."""

    if spec is not None:
        payload = canonical_spec_payload(spec)
        spec_json = canonical_json(payload)
        spec_sha = spec_sha256(spec)
    if spec_json is None or spec_sha is None:
        raise HeadlessCliError(
            'missing_evidence',
            'run record needs spec_json/spec_sha (or a spec model)')

    return _seal(
        CadHeadlessRunRecord,
        {
            'document_id': document_id,
            'verb': verb,
            'outcome': outcome,
            'verdict': verdict,
            'reason': reason,
            'spec_sha256': spec_sha,
            'spec_json': spec_json,
            'tool_version': tool_version,
            'tool_commit_sha': tool_commit_sha,
            'tool_commit_dirty': tool_commit_dirty,
            'python_version': python_version,
            'platform': platform,
            'env_fingerprint': env_fingerprint,
            'argv_sha256': argv_sha256,
            'backend_id': backend_id,
            'backend_is_simulated': backend_is_simulated,
            'record_refs': [
                ref.model_dump(mode='json') for ref in record_refs],
            'dry_run': dry_run,
            'cancelled': cancelled,
            'timeout_seconds': timeout_seconds,
            'started_at_utc': started_at_utc,
            'finished_at_utc': finished_at_utc,
            'elapsed_ms': elapsed_ms,
            'authority_version': HEADLESS_CLI_AUTHORITY_VERSION,
        },
        'run_record_id', 'run_sha256', 'hrun',
    )


def run_record_ref(record: CadHeadlessRunRecord) -> AuthorityRef:
    return AuthorityRef(
        kind='headless_run_record',
        ref_id=record.run_record_id,
        ref_sha256=record.run_sha256)


__all__ = [
    'EXIT_INTERNAL',
    'EXIT_OK',
    'EXIT_USAGE',
    'HEADLESS_CLI_AUTHORITY_VERSION',
    'HEADLESS_RESULT_FORMAT',
    'OUTCOME_EXIT_CODES',
    'SEALED_VERBS',
    'VERB_ALIASES',
    'CadHeadlessRunRecord',
    'HeadlessAdapterSpec',
    'HeadlessBackendSpec',
    'HeadlessBindingSpec',
    'HeadlessCalibrationSpec',
    'HeadlessCampaignPlanSpec',
    'HeadlessChainSpec',
    'HeadlessChannelVerifyPlanSpec',
    'HeadlessCliError',
    'HeadlessDeploymentSpec',
    'HeadlessDiagnosticSpec',
    'HeadlessOutcome',
    'HeadlessSweepSpec',
    'build_run_record',
    'canonical_spec_payload',
    'run_record_ref',
    'spec_model_for',
    'spec_sha256',
]
