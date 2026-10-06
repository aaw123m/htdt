"""Headless core for the guided verification wizard (REV59-GUIDEDWIZ).

The wizard consumes the REV59-CLOSEAUX bridge: every check in
``scripts/issue_verification_manifest.yaml`` seals into a
``ManifestGate``, every run/evidence commit lands as an append-only
``GateRunResult``, and ``evaluate_issue_verdict`` drives the
zero-knowledge closure display the user sees.

This module is the Qt-free half so the whole data path — manifest
parsing, argv construction, the bounded subprocess run, gate/result
persistence and digest-bound evidence commits — is testable offscreen.
The runner mirrors ``scripts/verify_open_issues.py`` conventions
(pytest argv, ``{python}``/``{work_dir}``/``{report_dir}`` substitution,
managed env vars) rather than importing the script, which is not a
package member.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from .cad_authority_resolver import AuthorityRef
from .cad_manifest_gate_repository import CadManifestGateRepository
from .cad_manifest_verification import (
    GateRunResult,
    ManifestGate,
    evaluate_issue_verdict,
    load_manifest_gates,
)
from .cad_repository import SceneRepository
from .cad_schema import ensure_native_schema
from .clock import utc_now_iso
from .managed_assets import MANAGED_ASSETS_DIRNAME, ManagedAssetStore

#: Verification state is app-scope: gates and results live under this
#: partition id rather than any one scene document.
VERIFICATION_DOCUMENT_ID = 'application'

#: Manifest location inside a repository checkout.
MANIFEST_RELATIVE_PATH = Path('scripts') / 'issue_verification_manifest.yaml'

#: Runner bounds, matching verify_open_issues.py's manifest validation.
MAX_TIMEOUT_SECONDS = 7200
DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_PYTEST_WORKERS = 4

_CHECK_KINDS = ('pytest', 'script', 'manual')


@dataclass(frozen=True, slots=True)
class WizardCheck:
    """One manifest check plus the runner metadata a sealed gate drops."""

    check_id: str
    kind: str  # 'pytest' | 'script' | 'manual'
    description: str
    issue_ref: str = ''  # 'issue-<n>' — owning manifest issue
    tests: tuple[str, ...] = ()
    command: tuple[str, ...] = ()
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    pytest_workers: int | None = None


@dataclass(frozen=True, slots=True)
class WizardIssue:
    """One manifest issue entry — the wizard's picker row."""

    issue_number: int | str
    issue_ref: str  # 'issue-<key>' — the gate store's partition key
    title: str | None
    notes: str | None
    checks: tuple[WizardCheck, ...]


def load_wizard_issues(manifest_path: str | Path) -> tuple[WizardIssue, ...]:
    """Parse the manifest for the wizard's runner view.

    Fail-closed like ``load_manifest_gates``: unknown kinds, missing ids
    and shapeless entries raise — a manifest the wizard half-understands
    is worse than no manifest at all.
    """
    path = Path(manifest_path)
    doc = yaml.safe_load(path.read_text(encoding='utf-8'))
    if not isinstance(doc, dict) or not isinstance(doc.get('issues'), list):
        raise ValueError('manifest has no issues list')
    defaults = doc.get('defaults') or {}
    default_timeout = defaults.get('timeout_seconds', DEFAULT_TIMEOUT_SECONDS)
    # The runner (verify_open_issues) rejects duplicate issue numbers,
    # but the bridge keys gates purely on ``issue_ref`` — so repeated
    # entries for one issue are merged here into a single picker row,
    # matching how evaluate_issue_verdict groups the sealed gates.
    merged: dict[str, WizardIssue] = {}
    for entry in doc['issues']:
        if not isinstance(entry, dict):
            raise ValueError('manifest issue entry is not a mapping')
        number = entry.get('issue')
        if number is None or not isinstance(number, (int, str)):
            raise ValueError('manifest issue entry lacks a number')
        issue_ref = f'issue-{number}'
        checks: list[WizardCheck] = []
        for check in entry.get('checks') or ():
            if not isinstance(check, dict):
                raise ValueError(f'issue {number}: check is not a mapping')
            kind = check.get('kind')
            if kind not in _CHECK_KINDS:
                raise ValueError(
                    f'issue {number}: unknown check kind {kind!r}'
                )
            check_id = check.get('id')
            if not isinstance(check_id, str) or not check_id.strip():
                raise ValueError(f'issue {number}: check lacks an id')
            timeout = check.get('timeout_seconds', default_timeout)
            if (
                not isinstance(timeout, int)
                or isinstance(timeout, bool)
                or not 0 < timeout <= MAX_TIMEOUT_SECONDS
            ):
                raise ValueError(
                    f'issue {number}/{check_id}: timeout_seconds must be '
                    f'1..{MAX_TIMEOUT_SECONDS}'
                )
            workers = check.get('pytest_workers')
            if workers is not None and (
                not isinstance(workers, int)
                or isinstance(workers, bool)
                or not 0 <= workers <= 32
            ):
                raise ValueError(
                    f'issue {number}/{check_id}: pytest_workers must be 0..32'
                )
            checks.append(
                WizardCheck(
                    check_id=check_id,
                    kind=kind,
                    issue_ref=issue_ref,
                    description=(check.get('description') or '').strip(),
                    tests=tuple(check.get('tests') or ()),
                    command=tuple(check.get('command') or ()),
                    timeout_seconds=timeout,
                    pytest_workers=workers,
                )
            )
        existing = merged.get(issue_ref)
        if existing is None:
            merged[issue_ref] = WizardIssue(
                issue_number=number,
                issue_ref=issue_ref,
                title=entry.get('title'),
                notes=entry.get('notes'),
                checks=tuple(checks),
            )
        else:
            merged[issue_ref] = WizardIssue(
                issue_number=existing.issue_number,
                issue_ref=issue_ref,
                title=existing.title or entry.get('title'),
                notes=existing.notes or entry.get('notes'),
                checks=existing.checks + tuple(checks),
            )
    return tuple(merged.values())


