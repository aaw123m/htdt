"""``htdt`` — the headless commissioning command line (#888).

One dispatcher (``htdt <family> <verb>``) over the sealed commissioning
authorities (#869 sweep engine, #875 campaigns, #876 channel
verification, #877 calibration wizard, #878 deployment pipeline, #885
diagnostic orchestrator). Machine contract:

* ``--json`` emits exactly one JSON envelope — sealed record ids and
  verdicts, never prose.
* Exit codes are fail-closed: 0 only when the workflow's own verdict is
  a success (or a ``--dry-run`` plan). Failed/blocked/unauthorized/
  missing-evidence/cancelled map to distinct non-zero codes, never 0 on
  partial.
* Every mutating verb seals one ``CadHeadlessRunRecord`` per invocation
  pinning spec sha + tool/repo/environment identity; ``--dry-run``
  performs no sealed writes.
* Device-mutating steps need explicit flags (``--arm``,
  ``--authorize-apply``, ``--authorize-rollback``, ``--authorize``,
  ``--auto-confirm-position``, ``--confirm-hardware``) — a spec file can
  never carry authorization.
* No GUI imports anywhere on this path: nothing here imports PySide6/Qt.

Verb map::

    htdt project create --name NAME [--document-id ID] [--description T]
    htdt project inspect (--project-id ID | --document-id ID)
    htdt sweep run --spec FILE --arm
    htdt campaign plan --spec FILE [--out FILE]
    htdt campaign run (--plan FILE | --plan-id ID)
                    [--backend-spec FILE] --arm [--auto-confirm-position]
    htdt channel-verify plan --spec FILE [--out FILE]
    htdt channel-verify run (--plan FILE | --plan-id ID)
                            [--backend-spec FILE] --arm
    htdt calibration run --spec FILE [--confirm-hardware]
    htdt deploy run --spec FILE [--preview-only]
                    [--authorize-apply OP] [--authorize-rollback OP]
    htdt diagnose run --spec FILE [--authorize OP]
    htdt records list --kind KIND [--document-id ID] [--verb VERB]
    htdt records export --kind KIND --id ID [--out FILE]
    htdt status
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
import platform as _platform
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .build_info import get_build_info
from .canonical_json import canonical_json, canonical_sha256
from .cad_repository import SceneRepository
from .project_library_repository import (
    ProjectLibraryError,
    ProjectLibraryRepository,
)
from .data_relocation import resolve_data_dir

from .cad_authority_resolver import AuthorityRef
from .cad_sweep_acquisition import (
    AcquisitionRequest,
    LevelSafetyPolicy,
    ArmConfirmation,
    FakeAudioBackend,
    MeasurementAcquisitionEngine,
    WasapiAudioBackend,
    default_fake_scenario,
)
from .cad_sweep_acquisition_evidence import (
    build_acquisition_run,
    build_stage_event,
    build_stimulus_definition,
)
from .cad_sweep_acquisition_repository import CadSweepAcquisitionRepository
from .cad_campaign_execution import (
    CampaignExecutionPlan,
    PositionConfirmation,
    build_campaign_execution_plan,
)
from .cad_campaign_execution_repository import CadCampaignExecutionRepository
from .cad_channel_identity_authority import build_channel_identity_chain
from .cad_channel_verification import (
    ChannelVerificationPlan,
    VerificationThresholds,
    build_verification_plan,
    run_verification_plan,
)
from .cad_channel_verification_repository import (
    CadChannelVerificationRepository,
)
from .cad_calibration_wizard import (
    CalibrationWizard,
    WizardStores,
)
from .cad_calibration_wizard_repository import CadCalibrationWizardRepository
from .cad_calibration_lifecycle_repository import (
    CadCalibrationLifecycleRepository,
)
from .cad_interface_loopback_repository import (
    CadInterfaceLoopbackRepository,
)
from .cad_deployment_pipeline import (
    DeploymentPipelineService,
    PipelineAuthorizationError,
    PipelineStageError,
)
from .cad_deployment_pipeline_repository import (
    CadDeploymentPipelineRepository,
)
from .cad_device_adapter import (
    AdapterDeviceBinding,
    build_device_binding,
)
from .cad_device_adapter_file import FILE_ADAPTER_ID, FileCalibrationAdapter
from .cad_avr_lan_adapter import (
    AVR_LAN_ADAPTER_ID,
    AvrLanApplyError,
    AvrLanCalibrationAdapter,
    FakeAvrLanTransport,
)
from .cad_diagnostic_orchestrator import (
    DiagnosticOrchestrator,
    DiagnosticOrchestrationError,
    DiagnosticSafetyError,
)
from .cad_diagnostic_orchestrator_repository import (
    CadDiagnosticOrchestratorRepository,
)
from .cad_diagnostic_hypothesis_repository import (
    CadDiagnosticHypothesisRepository,
)
from .cad_headless_cli import (
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_USAGE,
    HEADLESS_CLI_AUTHORITY_VERSION,
    HEADLESS_RESULT_FORMAT,
    OUTCOME_EXIT_CODES,
    SEALED_VERBS,
    CadHeadlessRunRecord,
    HeadlessBackendSpec,
    HeadlessCalibrationSpec,
    HeadlessCliError,
    HeadlessCampaignPlanSpec,
    HeadlessChannelVerifyPlanSpec,
    HeadlessDeploymentSpec,
    HeadlessDiagnosticSpec,
    HeadlessSweepSpec,
    build_run_record,
    canonical_spec_payload,
    spec_model_for,
)
from .cad_headless_cli_repository import CadHeadlessRunRepository


_FAKE_BACKEND_ID = 'fake-audio-io'


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _env_fingerprint() -> str:
    """sha1 of interpreter+platform+installed distributions (#833 convention)."""

    dists = sorted(
        f'{d.metadata["Name"]}=={d.version}'
        for d in importlib.metadata.distributions()
        if d.metadata.get('Name'))
    material = '|'.join(
        [sys.version.split()[0], _platform.system(),
         _platform.machine(), *dists])
    return hashlib.sha1(material.encode('utf-8')).hexdigest()


def _argv_sha256(argv: tuple[str, ...]) -> str:
    return canonical_sha256({'argv': list(argv)})


@dataclass
class _Deadline:
    """Cooperative timeout: checked in driver loops, cancels engines."""

    timeout_seconds: float | None
    started: float = field(default_factory=time.monotonic)

    def expired(self) -> bool:
        return (
            self.timeout_seconds is not None
            and time.monotonic() - self.started >= self.timeout_seconds)

    def arm_cancel(self, cancel: Callable[[], None]) -> threading.Timer | None:
        if self.timeout_seconds is None:
            return None
        timer = threading.Timer(self.timeout_seconds, cancel)
        timer.daemon = True
        timer.start()
        return timer


@dataclass
class _VerbOutcome:
    outcome: str
    verdict: str | None = None
    reason: str | None = None
    refs: tuple[AuthorityRef, ...] = ()
    data: dict[str, Any] | None = None
    #: Canonical spec material for verbs without a --spec document
    #: (plan-file runs, project create) — sealed into the run record.
    spec_payload: dict[str, Any] | None = None


class _Repositories:
    """Lazy repository accessors over one data dir."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._scene: SceneRepository | None = None
        self._cache: dict[str, Any] = {}

    @property
    def scene(self) -> SceneRepository:
        if self._scene is None:
            db = self.data_dir / 'cad-scenes.sqlite3'
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._scene = SceneRepository(db)
        return self._scene

    def _get(self, key: str, factory: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = factory()
        return self._cache[key]

    @property
    def library(self) -> ProjectLibraryRepository:
        return self._get('library', lambda: ProjectLibraryRepository(
            self.scene))

    @property
    def headless(self) -> CadHeadlessRunRepository:
        return self._get('headless', lambda: CadHeadlessRunRepository(
            self.scene))

    @property
    def sweep(self) -> CadSweepAcquisitionRepository:
        return self._get('sweep', lambda: CadSweepAcquisitionRepository(
            self.scene))

    @property
    def campaign(self) -> CadCampaignExecutionRepository:
        return self._get('campaign', lambda: CadCampaignExecutionRepository(
            self.scene, sweep_repository=self.sweep))

    @property
    def channel_verify(self) -> CadChannelVerificationRepository:
        return self._get(
            'channel_verify',
            lambda: CadChannelVerificationRepository(self.scene))

    @property
    def deployment(self) -> CadDeploymentPipelineRepository:
        return self._get('deployment', lambda: (
            CadDeploymentPipelineRepository(self.scene)))

    @property
    def diagnostic(self) -> CadDiagnosticOrchestratorRepository:
        return self._get('diagnostic', lambda: (
            CadDiagnosticOrchestratorRepository(self.scene)))

    @property
    def diagnostic_hypotheses(self) -> CadDiagnosticHypothesisRepository:
        return self._get('diag_hyp', lambda: (
            CadDiagnosticHypothesisRepository(self.scene)))

    def wizard_stores(self) -> WizardStores:
        return WizardStores(
            wizard=CadCalibrationWizardRepository(self.scene),
            sweep=self.sweep,
            interface=CadInterfaceLoopbackRepository(self.scene),
            lifecycle=CadCalibrationLifecycleRepository(self.scene),
        )


def _read_json_file(path: str) -> Any:
    try:
        with io.open(path, encoding='utf-8') as stream:
            return json.load(stream)
    except OSError as exc:
        raise HeadlessCliError(
            'missing_evidence', f'cannot read {path}: {exc}') from exc
    except json.JSONDecodeError as exc:
        raise HeadlessCliError(
            'missing_evidence', f'{path} is not JSON: {exc}') from exc


def _write_json_file(path: str, payload: Any) -> None:
    with io.open(path, 'w', encoding='utf-8', newline='\n') as stream:
        stream.write(canonical_json(payload))
        stream.write('\n')


def _load_spec(verb: str, spec_path: str | None) -> Any:
    """Load + validate the verb's spec document (extra='forbid')."""

    model = spec_model_for(verb)
    if model is None:
        return None
    if spec_path is None:
        raise HeadlessCliError(
            'missing_evidence',
            f'{verb} requires --spec FILE')
    raw = _read_json_file(spec_path)
    try:
        return model.model_validate(raw)
    except Exception as exc:
        raise HeadlessCliError(
            'missing_evidence',
            f'{spec_path} failed spec validation: {exc}') from exc


def _load_backend_spec(path: str | None) -> HeadlessBackendSpec:
    if path is None:
        return HeadlessBackendSpec()
    raw = _read_json_file(path)
    try:
        return HeadlessBackendSpec.model_validate(raw)
    except Exception as exc:
        raise HeadlessCliError(
            'missing_evidence',
            f'{path} failed backend-spec validation: {exc}') from exc


def _resolve_document_id(flag: str | None, spec: Any) -> str:
    document_id = flag or getattr(spec, 'document_id', None)
    if not document_id:
        raise HeadlessCliError(
            'missing_evidence',
            'no document_id — pass --document-id or set spec.document_id')
    return document_id


def _make_backend(
    spec: HeadlessBackendSpec,
    *,
    scenario_key: str | None = None,
) -> Any:
    """Instantiate the selected audio backend (fail-closed)."""

    if spec.backend == 'wasapi':
        backend = WasapiAudioBackend()
        if not backend.available():
            raise HeadlessCliError(
                'blocked',
                f'wasapi backend unavailable: {backend.unavailable_reason()}')
        return backend
    scenario_kwargs = dict(spec.fake_scenario or {})
    if scenario_key is not None:
        scenario_kwargs.update(
            (spec.fake_scenarios_by_channel or {}).get(scenario_key, {}))
    return FakeAudioBackend(default_fake_scenario(**scenario_kwargs))


def _arm_from_request(request: AcquisitionRequest) -> ArmConfirmation:
    """Operator arm attestation: echoes the request's routing + level."""

    routing = request.routing
    return ArmConfirmation(
        acknowledged_playback_device_id=routing.playback_device_id,
        acknowledged_playback_channel=routing.playback_channel,
        acknowledged_capture_device_id=routing.capture_device_id,
        acknowledged_capture_channel=routing.capture_channel,
        acknowledged_level_dbfs=request.stimulus.level_dbfs)


def _persist_engine_evidence(
    ctx: '_Ctx',
    document_id: str,
    engine: MeasurementAcquisitionEngine,
    stimulus_ref: AuthorityRef | None,
    campaign_ref: AuthorityRef | None = None,
    notes: tuple[str, ...] = (),
) -> tuple[AuthorityRef | None, AuthorityRef | None]:
    """Persist stimulus definition + acquisition run + stage events for a
    terminal engine. Mirrors the campaign acquisition_sink convention:
    only engines that reached a terminal stage produce evidence."""

    result = engine.result
    stimulus = result.stimulus
    if stimulus_ref is None and stimulus is not None:
        definition = build_stimulus_definition(
            document_id=document_id,
            stimulus=stimulus,
            created_at_utc=_utc_now(),
        )
        ctx.repos.sweep.save_stimulus_definition(definition)
        stimulus_ref = AuthorityRef(
            kind='sweep_stimulus_definition',
            ref_id=definition.stimulus_definition_id,
            ref_sha256=definition.stimulus_sha256)
    for index, transition in enumerate(engine.transitions):
        ctx.repos.sweep.save_stage_event(build_stage_event(
            document_id=document_id,
            run_id=engine.run_id,
            run_seq=index,
            stage=transition.stage,
            reason=transition.reason,
            entered_at_utc=transition.at_utc,
        ))
    if stimulus_ref is None:
        return None, None
    record = ctx.repos.sweep.record_run(
        document_id=document_id,
        engine=engine,
        result=result,
        stimulus_ref=stimulus_ref,
        campaign_ref=campaign_ref,
        notes=notes or ('htdt headless cli run',))
    return stimulus_ref, AuthorityRef(
        kind='sweep_acquisition_run',
        ref_id=record.acquisition_id,
        ref_sha256=record.acquisition_sha256)


# ---------------------------------------------------------------------------
# Verb handlers
# ---------------------------------------------------------------------------

@dataclass
class _Ctx:
    repos: _Repositories
    document_id: str
    args: argparse.Namespace
    deadline: _Deadline


def _verb_project_create(ctx: _Ctx) -> _VerbOutcome:
    args = ctx.args
    if ctx.args.dry_run:
        return _VerbOutcome(
            'dry_run', verdict='planned',
            data={'display_name': args.name,
                  'document_id': args.document_id},
            spec_payload={
                'name': args.name,
                'description': args.description,
                'document_id': args.document_id,
            })
    entry = ctx.repos.library.create_project(
        args.name,
        description=args.description,
        document_id=args.document_id,
    )
    ctx.document_id = entry.document_id
    ref = AuthorityRef(
        kind='project',
        ref_id=entry.project_id,
        ref_sha256=canonical_sha256(entry.model_dump(mode='json')))
    return _VerbOutcome(
        'succeeded',
        verdict='created',
        refs=(ref,),
        data={
            'project_id': entry.project_id,
            'document_id': entry.document_id,
            'display_name': entry.display_name,
        },
        spec_payload={
            'name': args.name,
            'description': args.description,
            'document_id': entry.document_id,
        })


def _verb_project_inspect(ctx: _Ctx) -> _VerbOutcome:
    args = ctx.args
    if args.project_id:
        entry = ctx.repos.library.get_project(args.project_id)
    else:
        entry = ctx.repos.library.get_by_document_id(
            args.document_id or '')
    if entry is None:
        raise HeadlessCliError(
            'missing_evidence',
            f'no project for {args.project_id or args.document_id}')
    return _VerbOutcome(
        'succeeded',
        verdict='found',
        data={'project': entry.model_dump(mode='json')})


def _verb_sweep_run(ctx: _Ctx) -> _VerbOutcome:
    args = ctx.args
    spec: HeadlessSweepSpec = args.resolved_spec
    backend = _make_backend(spec)
    engine = MeasurementAcquisitionEngine(backend=backend)
    request = AcquisitionRequest(
        stimulus=spec.stimulus,
        routing=spec.routing,
        level_policy=spec.level_policy,
        requires_absolute_level=spec.requires_absolute_level,
        calibration_state=spec.calibration_state,
        declared_synchronized=spec.declared_synchronized,
        quality_thresholds=spec.quality_thresholds,
    )
    report = engine.configure(request)
    if ctx.args.dry_run:
        return _VerbOutcome(
            'dry_run',
            verdict='precheck_ok' if report.ok else 'precheck_blocked',
            reason=(None if report.ok
                    else ';'.join(report.blocked_reasons)),
            data={
                'precheck': {
                    'ok': report.ok,
                    'blocked_reasons': list(report.blocked_reasons),
                    'playback_device': getattr(
                        report.playback_device, 'device_id', None),
                    'capture_device': getattr(
                        report.capture_device, 'device_id', None),
                    'calibration_state': report.calibration_state,
                },
            })
    if not report.ok:
        return _VerbOutcome(
            'blocked',
            verdict='precheck_blocked',
            reason=';'.join(report.blocked_reasons))
    if not args.arm:
        return _VerbOutcome(
            'unauthorized',
            verdict='arm_required',
            reason='sweep run requires --arm — device I/O is never '
                   'implicitly authorized')
    engine.arm(_arm_from_request(request))
    timer = ctx.deadline.arm_cancel(engine.cancel)
    try:
        engine.start()
    finally:
        if timer is not None:
            timer.cancel()
    stage = engine.stage
    refs: list[AuthorityRef] = []
    if stage == 'completed':
        stimulus_ref, run_ref = _persist_engine_evidence(
            ctx, ctx.document_id, engine, None)
        for ref in (stimulus_ref, run_ref):
            if ref is not None:
                refs.append(ref)
        quality = engine.result.quality
        verdict = quality.verdict if quality is not None else 'invalid'
        if verdict == 'invalid':
            return _VerbOutcome(
                'failed', verdict='invalid',
                reason='quality gate rejected the capture',
                refs=tuple(refs))
        return _VerbOutcome('succeeded', verdict=verdict, refs=tuple(refs))
    if stage == 'cancelled':
        return _VerbOutcome(
            'cancelled', verdict='cancelled',
            reason='run cancelled')
    reason = (
        engine.transitions[-1].reason
        if engine.transitions else f'engine stage {stage}')
    return _VerbOutcome('failed', verdict=stage, reason=reason)


def _verb_campaign_plan(ctx: _Ctx) -> _VerbOutcome:
    args = ctx.args
    spec: HeadlessCampaignPlanSpec = args.resolved_spec
    plan = build_campaign_execution_plan(
        document_id=ctx.document_id,
        campaign_ref=spec.campaign_ref,
        stimulus_template=spec.stimulus_template,
        channel_bindings=spec.channel_bindings,
        positions=spec.positions,
        generated_at_utc=_utc_now(),
        scene_ref=spec.scene_ref,
        repetitions_per_entry=spec.repetitions_per_entry,
        policy=spec.policy,
        level_policy=spec.level_policy,
        requires_absolute_level=spec.requires_absolute_level,
        declared_synchronized=spec.declared_synchronized,
        notes=spec.notes,
    )
    ref = AuthorityRef(
        kind='campaign_execution_plan',
        ref_id=plan.plan_id,
        ref_sha256=plan.plan_sha256)
    data = {'plan': canonical_spec_payload(plan)}
    if ctx.args.dry_run:
        return _VerbOutcome('dry_run', verdict='planned', refs=(ref,),
                            data=data)
    ctx.repos.campaign.save_plan(plan)
    if args.out:
        _write_json_file(args.out, plan.model_dump(mode='json'))
    return _VerbOutcome('succeeded', verdict='planned', refs=(ref,),
                        data=data)


def _load_plan(
    ctx: _Ctx,
    *,
    plan_arg: str | None,
    plan_id: str | None,
    model: Any,
    get: Callable[[str], Any],
    kind: str,
) -> Any:
    if plan_arg is not None:
        raw = _read_json_file(plan_arg)
        try:
            plan = model.model_validate(raw)
        except Exception as exc:
            raise HeadlessCliError(
                'missing_evidence',
                f'{plan_arg} failed {kind} validation: {exc}') from exc
        # Persist first — _SealedStore re-verifies the declared seal, so
        # a tampered plan file fails closed here, never silently runs.
        if not ctx.args.dry_run:
            if kind == 'campaign_execution_plan':
                ctx.repos.campaign.save_plan(plan)
            else:
                ctx.repos.channel_verify.save_plan(plan)
        return plan
    plan = get(plan_id or '')
    if plan is None:
        raise HeadlessCliError(
            'missing_evidence',
            f'no {kind} for id {plan_id}')
    return plan


def _verb_campaign_run(ctx: _Ctx) -> _VerbOutcome:
    args = ctx.args
    plan = _load_plan(
        ctx, plan_arg=args.plan, plan_id=args.plan_id,
        model=CampaignExecutionPlan,
        get=ctx.repos.campaign.get_plan,
        kind='campaign_execution_plan')
    ctx.document_id = plan.document_id
    backend_spec = _load_backend_spec(args.backend_spec)
    args.resolved_backend_spec = backend_spec
    if ctx.args.dry_run:
        return _VerbOutcome(
            'dry_run', verdict='planned',
            data={'plan_id': plan.plan_id})
    if not args.arm:
        return _VerbOutcome(
            'unauthorized', verdict='arm_required',
            reason='campaign run requires --arm — device I/O is never '
                   'implicitly authorized')
    backend = _make_backend(backend_spec)

    def engine_factory() -> MeasurementAcquisitionEngine:
        return MeasurementAcquisitionEngine(backend=backend)

    def arm_provider(_entry: Any, request: AcquisitionRequest):
        return _arm_from_request(request)

    runner = ctx.repos.campaign.runner_for(
        plan,
        engine_factory=engine_factory,
        arm_confirmation_provider=arm_provider,
    )
    while True:
        if ctx.deadline.expired():
            runner.cancel()
            return _VerbOutcome(
                'cancelled', verdict='timeout',
                reason='--timeout-seconds deadline reached')
        step = runner.step()
        action = step.action
        if action == 'terminal':
            break
        if action == 'awaiting_position':
            if not args.auto_confirm_position:
                return _VerbOutcome(
                    'blocked', verdict='awaiting_position',
                    reason=step.detail or 'operator position '
                                          'confirmation required')
            entry = next(
                (e for e in plan.queue if e.entry_key == step.entry_key),
                None)
            position_id = entry.position_id if entry else 'unknown'
            runner.confirm_position(PositionConfirmation(
                position_id=position_id,
                method='operator_attest',
                actor='operator',
                at_utc=_utc_now(),
                note='htdt --auto-confirm-position',
            ))
            continue
        if action in ('blocked', 'paused'):
            return _VerbOutcome(
                'blocked', verdict=action,
                reason=step.detail)
    outcome = runner.state.outcome
    spec_payload = {
        'plan_sha256': plan.plan_sha256,
        'backend': canonical_spec_payload(backend_spec),
    }
    if outcome == 'completed':
        return _VerbOutcome('succeeded', verdict='completed',
                            spec_payload=spec_payload)
    if outcome == 'cancelled':
        return _VerbOutcome('cancelled', verdict='cancelled',
                            spec_payload=spec_payload)
    if outcome == 'completed_with_failures':
        return _VerbOutcome(
            'failed', verdict='completed_with_failures',
            reason='campaign finished with failed entries',
            spec_payload=spec_payload)
    return _VerbOutcome('failed', verdict=outcome,
                        spec_payload=spec_payload)


def _verb_channel_verify_plan(ctx: _Ctx) -> _VerbOutcome:
    args = ctx.args
    spec: HeadlessChannelVerifyPlanSpec = args.resolved_spec
    chains = tuple(
        build_channel_identity_chain(
            document_id=ctx.document_id,
            logical_channel=chain.logical_channel,
            channel_class=chain.channel_class,
            expected_speaker_entity_ids=chain.expected_speaker_entity_ids,
            hops=chain.hops,
            declared_at_utc=_utc_now(),
        )
        for chain in spec.chains)
    plan = build_verification_plan(
        document_id=ctx.document_id,
        chains=chains,
        routings=spec.routings,
        sample_rate_hz=spec.sample_rate_hz,
        stimulus_start_hz=spec.stimulus_start_hz,
        stimulus_end_hz=spec.stimulus_end_hz,
        stimulus_duration_s=spec.stimulus_duration_s,
        stimulus_level_dbfs=spec.stimulus_level_dbfs,
        repetitions=spec.repetitions,
        reference_channel=spec.reference_channel,
        thresholds=(
            spec.thresholds
            if spec.thresholds is not None
            else VerificationThresholds()),
        polarity_expectations=spec.polarity_expectations,
        created_at_utc=_utc_now(),
    )
    ref = AuthorityRef(
        kind='channel_verification_plan',
        ref_id=plan.plan_id,
        ref_sha256=plan.plan_sha256)
    data = {'plan': canonical_spec_payload(plan)}
    if ctx.args.dry_run:
        return _VerbOutcome('dry_run', verdict='planned', refs=(ref,),
                            data=data)
    ctx.repos.channel_verify.save_plan(plan)
    # NOTE: chains are declared inputs (project authority), not outputs —
    # the plan's chain_refs pin them; persisting re-declared chains would
    # collide on their deterministic ids on every repeat run.
    if args.out:
        _write_json_file(args.out, plan.model_dump(mode='json'))
    return _VerbOutcome('succeeded', verdict='planned', refs=(ref,),
                        data=data)


def _verb_channel_verify_run(ctx: _Ctx) -> _VerbOutcome:
    args = ctx.args
    plan = _load_plan(
        ctx, plan_arg=args.plan, plan_id=args.plan_id,
        model=ChannelVerificationPlan,
        get=ctx.repos.channel_verify.get_plan,
        kind='channel_verification_plan')
    ctx.document_id = plan.document_id
    backend_spec = _load_backend_spec(args.backend_spec)
    args.resolved_backend_spec = backend_spec
    if ctx.args.dry_run:
        return _VerbOutcome(
            'dry_run', verdict='planned',
            data={'plan_id': plan.plan_id})
    if not args.arm:
        return _VerbOutcome(
            'unauthorized', verdict='arm_required',
            reason='channel-verify run requires --arm — device I/O is '
                   'never implicitly authorized')

    def backend_for(target: Any) -> Any:
        return _make_backend(
            backend_spec, scenario_key=target.logical_channel)

    verdict, results, _engines = run_verification_plan(
        plan=plan,
        backend=backend_for,
        arming=_arm_from_request,
        document_id=ctx.document_id,
        sweep_repository=ctx.repos.sweep,
    )
    refs: list[AuthorityRef] = []
    for result in results.values():
        ctx.repos.channel_verify.save_excitation_result(result)
        refs.append(AuthorityRef(
            kind='channel_excitation_result',
            ref_id=result.result_id,
            ref_sha256=result.result_sha256))
    ctx.repos.channel_verify.save_verdict(verdict)
    refs.append(AuthorityRef(
        kind='channel_verification_verdict',
        ref_id=verdict.verdict_id,
        ref_sha256=verdict.verdict_sha256))
    state = verdict.map_state
    spec_payload = {
        'plan_sha256': plan.plan_sha256,
        'backend': canonical_spec_payload(backend_spec),
    }
    if state == 'verified':
        return _VerbOutcome('succeeded', verdict=state, refs=tuple(refs),
                            spec_payload=spec_payload)
    if state == 'failed':
        return _VerbOutcome('failed', verdict=state, refs=tuple(refs),
                            spec_payload=spec_payload)
    # ambiguous / incomplete / unknown — could not reach the verdict
    return _VerbOutcome(
        'blocked', verdict=state, refs=tuple(refs),
        reason=f'map_state={state} — evidence insufficient',
        spec_payload=spec_payload)


def _verb_calibration_run(ctx: _Ctx) -> _VerbOutcome:
    args = ctx.args
    spec: HeadlessCalibrationSpec = args.resolved_spec
    backend = _make_backend(spec)
    # dry-run must not open any repository: empty WizardStores keeps the
    # wizard in-memory only (no sealed writes, no schema touch).
    stores = (
        WizardStores() if args.dry_run else ctx.repos.wizard_stores())
    wizard = CalibrationWizard(
        ctx.document_id,
        backend,
        stores=stores,
    )
    try:
        if spec.lane == 'interface_loopback':
            if spec.io_path is None:
                raise HeadlessCliError(
                    'missing_evidence',
                    'interface_loopback lane needs spec.io_path')
            wizard.begin_loopback(
                io_path=spec.io_path, routing=spec.routing,
                stimulus_spec=spec.stimulus_spec,
                created_by='htdt-cli', at_utc=_utc_now(),
                notes=spec.notes)
        elif spec.lane == 'spl_reference_check':
            if spec.instrument is None or spec.acceptance_profile is None:
                raise HeadlessCliError(
                    'missing_evidence',
                    'spl_reference_check lane needs spec.instrument and '
                    'spec.acceptance_profile')
            wizard.begin_reference_check(
                instrument=spec.instrument,
                acceptance_profile=spec.acceptance_profile,
                routing=spec.routing,
                stimulus_spec=spec.stimulus_spec,
                calibrator=spec.calibrator,
                created_by='htdt-cli', at_utc=_utc_now(),
                notes=spec.notes)
        else:
            if spec.campaign_ref is None or spec.check_plan is None:
                raise HeadlessCliError(
                    'missing_evidence',
                    'campaign_checks lane needs spec.campaign_ref and '
                    'spec.check_plan')
            wizard.begin_campaign_checks(
                campaign_ref=spec.campaign_ref, plan=spec.check_plan,
                routing=spec.routing,
                stimulus_spec=spec.stimulus_spec,
                created_by='htdt-cli', at_utc=_utc_now(),
                notes=spec.notes)
    except HeadlessCliError:
        raise
    except Exception as exc:
        raise HeadlessCliError(
            'blocked', f'wizard begin failed: {exc}') from exc
    if ctx.args.dry_run:
        return _VerbOutcome(
            'dry_run', verdict='begun',
            reason=f'lane {spec.lane} armed (parked at await stage)')
    if not args.arm:
        return _VerbOutcome(
            'unauthorized', verdict='arm_required',
            reason='calibration run requires --arm — wizard lanes '
                   'drive device I/O')
    confirms_left = spec.max_steps if args.confirm_hardware else 0
    while True:
        if ctx.deadline.expired():
            wizard.cancel()
            return _VerbOutcome(
                'cancelled', verdict='timeout',
                reason='--timeout-seconds deadline reached')
        state = wizard.state
        stage = state.current_stage
        if stage in ('completed', 'cancelled', 'failed'):
            break
        if stage.startswith('await_'):
            if not args.confirm_hardware:
                return _VerbOutcome(
                    'blocked', verdict=stage,
                    reason='parked at hardware gate — '
                           '--confirm-hardware required')
            if confirms_left <= 0:
                return _VerbOutcome(
                    'blocked', verdict=stage,
                    reason='confirm limit reached')
            confirms_left -= 1
            wizard.confirm_hardware(
                at_utc=_utc_now(), reason='htdt --confirm-hardware')
            continue
        wizard.run_automatic(max_steps=spec.max_steps)
    if stage == 'completed':
        return _VerbOutcome('succeeded', verdict='completed')
    if stage == 'cancelled':
        return _VerbOutcome('cancelled', verdict='cancelled')
    return _VerbOutcome('failed', verdict='failed')


def _build_adapter(spec: HeadlessDeploymentSpec | HeadlessDiagnosticSpec):
    adapter_spec = spec.adapter
    if adapter_spec is None:
        return None, None
    if adapter_spec.kind == 'file':
        if not adapter_spec.file_root:
            raise HeadlessCliError(
                'missing_evidence',
                'file adapter needs adapter.file_root')
        adapter = FileCalibrationAdapter(adapter_spec.file_root)
        return adapter, FILE_ADAPTER_ID
    if not adapter_spec.simulated:
        raise HeadlessCliError(
            'blocked',
            'avr-lan adapter is loopback-only on this CLI — '
            'simulated=false claims a real telnet transport that does '
            'not exist here; keep adapter.simulated=true or use the '
            'file adapter')
    transport = FakeAvrLanTransport(
        initial=adapter_spec.initial_gains)
    # The binding's device_serial IS the endpoint key the adapter resolves.
    endpoint = spec.binding.device_serial if spec.binding else 'target-1'
    transports = {endpoint: transport}
    adapter = AvrLanCalibrationAdapter(
        transports,
        approved_remote_endpoints=tuple(
            adapter_spec.approved_remote_endpoints),
        simulated=True)
    return adapter, AVR_LAN_ADAPTER_ID


def _build_binding(
    spec_binding: Any, adapter_id: str, bound_at_utc: str,
) -> AdapterDeviceBinding:
    return build_device_binding(
        binding_id=f'bind-{canonical_sha256({"m": spec_binding.device_model, "s": spec_binding.device_serial, "f": spec_binding.firmware_version})[:16]}',
        adapter_id=adapter_id,
        device_family=spec_binding.device_family,
        device_model=spec_binding.device_model,
        device_serial=spec_binding.device_serial,
        firmware_version=spec_binding.firmware_version,
        routing=tuple(tuple(pair) for pair in spec_binding.routing),
        bound_at_utc=bound_at_utc)


def _verb_deployment_run(ctx: _Ctx) -> _VerbOutcome:
    args = ctx.args
    spec: HeadlessDeploymentSpec = args.resolved_spec
    adapter, adapter_id = _build_adapter(spec)
    args.deployment_backend = (
        adapter_id,
        adapter is not None
        and adapter.capability().adapter_kind == 'simulated',
    )
    binding = _build_binding(spec.binding, adapter_id, _utc_now())
    service = DeploymentPipelineService(
        adapter, binding,
        target_ref=spec.target_ref,
        document_id=ctx.document_id,
        repository=None if ctx.args.dry_run else ctx.repos.deployment,
    )
    if ctx.args.dry_run:
        return _VerbOutcome(
            'dry_run', verdict='planned',
            data={'target_ref': spec.target_ref,
                  'adapter_id': adapter_id})
    now = _utc_now()
    service.open(at=now)
    service.compile(spec.export, at=_utc_now())
    service.preview(at=_utc_now())
    refs: list[AuthorityRef] = []
    latest = service.latest
    if latest is not None:
        refs.append(AuthorityRef(
            kind='deployment_pipeline_record',
            ref_id=latest.record_id,
            ref_sha256=latest.record_sha256))
    if args.preview_only:
        return _VerbOutcome(
            'succeeded', verdict='previewed', refs=tuple(refs))
    if not args.authorize_apply:
        return _VerbOutcome(
            'unauthorized', verdict='authorize_apply_required',
            refs=tuple(refs),
            reason='apply requires --authorize-apply OPERATOR_ID — '
                   'device mutation is never implicit')
    authorization = service.authorize(
        operator_id=args.authorize_apply, scope='apply', at=_utc_now())
    try:
        service.apply(authorization, at=_utc_now())
    except AvrLanApplyError as exc:
        return _VerbOutcome(
            'failed', verdict='partial_write', refs=tuple(refs),
            reason=f'partial write: {exc.applied_units}/'
                   f'{exc.total_units} units')
    service.verify_readback(at=_utc_now())
    latest = service.latest
    if latest is not None:
        refs.append(AuthorityRef(
            kind='deployment_pipeline_record',
            ref_id=latest.record_id,
            ref_sha256=latest.record_sha256))
    stage = latest.stage if latest is not None else 'unknown'
    if stage == 'readback_matched':
        return _VerbOutcome(
            'succeeded', verdict='readback_matched', refs=tuple(refs))
    if stage == 'readback_diverged':
        if args.authorize_rollback:
            rollback_auth = service.authorize(
                operator_id=args.authorize_rollback,
                scope='rollback', at=_utc_now())
            service.rollback(rollback_auth, at=_utc_now())
            final = service.latest
            if final is not None:
                refs.append(AuthorityRef(
                    kind='deployment_pipeline_record',
                    ref_id=final.record_id,
                    ref_sha256=final.record_sha256))
            return _VerbOutcome(
                'failed', verdict='rollback_verified', refs=tuple(refs),
                reason='readback diverged; rollback applied')
        return _VerbOutcome(
            'failed', verdict='readback_diverged', refs=tuple(refs),
            reason='readback diverged; no --authorize-rollback given')
    return _VerbOutcome(
        'failed', verdict=stage, refs=tuple(refs),
        reason=f'pipeline stage {stage}')


def _verb_diagnostic_run(ctx: _Ctx) -> _VerbOutcome:
    args = ctx.args
    spec: HeadlessDiagnosticSpec = args.resolved_spec
    if ctx.args.dry_run:
        return _VerbOutcome(
            'dry_run', verdict='planned',
            data={'fault_tree_id': spec.fault_tree_id})
    orchestrator = DiagnosticOrchestrator(
        repository=ctx.repos.diagnostic,
        hypothesis_repository=ctx.repos.diagnostic_hypotheses,
        sweep_repository=ctx.repos.sweep,
        channel_repository=ctx.repos.channel_verify,
    )
    symptom_ref = spec.symptom_ref or AuthorityRef(
        kind='operator_report',
        ref_id='htdt-cli',
        ref_sha256=canonical_sha256(
            {'summary': spec.symptom_summary}))
    session, _entries, _case, _hypotheses = orchestrator.open_session(
        document_id=ctx.document_id,
        symptom_ref=symptom_ref,
        symptom_summary=spec.symptom_summary,
        fault_tree_id=spec.fault_tree_id,
        reference_channel=spec.reference_channel,
        max_stimulus_level_dbfs=spec.max_stimulus_level_dbfs,
        operator_id='htdt-cli',
    )
    adapter, adapter_id = (None, None)
    binding = None
    if spec.adapter is not None:
        adapter, adapter_id = _build_adapter(spec)
        if spec.binding is not None:
            binding = _build_binding(
                spec.binding, adapter_id, _utc_now())
    refs: list[AuthorityRef] = [AuthorityRef(
        kind='diagnostic_session',
        ref_id=session.session_id,
        ref_sha256=session.session_sha256)]
    if not args.arm:
        return _VerbOutcome(
            'unauthorized', verdict='arm_required', refs=tuple(refs),
            reason='diagnose run requires --arm — plans may execute '
                   'device stimulus; session was parked')
    steps = 0
    while steps < spec.max_steps:
        if ctx.deadline.expired():
            orchestrator.abort(
                session, reason='--timeout-seconds deadline')
            return _VerbOutcome(
                'cancelled', verdict='timeout', refs=tuple(refs))
        state = orchestrator.session_state(session)
        if state.terminal:
            break
        if state.current_stage != 'test_planning':
            break
        plan = orchestrator.plan_next_test(session)
        if plan is None:
            break
        authorization = None
        if plan.safety_class == 'device_mutation':
            if not args.authorize:
                return _VerbOutcome(
                    'unauthorized', verdict='authorize_required',
                    refs=tuple(refs),
                    reason=f'plan {plan.plan_id} mutates a device — '
                           f'--authorize OPERATOR_ID required')
            authorization = orchestrator.authorize_plan(
                session, plan, operator_id=args.authorize,
            )
        backend = _make_backend(spec)
        orchestrator.execute_plan(
            session, plan,
            backend=backend,
            routings=spec.routings or None,
            arming=_arm_from_request,
            adapter=adapter,
            binding=binding,
            operator_facts=spec.operator_facts,
            authorization=authorization,
        )
        steps += 1
    state = orchestrator.session_state(session)
    resolution = None
    if not state.terminal:
        resolution, _v = orchestrator.resolve(session)
        state = orchestrator.session_state(session)
    final_stage = state.terminated_stage or state.current_stage
    verdict = final_stage
    if resolution is not None:
        refs.append(AuthorityRef(
            kind='diagnostic_resolution',
            ref_id=resolution.resolution_id,
            ref_sha256=resolution.resolution_sha256))
    if final_stage == 'resolved':
        return _VerbOutcome('succeeded', verdict=verdict, refs=tuple(refs))
    if final_stage == 'aborted':
        return _VerbOutcome('cancelled', verdict=verdict, refs=tuple(refs))
    if final_stage == 'needs_inspection':
        return _VerbOutcome('blocked', verdict=verdict, refs=tuple(refs),
                            reason='session needs physical inspection')
    return _VerbOutcome('failed', verdict=verdict, refs=tuple(refs))


_RECORD_KINDS: dict[str, tuple[str, str, str, str]] = {
    # kind -> (repo property, list method, id attr, sha attr)
    'headless_run': ('headless', 'list_runs', 'run_record_id',
                     'run_sha256'),
    'sweep_run': ('sweep', 'list_acquisition_runs', 'acquisition_id',
                  'acquisition_sha256'),
    'cv_plan': ('channel_verify', 'list_plans', 'plan_id',
                'plan_sha256'),
    'cv_verdict': ('channel_verify', 'list_verdicts', 'verdict_id',
                   'verdict_sha256'),
    'campaign_plan': ('campaign', 'list_plans', 'plan_id',
                      'plan_sha256'),
    'campaign_run': ('campaign', 'list_run_records', 'run_record_id',
                     'run_record_sha256'),
    'diagnostic_session': ('diagnostic', 'list_sessions', 'session_id',
                           'session_sha256'),
}

_RECORD_GETTERS: dict[str, tuple[str, str]] = {
    'headless_run': ('headless', 'get_run'),
    'sweep_run': ('sweep', 'get_run_by_run_id'),
    'cv_plan': ('channel_verify', 'get_plan'),
    'cv_verdict': ('channel_verify', 'get_verdict'),
    'campaign_plan': ('campaign', 'get_plan'),
    'campaign_run': ('campaign', 'get_run_record'),
    'diagnostic_session': ('diagnostic', 'get_session'),
    'project': ('library', 'get_by_document_id'),
}


def _verb_records_list(ctx: _Ctx) -> _VerbOutcome:
    args = ctx.args
    kind = args.kind
    repo_name, method, id_attr, sha_attr = _RECORD_KINDS[kind]
    repo = getattr(ctx.repos, repo_name)
    records = getattr(repo, method)(args.document_id)
    if kind == 'headless_run' and args.verb_filter:
        records = [r for r in records
                   if r.verb == args.verb_filter]
    items = [{
        'id': getattr(r, id_attr),
        'sha256': getattr(r, sha_attr),
    } for r in records]
    return _VerbOutcome(
        'succeeded', verdict='listed',
        data={'kind': kind, 'count': len(items), 'records': items})


def _verb_drill_list(ctx: _Ctx) -> _VerbOutcome:
    from .cad_recovery_drill import (
        RECOVERY_DRILL_MANIFEST_FORMAT,
        builtin_drill_manifest,
    )

    manifest = builtin_drill_manifest()
    return _VerbOutcome(
        'succeeded', verdict='listed',
        data={
            'format': RECOVERY_DRILL_MANIFEST_FORMAT,
            'manifest_sha256': manifest.manifest_sha256,
            'count': len(manifest.drills),
            'drills': [{
                'drill_id': spec.drill_id,
                'title': spec.title,
                'issue_refs': list(spec.issue_refs),
                'fault': spec.fault,
                'expected_terminal': spec.expectation.expected_terminal,
                'mutating': spec.mutating,
            } for spec in manifest.drills],
        })


def _verb_drill_run(ctx: _Ctx) -> _VerbOutcome:
    from .cad_recovery_drill import (
        DrillContext,
        builtin_drill_manifest,
        run_drill_manifest,
        select_drills,
    )

    args = ctx.args
    manifest = builtin_drill_manifest()
    try:
        selected = select_drills(
            manifest, tuple(args.drill_id or ()) or None)
    except ValueError as exc:
        raise HeadlessCliError(
            'missing_evidence', f'{exc}', verdict='unknown_drill')
    if args.dry_run:
        return _VerbOutcome(
            'succeeded', verdict='planned',
            data={
                'manifest_sha256': manifest.manifest_sha256,
                'planned': [d.drill_id for d in selected],
            })
    if not args.arm:
        return _VerbOutcome(
            'unauthorized', verdict='arm_required',
            reason='drill run requires --arm — drills inject faults and '
                   'write sealed evidence into their sandbox')
    work_root = (
        Path(args.work_root)
        if args.work_root
        else ctx.repos.data_dir / 'recovery-drill')
    if not ctx.document_id:
        # A run record must always seal — default the document binding
        # rather than let the record silently skip.
        ctx.document_id = 'recovery-drill'
    started = _utc_now()
    mono = time.monotonic()
    report = run_drill_manifest(
        manifest,
        DrillContext(
            work_root=work_root, document_id=ctx.document_id),
        drill_ids=tuple(args.drill_id or ()) or None,
        started_at_utc=started,
        finished_at_utc=_utc_now(),
        elapsed_ms=int((time.monotonic() - mono) * 1000),
    )
    if args.out:
        Path(args.out).write_text(
            json.dumps(report.model_dump(mode='json'), indent=2,
                       sort_keys=True),
            encoding='utf-8')
    refs = tuple(
        ref for result in report.results for ref in result.records)
    failing = [r.drill_id for r in report.results
               if r.verdict != 'recovered_as_designed']
    return _VerbOutcome(
        'failed' if failing else 'succeeded',
        verdict=report.overall_verdict,
        reason=(
            None if not failing
            else 'failing drills: ' + ','.join(failing)),
        refs=refs,
        data={
            'report_sha256': report.report_sha256,
            'manifest_sha256': manifest.manifest_sha256,
            'work_root': str(work_root),
            'report_path': args.out,
            'results': [{
                'drill_id': r.drill_id,
                'verdict': r.verdict,
                'observed_terminal': r.observed_terminal,
                'detail': r.detail,
            } for r in report.results],
        },
        spec_payload={
            'verb': 'drill.run',
            'manifest_sha256': manifest.manifest_sha256,
            'drill_ids': [d.drill_id for d in selected],
        })


def _verb_records_export(ctx: _Ctx) -> _VerbOutcome:
    args = ctx.args
    kind = args.kind
    repo_name, method = _RECORD_GETTERS[kind]
    repo = getattr(ctx.repos, repo_name)
    record = getattr(repo, method)(args.id)
    if record is None:
        raise HeadlessCliError(
            'missing_evidence',
            f'no {kind} record {args.id}')
    payload = record.model_dump(mode='json')
    if args.out:
        _write_json_file(args.out, payload)
    return _VerbOutcome(
        'succeeded', verdict='exported',
        data={'kind': kind, 'id': args.id,
              'payload': payload if not args.out else None,
              'out': args.out})


def _verb_status(ctx: _Ctx) -> _VerbOutcome:
    build = get_build_info()
    wasapi = WasapiAudioBackend()
    return _VerbOutcome(
        'succeeded', verdict='ok',
        data={
            'tool_version': build.display_version,
            'commit_sha': build.commit_sha,
            'dirty': build.dirty,
            'source': build.source,
            'env_fingerprint': _env_fingerprint(),
            'data_dir': str(ctx.repos.data_dir),
            'authority_version': HEADLESS_CLI_AUTHORITY_VERSION,
            'backends': {
                'fake': 'available',
                'wasapi': ('available' if wasapi.available()
                           else wasapi.unavailable_reason()),
            },
        })


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise HeadlessCliError(
            'missing_evidence', f'usage: {message}', verdict='usage')


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog='htdt',
        description='HTDT headless commissioning CLI (#888)')
    parser.add_argument('--data-dir', type=Path, default=None,
                        help='managed data dir (default: platform)')
    parser.add_argument('--json', action='store_true',
                        help='emit only the JSON result envelope')
    parser.add_argument('--dry-run', action='store_true',
                        help='plan without sealed writes or mutation')
    parser.add_argument('--timeout-seconds', type=float, default=None)
    parser.add_argument('--document-id', default=None)

    families = parser.add_subparsers(dest='family', required=True)

    project = families.add_parser('project')
    project_verbs = project.add_subparsers(dest='verb', required=True)
    pc = project_verbs.add_parser('create')
    pc.add_argument('--name', required=True)
    pc.add_argument('--description', default=None)
    pi = project_verbs.add_parser('inspect')
    pi.add_argument('--project-id', default=None)

    sweep = families.add_parser('sweep')
    sweep_verbs = sweep.add_subparsers(dest='verb', required=True)
    sr = sweep_verbs.add_parser('run')
    sr.add_argument('--spec', required=True)
    sr.add_argument('--arm', action='store_true')

    campaign = families.add_parser('campaign')
    campaign_verbs = campaign.add_subparsers(dest='verb', required=True)
    cp = campaign_verbs.add_parser('plan')
    cp.add_argument('--spec', required=True)
    cp.add_argument('--out', default=None)
    cr = campaign_verbs.add_parser('run')
    src = cr.add_mutually_exclusive_group(required=True)
    src.add_argument('--plan', default=None)
    src.add_argument('--plan-id', default=None)
    cr.add_argument('--backend-spec', default=None)
    cr.add_argument('--arm', action='store_true')
    cr.add_argument('--auto-confirm-position', action='store_true')

    cv = families.add_parser('channel-verify')
    cv_verbs = cv.add_subparsers(dest='verb', required=True)
    cvp = cv_verbs.add_parser('plan')
    cvp.add_argument('--spec', required=True)
    cvp.add_argument('--out', default=None)
    cvr = cv_verbs.add_parser('run')
    cvsrc = cvr.add_mutually_exclusive_group(required=True)
    cvsrc.add_argument('--plan', default=None)
    cvsrc.add_argument('--plan-id', default=None)
    cvr.add_argument('--backend-spec', default=None)
    cvr.add_argument('--arm', action='store_true')

    cal = families.add_parser('calibration')
    cal_verbs = cal.add_subparsers(dest='verb', required=True)
    calr = cal_verbs.add_parser('run')
    calr.add_argument('--spec', required=True)
    calr.add_argument('--arm', action='store_true')
    calr.add_argument('--confirm-hardware', action='store_true')

    dep = families.add_parser('deploy')
    dep_verbs = dep.add_subparsers(dest='verb', required=True)
    dr = dep_verbs.add_parser('run')
    dr.add_argument('--spec', required=True)
    dr.add_argument('--preview-only', action='store_true')
    dr.add_argument('--authorize-apply', default=None, metavar='OP')
    dr.add_argument('--authorize-rollback', default=None, metavar='OP')

    diag = families.add_parser('diagnose')
    diag_verbs = diag.add_subparsers(dest='verb', required=True)
    drun = diag_verbs.add_parser('run')
    drun.add_argument('--spec', required=True)
    drun.add_argument('--arm', action='store_true')
    drun.add_argument('--authorize', default=None, metavar='OP')

    rec = families.add_parser('records')
    rec_verbs = rec.add_subparsers(dest='verb', required=True)
    rl = rec_verbs.add_parser('list')
    rl.add_argument('--kind', required=True,
                   choices=sorted(_RECORD_KINDS))
    rl.add_argument('--verb', dest='verb_filter', default=None,
                    help='filter headless_run records to one verb')
    re_ = rec_verbs.add_parser('export')
    re_.add_argument('--kind', required=True,
                    choices=sorted(_RECORD_GETTERS))
    re_.add_argument('--id', required=True)
    re_.add_argument('--out', default=None)

    drill = families.add_parser('drill')
    drill_verbs = drill.add_subparsers(dest='verb', required=True)
    drill_verbs.add_parser('list')
    drl = drill_verbs.add_parser('run')
    drl.add_argument('--drill-id', action='append', default=None)
    drl.add_argument('--all', action='store_true')
    drl.add_argument('--arm', action='store_true')
    drl.add_argument('--out', default=None)
    drl.add_argument('--work-root', default=None)

    families.add_parser('status')
    return parser


