"""Control / automation scenario qualification authority (issue #601).

A control or automation scenario (startup, source/route switching,
shutdown, preset recall) is *declared* as an expected step sequence with
explicit feedback expectations and timeouts, and *qualified* only
against sealed execution records. This module is the fail-closed twin
of the #591 transport authority: a networked AV path that routes
packets correctly still proves nothing about whether "power on the
system" actually powers the system, routes the right source, reports
back, and fails audibly when it cannot.

Standards basis (see docs/reviews/rev56-ops.md): ANSI/AVIXA D402.02:2013
(R2024) *Audiovisual Systems Performance Verification* — itemized
functional verification, owner-approved test methods, documented
results — registered through the #599 external-standards registry as
``ansi-avixa-d402-02@2013-r2024``; AVIXA TR-111:2019 *Unified
Automation for Buildings* — the AV-in-building-automation context —
as ``avixa-tr-111@2019``.

Contract properties:

- a scenario is a *declaration of intent*, never a claim of function:
  expected steps, expected feedback, per-step timeout and the required
  failure-notification behaviour are versioned before any run;
- a run is *evidence*: per-step observed outcomes with elapsed time and
  observed feedback, never synthesized — unobserved steps read
  ``not_executed``, not "assumed ok";
- the qualification verdict is fail-closed:
  :func:`evaluate_scenario_qualification` returns ``not_executed`` when
  no run exists, ``stale`` when the bound run pins a different scenario
  revision, ``failed`` on timeout/wrong-feedback/missing-notification,
  ``unverified`` on partial coverage — never "動作保証" without records;
- timeouts are per-step declarations (a step that takes 30 s when 5 s
  was declared is a deviation, not "slow but fine");
- failure notification is a *verification target*: a scenario that
  requires operator notification on failure must show the notification
  actually occurred (or was demonstrably exercised) — declaring the
  requirement is not evidence it works;
- this module records what *should* happen and what *did* happen — it
  never executes control actions itself.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_equipment import EquipmentDataProvenance
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


CONTROL_SCENARIO_SCHEMA_VERSION = 1

CONTROL_SURFACE_AUTHORITY_VERSION = 'rev56-control-surface-1'
SCENARIO_AUTHORITY_VERSION = 'rev56-control-scenario-1'
SCENARIO_RUN_AUTHORITY_VERSION = 'rev56-control-run-1'
SCENARIO_QUALIFICATION_AUTHORITY_VERSION = 'rev56-control-qualification-1'
CONTROL_EVALUATION_VERSION = 'rev56-control-eval-1'

_SHA256 = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


# ---------------------------------------------------------------------------
# Taxonomies
# ---------------------------------------------------------------------------

ControllerFamily = Literal[
    'crestron',
    'amx_harman',
    'control4',
    'rti',
    'extron',
    'qsys',
    'kramer',
    'bsp_dsp_internal',
    'custom_script',
    'unknown',
]
"""Controller platform family — a taxonomy, not a capability claim."""

ScenarioKind = Literal[
    'startup',
    'shutdown',
    'routing_switch',
    'source_select',
    'preset_recall',
    'volume_level_set',
    'display_control',
    'occupancy_automation',
    'scheduled_automation',
    'emergency_override',
    'custom',
]
"""What kind of operator-facing scenario is declared."""

StepActionKind = Literal[
    'power_on',
    'power_off',
    'route_set',
    'source_select',
    'volume_set',
    'mute_set',
    'display_mode_set',
    'preset_recall',
    'wait_for_feedback',
    'notify_operator',
    'custom',
]
"""The action one scenario step intends to perform."""

StepOutcome = Literal[
    'verified',
    'timeout',
    'wrong_feedback',
    'command_rejected',
    'not_executed',
    'skipped',
    'manual_override',
]
"""Observed outcome of one step in a run."""

RunOutcome = Literal[
    'completed',
    'completed_with_deviations',
    'aborted_on_failure',
    'failed',
]
"""Overall outcome declared for one execution run."""

FailureNotificationOutcome = Literal[
    'notified',
    'not_required',
    'missed',
    'not_observed',
]
"""Whether the required failure notification was observed in a run."""

ScenarioCheck = Literal[
    'declaration',
    'feedback_expectation',
    'timeouts',
    'execution_coverage',
    'failure_notification',
    'revision_freshness',
]
"""The six dimensions :func:`evaluate_scenario_qualification` reports."""

ScenarioCheckResult = Literal[
    'verified', 'limited', 'failed', 'not_applicable'
]

ScenarioQualificationState = Literal[
    'qualified',
    'qualified_with_deviations',
    'unverified',
    'not_executed',
    'stale',
    'failed',
    'draft',
]
"""Fail-closed qualification verdict.