def resolve_check_argv(
    check: WizardCheck,
    *,
    python: str,
    work_dir: Path,
    report_dir: Path,
    repo_root: Path,
    default_workers: int = DEFAULT_PYTEST_WORKERS,
) -> list[str]:
    """Build the subprocess argv for one automated check.

    Same conventions as ``verify_open_issues.py``: pytest checks get the
    quiet short-tb xdist form plus an isolated ``--basetemp``; script
    checks substitute ``{python}``/``{work_dir}``/``{report_dir}``.
    Missing test files fail closed before any work dir is created.
    """
    work_dir = Path(work_dir)
    if check.kind == 'pytest':
        missing = [
            t
            for t in check.tests
            if not (Path(repo_root) / t.split('::')[0]).is_file()
        ]
        if missing:
            raise FileNotFoundError(f'test paths not found: {missing}')
        workers = (
            check.pytest_workers
            if check.pytest_workers is not None
            else default_workers
        )
        return [
            python,
            '-m',
            'pytest',
            *check.tests,
            '-q',
            '-p',
            'no:warnings',
            '--tb=short',
            '-n',
            str(workers),
            f'--basetemp={work_dir / "basetemp"}',
        ]
    if check.kind == 'script':
        if not check.command:
            raise ValueError(f'check {check.check_id} has no command argv')
        return [
            arg.replace('{python}', python)
            .replace('{work_dir}', str(work_dir))
            .replace('{report_dir}', str(report_dir))
            for arg in check.command
        ]
    raise ValueError('manual checks have no automated argv')


def check_env() -> dict:
    """Environment for check subprocesses — offscreen Qt, utf-8 IO."""
    env = dict(os.environ)
    env.setdefault('QT_QPA_PLATFORM', 'offscreen')
    env.setdefault('PYTHONIOENCODING', 'utf-8')
    return env