_HANDLERS: dict[str, Callable[[_Ctx], _VerbOutcome]] = {
    'project.create': _verb_project_create,
    'project.inspect': _verb_project_inspect,
    'sweep.run': _verb_sweep_run,
    'campaign.plan': _verb_campaign_plan,
    'campaign.run': _verb_campaign_run,
    'channel_verify.plan': _verb_channel_verify_plan,
    'channel_verify.run': _verb_channel_verify_run,
    'calibration.run': _verb_calibration_run,
    'deployment.run': _verb_deployment_run,
    'diagnostic.run': _verb_diagnostic_run,
    'records.list': _verb_records_list,
    'records.export': _verb_records_export,
    'drill.list': _verb_drill_list,
    'drill.run': _verb_drill_run,
    'status': _verb_status,
}

# family token -> canonical verb prefix
_FAMILY_PREFIX = {
    'project': 'project',
    'sweep': 'sweep',
    'campaign': 'campaign',
    'channel-verify': 'channel_verify',
    'calibration': 'calibration',
    'deploy': 'deployment',
    'diagnose': 'diagnostic',
    'records': 'records',
    'drill': 'drill',
    'status': '',
}


def _envelope(
    *,
    verb: str,
    outcome: str,
    verdict: str | None,
    reason: str | None,
    record: CadHeadlessRunRecord | None,
    refs: tuple[AuthorityRef, ...],
    data: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        'format': HEADLESS_RESULT_FORMAT,
        'verb': verb,
        'outcome': outcome,
        'exit_code': OUTCOME_EXIT_CODES.get(outcome, EXIT_INTERNAL),
        'verdict': verdict,
        'reason': reason,
        'run_record_id': record.run_record_id if record else None,
        'run_sha256': record.run_sha256 if record else None,
        'records': [
            {'kind': r.kind, 'ref_id': r.ref_id,
             'ref_sha256': r.ref_sha256}
            for r in refs],
        'data': data,
    }


