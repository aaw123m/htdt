#!/usr/bin/env python3
"""Verify progress on open GitHub issues against the verification manifest.

Reads ``scripts/issue_verification_manifest.yaml`` (or ``--manifest``), fetches
the repository's open issues (or an offline ``--issues`` list), runs every
mapped automated check in a bounded subprocess, and writes a machine-readable
``issue_verification_report.json`` plus a Markdown report into ``--report-dir``.

The tool is deliberately read-only against GitHub unless ``--post-summary``
is passed: it never edits issues. The optional per-issue status comment is
*upserted* — one marked comment per issue, edited in place each run, so the
latest verdict is always visible without digging through artifacts and
watchers are never spammed.

Verdicts (per issue):
  verified            - all automated checks passed and no manual evidence remains
  partially_verified  - all automated checks passed, manual evidence remains
  manual_required     - no automated checks are declared for the issue
  failing             - at least one automated check failed / errored / timed out
  unmapped            - issue is open but absent from the manifest
  not_open            - manifest entry refers to an issue that is not open,
                        or --issues asked for an issue that is not open

Flakiness is surfaced, not hidden: ``--rerun-failed`` retries failed/errored
checks and the report records every attempt plus a ``flaky`` flag, so a
fail-then-pass suite is visible as flaky instead of silently trusted.

``--use-cache`` optionally reuses verdicts keyed by (check config, HEAD sha,
python environment); it is off by default and refuses a dirty worktree —
a cache that lies is worse than no cache.

Exit status: 0 on a successful run (verdicts may still include failures —
they are data, not runner errors); 2 when the tool itself cannot complete
(malformed manifest, API failure without --offline, bad arguments, failed
comment posts); 3 when --fail-on-verdict matches a produced verdict.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

CHECK_KINDS = ('pytest', 'script', 'manual')
VERDICT_STRATEGIES = ('auto_and_manual',)
VERDICTS = (
    'verified',
    'partially_verified',
    'manual_required',
    'failing',
    'unmapped',
    'not_open',
)
MAX_TIMEOUT_SECONDS = 3600
DEFAULT_REPO = 'ka0923s-a11y/HTDT'
DEFAULT_MANIFEST = Path('scripts') / 'issue_verification_manifest.yaml'
DEFAULT_CACHE_PATH = Path('artifacts') / 'issue-verification' / 'cache.json'
TOKEN_ENV_VARS = ('GITHUB_TOKEN', 'GITHUB_KA0923S_PAT', 'GH_TOKEN')
# Check ids land in filesystem paths (logs/<id>.log, iv-<id>-* work dirs), so
# the manifest schema restricts them to a filename-safe charset.
CHECK_ID_RE = re.compile(r'[A-Za-z0-9._-]+')
# A charset-safe id can still be unusable as a filename on Windows: the DOS
# device names are reserved in every directory, and names made of only dots
# collapse to nothing. The CI lane runs windows-latest, so these are schema
# errors, not portability warnings.
_WINDOWS_RESERVED_STEMS = {
    'con', 'prn', 'aux', 'nul',
    *(f'com{i}' for i in range(1, 10)),
    *(f'lpt{i}' for i in range(1, 10)),
}
# Hidden marker inside the single status comment the runner upserts on each
# issue; keeps --post-summary from spamming a new comment per run.
COMMENT_MARKER = '<!-- verify-open-issues -->'
# The first content line of every upserted comment. The marker alone is not a
# safe ownership check — a human could quote it — so an upserted comment must
# start with the marker AND carry this signature line before the runner will
# edit or delete it. (GET /user cannot identify a GITHUB_TOKEN actor, so the
# body signature is the ownership proof available in both token worlds.)
_COMMENT_SIGNATURE = '自動検証'
CACHE_SCHEMA_VERSION = 1
GITHUB_API_VERSION = '2022-11-28'


class ManifestError(ValueError):
    """Raised when the verification manifest is malformed (fail-closed)."""


@dataclass
class Check:
    id: str
    kind: str
    description: str
    tests: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    timeout_seconds: int = 900
    pytest_workers: int | None = None


@dataclass
class IssueEntry:
    issue: int
    title: str | None
    notes: str | None
    checks: list[Check]


@dataclass
class Manifest:
    repo: str
    default_timeout: int
    default_workers: int
    issues: dict[int, IssueEntry]


@dataclass
class CheckResult:
    check: Check
    status: str  # passed | failed | timeout | error | manual | cached status of the final attempt
    duration_s: float = 0.0
    exit_code: int | None = None
    log_path: str | None = None
    detail: str | None = None
    # Every attempt's outcome, oldest first — a check that failed then passed
    # keeps the full trail so a flaky suite is visible, not silently trusted.
    attempts: list[dict] = field(default_factory=list)
    flaky: bool = False
    cached: bool = False


def _fail(message: str) -> NoReturn:
    raise ManifestError(message)


def _require(cond: bool, message: str) -> None:
    if not cond:
        _fail(message)


def load_manifest(path: Path) -> Manifest:
    """Parse and validate the manifest. Fail-closed: any schema violation is fatal."""
    if not path.is_file():
        _fail(f'manifest not found: {path}')
    if path.suffix.lower() in ('.yaml', '.yml'):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - environment guard
            _fail(f'PyYAML is required to parse {path.name}: {exc}')
        try:
            raw = yaml.safe_load(path.read_text(encoding='utf-8'))
        except yaml.YAMLError as exc:
            _fail(f'manifest YAML parse error: {exc}')
    elif path.suffix.lower() == '.json':
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except json.JSONDecodeError as exc:
            _fail(f'manifest JSON parse error: {exc}')
    else:
        _fail(f'unsupported manifest extension: {path.suffix} (use .yaml/.yml/.json)')

    _require(isinstance(raw, dict), 'manifest root must be a mapping')
    _require(raw.get('version') == 1, 'manifest version must be 1')
    defaults = raw.get('defaults') or {}
    _require(isinstance(defaults, dict), 'manifest defaults must be a mapping')
    default_timeout = defaults.get('timeout_seconds', 900)
    default_workers = defaults.get('pytest_workers', 4)
    _require(
        isinstance(default_timeout, int) and 0 < default_timeout <= MAX_TIMEOUT_SECONDS,
        f'defaults.timeout_seconds must be 1..{MAX_TIMEOUT_SECONDS}',
    )
    _require(
        isinstance(default_workers, int) and 0 <= default_workers <= 32,
        'defaults.pytest_workers must be 0..32',
    )
    repo = raw.get('repo')
    _require(isinstance(repo, str) and '/' in repo, 'manifest repo must be owner/name')

    raw_issues = raw.get('issues')
    _require(isinstance(raw_issues, list), 'manifest issues must be a list')
    issues: dict[int, IssueEntry] = {}
    # Every check writes logs/<id>.log into the run's single log dir, so ids
    # must be unique across the whole manifest, not just within an issue.
    seen_check_ids: dict[str, int] = {}
    for idx, entry in enumerate(raw_issues):
        where = f'issues[{idx}]'
        _require(isinstance(entry, dict), f'{where} must be a mapping')
        number = entry.get('issue')
        _require(
            isinstance(number, int) and not isinstance(number, bool) and number > 0,
            f'{where}.issue must be a positive integer',
        )
        _require(number not in issues, f'{where}: duplicate issue #{number}')
        rules = entry.get('verdict_rules') or {}
        _require(isinstance(rules, dict), f'{where}.verdict_rules must be a mapping')
        strategy = rules.get('strategy', 'auto_and_manual')
        _require(
            strategy in VERDICT_STRATEGIES,
            f'{where}.verdict_rules.strategy must be one of {VERDICT_STRATEGIES}',
        )
        raw_checks = entry.get('checks')
        _require(isinstance(raw_checks, list), f'{where}.checks must be a list')
        checks: list[Check] = []
        seen_ids: set[str] = set()
        for cidx, c in enumerate(raw_checks):
            cwhere = f'{where}.checks[{cidx}]'
            _require(isinstance(c, dict), f'{cwhere} must be a mapping')
            cid = c.get('id')
            _require(
                isinstance(cid, str) and CHECK_ID_RE.fullmatch(cid) is not None,
                f'{cwhere}.id must match {CHECK_ID_RE.pattern!r} '
                '(filename-safe: used for log paths)',
            )
            _require(cid not in seen_ids, f'{cwhere}: duplicate check id {cid!r}')
            seen_ids.add(cid)
            other = seen_check_ids.get(cid)
            _require(
                other is None,
                f'{cwhere}: check id {cid!r} is also used by issue #{other} '
                '— ids name log files and must be unique manifest-wide',
            )
            seen_check_ids[cid] = number
            _require(
                not cid.startswith('.'),
                f'{cwhere}.id must not start with a dot (hidden/unwritable '
                'name on some filesystems)',
            )
            _require(
                cid.split('.')[0].lower() not in _WINDOWS_RESERVED_STEMS,
                f'{cwhere}.id {cid!r} is a reserved device name on Windows '
                '(log paths like con.log/nul.log cannot be created)',
            )
            kind = c.get('kind')
            _require(kind in CHECK_KINDS, f'{cwhere}.kind must be one of {CHECK_KINDS}')
            desc = c.get('description')
            _require(
                isinstance(desc, str) and desc.strip(),
                f'{cwhere}.description is required',
            )
            timeout = c.get('timeout_seconds', default_timeout)
            _require(
                isinstance(timeout, int) and 0 < timeout <= MAX_TIMEOUT_SECONDS,
                f'{cwhere}.timeout_seconds must be 1..{MAX_TIMEOUT_SECONDS}',
            )
            workers = c.get('pytest_workers')
            if workers is not None:
                _require(
                    isinstance(workers, int) and 0 <= workers <= 32,
                    f'{cwhere}.pytest_workers must be 0..32',
                )
            tests = c.get('tests')
            command = c.get('command')
            if kind == 'pytest':
                _require(
                    isinstance(tests, list) and tests and all(
                        isinstance(t, str) and t.strip() for t in tests
                    ),
                    f'{cwhere}.tests must be a non-empty list of strings',
                )
            elif kind == 'script':
                _require(
                    isinstance(command, list) and command and all(
                        isinstance(a, str) and a.strip() for a in command
                    ),
                    f'{cwhere}.command must be a non-empty list of strings',
                )
            checks.append(
                Check(
                    id=cid,
                    kind=kind,
                    description=desc.strip(),
                    tests=[t.strip() for t in (tests or [])],
                    command=list(command or []),
                    timeout_seconds=timeout,
                    pytest_workers=workers,
                )
            )
        _require(checks, f'{where}.checks must not be empty (add a manual check)')
        for field_name in ('title', 'notes'):
            value = entry.get(field_name)
            _require(
                value is None or isinstance(value, str),
                f'{where}.{field_name} must be a string',
            )
        issues[number] = IssueEntry(
            issue=number,
            title=entry.get('title'),
            notes=entry.get('notes'),
            checks=checks,
        )
    return Manifest(
        repo=repo,
        default_timeout=default_timeout,
        default_workers=default_workers,
        issues=issues,
    )


def _api_json(
    method: str,
    url: str,
    token: str | None,
    payload: dict | None = None,
) -> tuple[Any, dict]:
    """One GitHub REST call. Returns (parsed body or None, response headers).

    Raises RuntimeError with the status/body on HTTP errors so callers can
    surface API failures verbatim instead of a bare urllib traceback.
    """
    data = (
        json.dumps(payload).encode('utf-8') if payload is not None else None
    )
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            'Accept': 'application/vnd.github+json',
            'Content-Type': 'application/json',
            'X-GitHub-Api-Version': GITHUB_API_VERSION,
        },
        method=method,
    )
    if token:
        request.add_header('Authorization', f'Bearer {token}')
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            body = json.loads(raw.decode('utf-8')) if raw.strip() else None
            # HTTP field names are case-insensitive; normalize so Link-header
            # parsing survives either HTTP/1.1 ('Link') or HTTP/2 ('link').
            headers = {k.lower(): v for k, v in response.headers.items()}
            return body, headers
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f'GitHub API {exc.code} for {url}: {exc.read()[:300]!r}'
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f'GitHub API unreachable: {exc}') from exc


def _next_link(headers: dict) -> str:
    for part in headers.get('link', '').split(','):
        if 'rel="next"' in part:
            return part.split(';')[0].strip().strip('<>')
    return ''


def fetch_open_issues(repo: str, token: str | None) -> list[dict]:
    """Fetch open issues (not PRs) via the GitHub REST API. Read-only."""
    issues: list[dict] = []
    url = (
        f'https://api.github.com/repos/{repo}/issues'
        '?state=open&per_page=100'
    )
    while url:
        page, headers = _api_json('GET', url, token)
        if not isinstance(page, list):
            raise RuntimeError(
                f'GitHub API returned a non-list issues page for {url}: '
                f'{str(page)[:200]!r}'
            )
        issues.extend(i for i in page if 'pull_request' not in i)
        url = _next_link(headers)
    return issues


def list_issue_comments(repo: str, issue: int, token: str) -> list[dict]:
    """All comments on one issue (paginated). Read-only."""
    comments: list[dict] = []
    url = (
        f'https://api.github.com/repos/{repo}/issues/{issue}/comments'
        '?per_page=100'
    )
    while url:
        page, headers = _api_json('GET', url, token)
        if not isinstance(page, list):
            raise RuntimeError(
                f'GitHub API returned a non-list comments page for {url}: '
                f'{str(page)[:200]!r}'
            )
        comments.extend(page)
        url = _next_link(headers)
    return comments


def _is_status_comment(body: str) -> bool:
    """True when a comment is one of this runner's upserted status comments.

    Two conditions, both required: the body *starts* with the marker (a human
    quoting the marker mid-comment is not ours), and it contains the fixed
    JA signature line every status comment carries. This is deliberately
    stricter than a bare substring match — a PATCH over a human's comment
    would destroy their text.
    """
    stripped = body.lstrip()
    return (
        stripped.startswith(COMMENT_MARKER)
        and _COMMENT_SIGNATURE in stripped
    )


def find_status_comment_ids(
    repo: str, issue: int, token: str
) -> list[int]:
    """Ids of the runner's own status comments on the issue (oldest first).

    Normally zero or one; concurrent posting runs (two dispatches racing the
    first POST) can leave duplicates — callers should keep the first and
    delete the rest so the stale copy stops shadowing the live verdict.
    """
    return [
        comment['id']
        for comment in list_issue_comments(repo, issue, token)
        if _is_status_comment(comment.get('body') or '')
    ]


def upsert_issue_comment(repo: str, issue: int, body: str, token: str) -> str:
    """Create the status comment or edit the existing one in place.

    Returns 'created' or 'updated'. Edit-in-place is deliberate: a fresh
    comment per run would spam watchers of every mapped issue. Duplicate
    marked comments (a past posting race) are self-healed here: the oldest
    is edited and the rest deleted, best-effort.
    """
    existing_ids = find_status_comment_ids(repo, issue, token)
    if existing_ids:
        _api_json(
            'PATCH',
            'https://api.github.com/repos/'
            f'{repo}/issues/comments/{existing_ids[0]}',
            token,
            {'body': body},
        )
        for duplicate_id in existing_ids[1:]:
            try:
                _api_json(
                    'DELETE',
                    'https://api.github.com/repos/'
                    f'{repo}/issues/comments/{duplicate_id}',
                    token,
                )
            except RuntimeError as exc:
                print(
                    f'[verify] duplicate status comment {duplicate_id} on '
                    f'#{issue} could not be deleted (ignored): {exc}',
                    file=sys.stderr,
                )
        return 'updated'
    _api_json(
        'POST',
        f'https://api.github.com/repos/{repo}/issues/{issue}/comments',
        token,
        {'body': body},
    )
    return 'created'


def find_token() -> str | None:
    for name in TOKEN_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _resolve_argv(check: Check, python: str, work_dir: Path, report_dir: Path) -> list[str]:
    return [
        a.replace('{python}', python)
        .replace('{work_dir}', str(work_dir))
        .replace('{report_dir}', str(report_dir))
        for a in check.command
    ]


# ---------------------------------------------------------------------------
# Process-tree management for check subprocesses.
#
# A check is not one process: pytest-xdist spawns workers, scripts spawn
# helpers, and any of them may outlive the direct child — holding basetemp
# handles on Windows, writing into the shared log, or burning CPU for the
# rest of the job. ``subprocess.run(timeout=)`` kills only the direct child,
# and ``taskkill /T`` has two further gaps REV45 left open: it cannot reach
# children whose parent already exited (the sweep must run on the dead pid
# and only finds them if the parent's exit hasn't re-parented them yet), and
# nothing reaps the tree if the *runner itself* dies mid-check.
#
# The robust Windows primitive is a kill-on-close Job Object: nested-job
# semantics put grandchildren (including most detached spawns) in the job,
# TerminateJobObject kills the whole membership in one call, and the kernel
# kills members when the last job handle closes — so a crashed or killed
# runner cannot orphan a check. taskkill stays as the fallback when job
# creation/assignment fails.
#
# Residual gap (documented, no stdlib fix): a child spawned with
# CREATE_BREAKAWAY_FROM_JOB permission, or a POSIX grandchild that called
# setsid, escapes containment by design; and a POSIX runner dying mid-check
# leaves the group orphaned (no kill-on-close primitive exists there).
# ---------------------------------------------------------------------------

if os.name == 'nt':
    import ctypes
    from ctypes import wintypes

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ('PerProcessUserTimeLimit', wintypes.LARGE_INTEGER),
            ('PerJobUserTimeLimit', wintypes.LARGE_INTEGER),
            ('LimitFlags', wintypes.DWORD),
            ('MinimumWorkingSetSize', ctypes.c_size_t),
            ('MaximumWorkingSetSize', ctypes.c_size_t),
            ('ActiveProcessLimit', wintypes.DWORD),
            ('Affinity', ctypes.c_size_t),
            ('PriorityClass', wintypes.DWORD),
            ('SchedulingClass', wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                'ReadOperationCount', 'WriteOperationCount',
                'OtherOperationCount', 'ReadTransferCount',
                'WriteTransferCount', 'OtherTransferCount',
            )
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ('BasicLimitInformation', _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ('IoInfo', _IO_COUNTERS),
            ('ProcessMemoryLimit', ctypes.c_size_t),
            ('JobMemoryLimit', ctypes.c_size_t),
            ('PeakProcessMemoryUsed', ctypes.c_size_t),
            ('PeakJobMemoryUsed', ctypes.c_size_t),
        ]

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JobObjectExtendedLimitInformation = 9
    _kernel32 = ctypes.windll.kernel32
    # Handle values are pointer-width; declare signatures or ctypes truncates
    # them to C int and every call targets garbage.
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [
        wintypes.LPVOID, wintypes.LPCWSTR,
    ]
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
    ]
    _kernel32.AssignProcessToJobObject.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE,
    ]
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def _create_kill_job() -> wintypes.HANDLE | None:
        """A job object that kills all member processes on last handle close.

        Returns None when job creation or configuration fails — callers fall
        back to taskkill rather than losing the run.
        """
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not _kernel32.SetInformationJobObject(
            job, _JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        ):
            _kernel32.CloseHandle(job)
            return None
        return job

    def _close_kill_job(job) -> None:
        _kernel32.CloseHandle(job)


def _sweep_process_tree(proc: subprocess.Popen, job) -> None:
    """Kill the check's whole descendant set — the straggler sweep.

    Safe and intentional after the direct child already exited: surviving
    children keep running only because they outlived their parent, which is
    exactly the leak this bounds. On Windows+job this is TerminateJobObject
    (membership survives parent death — this is what taskkill /T cannot do);
    the taskkill fallback still cleans up the common same-tree case. On
    POSIX the check ran in its own session so the pgid IS the child's pid —
    killpg(pid) reaches grandchildren even after the group leader exited
    (os.getpgid on a dead pid raises instead, which is how the REV45 code
    could leave the whole group behind on a post-exit sweep).
    """
    if os.name == 'nt':
        if job is not None:
            _kernel32.TerminateJobObject(job, 1)
            return
        try:
            subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


# Brief pause between retry attempts: after a process kill, Windows releases
# file handles asynchronously, so an instant rerun can still trip the
# basetemp wipe on a handle the kernel has not finished releasing.
_RETRY_DELAY_S = 2.0


def _run_attempt(
    argv: list[str],
    repo_root: Path,
    env: dict,
    timeout: int,
    log,
) -> dict:
    """Run one check attempt; always returns an attempt record, never raises."""
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=repo_root,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=(os.name != 'nt'),
        )
    except OSError as exc:
        return {
            'status': 'error', 'exit_code': None,
            'duration_s': round(time.monotonic() - started, 2),
            'detail': str(exc),
        }
    job = None
    if os.name == 'nt':
        job = _create_kill_job()
        if job is not None:
            if not _kernel32.AssignProcessToJobObject(job, proc._handle):
                _kernel32.CloseHandle(job)
                job = None
    timed_out = False
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        rc = None
    # Sweep surviving descendants on EVERY exit path — a passed or failed
    # check that spawned a lingering child leaks it into later checks
    # (basetemp locks, shared log handle) just as surely as a timed-out one.
    try:
        _sweep_process_tree(proc, job)
    except Exception:  # a failed sweep must not lose the attempt record
        pass
    if timed_out:
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
            proc.wait()
        if job is not None:
            _close_kill_job(job)
        return {
            'status': 'timeout', 'exit_code': None,
            'duration_s': round(time.monotonic() - started, 2),
            'detail': f'exceeded timeout_seconds={timeout}',
        }
    if job is not None:
        _close_kill_job(job)
    return {
        'status': 'passed' if rc == 0 else 'failed',
        'exit_code': rc,
        'duration_s': round(time.monotonic() - started, 2),
        'detail': None,
    }


# Statuses worth a retry: a 'timeout' already consumed the full check budget,
# so rerunning it would double the worst case — it is not retried.
RETRIABLE_STATUSES = ('failed', 'error')


def run_check(
    check: Check,
    repo_root: Path,
    python: str,
    report_dir: Path,
    default_workers: int,
    log_dir: Path,
    rerun_failed: int = 0,
    env: dict | None = None,
) -> CheckResult:
    if check.kind == 'manual':
        return CheckResult(check=check, status='manual')

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
            return CheckResult(
                check=check, status='error',
                detail=f'test paths not found: {missing}',
            )
        argv_tail = [
            python, '-m', 'pytest', *check.tests,
            '-q', '-p', 'no:warnings', '--tb=short',
            '-n', str(workers),
        ]
    # The work dir is created only after cheap bail-outs so an 'error' result
    # does not leave an empty iv-<id>-* dir behind.
    work_dir = Path(tempfile.mkdtemp(prefix=f'iv-{check.id}-', dir=report_dir))
    if check.kind == 'pytest':
        argv = argv_tail + [f'--basetemp={work_dir / "basetemp"}']
    else:
        argv = _resolve_argv(check, python, work_dir, report_dir)

    if env is None:
        env = dict(os.environ)
        env.setdefault('QT_QPA_PLATFORM', 'offscreen')
        env.setdefault('PYTHONIOENCODING', 'utf-8')

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
                time.sleep(_RETRY_DELAY_S)
            record = _run_attempt(argv, repo_root, env, check.timeout_seconds, log)
            attempts.append(record)
            if (
                attempt >= max_attempts
                or record['status'] not in RETRIABLE_STATUSES
            ):
                break
    final = attempts[-1]
    return CheckResult(
        check=check,
        status=final['status'],
        duration_s=round(time.monotonic() - started, 2),
        exit_code=final['exit_code'],
        log_path=f'logs/{log_path.name}',
        detail=final['detail'],
        attempts=attempts,
        flaky=len({a['status'] for a in attempts}) > 1,
    )


def _git_head(repo_root: Path) -> tuple[str | None, bool]:
    """Return (full HEAD sha, worktree_dirty).

    (None, True) when git fails — biased toward 'no cache' because a keyless
    or lying cache is worse than re-running a check.
    """
    try:
        sha = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=repo_root, capture_output=True, text=True, timeout=15,
        )
        status = subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=repo_root, capture_output=True, text=True, timeout=30,
        )
        head = sha.stdout.strip() if sha.returncode == 0 else None
        dirty = status.returncode != 0 or bool(status.stdout.strip())
        return head or None, dirty
    except (OSError, subprocess.TimeoutExpired):
        return None, True


_FINGERPRINT_SNIPPET = (
    'import importlib.metadata, platform, hashlib;'
    'dists=";".join(sorted(((d.metadata["Name"] or "").lower()'
    '+"=="+d.version) for d in importlib.metadata.distributions()));'
    'print(platform.python_version(), platform.system(),'
    ' platform.machine(),'
    ' hashlib.sha1(dists.encode()).hexdigest())'
)

# Environment knobs the runner injects into every check subprocess (when not
# already set). They can change a check's outcome — offscreen vs on-screen
# Qt is a verdict-changing difference — so they belong to the cache key.
_MANAGED_ENV_VARS = ('QT_QPA_PLATFORM', 'PYTHONIOENCODING')


def _check_env() -> dict:
    """The environment every check subprocess runs under (single source —
    built once so the cache key sees exactly what the checks saw)."""
    env = dict(os.environ)
    for name in _MANAGED_ENV_VARS:
        env.setdefault(name, 'offscreen' if name == 'QT_QPA_PLATFORM'
                       else 'utf-8')
    return env


def _env_fingerprint(python: str) -> str:
    """Fingerprint of the check interpreter: python version + OS + machine
    arch + installed distribution set. Deterministic (sha1, not hash())."""
    try:
        proc = subprocess.run(
            [python, '-c', _FINGERPRINT_SNIPPET],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return 'unknown'


def _cache_key(check: Check, head_sha: str, env_fp: str, env: dict) -> str:
    """Key covering everything that can change a check's outcome: the
    execution-relevant check fields (not cosmetic ones like description),
    the exact commit, the interpreter environment, and the env vars the
    runner injects into every check subprocess."""
    material = json.dumps(
        {
            'id': check.id,
            'kind': check.kind,
            'tests': check.tests,
            'command': check.command,
            'timeout_seconds': check.timeout_seconds,
            'pytest_workers': check.pytest_workers,
            'head': head_sha,
            'env': env_fp,
            'managed_env': {k: env.get(k) for k in _MANAGED_ENV_VARS},
        },
        sort_keys=True,
    )
    return hashlib.sha1(material.encode('utf-8')).hexdigest()


def _load_cache(path: Path) -> dict:
    """Read the verdict cache; any corruption degrades to an empty cache
    (a cache that lies is worse than no cache)."""
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
        if (
            isinstance(raw, dict)
            and raw.get('version') == CACHE_SCHEMA_VERSION
            and isinstance(raw.get('entries'), dict)
        ):
            return raw['entries']
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_cache(path: Path, entries: dict) -> None:
    """Atomic-ish write (tmp + replace); failure never breaks the run."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(
            json.dumps(
                {'version': CACHE_SCHEMA_VERSION, 'entries': entries},
                ensure_ascii=False, indent=2,
            ),
            encoding='utf-8',
        )
        os.replace(tmp, path)
    except OSError as exc:
        print(f'[verify] cache write failed (ignored): {exc}', file=sys.stderr)


