"""Lifecycle-gate operator plans + sealed acceptance runs (#1030).

``scripts/issue_lifecycle_manifest.yaml`` classifies every open issue's
remaining work into ``remaining_gates`` of ``kind``
``physical``/``manual``/``structural``/``verification``.  The physical
and manual gates are the ones software can never clear alone — real
rooms, real devices, maintainer reviews.  This module turns each of
those gates into a *minimal-step operator plan*:

1. ``environment_snapshot`` — pin tool/OS/Python/environment identity.
2. ``evidence_ref_integrity`` — verify the issue's declared
   ``evidence_refs`` resolve repo-locally (same rules as
   ``scripts/issue_lifecycle.py`` — absolute/``..`` escapes can never be
   evidence).
3. ``prior_evidence_listing`` — enumerate earlier sealed acceptance
   runs for this gate so an operator never re-walks a satisfied gate
   blindly.
4. ``operator_action`` — the single human step: perform the gate's
   described action and attest (with optional sha-pinned evidence
   files).

Exactly one human step per gate — everything software can honestly do
is automated, everything it cannot is one explicit attestation, never
an implied pass.  The run seals into ``cad_gate_acceptance_runs`` as a
:class:`CadGateAcceptanceRun`; a run produced on another machine (the
owned-Windows gate box, a maintainer's review workstation) exports as a
``GATE_RUN_EXPORT_FORMAT`` envelope and imports with full seal
re-verification — provenance stays bound in the environment block.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_json, canonical_sha256 as _hash
from .cad_delegated_provider import _require_iso8601, _seal

_SHA256_PATTERN = r'^[0-9a-f]{64}$'

LIFECYCLE_GATE_AUTHORITY_VERSION = 'lifecycle-gate-1'

#: Envelope format token for exported acceptance runs.
GATE_RUN_EXPORT_FORMAT = 'htdt-gate-run-export-1'

DEFAULT_MANIFEST_PATH = 'scripts/issue_lifecycle_manifest.yaml'
DEFAULT_DOCUMENT_ID = 'lifecycle-gates'

#: Manifest gate kinds this runner minimizes.
OPERATOR_GATE_KINDS: tuple[str, ...] = ('physical', 'manual')

GateStepKind = Literal['auto', 'operator']
GateStepAutoAction = Literal[
    'environment_snapshot',
    'evidence_ref_integrity',
    'prior_evidence_listing',
]
GateStepStatus = Literal['pending', 'passed', 'failed', 'skipped']
GateStepVerdictSource = Literal['none', 'auto_check', 'attestation']
GateRunVerdict = Literal[
    'gate_satisfied', 'awaiting_attestation', 'steps_failed']


# ---------------------------------------------------------------------------
# Manifest reading
# ---------------------------------------------------------------------------


class LifecycleGate(BaseModel):
    """One physical/manual gate row in the lifecycle manifest."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    issue: int = Field(ge=1)
    lifecycle: str = Field(min_length=1)
    gate_index: int = Field(ge=0)
    gate_kind: str = Field(min_length=1)
    description: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()
    landed_prs: tuple[int, ...] = ()

    def gate_key(self) -> str:
        """Stable selection key for a gate inside its issue entry."""
        return f'{self.issue}:{self.gate_index}'


class LifecycleGateManifestError(RuntimeError):
    """Manifest unreadable/invalid — fail closed, never guess gates."""