``draft`` = declared with no steps yet (not evaluable).
``not_executed`` = steps declared, never run — the default state and
explicitly *not* a guarantee of operation.
``stale`` = the newest run pins a different scenario revision.
``unverified`` = runs exist but coverage is partial.
``qualified_with_deviations`` = all steps covered, deviations recorded.
``failed`` = at least one step timed out, returned wrong feedback, was
rejected, or a required notification was missed.
``qualified`` = every declared step verified within its timeout across a
completed run pinning this exact scenario revision.
"""


# ---------------------------------------------------------------------------
# Control surface declaration
# ---------------------------------------------------------------------------

class ControlSurfaceDeclaration(BaseModel):
    """Sealed declaration of one control/automation surface.

    Declares *what controls what* — the controller family, its program /
    project identity and version pin, and the subjects it claims to
    drive — without asserting the control actually works.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    surface_id: str = Field(min_length=1)
    surface_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    controller_ref: AuthorityRef | None = None
    controller_family: ControllerFamily
    program_identity: str | None = None
    program_version: str | None = None
    program_sha256: str | None = Field(default=None, pattern=_SHA256)
    controlled_subject_refs: tuple[AuthorityRef, ...] = ()
    interface_note: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _seal(self) -> 'ControlSurfaceDeclaration':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        if self.program_sha256 is not None and self.program_version is None:
            raise ValueError(
                'a program hash requires the version it pins — a bare '
                'hash is not an identifiable program revision'
            )
        expected = _hash(self.identity_payload())
        if self.surface_sha256 != expected:
            raise ValueError('control surface sha256 does not match payload')
        if self.surface_id != _semantic_id('ctrlsurf', expected):
            raise ValueError('control surface id does not match payload')
        return self

    def identity_payload(self) -> dict[str, object]:
        return self.model_dump(
            mode='json', exclude={'surface_id', 'surface_sha256'}
        )


# ---------------------------------------------------------------------------
# Scenario declaration
# ---------------------------------------------------------------------------

class ControlScenarioStep(BaseModel):
    """One expected step inside a declared scenario.

    ``expected_feedback`` is the observable that proves the step did what
    it claimed (a device reports power-on, a route reports its new map,
    an LED/display state). A step with no expectation cannot be verified
    — the model allows it but the evaluator never counts it as verified.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    position: int = Field(ge=1)
    action_kind: StepActionKind
    target_label: str | None = None
    target_ref: AuthorityRef | None = None
    action_detail: str | None = None
    expected_feedback: str | None = None
    timeout_ms: int = Field(gt=0)
    continue_on_failure: bool = False

    @model_validator(mode='after')
    def _check(self) -> 'ControlScenarioStep':
        if self.target_label is None and self.target_ref is None:
            raise ValueError(
                'a step requires a target — a label or an authority ref '
                '(a step against nothing verifies nothing)'
            )
        return self


class ControlScenario(BaseModel):
    """Sealed expected-step-sequence declaration for one scenario."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    scenario_id: str = Field(min_length=1)
    scenario_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    surface_ref: AuthorityRef | None = None
    kind: ScenarioKind
    name: str = Field(min_length=1)
    description: str | None = None
    steps: tuple[ControlScenarioStep, ...] = ()
    failure_notification_required: bool = False
    failure_notification_channel: str | None = None
    overall_timeout_ms: int | None = Field(default=None, gt=0)
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _seal(self) -> 'ControlScenario':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        positions = [s.position for s in self.steps]
        if len(set(positions)) != len(positions):
            raise ValueError('scenario step positions must be unique')
        if positions and positions != sorted(positions):
            raise ValueError('scenario steps must be ordered by position')
        if self.failure_notification_required and (
            self.failure_notification_channel is None
        ):
            raise ValueError(
                'a required failure notification must name its channel — '
                '"notify someone" without a channel cannot be verified'
            )
        expected = _hash(self.identity_payload())
        if self.scenario_sha256 != expected:
            raise ValueError('scenario sha256 does not match payload')
        if self.scenario_id != _semantic_id('ctrlscn', expected):
            raise ValueError('scenario id does not match payload')
        return self

    def identity_payload(self) -> dict[str, object]:
        return self.model_dump(
            mode='json', exclude={'scenario_id', 'scenario_sha256'}
        )


