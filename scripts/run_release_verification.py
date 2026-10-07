#!/usr/bin/env python3
"""Release-candidate software verification gate (issue #833).

Runs the *release verification manifest*
(``scripts/release_verification_manifest.yaml``) — a versioned set of
bounded check CLASSES covering the product's correctness surface — and
writes evidence that proves exactly what was verified:

    exact git revision + dirty state
    → versioned manifest (sha256 recorded)
    → deterministic class/check selection (manifest order)
    → local/Windows execution in bounded subprocesses
    → release_verification_evidence.json + release-verification-<date>.md
    → release/package provenance binding (build-installer.ps1
      -VerificationEvidence)

This complements — never replaces — the per-issue triage runner
(``verify_open_issues.py``) and the separately typed physical gates
(owned-room campaign, R140 GPU validation, UX160 owned-Windows
acceptance), which the manifest lists under ``external_gates`` so a green
software verdict can never be mistaken for them.

Verdicts (``verdict`` in the evidence):

  passed      - run completed; every REQUIRED check passed and every check
                that executed passed. Optional checks may only be absent
                as ``skipped`` with an explicit reason.
  failed      - run completed; some executed check failed/errored/timed
                out, or a required check did not run green.
  incomplete  - the run was interrupted before finishing the scheduled
                checks. Partial evidence is still written; it can never
                be presented as PASS.

Exit status: 0 = verdict passed; 1 = verdict failed; 3 = incomplete
(interrupted); 2 = the tool itself could not complete (malformed
manifest, bad arguments, internal error). A timeout or cancelled check is
a recorded check status, never a pass — the subprocess bounds come from
the same job-object machinery as ``verify_open_issues.py``.

Profiles: ``--profile release`` runs every class (the canonical
release-candidate path); ``--profile dev`` runs only classes marked
``include_in_dev`` — a fast local subset that is NOT release evidence.
``--classes`` scopes further; any scoped run reports ``coverage: scoped``
and cannot satisfy release-candidate binding even when green.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

# ---------------------------------------------------------------------------
# Sibling-script reuse: verify_open_issues.py owns the battle-tested bounded
# subprocess machinery (Windows kill-on-close Job Objects, timeout sweeps,
# per-check logs, attempt records, git/env fingerprints). Loaded by path so
# this runner works identically whether invoked as a script, under pytest's
# importlib loader, or from any cwd.
# ---------------------------------------------------------------------------

_SIBLING_SPEC = importlib.util.spec_from_file_location(
    'verify_open_issues', Path(__file__).resolve().with_name('verify_open_issues.py')
)
assert _SIBLING_SPEC is not None and _SIBLING_SPEC.loader is not None
verify = importlib.util.module_from_spec(_SIBLING_SPEC)
# Register before exec: dataclass field introspection reads sys.modules.
sys.modules.setdefault(_SIBLING_SPEC.name, verify)
_SIBLING_SPEC.loader.exec_module(verify)

SCHEMA_ID = 'htdt-release-verification/1'
DEFAULT_MANIFEST = Path('scripts') / 'release_verification_manifest.yaml'
EVIDENCE_JSON = 'release_verification_evidence.json'

CHECK_KINDS = ('pytest', 'script')
CHECK_STATUSES = (
    'passed', 'failed', 'timeout', 'error', 'skipped', 'not_executed',
)
VERDICTS = ('passed', 'failed', 'incomplete')
PROFILES = ('release', 'dev')
MAX_TIMEOUT_SECONDS = 3600

EXIT_PASSED = 0
EXIT_FAILED = 1
EXIT_TOOL_ERROR = 2
EXIT_INCOMPLETE = 3


@dataclass
class ReleaseCheck:
    id: str
    kind: str
    description: str
    tests: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = 900
    pytest_workers: int | None = None


@dataclass
class CheckClass:
    id: str
    title: str
    description: str
    required: bool
    include_in_dev: bool
    requires_capability: str | None
    checks: list[ReleaseCheck]


@dataclass
class ExternalGate:
    id: str
    title: str
    evidence: str


@dataclass
class ReleaseManifest:
    version: int
    repo: str
    default_timeout: int
    default_workers: int
    classes: list[CheckClass]
    external_gates: list[ExternalGate]
    path: Path
    sha256: str


@dataclass
class CheckOutcome:
    check: ReleaseCheck
    status: str  # CHECK_STATUSES
    duration_s: float = 0.0
    exit_code: int | None = None
    log_path: str | None = None
    detail: str | None = None
    skip_reason: str | None = None
    attempts: list[dict] = field(default_factory=list)
    flaky: bool = False


class ManifestError(Exception):
    pass


def _fail(message: str) -> NoReturn:
    raise ManifestError(message)


def _require(cond: bool, message: str) -> None:
    if not cond:
        _fail(message)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path) -> ReleaseManifest:
    """Parse and validate the release manifest. Fail-closed, like the
    issue-verification manifest: any schema violation is fatal."""
    if not path.is_file():
        _fail(f'manifest not found: {path}')
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment guard
        _fail(f'PyYAML is required to parse {path.name}: {exc}')
    try:
        raw_text = path.read_text(encoding='utf-8')
        raw = yaml.safe_load(raw_text)
    except (OSError, yaml.YAMLError) as exc:
        _fail(f'manifest YAML parse error: {exc}')

    _require(isinstance(raw, dict), 'manifest root must be a mapping')
    _require(raw.get('version') == 1, 'manifest version must be 1')
    repo = raw.get('repo')
    _require(isinstance(repo, str) and '/' in repo,
             'manifest repo must be owner/name')

    defaults = raw.get('defaults') or {}
    _require(isinstance(defaults, dict), 'manifest defaults must be a mapping')
    default_timeout = defaults.get('timeout_seconds', 900)
    default_workers = defaults.get('pytest_workers', 4)
    _require(
        isinstance(default_timeout, int)
        and 0 < default_timeout <= MAX_TIMEOUT_SECONDS,
        f'defaults.timeout_seconds must be 1..{MAX_TIMEOUT_SECONDS}',
    )
    _require(
        isinstance(default_workers, int) and 0 <= default_workers <= 32,
        'defaults.pytest_workers must be 0..32',
    )

    external_gates: list[ExternalGate] = []
    seen_gate_ids: set[str] = set()
    for gidx, g in enumerate(raw.get('external_gates') or []):
        gwhere = f'external_gates[{gidx}]'
        _require(isinstance(g, dict), f'{gwhere} must be a mapping')
        gid = g.get('id')
        _require(
            isinstance(gid, str)
            and verify.CHECK_ID_RE.fullmatch(gid) is not None,
            f'{gwhere}.id must match {verify.CHECK_ID_RE.pattern!r}',
        )
        _require(gid not in seen_gate_ids, f'{gwhere}: duplicate id {gid!r}')
        seen_gate_ids.add(gid)
        for fname in ('title', 'evidence'):
            _require(
                isinstance(g.get(fname), str) and g[fname].strip(),
                f'{gwhere}.{fname} is required',
            )
        external_gates.append(
            ExternalGate(gid, g['title'].strip(), g['evidence'].strip())
        )

    raw_classes = raw.get('classes')
    _require(
        isinstance(raw_classes, list) and raw_classes,
        'manifest classes must be a non-empty list',
    )
    classes: list[CheckClass] = []
    seen_class_ids: set[str] = set()
    seen_check_ids: set[str] = set()
    for cidx, c in enumerate(raw_classes):
        where = f'classes[{cidx}]'
        _require(isinstance(c, dict), f'{where} must be a mapping')
        cid = c.get('id')
        _require(
            isinstance(cid, str)
            and verify.CHECK_ID_RE.fullmatch(cid) is not None,
            f'{where}.id must match {verify.CHECK_ID_RE.pattern!r}',
        )
        _require(cid not in seen_class_ids, f'{where}: duplicate class {cid!r}')
        seen_class_ids.add(cid)
        _require(
            isinstance(c.get('title'), str) and c['title'].strip(),
            f'{where}.title is required',
        )
        _require(
            isinstance(c.get('description'), str) and c['description'].strip(),
            f'{where}.description is required',
        )
        required = c.get('required', True)
        _require(isinstance(required, bool),
                 f'{where}.required must be a boolean')
        include_in_dev = c.get('include_in_dev', False)
        _require(isinstance(include_in_dev, bool),
                 f'{where}.include_in_dev must be a boolean')
        capability = c.get('requires_capability')
        _require(
            capability is None or (
                isinstance(capability, str)
                and verify.CHECK_ID_RE.fullmatch(capability) is not None
            ),
            f'{where}.requires_capability must match '
            f'{verify.CHECK_ID_RE.pattern!r}',
        )
        _require(
            capability is None or capability in CAPABILITY_PROBES,
            f'{where}.requires_capability {capability!r} is unknown '
            f'(known: {sorted(CAPABILITY_PROBES)})',
        )
        _require(
            capability is None or not required,
            f'{where}: a required class cannot depend on an optional '
            'capability — environment-unsupported must never silently '
            'downgrade the required set; split the checks instead',
        )

        raw_checks = c.get('checks')
        _require(
            isinstance(raw_checks, list) and raw_checks,
            f'{where}.checks must be a non-empty list',
        )
        checks: list[ReleaseCheck] = []
        for kidx, k in enumerate(raw_checks):
            kwhere = f'{where}.checks[{kidx}]'
            _require(isinstance(k, dict), f'{kwhere} must be a mapping')
            kid = k.get('id')
            _require(
                isinstance(kid, str)
                and verify.CHECK_ID_RE.fullmatch(kid) is not None,
                f'{kwhere}.id must match {verify.CHECK_ID_RE.pattern!r} '
                '(filename-safe: used for log paths)',
            )
            _require(
                not kid.startswith('.'),
                f'{kwhere}.id must not start with a dot',
            )
            _require(
                kid.split('.')[0].lower() not in verify._WINDOWS_RESERVED_STEMS,
                f'{kwhere}.id {kid!r} is a reserved device name on Windows',
            )
            _require(
                kid not in seen_check_ids,
                f'{kwhere}: check id {kid!r} is not unique manifest-wide '
                '(ids name log files)',
            )
            seen_check_ids.add(kid)
            kind = k.get('kind')
            _require(kind in CHECK_KINDS,
                     f'{kwhere}.kind must be one of {CHECK_KINDS}')
            kdesc = k.get('description')
            _require(isinstance(kdesc, str) and kdesc.strip(),
                     f'{kwhere}.description is required')
            timeout = k.get('timeout_seconds', default_timeout)
            _require(
                isinstance(timeout, int)
                and 0 < timeout <= MAX_TIMEOUT_SECONDS,
                f'{kwhere}.timeout_seconds must be 1..{MAX_TIMEOUT_SECONDS}',
            )
            workers = k.get('pytest_workers')
            _require(
                workers is None
                or (isinstance(workers, int) and 0 <= workers <= 32),
                f'{kwhere}.pytest_workers must be 0..32',
            )
            env = k.get('env') or {}
            _require(
                isinstance(env, dict) and all(
                    isinstance(ek, str) and ek and isinstance(ev, str)
                    for ek, ev in env.items()
                ),
                f'{kwhere}.env must be a mapping of str->str',
            )
            tests = k.get('tests')
            command = k.get('command')
            if kind == 'pytest':
                _require(
                    isinstance(tests, list) and tests and all(
                        isinstance(t, str) and t.strip() for t in tests
                    ),
                    f'{kwhere}.tests must be a non-empty list of strings',
                )
            else:
                _require(
                    isinstance(command, list) and command and all(
                        isinstance(a, str) and a.strip() for a in command
                    ),
                    f'{kwhere}.command must be a non-empty list of strings',
                )
            checks.append(
                ReleaseCheck(
                    id=kid,
                    kind=kind,
                    description=kdesc.strip(),
                    tests=[t.strip() for t in (tests or [])],
                    command=list(command or []),
                    env={ek: ev for ek, ev in env.items()},
                    timeout_seconds=timeout,
                    pytest_workers=workers,
                )
            )
        classes.append(
            CheckClass(
                id=cid,
                title=c['title'].strip(),
                description=c['description'].strip(),
                required=required,
                include_in_dev=include_in_dev,
                requires_capability=capability,
                checks=checks,
            )
        )
    return ReleaseManifest(
        version=1,
        repo=repo,
        default_timeout=default_timeout,
        default_workers=default_workers,
        classes=classes,
        external_gates=external_gates,
        path=path,
        sha256=_sha256_file(path),
    )


# ---------------------------------------------------------------------------
# Capability probes — the "where the environment supports it" axis. A probe
# only reports whether the check CAN run; an unsupported optional class is
# recorded as skipped with the probe's reason, never silently dropped.
# ---------------------------------------------------------------------------

def _probe_native_package(repo_root: Path) -> tuple[bool, str]:
    exe = repo_root / 'dist-native' / 'HTDT' / 'HTDT.exe'
    info = (
        repo_root / 'dist-native' / 'HTDT' / '_internal'
        / 'htdt_build' / 'build_info.json'
    )
    if exe.is_file() and info.is_file():
        return True, f'found {exe}'
    missing = 'HTDT.exe' if not exe.is_file() else 'build_info.json'
    return (
        False,
        f'native package not built ({missing} missing under '
        'dist-native/HTDT; run scripts/build-native.ps1)',
    )


def _find_iscc() -> str | None:
    """ISCC.exe discovery — the same candidate list build-installer.ps1 uses."""
    import shutil
    on_path = shutil.which('ISCC.exe') or shutil.which('iscc')
    if on_path:
        return on_path
    candidates: list[Path] = []
    pf86 = os.environ.get('ProgramFiles(x86)')
    pf = os.environ.get('ProgramFiles')
    local = os.environ.get('LOCALAPPDATA')
    if pf86:
        candidates.append(Path(pf86) / 'Inno Setup 6' / 'ISCC.exe')
    if pf:
        candidates += [
            Path(pf) / 'Inno Setup 6' / 'ISCC.exe',
            Path(pf) / 'Inno Setup 7' / 'ISCC.exe',
        ]
    if pf86:
        candidates.append(Path(pf86) / 'Inno Setup 7' / 'ISCC.exe')
    if local:
        candidates.append(Path(local) / 'Programs' / 'Inno Setup 7' / 'ISCC.exe')
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def _probe_inno_setup(repo_root: Path) -> tuple[bool, str]:
    found = _find_iscc()
    if found:
        return True, f'ISCC.exe: {found}'
    return False, 'Inno Setup (ISCC.exe) not installed'


CAPABILITY_PROBES = {
    'native_package': _probe_native_package,
    'inno_setup': _probe_inno_setup,
}


def _resolve_argv(check: ReleaseCheck, python: str, work_dir: Path,
                  report_dir: Path, repo_root: Path) -> list[str]:
    return [
        a.replace('{python}', python)
        .replace('{work_dir}', str(work_dir))
        .replace('{report_dir}', str(report_dir))
        .replace('{repo_root}', str(repo_root))
        for a in check.command
    ]


def _resolve_check_env(check: ReleaseCheck, base_env: dict,
                       repo_root: Path) -> dict:
    env = dict(base_env)
    for key, value in check.env.items():
        env[key] = value.replace('{repo_root}', str(repo_root))
    return env


def run_check(
    check: ReleaseCheck,
    repo_root: Path,
    python: str,
    report_dir: Path,
    default_workers: int,
    log_dir: Path,
    rerun_failed: int,
    base_env: dict,
) -> CheckOutcome:
    """Run one manifest check in a bounded subprocess (job-object sweep on
    Windows via the shared verify_open_issues machinery)."""
    if check.kind == 'pytest':
        workers = (
            check.pytest_workers
            if check.pytest_workers is not None
            else default_workers
        )
        missing = [
            t for t in check.tests
            if not (repo_root / t.split('::')[0]).is_file()
        ]
        if missing:
            return CheckOutcome(
                check=check, status='error',
                detail=f'test paths not found: {missing}',
            )
        argv_tail = [
            python, '-m', 'pytest', *check.tests,
            '-q', '-p', 'no:warnings', '--tb=short',
            '-n', str(workers),
        ]
    # Work dir created only after cheap bail-outs so an 'error' result does
    # not leave an empty rv-<id>-* dir behind.
    work_dir = report_dir / f'rv-{check.id}'
    work_dir.mkdir(parents=True, exist_ok=True)
    if check.kind == 'pytest':
        argv = argv_tail + [f'--basetemp={work_dir / "basetemp"}']
    else:
        argv = _resolve_argv(check, python, work_dir, report_dir, repo_root)

    env = _resolve_check_env(check, base_env, repo_root)

    log_path = log_dir / f'{check.id}.log'
    started = time.monotonic()
    attempts: list[dict] = []
    with log_path.open('wb') as log:
        log.write(('argv: ' + ' '.join(argv) + '\n\n').encode('utf-8'))
        max_attempts = 1 + max(0, rerun_failed)
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                log.write(
                    f'\n===== attempt {attempt}/{max_attempts} =====\n\n'
                    .encode('utf-8')
                )
                time.sleep(verify._RETRY_DELAY_S)
            record = verify._run_attempt(
                argv, repo_root, env, check.timeout_seconds, log
            )
            attempts.append(record)
            if (
                attempt >= max_attempts
                or record['status'] not in verify.RETRIABLE_STATUSES
            ):
                break
    final = attempts[-1]
    return CheckOutcome(
        check=check,
        status=final['status'],
        duration_s=round(time.monotonic() - started, 2),
        exit_code=final['exit_code'],
        log_path=f'logs/{log_path.name}',
        detail=final['detail'],
        attempts=attempts,
        flaky=len({a['status'] for a in attempts}) > 1,
    )


def select_classes(manifest: ReleaseManifest, profile: str,
                   only: list[str] | None) -> list[CheckClass]:
    """Deterministic selection: manifest order, filtered by profile/only."""
    classes = manifest.classes
    if profile == 'dev':
        classes = [c for c in classes if c.include_in_dev]
    if only is not None:
        wanted = set(only)
        unknown = wanted - {c.id for c in manifest.classes}
        if unknown:
            _fail(
                f'--classes names unknown ids: {sorted(unknown)} '
                f'(manifest: {[c.id for c in manifest.classes]})'
            )
        classes = [c for c in classes if c.id in wanted]
    return classes


def compute_verdict(
    class_results: list[tuple[CheckClass, list[CheckOutcome]]],
    run_completed: bool,
) -> tuple[str, list[str]]:
    """Overall software-verification verdict. Fail-closed rules:

    - interrupted run -> 'incomplete' (never pass, never fail-labelled);
    - any executed check that did not pass -> 'failed';
    - a required check that skipped/not_executed -> 'failed';
    - optional checks may skip only with a recorded reason.
    """
    if not run_completed:
        return 'incomplete', [
            'run interrupted before all scheduled checks completed'
        ]
    reasons: list[str] = []
    for cls, outcomes in class_results:
        for o in outcomes:
            if o.status == 'skipped':
                if cls.required:
                    reasons.append(
                        f'required check {cls.id}/{o.check.id} did not run '
                        f'({o.skip_reason or "no reason recorded"})'
                    )
                continue
            if o.status != 'passed':
                reasons.append(
                    f'{cls.id}/{o.check.id}: {o.status}'
                    + (f' — {o.detail}' if o.detail else '')
                )
    return ('failed', reasons) if reasons else ('passed', [])


def _git_metadata(repo_root: Path) -> dict:
    head_sha, dirty = verify._git_head(repo_root)
    branch = None
    try:
        proc = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            cwd=repo_root, capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            branch = proc.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {
        'commit_sha': head_sha,
        'branch': branch,
        'dirty': dirty,
    }


def _toolchain(python: str, repo_root: Path, check_env: dict) -> dict:
    pip_version = None
    try:
        proc = subprocess.run(
            [python, '-c', 'import pip; print(pip.__version__)'],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            pip_version = proc.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        pass
    lock_path = repo_root / 'backend' / 'requirements-n05-windows.lock'
    lock = {
        'path': 'backend/requirements-n05-windows.lock',
        'sha256': _sha256_file(lock_path) if lock_path.is_file() else None,
    }
    return {
        'python_executable': python,
        'python_version': platform.python_version(),
        'python_implementation': platform.python_implementation(),
        'platform': platform.platform(),
        'machine': platform.machine(),
        'pip_version': pip_version,
        # python version + OS + arch + installed-distribution sha1: the
        # same fingerprint verify_open_issues uses for its cache key.
        'environment_fingerprint': verify._env_fingerprint(python),
        'lock_file': lock,
        'managed_env': {
            name: check_env.get(name)
            for name in verify._MANAGED_ENV_VARS
        },
    }


def _md_cell(text: str) -> str:
    return text.replace('|', '\\|').replace('\n', ' ')


def render_markdown(report: dict) -> str:
    lines: list[str] = []
    lines.append('# Release verification report')
    lines.append('')
    lines.append(f"- schema: `{report['schema']}`")
    lines.append(f"- repo: `{report['repo']}`")
    lines.append(f"- generated_at_utc: {report['generated_at_utc']}")
    lines.append(f"- duration_s: {report.get('duration_s')}")
    rev = report['revision']
    lines.append(
        f"- revision: `{rev.get('commit_sha')}` "
        f"(branch `{rev.get('branch')}`, dirty={rev.get('dirty')})"
    )
    manifest = report['manifest']
    lines.append(
        f"- manifest: `{manifest['path']}` v{manifest['version']} "
        f"sha256 `{manifest['sha256']}`"
    )
    lines.append(
        f"- profile: `{report['profile']}` coverage: `{report['coverage']}`"
    )
    tc = report['toolchain']
    lines.append(
        f"- toolchain: python {tc['python_version']} "
        f"({tc['python_implementation']}, {tc['machine']}), "
        f"pip {tc.get('pip_version')}, "
        f"env fingerprint `{tc['environment_fingerprint']}`"
    )
    lock = tc.get('lock_file') or {}
    lines.append(
        f"- dependency lock: `{lock.get('path')}` "
        f"sha256 `{lock.get('sha256')}`"
    )
    lines.append('')
    verdict = report['verdict']
    lines.append(f'## Verdict: **{verdict}**')
    lines.append('')
    if report.get('verdict_reasons'):
        for r in report['verdict_reasons']:
            lines.append(f'- {r}')
        lines.append('')
    summary = report['summary']
    lines.append(
        'Summary: '
        + ' / '.join(f'{k}={v}' for k, v in summary.items() if v)
    )
    lines.append('')
    lines.append('| Class | Required | Status | Checks |')
    lines.append('|---|---|---|---|')
    for item in report['classes']:
        checks = ', '.join(
            f"{c['id']}:{c['status']}"
            + (f' ({c["skip_reason"]})' if c.get('skip_reason') else '')
            for c in item['checks']
        )
        lines.append(
            f"| {_md_cell(item['id'])} — {_md_cell(item['title'])} "
            f"| {'yes' if item['required'] else 'optional'} "
            f"| **{item['status']}** | {checks} |"
        )
    lines.append('')
    lines.append('## Detail')
    lines.append('')
    for item in report['classes']:
        lines.append(f"### {item['id']} — {item['status']}")
        for c in item['checks']:
            bits = [f"- `{c['id']}` ({c['kind']}): **{c['status']}**"]
            if c.get('duration_s') is not None and c['status'] != 'skipped':
                bits.append(f"{c['duration_s']}s")
            if c.get('exit_code') is not None:
                bits.append(f"exit={c['exit_code']}")
            if c.get('log_path'):
                bits.append(f"log `{c['log_path']}`")
            lines.append(' '.join(bits))
            if c.get('skip_reason'):
                lines.append(f'  - skip reason: {c["skip_reason"]}')
            if c.get('flaky'):
                lines.append('  - flaky: attempts ' + '→'.join(
                    a['status'] for a in c.get('attempts') or []
                ))
            if c.get('detail'):
                detail = str(c['detail']).replace('\n', ' ')
                lines.append(f'  - detail: {detail}')
        lines.append('')
    if report.get('external_gates'):
        lines.append('## External gates (NOT covered by this verdict)')
        lines.append('')
        for g in report['external_gates']:
            lines.append(
                f"- `{g['id']}` — {_md_cell(g['title'])}: "
                f"{_md_cell(g['evidence'])}"
            )
        lines.append('')
    return '\n'.join(lines)


def _class_status(outcomes: list[CheckOutcome]) -> str:
    statuses = {o.status for o in outcomes}
    if statuses == {'skipped'}:
        return 'skipped'
    if statuses == {'not_executed'}:
        return 'not_executed'
    if statuses <= {'passed', 'skipped'}:
        return 'passed'
    if 'timeout' in statuses:
        return 'timeout'
    if 'error' in statuses:
        return 'error'
    if 'failed' in statuses:
        return 'failed'
    return 'not_executed'


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument(
        '--profile', choices=PROFILES, default='release',
        help='release = every class (canonical release-candidate set); '
             'dev = include_in_dev classes only (fast, NOT release evidence)',
    )
    parser.add_argument(
        '--classes', default=None, metavar='IDS',
        help='comma-separated class ids to run instead of the full profile '
             '(scoped run — recorded as coverage=scoped)',
    )
    parser.add_argument('--manifest', type=Path, default=None)
    parser.add_argument('--repo-root', type=Path, default=None)
    parser.add_argument('--report-dir', type=Path, default=None)
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument(
        '--dry-run', action='store_true',
        help='print the resolved plan (classes, checks, capability probes) '
             'and exit without executing',
    )
    parser.add_argument(
        '--rerun-failed', type=int, default=1, metavar='N',
        help='retry a failed/errored check up to N more times (default: 1 — '
             'known xdist/Qt teardown flakes; attempts are recorded so a '
             'flaky pass stays visible). Timeouts are never retried.',
    )
    return parser


def _write_json_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Windows consoles default to cp1252; evidence files are always UTF-8,
    # but console output must never crash on JA text.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError):
            pass

    repo_root = (
        args.repo_root or Path(__file__).resolve().parent.parent
    ).resolve()
    manifest_path = (
        args.manifest or repo_root / DEFAULT_MANIFEST
    ).resolve()
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        print(f'[release-verify] manifest error: {exc}', file=sys.stderr)
        return EXIT_TOOL_ERROR

    only = None
    if args.classes:
        only = [s.strip() for s in args.classes.split(',') if s.strip()]
        if not only:
            print(
                '[release-verify] --classes parsed to an empty list',
                file=sys.stderr,
            )
            return EXIT_TOOL_ERROR
    try:
        classes = select_classes(manifest, args.profile, only)
    except ManifestError as exc:
        print(f'[release-verify] selection error: {exc}', file=sys.stderr)
        return EXIT_TOOL_ERROR

    stamp = datetime.now(timezone.utc)
    report_dir = (
        args.report_dir
        or repo_root / 'artifacts' / f'release-verification-{stamp:%Y%m%dT%H%M%SZ}'
    ).resolve()
    log_dir = report_dir / 'logs'

    # Capability probes are resolved once up front and recorded — the skip
    # reason in the evidence is exactly this output.
    capabilities = {
        cls.requires_capability: CAPABILITY_PROBES[cls.requires_capability](
            repo_root
        )
        for cls in classes if cls.requires_capability
    }

    coverage = 'full' if (args.profile == 'release' and only is None) else 'scoped'

    if args.dry_run:
        print(
            f'[release-verify] repo={repo_root} manifest={manifest_path} '
            f'(sha256 {manifest.sha256})'
        )
        print(
            f'[release-verify] profile={args.profile} '
            f'coverage={coverage} report_dir={report_dir}'
        )
        for cls in classes:
            cap = ''
            if cls.requires_capability:
                ok, detail = capabilities[cls.requires_capability]
                cap = f' [requires {cls.requires_capability}: {"ok" if ok else "UNSUPPORTED — would skip"}; {detail}]'
            req = 'required' if cls.required else 'optional'
            print(f'  class {cls.id} ({req}){cap} — {cls.title}')
            for chk in cls.checks:
                target = (
                    f'{len(chk.tests)} test file(s)' if chk.kind == 'pytest'
                    else ' '.join(chk.command)
                )
                print(f'    - [{chk.kind}] {chk.id}: {target}')
        if not classes:
            print('  (no classes selected)')
        return EXIT_PASSED

    if not classes:
        print(
            '[release-verify] selection is empty — nothing was verified; '
            'an empty run must never produce a verdict',
            file=sys.stderr,
        )
        return EXIT_TOOL_ERROR

    report_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(exist_ok=True)

    started = time.monotonic()
    revision = _git_metadata(repo_root)
    base_env = verify._check_env()
    toolchain = _toolchain(args.python, repo_root, base_env)

    class_results: list[tuple[CheckClass, list[CheckOutcome]]] = []
    run_completed = False
    interrupted_at: str | None = None
    tool_error = False

    def _mark_remaining_not_executed() -> None:
        """Everything still scheduled is explicitly not_executed — absence
        of evidence must never read as absence of a problem."""
        executed = {
            o.check.id for _, outcomes in class_results for o in outcomes
        }
        seen_classes = {id(cls) for cls, _ in class_results}
        for cls, outcomes in class_results:
            outcomes.extend(
                CheckOutcome(
                    check=chk, status='not_executed',
                    detail='run ended before this check executed',
                )
                for chk in cls.checks if chk.id not in executed
            )
        for cls in classes:
            if id(cls) not in seen_classes:
                class_results.append((
                    cls,
                    [
                        CheckOutcome(
                            check=chk, status='not_executed',
                            detail='run ended before this check executed',
                        )
                        for chk in cls.checks
                    ],
                ))

    try:
        for cls in classes:
            outcomes: list[CheckOutcome] = []
            class_results.append((cls, outcomes))
            if cls.requires_capability:
                supported, cap_detail = capabilities[cls.requires_capability]
                if not supported:
                    reason = (
                        f'capability {cls.requires_capability} unsupported: '
                        f'{cap_detail}'
                    )
                    print(f'[release-verify] SKIP {cls.id}: {reason}')
                    outcomes.extend(
                        CheckOutcome(
                            check=chk, status='skipped', skip_reason=reason,
                        )
                        for chk in cls.checks
                    )
                    continue
            for chk in cls.checks:
                print(
                    f'[release-verify] RUN {cls.id}/{chk.id} '
                    f'({chk.kind}, timeout={chk.timeout_seconds}s)'
                )
                outcome = run_check(
                    chk, repo_root, args.python, report_dir,
                    manifest.default_workers, log_dir,
                    args.rerun_failed, base_env,
                )
                outcomes.append(outcome)
                print(
                    f'[release-verify] {outcome.status.upper()} '
                    f'{cls.id}/{chk.id} ({outcome.duration_s}s)'
                )
        run_completed = True
    except KeyboardInterrupt:
        interrupted_at = datetime.now(timezone.utc).strftime(
            '%Y-%m-%dT%H:%M:%SZ'
        )
        print(
            '\n[release-verify] INTERRUPTED — recording partial evidence',
            file=sys.stderr,
        )
        _mark_remaining_not_executed()
    except Exception as exc:  # tool failure: still write what we have
        import traceback
        traceback.print_exc()
        interrupted_at = datetime.now(timezone.utc).strftime(
            '%Y-%m-%dT%H:%M:%SZ'
        )
        tool_error = True
        _mark_remaining_not_executed()
        print(f'[release-verify] internal error: {exc!r}', file=sys.stderr)

    verdict, verdict_reasons = compute_verdict(class_results, run_completed)

    summary = {s: 0 for s in CHECK_STATUSES}
    class_entries: list[dict] = []
    for cls, outcomes in class_results:
        for o in outcomes:
            summary[o.status] = summary.get(o.status, 0) + 1
        class_entries.append({
            'id': cls.id,
            'title': cls.title,
            'description': cls.description,
            'required': cls.required,
            'include_in_dev': cls.include_in_dev,
            'requires_capability': cls.requires_capability,
            'status': _class_status(outcomes),
            'checks': [
                {
                    'id': o.check.id,
                    'kind': o.check.kind,
                    'status': o.status,
                    'duration_s': o.duration_s,
                    'exit_code': o.exit_code,
                    'log_path': o.log_path,
                    'detail': o.detail,
                    'skip_reason': o.skip_reason,
                    'description': o.check.description,
                    'attempts': o.attempts,
                    'flaky': o.flaky,
                }
                for o in outcomes
            ],
        })

    report = {
        'schema': SCHEMA_ID,
        'generated_at_utc': stamp.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'duration_s': round(time.monotonic() - started, 2),
        'run_completed': run_completed,
        'interrupted_at_utc': interrupted_at,
        'repo': manifest.repo,
        'profile': args.profile,
        'coverage': coverage,
        'selection': {
            'profile': args.profile,
            'requested_classes': only,
            'executed_class_ids': [cls.id for cls, _ in class_results],
        },
        'revision': revision,
        'manifest': {
            'path': manifest_path.relative_to(repo_root).as_posix()
            if manifest_path.is_relative_to(repo_root)
            else manifest_path.as_posix(),
            'version': manifest.version,
            'sha256': manifest.sha256,
        },
        'toolchain': toolchain,
        'runner': {
            'script': 'scripts/run_release_verification.py',
            'sha256': _sha256_file(Path(__file__).resolve()),
        },
        'verdict': verdict,
        'verdict_reasons': verdict_reasons,
        'summary': summary,
        'classes': class_entries,
        'external_gates': [
            {'id': g.id, 'title': g.title, 'evidence': g.evidence}
            for g in manifest.external_gates
        ],
    }

    json_path = report_dir / EVIDENCE_JSON
    try:
        _write_json_atomic(json_path, report)
        md_path = report_dir / f'release-verification-{stamp:%Y-%m-%d}.md'
        md_path.write_text(render_markdown(report), encoding='utf-8')
    except OSError as exc:
        print(f'[release-verify] evidence write failed: {exc}',
              file=sys.stderr)
        return EXIT_TOOL_ERROR

    print(f'[release-verify] evidence: {json_path}')
    print(f'[release-verify] markdown: {md_path}')
    print(
        f'[release-verify] verdict: {verdict} '
        f'({", ".join(f"{k}={v}" for k, v in summary.items() if v)})'
    )

    if tool_error:
        return EXIT_TOOL_ERROR
    if not run_completed:
        return EXIT_INCOMPLETE
    return EXIT_PASSED if verdict == 'passed' else EXIT_FAILED


if __name__ == '__main__':
    raise SystemExit(main())