def _manifest_sha256(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise LifecycleGateManifestError(
            f'lifecycle manifest unreadable: {path}: {exc}') from exc
    return hashlib.sha256(data).hexdigest()


def load_lifecycle_gates(
    manifest_path: str | Path,
    *,
    issues: tuple[int, ...] | None = None,
    kinds: tuple[str, ...] = OPERATOR_GATE_KINDS,
) -> tuple[str, tuple[LifecycleGate, ...]]:
    """Read the manifest and return ``(manifest_sha256, gates)``.

    ``gates`` covers every ``remaining_gates`` row whose kind is in
    ``kinds`` for the selected issues (``None`` = all issues).  A gate's
    ``gate_index`` is its ordinal position inside the issue's
    ``remaining_gates`` list — stable across manifest edits.
    """

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - dev env has PyYAML
        raise LifecycleGateManifestError(
            'PyYAML is required to read the lifecycle manifest') from exc
    path = Path(manifest_path)
    sha = _manifest_sha256(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding='utf-8'))
    except Exception as exc:
        raise LifecycleGateManifestError(
            f'{path}: lifecycle manifest parse failed: {exc}') from exc
    if not isinstance(raw, dict) or not isinstance(
            raw.get('entries'), list):
        raise LifecycleGateManifestError(
            f'{path}: lifecycle manifest must be a mapping with entries')
    wanted = set(kinds)
    gates: list[LifecycleGate] = []
    for item in raw['entries']:
        if not isinstance(item, dict):
            continue
        issue = item.get('issue')
        if not isinstance(issue, int):
            continue
        if issues is not None and issue not in issues:
            continue
        lifecycle = str(item.get('lifecycle') or '')
        landed = item.get('landed') or {}
        prs = tuple(
            p for p in (landed.get('prs') or []) if isinstance(p, int))
        evidence_refs = tuple(
            str(r) for r in (item.get('evidence_refs') or []))
        for index, gate in enumerate(item.get('remaining_gates') or []):
            if not isinstance(gate, dict):
                continue
            kind = str(gate.get('kind') or '')
            if kind not in wanted:
                continue
            description = str(gate.get('description') or '').strip()
            if not description:
                raise LifecycleGateManifestError(
                    f'{path}: issue {issue} gate {index} has an empty '
                    'description — the operator plan would have no '
                    'instruction')
            gates.append(LifecycleGate(
                issue=issue,
                lifecycle=lifecycle,
                gate_index=index,
                gate_kind=kind,
                description=description,
                evidence_refs=evidence_refs,
                landed_prs=prs,
            ))
    return sha, tuple(gates)


# ---------------------------------------------------------------------------
# Operator plan
# ---------------------------------------------------------------------------


class GatePlanStep(BaseModel):
    """One ordered step in a minimal operator plan."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    step_id: str = Field(min_length=1)
    kind: GateStepKind
    title_ja: str = Field(min_length=1)
    instruction_ja: str = Field(min_length=1)
    auto_action: GateStepAutoAction | None = None
    source_ref: str = ''

    @model_validator(mode='after')
    def _kind_consistent(self) -> 'GatePlanStep':
        if self.kind == 'auto' and self.auto_action is None:
            raise ValueError('auto steps require auto_action')
        if self.kind == 'operator' and self.auto_action is not None:
            raise ValueError('operator steps never carry auto_action')
        return self


class GateOperatorPlan(BaseModel):
    """Sealed minimal-step operator plan for one lifecycle gate.

    Deterministic: the same manifest bytes + gate produce the same
    ``plan_id``/``plan_sha256`` — re-planning is idempotent, never a new
    authority row.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    plan_id: str = Field(min_length=1)
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    issue: int = Field(ge=1)
    gate_index: int = Field(ge=0)
    gate_kind: str = Field(min_length=1)
    lifecycle: str = Field(min_length=1)
    gate_description: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()
    landed_prs: tuple[int, ...] = ()
    manifest_path: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    steps: tuple[GatePlanStep, ...]
    operator_step_count: int = Field(ge=0)
    runner_version: str = LIFECYCLE_GATE_AUTHORITY_VERSION

    @model_validator(mode='after')
    def _valid(self) -> 'GateOperatorPlan':
        auto = sum(1 for s in self.steps if s.kind == 'auto')
        operator = sum(1 for s in self.steps if s.kind == 'operator')
        if auto < 1:
            raise ValueError('plan needs at least one auto step')
        if operator < 1:
            raise ValueError('plan needs at least one operator step')
        if operator != self.operator_step_count:
            raise ValueError('operator_step_count disagrees with steps')
        if self.plan_sha256 != _hash(self.identity_payload()):
            raise ValueError('gate plan hash mismatch')
        if self.plan_id != f'gplan-{self.plan_sha256[:24]}':
            raise ValueError('gate plan id does not derive from its sha')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('plan_id', None)
        payload.pop('plan_sha256', None)
        return payload

    def plan_ref(self) -> AuthorityRef:
        return AuthorityRef(
            kind='gate_operator_plan',
            ref_id=self.plan_id,
            ref_sha256=self.plan_sha256)