def _emit(envelope: dict[str, Any], json_mode: bool) -> None:
    if json_mode:
        sys.stdout.write(canonical_json(envelope) + '\n')
        return
    parts = [
        f"htdt {envelope['verb']}",
        f"outcome={envelope['outcome']}",
    ]
    if envelope['verdict']:
        parts.append(f"verdict={envelope['verdict']}")
    if envelope['run_record_id']:
        parts.append(f"run={envelope['run_record_id']}")
    if envelope['reason']:
        parts.append(f"reason={envelope['reason']}")
    sys.stdout.write(' '.join(parts) + '\n')


_GLOBAL_BOOL_FLAGS = {'--json', '--dry-run'}
_GLOBAL_VALUE_FLAGS = {'--data-dir', '--timeout-seconds', '--document-id'}


def _hoist_global_flags(argv: list[str]) -> list[str]:
    """Move global flags ahead of the verb tokens so ``--json`` works
    anywhere on the command line (argparse subparsers otherwise require
    globals to precede the family)."""

    head: list[str] = []
    tail: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        name, eq, value = token.partition('=')
        if token in _GLOBAL_BOOL_FLAGS:
            head.append(token)
        elif name in _GLOBAL_VALUE_FLAGS:
            head.append(token)
            if not eq and index + 1 < len(argv):
                index += 1
                head.append(argv[index])
        else:
            tail.append(token)
        index += 1
    return head + tail


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = '--json' in argv
    argv = _hoist_global_flags(argv)
    try:
        sys.stdout.reconfigure(errors='backslashreplace')
    except (AttributeError, ValueError):
        pass
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except HeadlessCliError as exc:
        envelope = _envelope(
            verb='?', outcome='missing_evidence', verdict='usage',
            reason=exc.reason, record=None, refs=(), data=None)
        envelope['exit_code'] = EXIT_USAGE
        _emit(envelope, json_mode)
        return EXIT_USAGE
    family = _FAMILY_PREFIX[args.family]
    verb = f'{family}.{args.verb}' if family else 'status'
    args.verb_token = verb

    data_dir, _source = resolve_data_dir(args.data_dir)
    repos = _Repositories(data_dir)
    deadline = _Deadline(args.timeout_seconds)
    started_at = _utc_now()
    monotonic_start = time.monotonic()

    # Resolve + validate the input spec before opening repositories so a
    # malformed spec never touches storage.
    spec = None
    spec_arg = getattr(args, 'spec', None)
    try:
        if spec_arg or verb in (
                'sweep.run', 'campaign.plan', 'channel_verify.plan',
                'calibration.run', 'deployment.run', 'diagnostic.run'):
            spec = _load_spec(verb, spec_arg)
            args.resolved_spec = spec
    except HeadlessCliError as exc:
        envelope = _envelope(
            verb=verb, outcome=exc.outcome, verdict=exc.verdict,
            reason=exc.reason, record=None, refs=(), data=None)
        _emit(envelope, json_mode)
        return envelope['exit_code']
    document_id = ''
    sealed = verb in SEALED_VERBS
    record: CadHeadlessRunRecord | None = None
    try:
        if verb == 'project.inspect':
            document_id = args.document_id or args.project_id or ''
        elif verb == 'project.create':
            document_id = args.document_id or ''
        elif spec is not None:
            document_id = _resolve_document_id(
                args.document_id, spec)
        else:
            document_id = args.document_id or ''
        ctx = _Ctx(repos=repos, document_id=document_id,
                   args=args, deadline=deadline)
        handler = _HANDLERS[verb]
        try:
            result = handler(ctx)
        except HeadlessCliError as exc:
            result = _VerbOutcome(exc.outcome, verdict=exc.verdict,
                                  reason=exc.reason)
        except (
            ProjectLibraryError, PipelineStageError,
            PipelineAuthorizationError, DiagnosticOrchestrationError,
            DiagnosticSafetyError, ValueError,
        ) as exc:
            result = _VerbOutcome('failed', verdict='error',
                                  reason=f'{type(exc).__name__}: {exc}')
        except Exception as exc:  # error-boundary: seal the attempt too
            result = _VerbOutcome('failed', verdict='internal',
                                  reason=f'{type(exc).__name__}: {exc}')
        finished_at = _utc_now()
        elapsed_ms = int(
            (time.monotonic() - monotonic_start) * 1000)
        backend_id = None
        backend_is_simulated: bool | None = None
        if spec is not None and isinstance(spec, HeadlessBackendSpec):
            backend_id = spec.backend
            backend_is_simulated = spec.backend == 'fake'
        elif verb in ('campaign.run', 'channel_verify.run'):
            bspec = getattr(args, 'resolved_backend_spec', None)
            if bspec is not None:
                backend_id = bspec.backend
                backend_is_simulated = bspec.backend == 'fake'
            elif getattr(args, 'backend_spec', None) is None:
                backend_id = 'fake'
                backend_is_simulated = True
        elif verb == 'deployment.run':
            dep_backend = getattr(args, 'deployment_backend', None)
            if dep_backend is not None:
                backend_id, backend_is_simulated = dep_backend
        if backend_is_simulated is None:
            # Substrate never resolved (handler failed before building
            # it) — the sealed record claims simulated rather than a
            # real lane the run cannot prove.
            backend_is_simulated = True
        seal_document_id = ctx.document_id or document_id
        if sealed and not args.dry_run and seal_document_id:
            spec_json = None
            spec_sha = None
            if spec is None:
                spec_payload = result.spec_payload or {
                    'verb': verb}
                spec_json = canonical_json(spec_payload)
                spec_sha = canonical_sha256(spec_payload)
            build = get_build_info()
            record = build_run_record(
                document_id=seal_document_id,
                verb=verb,
                outcome=result.outcome,
                spec=spec,
                spec_json=spec_json,
                spec_sha=spec_sha,
                tool_version=build.display_version,
                tool_commit_sha=build.commit_sha,
                tool_commit_dirty=build.dirty,
                python_version=sys.version.split()[0],
                platform=_platform.system(),
                env_fingerprint=_env_fingerprint(),
                argv_sha256=_argv_sha256(tuple(argv)),
                backend_id=backend_id,
                backend_is_simulated=backend_is_simulated,
                record_refs=result.refs,
                verdict=result.verdict,
                reason=result.reason,
                dry_run=False,
                cancelled=result.outcome == 'cancelled',
                timeout_seconds=args.timeout_seconds,
                started_at_utc=started_at,
                finished_at_utc=finished_at,
                elapsed_ms=elapsed_ms,
            )
            try:
                repos.headless.save_run(record)
            except Exception as exc:  # error-boundary: never mask outcome
                result.reason = (
                    f'{result.reason or ""} [record-save failed: {exc}]')
        outcome = result.outcome if not args.dry_run else 'dry_run'
        _emit(_envelope(
            verb=verb, outcome=outcome, verdict=result.verdict,
            reason=result.reason, record=record, refs=result.refs,
            data=result.data), json_mode)
        return OUTCOME_EXIT_CODES.get(outcome, EXIT_INTERNAL)
    except HeadlessCliError as exc:
        _emit(_envelope(
            verb=verb, outcome=exc.outcome, verdict=exc.verdict,
            reason=exc.reason, record=None, refs=(), data=None),
            json_mode)
        return OUTCOME_EXIT_CODES.get(exc.outcome, EXIT_INTERNAL)


if __name__ == '__main__':
    raise SystemExit(main())
