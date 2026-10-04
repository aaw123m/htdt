"""Guided acceptance-run model (REV48-HWGUIDE).

A physical acceptance gate (M10 real-data acceptance, the O60R owned-room
campaign, the UX160 owned-Windows matrix, …) is turned into an ordered step
list the user walks through in the app. Each step is one of three honest
kinds:

* ``auto`` — a check the software really executes (REW API probe, managed
  asset SHA-256 match, backup/restore roundtrip, …). A step is only marked
  auto-verified when the check actually ran and returned a verdict.
* ``guided_manual`` — requires a physical action (aim the UMIK-1, route the
  AVR). The app shows the exact instruction, auto-captures whatever evidence
  is checkable (device list, file hash, measurement count), and the human
  confirms — minimal clicks, explicitly labelled human-confirmed.
* ``attest`` — genuinely unverifiable by software (perceived quality, a
  physical-world observation). Records a typed attestation plus optional
  file-picked evidence, honestly labelled as a human attestation.

The run itself is persisted append-only: every status change lands as a new
revision row whose chained ``run_sha256`` lets a verifier re-derive what was
checked, when, on which machine, and against which code state.
"""

from __future__ import annotations

from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical_json import canonical_json, canonical_sha256
from .clock import utc_now_iso

ACCEPTANCE_RUN_SCHEMA = 'htdt.acceptance-run'
ACCEPTANCE_RUN_SCHEMA_VERSION = 1

ACCEPTANCE_EVIDENCE_BUNDLE_SCHEMA = 'htdt.acceptance-evidence-bundle'
ACCEPTANCE_EVIDENCE_BUNDLE_SCHEMA_VERSION = 1

#: How a step is satisfied — the three-step-kind contract from REV48.
AcceptanceStepKind = Literal['auto', 'guided_manual', 'attest']

#: Step lifecycle. ``pending`` covers "not yet resolved", including a step
#: whose check deferred (e.g. restart-persistence waiting for a relaunch).
AcceptanceStepStatus = Literal[
    'pending', 'passed', 'failed', 'blocked', 'skipped'
]

#: How the recorded status was reached — never claim machine proof for a
#: human confirmation.
AcceptanceVerdictSource = Literal[
    'none', 'auto_check', 'human_confirm', 'attestation'
]

#: Run lifecycle.
AcceptanceRunStatus = Literal['in_progress', 'passed', 'failed', 'partial']

#: Auto-check verdicts mapped onto step statuses.
AUTO_CHECK_VERDICTS = ('pass', 'fail', 'unavailable', 'deferred')

#: Step statuses a fresh run starts with; every step begins pending.
_STEP_TERMINAL = frozenset({'passed', 'failed', 'blocked', 'skipped'})


class GateStepDef(BaseModel):
    """One ordered step inside a gate manifest definition."""

    model_config = ConfigDict(frozen=True)

    step_id: str
    kind: AcceptanceStepKind
    title_ja: str
    instruction_ja: str
    #: Auto-check identifier (e.g. ``rew_engine_probe``). May be present on
    #: any kind: on ``auto`` it IS the step; on ``guided_manual`` it runs as
    #: the auto-capture whose outcome is recorded as evidence while the human
    #: still confirms.
    auto_check: str | None = None
    #: Evidence kinds that must be attached before the step may pass.
    evidence_required: tuple[str, ...] = ()
    #: Free-text input the auto-check needs (e.g. a campaign id), with the
    #: label shown to the user.
    input_label_ja: str | None = None
    #: Source doc/script identifier this step was derived from (provenance
    #: for the manifest — e.g. 'WINDOWS_ACCEPTANCE.md §2 item 4').
    source_ref: str = ''


class GateDefinition(BaseModel):
    """A machine-readable acceptance gate manifest."""

    model_config = ConfigDict(frozen=True)

    gate_id: str
    title_ja: str
    description_ja: str
    source_ref: str
    steps: tuple[GateStepDef, ...]

    def manifest_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode='json'))