def build_gate_operator_plan(
    gate: LifecycleGate,
    *,
    manifest_sha256: str,
    manifest_path: str,
    document_id: str,
) -> GateOperatorPlan:
    """Derive the minimal-step plan for one lifecycle gate."""

    steps = (
        GatePlanStep(
            step_id='environment',
            kind='auto',
            title_ja='実行環境を記録',
            instruction_ja='ツール・OS・Python・環境フィンガープリントを'
            '実行記録へ保存します。',
            auto_action='environment_snapshot',
            source_ref='lifecycle-gate-runner',
        ),
        GatePlanStep(
            step_id='evidence-refs',
            kind='auto',
            title_ja='宣言証跡の整合を確認',
            instruction_ja='issueのevidence_refsがリポジトリ内で'
            '解決できることを確認します。',
            auto_action='evidence_ref_integrity',
            source_ref='scripts/issue_lifecycle_manifest.yaml',
        ),
        GatePlanStep(
            step_id='prior-evidence',
            kind='auto',
            title_ja='既存の受入記録を照合',
            instruction_ja='このゲートの既存封緘受入ランを列挙します。',
            auto_action='prior_evidence_listing',
            source_ref='cad_gate_acceptance_runs',
        ),
        GatePlanStep(
            step_id='operator',
            kind='operator',
            title_ja=(
                'ゲートの物理作業を実施' if gate.gate_kind == 'physical'
                else 'ゲートのレビュー/受入を実施'),
            instruction_ja=gate.description,
            source_ref=(
                f'issue_lifecycle_manifest.yaml issue {gate.issue} '
                f'gate {gate.gate_index}'),
        ),
    )
    return _seal(
        GateOperatorPlan,
        {
            'document_id': document_id,
            'issue': gate.issue,
            'gate_index': gate.gate_index,
            'gate_kind': gate.gate_kind,
            'lifecycle': gate.lifecycle,
            'gate_description': gate.description,
            'evidence_refs': list(gate.evidence_refs),
            'landed_prs': list(gate.landed_prs),
            'manifest_path': manifest_path,
            'manifest_sha256': manifest_sha256,
            'steps': [s.model_dump(mode='python') for s in steps],
            'operator_step_count': 1,
            'runner_version': LIFECYCLE_GATE_AUTHORITY_VERSION,
        },
        'plan_id', 'plan_sha256', 'gplan',
    )


def build_gate_plans(
    manifest_path: str | Path,
    *,
    issues: tuple[int, ...] | None = None,
    kinds: tuple[str, ...] = OPERATOR_GATE_KINDS,
    document_id: str = DEFAULT_DOCUMENT_ID,
) -> tuple[GateOperatorPlan, ...]:
    """Minimal-step plans for every selected physical/manual gate.

    Plans are a pure function of the manifest bytes: re-planning a gate
    produces the identical sealed record, so planning is idempotent and
    the store never accumulates duplicate plans for one manifest state.
    """

    sha, gates = load_lifecycle_gates(
        manifest_path, issues=issues, kinds=kinds)
    return tuple(
        build_gate_operator_plan(
            gate,
            manifest_sha256=sha,
            manifest_path=str(manifest_path).replace('\\', '/'),
            document_id=document_id,
        )
        for gate in gates)


# ---------------------------------------------------------------------------
# Acceptance run
# ---------------------------------------------------------------------------


class GateEvidenceFile(BaseModel):
    """One operator-attached evidence artifact, bound by content hash."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    filename: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    size_bytes: int = Field(ge=0)
    stored_path: str | None = None


class GateRunStepResult(BaseModel):
    """Recorded outcome of one plan step inside an acceptance run."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    step_id: str = Field(min_length=1)
    kind: GateStepKind
    status: GateStepStatus = 'pending'
    verdict_source: GateStepVerdictSource = 'none'
    auto_action: GateStepAutoAction | None = None
    detail: dict[str, Any] | None = None

    @model_validator(mode='after')
    def _source_matches(self) -> 'GateRunStepResult':
        if self.status == 'pending':
            return self
        if self.verdict_source == 'none':
            raise ValueError(
                f'step {self.step_id} resolved without a verdict source')
        if self.verdict_source == 'auto_check' and not self.auto_action:
            raise ValueError(
                f'step {self.step_id} claims auto_check with no action')
        return self