def compute_verdict(entry: IssueEntry, results: list[CheckResult]) -> str:
    automated = [r for r in results if r.check.kind != 'manual']
    manual = [r for r in results if r.check.kind == 'manual']
    if not automated:
        return 'manual_required'
    if any(r.status in ('failed', 'timeout', 'error') for r in automated):
        return 'failing'
    if manual:
        return 'partially_verified'
    return 'verified'


def _md_cell(text: str) -> str:
    """Escape a value for a Markdown table cell (pipes, newlines)."""
    return text.replace('|', '\\|').replace('\n', ' ')


def _check_status_label(check: dict) -> str:
    """`failed→passed` when a retry changed the outcome, else the status."""
    attempts = [a['status'] for a in check.get('attempts') or []]
    label = '→'.join(attempts) if len(attempts) > 1 else check['status']
    if check.get('cached'):
        label += ' (cached)'
    return label


_VERDICT_ORDER = (
    'verified', 'failing', 'partially_verified', 'manual_required',
    'unmapped', 'not_open',
)
_VERDICT_HEADLINES = {
    'verified': 'Close candidates (all green, no manual gates)',
    'failing': 'Needs attention — an automated check is red',
    'partially_verified': 'Software green; manual/physical evidence remains',
    'manual_required': 'Manual triage only — no automated checks',
    'unmapped': 'Open but not in the manifest — add an entry',
    'not_open': 'Stale manifest entries — issue is closed, remove entry',
}