# ---------------------------------------------------------------------------
# Execution record
# ---------------------------------------------------------------------------

class ControlRunStepRecord(BaseModel):
    """Observed outcome of one declared step in one run."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    step_position: int = Field(ge=1)
    outcome: StepOutcome
    elapsed_ms: int | None = Field(default=None, ge=0)
    observed_feedback: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'ControlRunStepRecord':
        if self.outcome == 'verified' and self.elapsed_ms is None:
            raise ValueError(
                'a verified step requires its observed elapsed time — '
                'a timeout declaration is meaningless without it'
            )
        if self.outcome in ('verified', 'wrong_feedback') and (
            self.observed_feedback is None
        ):
            raise ValueError(
                f'{self.outcome} requires the observed feedback — '
                'the record must say what actually happened'
            )
        return self


class ControlScenarioRun(BaseModel):
    """Sealed execution record of one scenario.

    ``scenario_ref`` pins the exact scenario revision executed; a run
    against an older revision is evidence about that older revision, not
    the current one (the evaluator reports ``stale``).
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    run_id: str = Field(min_length=1)
    run_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    scenario_ref: AuthorityRef
    outcome: RunOutcome
    step_records: tuple[ControlRunStepRecord, ...] = ()
    failure_notification_outcome: FailureNotificationOutcome = (
        'not_observed'
    )
    executed_by: str | None = None
    execution_note: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    authority_version: str = Field(min_length=1)
    started_at_utc: str = Field(min_length=1)
    finished_at_utc: str | None = None

    @model_validator(mode='after')
    def _seal(self) -> 'ControlScenarioRun':
        _require_iso8601(self.started_at_utc, 'started_at_utc')
        if self.finished_at_utc is not None:
            _require_iso8601(self.finished_at_utc, 'finished_at_utc')
        positions = [s.step_position for s in self.step_records]
        if len(set(positions)) != len(positions):
            raise ValueError(
                'run step records must not duplicate a step position'
            )
        if self.outcome == 'completed' and (
            self.finished_at_utc is None
        ):
            raise ValueError(
                'a completed run requires its finish time'
            )
        expected = _hash(self.identity_payload())
        if self.run_sha256 != expected:
            raise ValueError('scenario run sha256 does not match payload')
        if self.run_id != _semantic_id('ctrlrun', expected):
            raise ValueError('scenario run id does not match payload')
        return self

    def identity_payload(self) -> dict[str, object]:
        return self.model_dump(
            mode='json', exclude={'run_id', 'run_sha256'}
        )


# ---------------------------------------------------------------------------
# Qualification verdict
# ---------------------------------------------------------------------------