class CadGateAcceptanceRun(BaseModel):
    """Sealed record of one operator-plan execution.

    ``gate_satisfied`` means every software check passed AND the operator
    attested the physical/manual action — it is the operator's attested
    claim pinned to this environment, never machine proof of the
    physical world.  ``non_claims`` states what the record does not
    assert.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    run_id: str = Field(min_length=1)
    run_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    issue: int = Field(ge=1)
    gate_index: int = Field(ge=0)
    gate_kind: str = Field(min_length=1)
    plan_ref: AuthorityRef
    environment: dict[str, Any] = Field(default_factory=dict)
    steps: tuple[GateRunStepResult, ...]
    operator_id: str | None = None
    operator_attestation: str | None = None
    evidence: tuple[GateEvidenceFile, ...] = ()
    prior_run_refs: tuple[str, ...] = ()
    verdict: GateRunVerdict
    non_claims: tuple[str, ...] = ()
    runner_version: str = LIFECYCLE_GATE_AUTHORITY_VERSION
    started_at_utc: str = Field(min_length=1)
    finished_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _valid(self) -> 'CadGateAcceptanceRun':
        _require_iso8601(self.started_at_utc, 'started_at_utc')
        _require_iso8601(self.finished_at_utc, 'finished_at_utc')
        expected = derive_run_verdict(self.steps)
        if self.verdict != expected:
            raise ValueError(
                f'verdict {self.verdict} disagrees with steps '
                f'({expected})')
        if self.verdict == 'gate_satisfied' and not (
                self.operator_attestation
                and self.operator_attestation.strip()):
            raise ValueError(
                'gate_satisfied requires the operator attestation')
        has_operator = any(s.kind == 'operator' for s in self.steps)
        if not has_operator:
            raise ValueError('run without the operator step is invalid')
        if self.run_sha256 != _hash(self.identity_payload()):
            raise ValueError('gate acceptance run hash mismatch')
        if self.run_id != f'garun-{self.run_sha256[:24]}':
            raise ValueError('run id does not derive from its sha')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('run_id', None)
        payload.pop('run_sha256', None)
        return payload

    def run_ref(self) -> AuthorityRef:
        return AuthorityRef(
            kind='gate_acceptance_run',
            ref_id=self.run_id,
            ref_sha256=self.run_sha256)


def derive_run_verdict(
    steps: tuple[GateRunStepResult, ...] | list[GateRunStepResult],
) -> GateRunVerdict:
    """Fail-closed verdict over recorded step results."""

    if any(s.status == 'failed' for s in steps):
        return 'steps_failed'
    operator = [s for s in steps if s.kind == 'operator']
    if not operator or operator[0].status != 'passed':
        return 'awaiting_attestation'
    if all(s.status == 'passed' for s in steps):
        return 'gate_satisfied'
    return 'awaiting_attestation'


def resolve_evidence_ref(
    ref: str, repo_root: Path,
) -> tuple[bool, str]:
    """Repo-local evidence-ref check (same rules as issue_lifecycle).

    Returns ``(True, path)`` when the ref resolves to an existing file
    under the repo root; otherwise ``(False, reason_code)``.  Absolute
    paths and ``..`` escapes are never evidence.
    """

    pure = ref.replace('\\', '/').strip()
    parts = [p for p in pure.split('/') if p not in ('', '.')]
    if (
        not parts
        or pure.startswith(('/', '~'))
        or (len(pure) >= 2 and pure[1] == ':')
        or pure.startswith('//')
        or '..' in parts
    ):
        return False, 'evidence_ref_not_repo_local'
    path = repo_root.joinpath(*parts)
    try:
        path.relative_to(repo_root)
    except ValueError:
        return False, 'evidence_ref_not_repo_local'
    if not path.is_file():
        return False, 'evidence_ref_unresolvable'
    return True, str(path)


def run_gate_plan(
    plan: GateOperatorPlan,
    *,
    environment: dict[str, Any],
    repo_root: Path | None = None,
    operator_attestation: str | None = None,
    operator_id: str | None = None,
    evidence: tuple[GateEvidenceFile, ...] | list[GateEvidenceFile] = (),
    prior_run_ids: tuple[str, ...] | list[str] = (),
    started_at_utc: str,
    finished_at_utc: str,
) -> CadGateAcceptanceRun:
    """Execute one operator plan and seal the acceptance run.

    Auto steps really execute here: environment capture binds the
    supplied ``environment`` block, evidence-ref integrity resolves
    every declared ref under ``repo_root`` (required when the plan
    declares refs), and the prior-evidence listing records the supplied
    ``prior_run_ids``.  The operator step resolves only from a supplied
    ``operator_attestation`` — it can never be auto-passed.
    """

    results: list[GateRunStepResult] = []
    for step in plan.steps:
        if step.auto_action == 'environment_snapshot':
            results.append(GateRunStepResult(
                step_id=step.step_id,
                kind=step.kind,
                status='passed',
                verdict_source='auto_check',
                auto_action=step.auto_action,
                detail={'environment': dict(environment)},
            ))
        elif step.auto_action == 'evidence_ref_integrity':
            detail: dict[str, Any] = {'refs': {}, 'resolved': 0,
                                      'unresolved': 0}
            failed = False
            if repo_root is None and plan.evidence_refs:
                failed = True
                detail['error'] = (
                    'repo_root unavailable — evidence refs cannot be '
                    'resolved on this lane')
            else:
                for ref in plan.evidence_refs:
                    ok, info = resolve_evidence_ref(ref, repo_root)
                    detail['refs'][ref] = 'resolved' if ok else info
                    detail['resolved' if ok else 'unresolved'] += 1
                    if not ok:
                        failed = True
            results.append(GateRunStepResult(
                step_id=step.step_id,
                kind=step.kind,
                status='failed' if failed else 'passed',
                verdict_source='auto_check',
                auto_action=step.auto_action,
                detail=detail,
            ))
        elif step.auto_action == 'prior_evidence_listing':
            results.append(GateRunStepResult(
                step_id=step.step_id,
                kind=step.kind,
                status='passed',
                verdict_source='auto_check',
                auto_action=step.auto_action,
                detail={'prior_run_ids': list(prior_run_ids)},
            ))
        elif step.kind == 'operator':
            attested = bool(
                operator_attestation and operator_attestation.strip())
            results.append(GateRunStepResult(
                step_id=step.step_id,
                kind=step.kind,
                status='passed' if attested else 'pending',
                verdict_source='attestation' if attested else 'none',
                detail={
                    'instruction_ja': step.instruction_ja,
                    'attestation_supplied': attested,
                },
            ))
        else:  # pragma: no cover - every step kind is handled
            results.append(GateRunStepResult(
                step_id=step.step_id,
                kind=step.kind,
                status='skipped',
                verdict_source='none',
            ))
    non_claims = [
        'operatorステップは人間の証明であり、ソフトウェアによる'
        '物理実施の機械的証明ではありません。',
        'この記録はゲート実行の証跡であり、issueのドメイン受理判定'
        'そのものではありません。',
    ]
    if environment.get('simulated') or environment.get(
            'capture_mode') == 'offscreen_fixture':
        non_claims.append(
            'offscreen/シミュレート環境での記録であり、実機物理検証を'
            '主張しません。')
    return _seal(
        CadGateAcceptanceRun,
        {
            'document_id': plan.document_id,
            'issue': plan.issue,
            'gate_index': plan.gate_index,
            'gate_kind': plan.gate_kind,
            'plan_ref': plan.plan_ref().model_dump(mode='python'),
            'environment': dict(environment),
            'steps': [s.model_dump(mode='python') for s in results],
            'operator_id': operator_id,
            'operator_attestation': operator_attestation,
            'evidence': [e.model_dump(mode='python') for e in evidence],
            'prior_run_refs': list(prior_run_ids),
            'verdict': derive_run_verdict(results),
            'non_claims': non_claims,
            'runner_version': LIFECYCLE_GATE_AUTHORITY_VERSION,
            'started_at_utc': started_at_utc,
            'finished_at_utc': finished_at_utc,
        },
        'run_id', 'run_sha256', 'garun',
    )


# ---------------------------------------------------------------------------
# Export / import envelope
# ---------------------------------------------------------------------------


def export_run_envelope(
    run: CadGateAcceptanceRun,
    *,
    exported_at_utc: str,
    exporter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap a sealed run for transport to another machine/data-dir."""

    return {
        'format': GATE_RUN_EXPORT_FORMAT,
        'record': run.model_dump(mode='json'),
        'exported_at_utc': exported_at_utc,
        'exporter': dict(exporter or {}),
    }