def render_markdown(report: dict) -> str:
    lines: list[str] = []
    lines.append('# Issue verification report')
    lines.append('')
    lines.append(f"- repo: `{report['repo']}`")
    lines.append(f"- generated_at_utc: {report['generated_at_utc']}")
    lines.append(f"- mode: `{report['mode']}`")
    lines.append(f"- git_ref: `{report['git_ref']}`")
    lines.append(f"- python: `{report['python']}`")
    if report.get('cache'):
        lines.append(f"- cache: `{report['cache']}`")
    lines.append('')
    summary = report['summary']
    lines.append(
        '## Summary: '
        + ' / '.join(f'{k}={v}' for k, v in summary.items() if v)
    )
    lines.append('')
    # Triage-first view: the question a maintainer asks is "what can I close
    # now / what is red / what is stale", so issues are grouped by verdict
    # before the per-issue table.
    lines.append('## At a glance')
    by_verdict: dict[str, list[dict]] = {}
    for item in report['issues']:
        by_verdict.setdefault(item['verdict'], []).append(item)
    for verdict in _VERDICT_ORDER:
        items = by_verdict.get(verdict)
        if not items:
            continue
        refs = ', '.join(f"#{i['issue']}" for i in items)
        lines.append(f"- **{_VERDICT_HEADLINES[verdict]}**: {refs}")
    extra_not_open = [
        n for n in report['not_open_issues']
        if n not in {i['issue'] for i in by_verdict.get('not_open', [])}
    ]
    if extra_not_open:
        refs = ', '.join(f'#{n}' for n in extra_not_open)
        lines.append(f'- **{_VERDICT_HEADLINES["not_open"]}**: {refs}')
    lines.append('')
    lines.append('| Issue | Verdict | Checks | Manual evidence required |')
    lines.append('|---|---|---|---|')
    for item in report['issues']:
        checks = ', '.join(
            f"{c['id']}:{_check_status_label(c)}" for c in item['checks']
            if c['kind'] != 'manual'
        ) or '—'
        manual = '<br>'.join(
            _md_cell(m['description'].split('\n')[0])
            for m in item['manual_required']
        ) or '—'
        title = _md_cell(item.get('title') or '')
        lines.append(
            f"| #{item['issue']} {title} "
            f"| **{item['verdict']}** | {checks} | {manual} |"
        )
    lines.append('')
    for item in report['issues']:
        lines.append(f"## #{item['issue']} {item.get('title') or ''} — {item['verdict']}")
        if item.get('notes'):
            lines.append(f"> {item['notes']}")
        for c in item['checks']:
            bits = [
                f"- `{c['id']}` ({c['kind']}): **{_check_status_label(c)}**"
            ]
            if c.get('duration_s') is not None and c['kind'] != 'manual':
                bits.append(f"{c['duration_s']}s")
            if c.get('exit_code') is not None:
                bits.append(f"exit={c['exit_code']}")
            if c.get('log_path'):
                bits.append(f"log `{c['log_path']}`")
            lines.append(' '.join(bits))
            if c.get('detail'):
                detail = str(c['detail']).replace('\n', ' ').replace('\r', ' ')
                lines.append(f'  - detail: {detail}')
        for m in item['manual_required']:
            lines.append(f"- manual `{m['id']}`: {m['description']}")
        lines.append('')
    if report['unmapped_issues']:
        lines.append('## Open issues with no manifest entry (unmapped)')
        for i in report['unmapped_issues']:
            lines.append(f"- #{i['number']} {i.get('title') or ''}")
        lines.append('')
    if report['not_open_issues']:
        lines.append('## Manifest entries for issues that are not open (stale)')
        for n in report['not_open_issues']:
            lines.append(f'- #{n}')
        lines.append('')
    return '\n'.join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--repo', default=None, help='owner/name (default: manifest repo)')
    parser.add_argument('--manifest', type=Path, default=None)
    parser.add_argument('--repo-root', type=Path, default=None)
    parser.add_argument(
        '--issues', default=None,
        help='comma-separated issue numbers to verify (with --offline this '
             'also defines the open-issue set)',
    )
    parser.add_argument(
        '--offline', action='store_true',
        help='do not read from the GitHub API; open issues come from '
             '--issues (or every manifest entry when omitted). '
             '--post-summary still writes comments if given.',
    )
    parser.add_argument('--report-dir', type=Path, default=None)
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--dry-run', action='store_true', help='print the plan and exit')
    parser.add_argument(
        '--rerun-failed', type=int, default=1, metavar='N',
        help='retry a failed/errored check up to N more times before '
             'reporting it (default: 1 — this suite has known xdist/Qt '
             'teardown flakes; attempts are recorded in the report so a '
             'flaky pass stays visible). Timeouts are never retried: they '
             'already consumed the full check budget. 0 = single attempt.',
    )
    parser.add_argument(
        '--use-cache', action='store_true',
        help='reuse the verdict of a check whose (check config, HEAD sha, '
             'python environment) key already has a stored result — skips '
             're-verifying code that did not change. Disabled on a dirty '
             'worktree unless --allow-dirty-cache is also given.',
    )
    parser.add_argument(
        '--cache-file', type=Path, default=None,
        help=f'verdict cache path (default: {DEFAULT_CACHE_PATH})',
    )
    parser.add_argument(
        '--allow-dirty-cache', action='store_true',
        help='use/write the verdict cache even with a dirty worktree '
             '(unsafe: local edits are not captured by the cache key)',
    )
    parser.add_argument(
        '--fail-on-verdict', default=None, metavar='VERDICTS',
        help='comma-separated verdicts that flip the exit code to 3 '
             '(e.g. "failing,unmapped"); default: verdicts never change '
             'the exit status — they are data, not a gate',
    )
    parser.add_argument(
        '--post-summary', action='store_true',
        help='upsert the marked status comment on each mapped issue after '
             'the run — one comment per issue, edited in place, no spam '
             '(requires issues:write token; off by default)',
    )
    parser.add_argument(
        '--post-summary-from', type=Path, default=None, metavar='REPORT_JSON',
        help='upsert verdict comments from a previously written report and '
             'exit — reuses results instead of re-running checks',
    )
    return parser