class AcceptanceEvidenceRef(BaseModel):
    """Reference to a file installed into the managed-assets store."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    kind: str
    filename: str
    sha256: str
    relative_path: str
    size_bytes: int
    recorded_at_utc: str


class AcceptanceStepRecord(BaseModel):
    """Recorded state of one step inside a run revision."""

    model_config = ConfigDict(frozen=True)

    step_id: str
    kind: AcceptanceStepKind
    status: AcceptanceStepStatus = 'pending'
    verdict_source: AcceptanceVerdictSource = 'none'
    auto_check: str | None = None
    #: Latest check outcome detail (verdict, machine-readable evidence,
    #: deferred notes) — kept small and JSON-canonical.
    check_detail: dict[str, Any] | None = None
    evidence: tuple[AcceptanceEvidenceRef, ...] = ()
    note: str = ''
    attestation: str = ''
    recorded_at_utc: str | None = None

    @model_validator(mode='after')
    def _source_matches_status(self) -> 'AcceptanceStepRecord':
        if self.status == 'pending':
            return self
        if self.verdict_source == 'none':
            raise ValueError(
                f'step {self.step_id} resolved without a verdict source'
            )
        if self.verdict_source == 'auto_check' and not self.auto_check:
            raise ValueError(
                f'step {self.step_id} claims auto_check with no check id'
            )
        return self


class AcceptanceRun(BaseModel):
    """One revisioned snapshot of a guided acceptance run."""

    model_config = ConfigDict(frozen=True)

    schema: Literal['htdt.acceptance-run'] = ACCEPTANCE_RUN_SCHEMA
    schema_version: int = ACCEPTANCE_RUN_SCHEMA_VERSION
    run_id: str
    revision: int
    gate_id: str
    status: AcceptanceRunStatus = 'in_progress'
    steps: tuple[AcceptanceStepRecord, ...]
    #: Machine/code provenance captured at run start — the replay anchor:
    #: os, python, pyside6/qt, app version, hostname, data-dir fingerprint.
    environment: dict[str, Any] = Field(default_factory=dict)
    gate_manifest_sha256: str = ''
    started_at_utc: str
    finished_at_utc: str | None = None
    recorded_at_utc: str
    prev_run_sha256: str = ''
    run_sha256: str = ''

    def semantic_sha256(self) -> str:
        return canonical_sha256(self.identity_payload())

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('run_sha256', None)
        return payload


def compute_run_status(
    steps: Iterable[AcceptanceStepRecord],
) -> AcceptanceRunStatus:
    statuses = [step.status for step in steps]
    if any(status == 'pending' for status in statuses):
        return 'in_progress'
    if any(status == 'failed' for status in statuses):
        return 'failed'
    if all(status == 'passed' for status in statuses):
        return 'passed'
    return 'partial'


def build_acceptance_run(
    gate: GateDefinition,
    *,
    run_id: str,
    environment: dict[str, Any],
) -> AcceptanceRun:
    """Fresh in-progress run for a gate; all steps start pending."""
    now = utc_now_iso()
    run = AcceptanceRun(
        run_id=run_id,
        revision=1,
        gate_id=gate.gate_id,
        status='in_progress',
        steps=tuple(
            AcceptanceStepRecord(
                step_id=step.step_id,
                kind=step.kind,
                status='pending',
                auto_check=step.auto_check,
            )
            for step in gate.steps
        ),
        environment=dict(environment),
        gate_manifest_sha256=gate.manifest_sha256(),
        started_at_utc=now,
        recorded_at_utc=now,
    )
    return run


def next_run_revision(
    run: AcceptanceRun,
    steps: Iterable[AcceptanceStepRecord],
) -> AcceptanceRun:
    """Derived next revision: recompute status + chain the identity hash."""
    step_tuple = tuple(steps)
    status = compute_run_status(step_tuple)
    now = utc_now_iso()
    finished = None if status == 'in_progress' else now
    revision = AcceptanceRun(
        **{
            **run.model_dump(),
            'revision': run.revision + 1,
            'status': status,
            'steps': step_tuple,
            'finished_at_utc': finished if finished else run.finished_at_utc,
            'recorded_at_utc': now,
            'prev_run_sha256': run.run_sha256,
            'run_sha256': '',
        }
    )
    return revision


def build_evidence_bundle(
    run: AcceptanceRun,
    gate: GateDefinition,
) -> dict[str, Any]:
    """Machine-readable run outcome: what passed, what was attested, what
    remains open — with honest auto vs human labels."""
    steps_out: list[dict[str, Any]] = []
    counts = {
        'auto_verified': 0,
        'human_confirmed': 0,
        'attested': 0,
        'failed': 0,
        'blocked': 0,
        'skipped': 0,
        'pending': 0,
    }
    defs = {step.step_id: step for step in gate.steps}
    for step in run.steps:
        definition = defs.get(step.step_id)
        if step.status == 'passed':
            if step.verdict_source == 'auto_check':
                counts['auto_verified'] += 1
            elif step.verdict_source == 'attestation':
                counts['attested'] += 1
            else:
                counts['human_confirmed'] += 1
        elif step.status in counts:
            counts[step.status] += 1
        steps_out.append(
            {
                'step_id': step.step_id,
                'kind': step.kind,
                'title_ja': definition.title_ja if definition else '',
                'status': step.status,
                'verdict_source': step.verdict_source,
                'auto_check': step.auto_check,
                'check_detail': step.check_detail,
                'evidence': [e.model_dump(mode='json') for e in step.evidence],
                'note': step.note,
                'attestation': step.attestation,
                'recorded_at_utc': step.recorded_at_utc,
            }
        )
    return {
        'schema': ACCEPTANCE_EVIDENCE_BUNDLE_SCHEMA,
        'schema_version': ACCEPTANCE_EVIDENCE_BUNDLE_SCHEMA_VERSION,
        'run_id': run.run_id,
        'gate_id': run.gate_id,
        'gate_title_ja': gate.title_ja,
        'gate_manifest_sha256': run.gate_manifest_sha256,
        'status': run.status,
        'revision': run.revision,
        'run_sha256': run.run_sha256,
        'environment': dict(run.environment),
        'started_at_utc': run.started_at_utc,
        'finished_at_utc': run.finished_at_utc,
        'summary': counts,
        'steps': steps_out,
    }


def bundle_payload(bundle: dict[str, Any]) -> bytes:
    return canonical_json(bundle).encode('utf-8')