class ControlScenarioQualification(BaseModel):
    """Sealed fail-closed qualification verdict for one scenario."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    qualification_id: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    scenario_ref: AuthorityRef
    state: ScenarioQualificationState
    checks: tuple[tuple[ScenarioCheck, ScenarioCheckResult], ...] = ()
    reasons: tuple[str, ...] = ()
    deviations: tuple[str, ...] = ()
    run_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _seal(self) -> 'ControlScenarioQualification':
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        if self.state == 'qualified' and not self.run_refs:
            raise ValueError(
                'a qualified verdict requires at least one bound run — '
                'no claim of operation without evidence'
            )
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError(
                'qualification sha256 does not match payload'
            )
        if self.qualification_id != _semantic_id('ctrlqual', expected):
            raise ValueError('qualification id does not match payload')
        return self

    def identity_payload(self) -> dict[str, object]:
        return self.model_dump(
            mode='json',
            exclude={'qualification_id', 'qualification_sha256'},
        )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _seal(model, payload: dict[str, object], id_field: str,
          sha_field: str, prefix: str):
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{id_field: _semantic_id(prefix, digest), sha_field: digest},
    )


def build_control_surface(
    *,
    document_id: str,
    controller_family: ControllerFamily,
    controller_ref: AuthorityRef | None = None,
    program_identity: str | None = None,
    program_version: str | None = None,
    program_sha256: str | None = None,
    controlled_subject_refs: tuple[AuthorityRef, ...] = (),
    interface_note: str | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
    declared_at_utc: str | None = None,
) -> ControlSurfaceDeclaration:
    """Seal a control-surface declaration."""
    payload = dict(
        document_id=document_id,
        controller_ref=controller_ref,
        controller_family=controller_family,
        program_identity=program_identity,
        program_version=program_version,
        program_sha256=program_sha256,
        controlled_subject_refs=controlled_subject_refs,
        interface_note=interface_note,
        provenance=provenance,
        authority_version=CONTROL_SURFACE_AUTHORITY_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
    )
    return _seal(
        ControlSurfaceDeclaration, payload,
        'surface_id', 'surface_sha256', 'ctrlsurf',
    )


def build_scenario(
    *,
    document_id: str,
    kind: ScenarioKind,
    name: str,
    steps: tuple[ControlScenarioStep, ...] = (),
    surface_ref: AuthorityRef | None = None,
    description: str | None = None,
    failure_notification_required: bool = False,
    failure_notification_channel: str | None = None,
    overall_timeout_ms: int | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
    declared_at_utc: str | None = None,
) -> ControlScenario:
    """Seal a control-scenario declaration."""
    payload = dict(
        document_id=document_id,
        surface_ref=surface_ref,
        kind=kind,
        name=name,
        description=description,
        steps=steps,
        failure_notification_required=failure_notification_required,
        failure_notification_channel=failure_notification_channel,
        overall_timeout_ms=overall_timeout_ms,
        provenance=provenance,
        authority_version=SCENARIO_AUTHORITY_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
    )
    return _seal(
        ControlScenario, payload,
        'scenario_id', 'scenario_sha256', 'ctrlscn',
    )


def build_scenario_run(
    *,
    document_id: str,
    scenario: ControlScenario,
    outcome: RunOutcome,
    step_records: tuple[ControlRunStepRecord, ...] = (),
    failure_notification_outcome: FailureNotificationOutcome = (
        'not_observed'
    ),
    executed_by: str | None = None,
    execution_note: str | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
    started_at_utc: str,
    finished_at_utc: str | None = None,
) -> ControlScenarioRun:
    """Seal an execution run pinning the exact scenario revision."""
    payload = dict(
        document_id=document_id,
        scenario_ref=AuthorityRef(
            kind='control_scenario',
            ref_id=scenario.scenario_id,
            ref_sha256=scenario.scenario_sha256,
        ),
        outcome=outcome,
        step_records=step_records,
        failure_notification_outcome=failure_notification_outcome,
        executed_by=executed_by,
        execution_note=execution_note,
        provenance=provenance,
        authority_version=SCENARIO_RUN_AUTHORITY_VERSION,
        started_at_utc=started_at_utc,
        finished_at_utc=finished_at_utc,
    )
    return _seal(
        ControlScenarioRun, payload,
        'run_id', 'run_sha256', 'ctrlrun',
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _seal_qualification(
    *,
    document_id: str,
    scenario: ControlScenario,
    state: ScenarioQualificationState,
    checks: tuple[tuple[ScenarioCheck, ScenarioCheckResult], ...],
    reasons: tuple[str, ...],
    deviations: tuple[str, ...],
    run_refs: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    evaluated_at_utc: str,
) -> ControlScenarioQualification:
    payload = dict(
        document_id=document_id,
        scenario_ref=AuthorityRef(
            kind='control_scenario',
            ref_id=scenario.scenario_id,
            ref_sha256=scenario.scenario_sha256,
        ),
        state=state,
        checks=checks,
        reasons=reasons,
        deviations=deviations,
        run_refs=run_refs,
        evidence_refs=evidence_refs,
        evaluation_version=CONTROL_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal(
        ControlScenarioQualification, payload,
        'qualification_id', 'qualification_sha256', 'ctrlqual',
    )


def evaluate_scenario_qualification(
    *,
    document_id: str,
    scenario: ControlScenario,
    runs: tuple[ControlScenarioRun, ...] = (),
    evaluated_at_utc: str | None = None,
) -> ControlScenarioQualification:
    """Fail-closed qualification verdict for one declared scenario.

    A scenario that was never run is ``not_executed`` — never "probably
    works". A run pinning a different scenario revision makes the
    verdict ``stale``. Any timed-out, wrong-feedback, rejected or
    uncovered step, or a missed required failure notification, makes it
    ``failed`` / ``unverified``.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    checks: list[tuple[ScenarioCheck, ScenarioCheckResult]] = []
    reasons: list[str] = []
    deviations: list[str] = []

    # --- declaration ---------------------------------------------------
    if not scenario.steps:
        checks.extend((
            ('declaration', 'limited'),
            ('feedback_expectation', 'not_applicable'),
            ('timeouts', 'not_applicable'),
            ('execution_coverage', 'not_applicable'),
            ('failure_notification', 'not_applicable'),
            ('revision_freshness', 'not_applicable'),
        ))
        return _seal_qualification(
            document_id=document_id,
            scenario=scenario,
            state='draft',
            checks=tuple(checks),
            reasons=('scenario declares no steps — nothing to qualify',),
            deviations=(),
            run_refs=(),
            evidence_refs=(),
            evaluated_at_utc=evaluated_at_utc,
        )
    checks.append(('declaration', 'verified'))

    # --- feedback expectation -------------------------------------------
    without_feedback = [
        s.position for s in scenario.steps if s.expected_feedback is None
    ]
    if not without_feedback:
        checks.append(('feedback_expectation', 'verified'))
    else:
        checks.append(('feedback_expectation', 'limited'))
        deviations.append(
            f'step(s) {without_feedback} declare no expected feedback — '
            'they can never count as verified'
        )

    # --- timeouts --------------------------------------------------------
    checks.append(('timeouts', 'verified'))

    # --- execution coverage ---------------------------------------------
    matching_runs = [
        r for r in runs
        if r.scenario_ref.ref_sha256 == scenario.scenario_sha256
    ]
    stale_runs = [
        r for r in runs
        if r.scenario_ref.ref_id == scenario.scenario_id
        and r.scenario_ref.ref_sha256 != scenario.scenario_sha256
    ]
    if not matching_runs:
        checks.extend((
            ('execution_coverage', 'failed'),
            (
                'failure_notification',
                'not_applicable'
                if not scenario.failure_notification_required
                else 'failed',
            ),
            ('revision_freshness', 'limited' if stale_runs else 'not_applicable'),
        ))
        if scenario.failure_notification_required:
            reasons.append(
                'failure notification is required but no run exists to '
                'show it works'
            )
        if stale_runs:
            reasons.append(
                'only runs pinning an older scenario revision exist'
            )
        return _seal_qualification(
            document_id=document_id,
            scenario=scenario,
            state='stale' if stale_runs else 'not_executed',
            checks=tuple(checks),
            reasons=tuple(
                reasons + ['no execution run pins this scenario revision']
            ),
            deviations=tuple(deviations),
            run_refs=tuple(r.run_id for r in stale_runs[:4]),
            evidence_refs=(),
            evaluated_at_utc=evaluated_at_utc,
        )

    # Newest matching run is the evidence basis.
    run = max(matching_runs, key=lambda r: r.started_at_utc)
    record_map = {s.step_position: s for s in run.step_records}

    uncovered: list[int] = []
    timed_out: list[int] = []
    wrong_feedback: list[int] = []
    rejected: list[int] = []
    over_declared_timeout: list[int] = []
    manual: list[int] = []
    not_executed_steps: list[int] = []
    for step in scenario.steps:
        rec = record_map.get(step.position)
        if rec is None:
            uncovered.append(step.position)
            continue
        if rec.outcome == 'verified':
            if (
                step.expected_feedback is not None
                and rec.elapsed_ms is not None
                and rec.elapsed_ms > step.timeout_ms
            ):
                over_declared_timeout.append(step.position)
            continue
        if rec.outcome == 'timeout':
            timed_out.append(step.position)
        elif rec.outcome == 'wrong_feedback':
            wrong_feedback.append(step.position)
        elif rec.outcome == 'command_rejected':
            rejected.append(step.position)
        elif rec.outcome == 'manual_override':
            manual.append(step.position)
        elif rec.outcome in ('not_executed', 'skipped'):
            not_executed_steps.append(step.position)

    if uncovered or not_executed_steps:
        checks.append(('execution_coverage', 'limited'))
        deviations.append(
            f'step(s) {uncovered + not_executed_steps} have no observed '
            'outcome in the newest run'
        )
    elif timed_out or wrong_feedback or rejected:
        checks.append(('execution_coverage', 'failed'))
    elif manual or over_declared_timeout or (
        run.outcome == 'completed_with_deviations'
    ):
        checks.append(('execution_coverage', 'limited'))
    else:
        checks.append(('execution_coverage', 'verified'))

    # --- failure notification --------------------------------------------
    if not scenario.failure_notification_required:
        checks.append(('failure_notification', 'not_applicable'))
    elif run.failure_notification_outcome == 'notified':
        checks.append(('failure_notification', 'verified'))
    elif run.failure_notification_outcome == 'missed':
        checks.append(('failure_notification', 'failed'))
        reasons.append(
            'a required failure notification was missed in the newest run'
        )
    else:
        checks.append(('failure_notification', 'limited'))
        deviations.append(
            'failure notification is required but the newest run did '
            'not observe one (not_observed)'
        )

    # --- revision freshness -----------------------------------------------
    checks.append((
        'revision_freshness',
        'limited' if stale_runs else 'verified',
    ))
    if stale_runs:
        deviations.append(
            'run(s) pinning older scenario revisions also exist — '
            'history is preserved but only the pinned revision counts'
        )

    # --- verdict -----------------------------------------------------------
    check_map = dict(checks)
    if run.outcome in ('failed', 'aborted_on_failure') or (
        check_map['execution_coverage'] == 'failed'
    ) or check_map['failure_notification'] == 'failed':
        state: ScenarioQualificationState = 'failed'
    elif check_map['execution_coverage'] == 'limited' or (
        check_map['failure_notification'] == 'limited'
    ):
        state = 'unverified'
    elif check_map['execution_coverage'] == 'verified' and (
        run.outcome == 'completed'
    ) and not (manual or over_declared_timeout):
        state = 'qualified'
    else:
        state = 'qualified_with_deviations'

    if timed_out:
        reasons.append(f'step(s) {timed_out} timed out')
    if wrong_feedback:
        reasons.append(
            f'step(s) {wrong_feedback} reported wrong feedback'
        )
    if rejected:
        reasons.append(f'step(s) {rejected} were rejected by the target')
    if manual:
        deviations.append(
            f'step(s) {manual} completed by manual override — the '
            'automation path itself is unproven'
        )
    if over_declared_timeout:
        deviations.append(
            f'step(s) {over_declared_timeout} verified but exceeded the '
            'declared per-step timeout'
        )

    evidence = [run.run_id]
    if scenario.surface_ref is not None:
        evidence.append(scenario.surface_ref.ref_id)
    return _seal_qualification(
        document_id=document_id,
        scenario=scenario,
        state=state,
        checks=tuple(checks),
        reasons=tuple(reasons),
        deviations=tuple(deviations),
        run_refs=tuple(r.run_id for r in matching_runs[:4]),
        evidence_refs=tuple(evidence),
        evaluated_at_utc=evaluated_at_utc,
    )


__all__ = [
    'CONTROL_EVALUATION_VERSION',
    'CONTROL_SCENARIO_SCHEMA_VERSION',
    'CONTROL_SURFACE_AUTHORITY_VERSION',
    'SCENARIO_AUTHORITY_VERSION',
    'SCENARIO_QUALIFICATION_AUTHORITY_VERSION',
    'SCENARIO_RUN_AUTHORITY_VERSION',
    'ControlRunStepRecord',
    'ControlScenario',
    'ControlScenarioQualification',
    'ControlScenarioRun',
    'ControlScenarioStep',
    'ControlSurfaceDeclaration',
    'ControllerFamily',
    'FailureNotificationOutcome',
    'RunOutcome',
    'ScenarioCheck',
    'ScenarioCheckResult',
    'ScenarioKind',
    'ScenarioQualificationState',
    'StepActionKind',
    'StepOutcome',
    'build_control_surface',
    'build_scenario',
    'build_scenario_run',
    'evaluate_scenario_qualification',
]