def parse_run_envelope(raw: Any) -> CadGateAcceptanceRun:
    """Fail-closed import: validate the envelope AND the sealed record.

    ``model_validate`` re-derives ``run_sha256``/``run_id`` inside the
    record validator, so a tampered or schema-drifted file can never
    enter the store.
    """

    if not isinstance(raw, dict):
        raise ValueError('gate run export must be a JSON object')
    if raw.get('format') != GATE_RUN_EXPORT_FORMAT:
        raise ValueError(
            f"gate run export format must be {GATE_RUN_EXPORT_FORMAT!r}")
    record = raw.get('record')
    if not isinstance(record, dict):
        raise ValueError('gate run export carries no record')
    return CadGateAcceptanceRun.model_validate(record)


__all__ = [
    'DEFAULT_DOCUMENT_ID',
    'DEFAULT_MANIFEST_PATH',
    'GATE_RUN_EXPORT_FORMAT',
    'LIFECYCLE_GATE_AUTHORITY_VERSION',
    'OPERATOR_GATE_KINDS',
    'CadGateAcceptanceRun',
    'GateEvidenceFile',
    'GateOperatorPlan',
    'GatePlanStep',
    'GateRunStepResult',
    'LifecycleGate',
    'LifecycleGateManifestError',
    'build_gate_operator_plan',
    'build_gate_plans',
    'derive_run_verdict',
    'export_run_envelope',
    'load_lifecycle_gates',
    'parse_run_envelope',
    'resolve_evidence_ref',
    'run_gate_plan',
]