def _run_all_checks(
    *,
    args,
    manifest: Manifest,
    open_numbers: list[int],
    live_issues: dict[int, dict],
    report_dir: Path,
    log_dir: Path,
    cache_active: bool,
    cache_entries: dict,
    head_sha: str | None,
    check_env: dict,
    repo_root: Path,
    issue_reports: list[dict],
    unmapped: list[dict],
) -> int:
    """Run every mapped check over the open-issue set; returns cache hits.

    Mutates issue_reports / unmapped / cache_entries. Extracted from main()
    so an unexpected failure is catchable as a clean exit-2 tool failure.
    """
    env_fp: str | None = None
    cache_hits = 0
    for n in sorted(set(open_numbers)):
        live = live_issues.get(n, {})
        entry = manifest.issues.get(n)
        if entry is None:
            unmapped.append({'number': n, 'title': live.get('title', '')})
            issue_reports.append({
                'issue': n, 'title': live.get('title', ''),
                'verdict': 'unmapped', 'checks': [], 'manual_required': [],
            })
            continue
        results: list[CheckResult] = []
        for c in entry.checks:
            key = None
            if cache_active and c.kind != 'manual':
                if env_fp is None:
                    env_fp = _env_fingerprint(args.python)
                key = _cache_key(c, head_sha, env_fp, check_env)
                hit = cache_entries.get(key)
                if not (
                    isinstance(hit, dict) and isinstance(hit.get('status'), str)
                ):
                    hit = None  # malformed entry — never trust it
                if hit is not None:
                    cache_hits += 1
                    results.append(CheckResult(
                        check=c,
                        status=hit['status'],
                        duration_s=float(hit.get('duration_s') or 0.0),
                        exit_code=hit.get('exit_code'),
                        detail=hit.get('detail'),
                        attempts=list(hit.get('attempts') or []),
                        flaky=bool(hit.get('flaky')),
                        cached=True,
                    ))
                    continue
            result = run_check(
                c, repo_root, args.python, report_dir,
                manifest.default_workers, log_dir,
                rerun_failed=args.rerun_failed,
                env=check_env,
            )
            results.append(result)
            if cache_active and key is not None and result.status != 'manual':
                cache_entries[key] = {
                    'status': result.status,
                    'exit_code': result.exit_code,
                    'detail': result.detail,
                    'duration_s': result.duration_s,
                    'attempts': result.attempts,
                    'flaky': result.flaky,
                    'git_ref': head_sha,
                    'recorded_at_utc': datetime.now(timezone.utc).strftime(
                        '%Y-%m-%dT%H:%M:%SZ'
                    ),
                }
        verdict = compute_verdict(entry, results)
        issue_reports.append({
            'issue': n,
            'title': live.get('title') or entry.title or '',
            'verdict': verdict,
            'notes': entry.notes,
            'checks': [
                {
                    'id': r.check.id, 'kind': r.check.kind, 'status': r.status,
                    'duration_s': r.duration_s, 'exit_code': r.exit_code,
                    'log_path': r.log_path, 'detail': r.detail,
                    'description': r.check.description,
                    'attempts': r.attempts, 'flaky': r.flaky,
                    'cached': r.cached,
                }
                for r in results if r.check.kind != 'manual'
            ],
            'manual_required': [
                {'id': r.check.id, 'description': r.check.description}
                for r in results if r.check.kind == 'manual'
            ],
        })
    return cache_hits


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Windows consoles default to cp1252; report files are always UTF-8, but
    # console output must never crash on JA text.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError):
            pass
    repo_root = (args.repo_root or Path(__file__).resolve().parent.parent).resolve()
    manifest_path = (args.manifest or repo_root / DEFAULT_MANIFEST).resolve()
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        print(f'[verify] manifest error: {exc}', file=sys.stderr)
        return 2
    repo = args.repo or manifest.repo

    if args.fail_on_verdict:
        bad = [
            v for v in (s.strip() for s in args.fail_on_verdict.split(','))
            if v and v not in VERDICTS
        ]
        if bad:
            print(
                f'[verify] --fail-on-verdict got unknown verdicts {bad} '
                f'(valid: {", ".join(VERDICTS)})',
                file=sys.stderr,
            )
            return 2
        fail_on = {v.strip() for v in args.fail_on_verdict.split(',') if v.strip()}
    else:
        fail_on = set()

    # --post-summary-from only needs the manifest (for the repo) and the
    # saved report: it must NOT fetch live issues first — a network hiccup
    # would otherwise abort a purely-local replay.
    if args.post_summary_from:
        report_path = args.post_summary_from
        try:
            saved = json.loads(report_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            print(f'[verify] cannot read report {report_path}: {exc}',
                  file=sys.stderr)
            return 2
        return _post_summaries(repo, saved)

    if args.issues:
        try:
            wanted = {
                int(p) for p in args.issues.split(',') if p.strip()
            }
        except ValueError:
            print(
                f'[verify] --issues must be comma-separated integers, '
                f'got {args.issues!r}',
                file=sys.stderr,
            )
            return 2
        if any(n <= 0 for n in wanted):
            print(
                f'[verify] --issues must be positive issue numbers, '
                f'got {args.issues!r}',
                file=sys.stderr,
            )
            return 2
    else:
        wanted = None

    not_open_wanted: set[int] = set()
    if args.offline:
        if wanted:
            open_numbers = sorted(wanted)
        else:
            open_numbers = sorted(manifest.issues)
        live_issues: dict[int, dict] = {
            n: {'number': n, 'title': manifest.issues[n].title or ''}
            for n in open_numbers if n in manifest.issues
        }
        for n in open_numbers:
            live_issues.setdefault(n, {'number': n, 'title': ''})
    else:
        token = find_token()
        try:
            open_list = fetch_open_issues(repo, token)
        except RuntimeError as exc:
            print(f'[verify] {exc}', file=sys.stderr)
            return 2
        live_issues = {i['number']: i for i in open_list}
        open_numbers = sorted(live_issues)
        if wanted:
            open_numbers = [n for n in open_numbers if n in wanted]
            # Explicitly requested but not open: report the truth instead
            # of running checks on a closed issue and calling it verified.
            not_open_wanted = set(wanted) - set(open_numbers)

    stamp = datetime.now(timezone.utc)
    report_dir = (
        args.report_dir
        or repo_root / 'artifacts' / f'issue-verification-{stamp:%Y%m%dT%H%M%SZ}'
    ).resolve()

    log_dir = report_dir / 'logs'
    report_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(exist_ok=True)

    plan_rows = []
    for n in open_numbers:
        entry = manifest.issues.get(n)
        if entry is None:
            plan_rows.append((n, 'unmapped', []))
            continue
        plan_rows.append((n, 'mapped', entry.checks))

    if args.dry_run:
        print(f'[verify] repo={repo} manifest={manifest_path}')
        print(f'[verify] report_dir={report_dir}')
        for n, state, checks in plan_rows:
            title = live_issues.get(n, {}).get('title', '')
            print(f'  #{n} {state} — {title}')
            for c in checks:
                print(f'    - [{c.kind}] {c.id}: {c.description.splitlines()[0][:80]}')
        for n in sorted(not_open_wanted):
            print(f'  #{n} not_open — (requested but not open)')
        return 0

    head_sha, worktree_dirty = _git_head(repo_root)
    git_ref = head_sha[:8] if head_sha else 'unknown'

    cache_path = (
        args.cache_file or repo_root / DEFAULT_CACHE_PATH
    ).resolve()
    cache_entries: dict = {}
    cache_active = False
    if args.use_cache:
        if not head_sha:
            print('[verify] --use-cache ignored: HEAD sha unavailable',
                  file=sys.stderr)
        elif worktree_dirty and not args.allow_dirty_cache:
            print(
                '[verify] worktree is dirty — cache disabled '
                '(use --allow-dirty-cache to override)',
                file=sys.stderr,
            )
        else:
            cache_entries = _load_cache(cache_path)
            cache_active = True
            print(
                f'[verify] verdict cache: {cache_path} '
                f'({len(cache_entries)} entries)'
            )
    check_env = _check_env()

    issue_reports: list[dict] = []
    unmapped: list[dict] = []
    try:
        cache_hits = _run_all_checks(
            args=args, manifest=manifest, open_numbers=open_numbers,
            live_issues=live_issues, report_dir=report_dir, log_dir=log_dir,
            cache_active=cache_active, cache_entries=cache_entries,
            head_sha=head_sha, check_env=check_env, repo_root=repo_root,
            issue_reports=issue_reports, unmapped=unmapped,
        )
    except Exception as exc:  # unexpected tool failure: honor the exit-code
        import traceback                      # contract ('2', not a bare
        traceback.print_exc()                # traceback exit code)
        print(f'[verify] internal error: {exc!r}', file=sys.stderr)
        return 2

    for n in sorted(not_open_wanted):
        issue_reports.append({
            'issue': n,
            'title': manifest.issues[n].title or ''
            if n in manifest.issues else '',
            'verdict': 'not_open', 'checks': [], 'manual_required': [],
        })

    if cache_active:
        _save_cache(cache_path, cache_entries)

    # Stale = manifest entry whose issue is genuinely not open. Compare with
    # the live open set, NOT open_numbers — entries merely excluded by an
    # --issues filter are open but unrequested, not stale. Offline runs
    # cannot know open state, so they skip this surface entirely.
    not_open = [] if args.offline else [
        n for n in manifest.issues if n not in live_issues
    ]
    summary = {v: 0 for v in VERDICTS}
    for item in issue_reports:
        summary[item['verdict']] = summary.get(item['verdict'], 0) + 1

    report = {
        'generated_at_utc': stamp.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'repo': repo,
        'mode': 'offline' if args.offline else 'live',
        'git_ref': git_ref,
        'worktree_dirty': worktree_dirty,
        'python': f'{platform.python_version()} ({platform.system()})',
        'manifest': str(manifest_path),
        'summary': summary,
        'issues': issue_reports,
        'unmapped_issues': unmapped,
        'not_open_issues': not_open,
    }
    if args.use_cache:
        report['cache'] = str(cache_path) if cache_active else 'disabled'
        report['cache_hits'] = cache_hits
    json_path = report_dir / 'issue_verification_report.json'
    # Atomic-ish write: a runner killed mid-write must not leave a torn JSON
    # that --post-summary-from would half-trust.
    tmp_json = json_path.with_suffix('.json.tmp')
    tmp_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    os.replace(tmp_json, json_path)
    md_path = report_dir / f'issue-verification-{stamp:%Y-%m-%d}.md'
    md_path.write_text(render_markdown(report), encoding='utf-8')

    print(f'[verify] report: {json_path}')
    print(f'[verify] markdown: {md_path}')
    print('[verify] summary: ' + ', '.join(
        f'{k}={v}' for k, v in summary.items() if v
    ))

    if args.post_summary:
        code = _post_summaries(repo, report)
        if code != 0:
            return code
    if fail_on and any(
        item['verdict'] in fail_on for item in issue_reports
    ):
        present = sorted(
            {item['verdict'] for item in issue_reports} & fail_on
        )
        print(
            f'[verify] --fail-on-verdict hit: {present}',
            file=sys.stderr,
        )
        return 3
    return 0


def _comment_body(repo: str, item: dict, stamp: str, git_ref: str) -> str:
    """The upserted per-issue status comment (JA — user-facing surface)."""
    lines = [
        COMMENT_MARKER,
        f"自動検証: **{item['verdict']}** ({stamp} UTC, ref `{git_ref}`)",
    ]
    failing = [
        c['id'] for c in item.get('checks', [])
        if c.get('status') in ('failed', 'timeout', 'error')
    ]
    flaky = [
        c['id'] for c in item.get('checks', []) if c.get('flaky')
    ]
    if failing:
        lines.append(
            '失敗チェック: ' + ', '.join(f'`{c}`' for c in failing)
        )
    if flaky:
        lines.append(
            'flaky 検出（最初の実行と結果が不一致）: '
            + ', '.join(f'`{c}`' for c in flaky)
        )
    manual = item.get('manual_required') or []
    if manual:
        lines.append(f'手動エビデンス残り: {len(manual)} 件')
    lines.append(
        '詳細: `verify-open-issues` workflow のレポート成果物を参照 '
        f'(https://github.com/{repo}/actions/workflows/verify-open-issues.yml)'
    )
    return '\n'.join(lines)


def _post_summaries(repo: str, report: dict) -> int:
    """Upsert one marked status comment per mapped issue.

    Skips unmapped / not_open items (commenting on a closed issue or one
    with no manifest entry is spam). Any post failure exits non-zero — a
    write that silently half-fails would mislead.
    """
    token = find_token()
    if not token:
        print('[verify] --post-summary needs a token in env', file=sys.stderr)
        return 2
    stamp = report.get('generated_at_utc', '')
    git_ref = report.get('git_ref', '')
    failures = 0
    for item in report.get('issues', []):
        if item.get('verdict') in ('unmapped', 'not_open'):
            continue
        body = _comment_body(repo, item, stamp, git_ref)
        try:
            action = upsert_issue_comment(repo, item['issue'], body, token)
            print(f"[verify] {action} status comment on #{item['issue']}")
        except RuntimeError as exc:
            failures += 1
            print(f"[verify] comment failed on #{item['issue']}: {exc}",
                  file=sys.stderr)
    if failures:
        print(f'[verify] {failures} status comment(s) failed',
              file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