@dataclass(frozen=True, slots=True)
class CheckRunOutcome:
    """One bounded check run; ``status`` maps 1:1 to a gate outcome."""

    status: str  # 'passed' | 'failed' | 'timeout' | 'error'
    detail: str
    duration_s: float
    log_path: Path | None


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Best-effort descendant sweep after a timeout (bounded subset of
    verify_open_issues.py's job-object machinery — adequate for one
    interactive check at a time)."""
    if os.name == 'nt':
        try:
            subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def run_check(
    check: WizardCheck,
    *,
    repo_root: Path,
    report_dir: Path,
    python: str | None = None,
    env: dict | None = None,
) -> CheckRunOutcome:
    """Run one automated check in a bounded subprocess.

    Never raises: spawn failures, timeouts and missing targets come back
    as ``error``/``timeout`` outcomes so the GUI commits exactly what
    happened — a check that cannot run is recorded, not faked.
    """
    python = python or sys.executable
    report_dir = Path(report_dir)
    repo_root = Path(repo_root)
    work_dir = Path(
        tempfile.mkdtemp(prefix=f'vw-{check.check_id}-', dir=report_dir)
    )
    log_dir = report_dir / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f'{check.check_id}.log'
    try:
        argv = resolve_check_argv(
            check,
            python=python,
            work_dir=work_dir,
            report_dir=report_dir,
            repo_root=repo_root,
        )
    except (FileNotFoundError, ValueError) as exc:
        return CheckRunOutcome(
            status='error',
            detail=str(exc),
            duration_s=0.0,
            log_path=None,
        )
    started = time.monotonic()
    with log_path.open('wb') as log:
        log.write(('argv: ' + ' '.join(argv) + '\n\n').encode('utf-8'))
        try:
            proc = subprocess.Popen(
                argv,
                cwd=repo_root,
                env=env if env is not None else check_env(),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=(os.name != 'nt'),
            )
        except OSError as exc:
            return CheckRunOutcome(
                status='error',
                detail=str(exc),
                duration_s=round(time.monotonic() - started, 2),
                log_path=log_path,
            )
        timed_out = False
        try:
            rc = proc.wait(timeout=check.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            rc = None
        if timed_out:
            try:
                _kill_process_tree(proc)
            except Exception:
                pass
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.kill()
                proc.wait()
            return CheckRunOutcome(
                status='timeout',
                detail=f'exceeded timeout_seconds={check.timeout_seconds}',
                duration_s=round(time.monotonic() - started, 2),
                log_path=log_path,
            )
    return CheckRunOutcome(
        status='passed' if rc == 0 else 'failed',
        detail='' if rc == 0 else f'exit code {rc}',
        duration_s=round(time.monotonic() - started, 2),
        log_path=log_path,
    )


class ManifestGateStore:
    """App-scope persistence: sealed gates, run results, evidence assets.

    Wraps ``CadManifestGateRepository`` on the canonical authority
    database plus the managed-assets store, so a verifier holding the
    data directory can replay every gate, result and bound evidence
    byte the wizard commits.
    """

    def __init__(
        self,
        db_path: str | Path,
        document_id: str = VERIFICATION_DOCUMENT_ID,
    ) -> None:
        self.db_path = Path(db_path)
        self.document_id = document_id
        # sqlite cannot create the db file in a missing directory.
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        ensure_native_schema(self.db_path)
        self.repository = CadManifestGateRepository(
            SceneRepository(self.db_path)
        )
        self.assets = ManagedAssetStore(
            self.db_path.parent / MANAGED_ASSETS_DIRNAME
        )

    def _gate_ref(self, gate: ManifestGate) -> AuthorityRef:
        return AuthorityRef(
            kind='manifest_gate',
            ref_id=gate.gate_id,
            ref_sha256=gate.gate_sha256,
        )

    def load_gates(
        self, manifest_path: str | Path
    ) -> tuple[ManifestGate, ...]:
        """Seal every manifest check and persist gates idempotently."""
        gates = load_manifest_gates(self.document_id, manifest_path)
        for gate in gates:
            self.repository.save_manifest_gate(gate)
        return gates

    def latest_results(self) -> dict[str, GateRunResult]:
        """Newest committed result per gate id."""
        latest: dict[str, GateRunResult] = {}
        for result in self.repository.list_gate_run_results(
            document_id=self.document_id
        ):
            latest[result.gate_ref.ref_id] = result
        return latest

    def issue_verdict(
        self,
        gates: tuple[ManifestGate, ...],
        latest: dict[str, GateRunResult] | None = None,
    ) -> tuple[str, str]:
        return evaluate_issue_verdict(
            gates, latest if latest is not None else self.latest_results()
        )

    def _install_asset(self, payload: bytes) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        self.assets.ensure_installed(digest, payload)
        return digest

    def record_check_outcome(
        self,
        gate: ManifestGate,
        outcome: CheckRunOutcome,
        *,
        detail: str = '',
    ) -> GateRunResult:
        """Commit one automated-check outcome as a sealed result.

        The check log is installed as a managed asset and its digest is
        recorded in ``detail`` so the row binds the exact bytes the run
        produced.
        """
        bits = [b for b in (detail, outcome.detail) if b]
        if outcome.log_path is not None and outcome.log_path.is_file():
            log_digest = self._install_asset(outcome.log_path.read_bytes())
            bits.append(f'log sha256={log_digest}')
        bits.append(f'duration {outcome.duration_s}s')
        result = GateRunResult.create(
            {
                'document_id': self.document_id,
                'gate_ref': self._gate_ref(gate),
                'outcome': outcome.status,
                'finished_at_utc': utc_now_iso(),
                'detail': ' | '.join(bits)[:500],
            }
        )
        self.repository.save_gate_run_result(result)
        return result

    def commit_evidence(
        self,
        gate: ManifestGate,
        *,
        files: tuple[tuple[str, bytes], ...] = (),
        note: str = '',
    ) -> GateRunResult:
        """Commit manual-check evidence → ``evidence_committed`` result.

        Every attached payload installs into the managed store; a bundle
        JSON (note + per-file digests + timestamp) is itself installed,
        and its digest becomes the bound ``AuthorityRef`` — the sealed
        result is replayable down to the evidence bytes. An attestation
        with no files is honest evidence too: the bundle IS the
        attestation record.
        """
        entries = [
            {
                'filename': filename,
                'sha256': self._install_asset(payload),
                'size_bytes': len(payload),
            }
            for filename, payload in files
        ]
        bundle = json.dumps(
            {
                'kind': 'verification-evidence-bundle',
                'gate_id': gate.gate_id,
                'check_id': gate.check_id,
                'issue_ref': gate.issue_ref,
                'note': note,
                'files': entries,
                'recorded_at_utc': utc_now_iso(),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode('utf-8')
        bundle_digest = self._install_asset(bundle)
        result = GateRunResult.create(
            {
                'document_id': self.document_id,
                'gate_ref': self._gate_ref(gate),
                'outcome': 'evidence_committed',
                'evidence_ref': AuthorityRef(
                    kind='evidence',
                    ref_id=f'{MANAGED_ASSETS_DIRNAME}/{bundle_digest}',
                    ref_sha256=bundle_digest,
                ),
                'finished_at_utc': utc_now_iso(),
                'detail': (note or f'{len(entries)} file(s)')[:500],
            }
        )
        self.repository.save_gate_run_result(result)
        return result
