#!/usr/bin/env python3
"""Verify progress on open GitHub issues against the verification manifest.

Reads ``scripts/issue_verification_manifest.yaml`` (or ``--manifest``), fetches
the repository's open issues (or an offline ``--issues`` list), runs every
mapped automated check in a bounded subprocess, and writes a machine-readable
``issue_verification_report.json`` plus a Markdown report into ``--report-dir``.

The tool is deliberately read-only against GitHub unless ``--post-summary``
is passed: it never edits issues, and the optional per-issue comment posts a
single status line only.

Verdicts (per issue):
  verified            - all automated checks passed and no manual evidence remains
  partially_verified  - all automated checks passed, manual evidence remains
  manual_required     - no automated checks are declared for the issue
  failing             - at least one automated check failed / errored / timed out
  unmapped            - issue is open but absent from the manifest
  not_open            - manifest entry refers to an issue that is not open

Exit status: 0 on a successful run (verdicts may still include failures —
they are data, not runner errors); non-zero only when the tool itself cannot
complete (malformed manifest, API failure without --offline, bad arguments).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
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
TOKEN_ENV_VARS = ('GITHUB_TOKEN', 'GITHUB_KA0923S_PAT', 'GH_TOKEN')


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
    status: str  # passed | failed | timeout | error | manual
    duration_s: float = 0.0
    exit_code: int | None = None
    log_path: str | None = None
    detail: str | None = None


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
                isinstance(cid, str) and cid and ' ' not in cid,
                f'{cwhere}.id must be a non-empty string without spaces',
            )
            _require(cid not in seen_ids, f'{cwhere}: duplicate check id {cid!r}')
            seen_ids.add(cid)
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


def fetch_open_issues(repo: str, token: str | None) -> list[dict]:
    """Fetch open issues (not PRs) via the GitHub REST API. Read-only."""
    issues: list[dict] = []
    url = (
        f'https://api.github.com/repos/{repo}/issues'
        '?state=open&per_page=100'
    )
    while url:
        request = urllib.request.Request(
            url, headers={'Accept': 'application/vnd.github+json'}
        )
        if token:
            request.add_header('Authorization', f'Bearer {token}')
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                page = json.loads(response.read().decode('utf-8'))
                link = response.headers.get('Link', '')
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f'GitHub API {exc.code} for {url}: {exc.read()[:300]!r}'
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f'GitHub API unreachable: {exc}') from exc
        issues.extend(i for i in page if 'pull_request' not in i)
        url = ''
        for part in link.split(','):
            if 'rel="next"' in part:
                url = part.split(';')[0].strip().strip('<>')
    return issues


def find_token() -> str | None:
    for name in TOKEN_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    return None


def post_issue_comment(repo: str, issue: int, body: str, token: str) -> None:
    request = urllib.request.Request(
        f'https://api.github.com/repos/{repo}/issues/{issue}/comments',
        data=json.dumps({'body': body}).encode('utf-8'),
        headers={
            'Accept': 'application/vnd.github+json',
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def _resolve_argv(check: Check, python: str, work_dir: Path, report_dir: Path) -> list[str]:
    return [
        a.replace('{python}', python)
        .replace('{work_dir}', str(work_dir))
        .replace('{report_dir}', str(report_dir))
        for a in check.command
    ]


def run_check(
    check: Check,
    repo_root: Path,
    python: str,
    report_dir: Path,
    default_workers: int,
    log_dir: Path,
) -> CheckResult:
    if check.kind == 'manual':
        return CheckResult(check=check, status='manual')

    work_dir = Path(tempfile.mkdtemp(prefix=f'iv-{check.id}-', dir=report_dir))
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
        argv = [
            python, '-m', 'pytest', *check.tests,
            '-q', '-p', 'no:warnings', '--tb=short',
            '-n', str(workers),
            f'--basetemp={work_dir / "basetemp"}',
        ]
    else:
        argv = _resolve_argv(check, python, work_dir, report_dir)

    env = dict(os.environ)
    env.setdefault('QT_QPA_PLATFORM', 'offscreen')
    env.setdefault('PYTHONIOENCODING', 'utf-8')

    log_path = log_dir / f'{check.id}.log'
    started = time.monotonic()
    try:
        with log_path.open('wb') as log:
            log.write(('argv: ' + ' '.join(argv) + '\n\n').encode('utf-8'))
            proc = subprocess.run(
                argv,
                cwd=repo_root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=check.timeout_seconds,
            )
        duration = time.monotonic() - started
        status = 'passed' if proc.returncode == 0 else 'failed'
        return CheckResult(
            check=check,
            status=status,
            duration_s=round(duration, 2),
            exit_code=proc.returncode,
            log_path=log_path.name,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            check=check,
            status='timeout',
            duration_s=round(time.monotonic() - started, 2),
            log_path=log_path.name,
            detail=f'exceeded timeout_seconds={check.timeout_seconds}',
        )
    except OSError as exc:
        return CheckResult(
            check=check,
            status='error',
            duration_s=round(time.monotonic() - started, 2),
            detail=str(exc),
        )


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


def render_markdown(report: dict) -> str:
    lines: list[str] = []
    lines.append('# Issue verification report')
    lines.append('')
    lines.append(f"- repo: `{report['repo']}`")
    lines.append(f"- generated_at_utc: {report['generated_at_utc']}")
    lines.append(f"- mode: `{report['mode']}`")
    lines.append(f"- git_ref: `{report['git_ref']}`")
    lines.append(f"- python: `{report['python']}`")
    lines.append('')
    summary = report['summary']
    lines.append(
        '## Summary: '
        + ' / '.join(f'{k}={v}' for k, v in summary.items() if v)
    )
    lines.append('')
    lines.append('| Issue | Verdict | Checks | Manual evidence required |')
    lines.append('|---|---|---|---|')
    for item in report['issues']:
        checks = ', '.join(
            f"{c['id']}:{c['status']}" for c in item['checks']
            if c['kind'] != 'manual'
        ) or '—'
        manual = '<br>'.join(
            m['description'].split('\n')[0] for m in item['manual_required']
        ) or '—'
        lines.append(
            f"| #{item['issue']} {item.get('title') or ''} "
            f"| **{item['verdict']}** | {checks} | {manual} |"
        )
    lines.append('')
    for item in report['issues']:
        lines.append(f"## #{item['issue']} {item.get('title') or ''} — {item['verdict']}")
        if item.get('notes'):
            lines.append(f"> {item['notes']}")
        for c in item['checks']:
            bits = [f"- `{c['id']}` ({c['kind']}): **{c['status']}**"]
            if c.get('duration_s') is not None and c['kind'] != 'manual':
                bits.append(f"{c['duration_s']}s")
            if c.get('exit_code') is not None:
                bits.append(f"exit={c['exit_code']}")
            if c.get('log_path'):
                bits.append(f"log `{c['log_path']}`")
            lines.append(' '.join(bits))
            if c.get('detail'):
                lines.append(f"  - detail: {c['detail']}")
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
        help='do not call the GitHub API; open issues come from --issues '
             '(or every manifest entry when omitted)',
    )
    parser.add_argument('--report-dir', type=Path, default=None)
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--dry-run', action='store_true', help='print the plan and exit')
    parser.add_argument(
        '--post-summary', action='store_true',
        help='post a one-line verdict comment on each mapped issue after '
             'the run (requires issues:write token; off by default)',
    )
    parser.add_argument(
        '--post-summary-from', type=Path, default=None, metavar='REPORT_JSON',
        help='post verdict comments from a previously written report and '
             'exit — reuses results instead of re-running checks',
    )
    return parser


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

    wanted = (
        {int(p) for p in args.issues.split(',') if p.strip()}
        if args.issues else None
    )

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
            for n in sorted(wanted - set(open_numbers)):
                live_issues[n] = {'number': n, 'title': '(not open)'}
                open_numbers.append(n)

    stamp = datetime.now(timezone.utc)
    report_dir = (
        args.report_dir
        or repo_root / 'artifacts' / f'issue-verification-{stamp:%Y%m%dT%H%M%SZ}'
    ).resolve()

    if args.post_summary_from:
        report_path = args.post_summary_from
        try:
            saved = json.loads(report_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            print(f'[verify] cannot read report {report_path}: {exc}',
                  file=sys.stderr)
            return 2
        return _post_summaries(repo, saved)

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
        return 0

    issue_reports: list[dict] = []
    unmapped: list[dict] = []
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
        results = [
            run_check(
                c, repo_root, args.python, report_dir,
                manifest.default_workers, log_dir,
            )
            for c in entry.checks
        ]
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
                }
                for r in results if r.check.kind != 'manual'
            ],
            'manual_required': [
                {'id': r.check.id, 'description': r.check.description}
                for r in results if r.check.kind == 'manual'
            ],
        })

    not_open = [
        n for n in manifest.issues
        if n not in set(open_numbers) and not args.offline
    ]
    summary = {v: 0 for v in VERDICTS}
    for item in issue_reports:
        summary[item['verdict']] = summary.get(item['verdict'], 0) + 1

    git_ref = 'unknown'
    try:
        proc = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=repo_root, capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            git_ref = proc.stdout.strip()
    except OSError:
        pass

    report = {
        'generated_at_utc': stamp.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'repo': repo,
        'mode': 'offline' if args.offline else 'live',
        'git_ref': git_ref,
        'python': f'{platform.python_version()} ({platform.system()})',
        'manifest': str(manifest_path),
        'summary': summary,
        'issues': issue_reports,
        'unmapped_issues': unmapped,
        'not_open_issues': not_open,
    }
    json_path = report_dir / 'issue_verification_report.json'
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    md_path = report_dir / f'issue-verification-{stamp:%Y-%m-%d}.md'
    md_path.write_text(render_markdown(report), encoding='utf-8')

    print(f'[verify] report: {json_path}')
    print(f'[verify] markdown: {md_path}')
    print('[verify] summary: ' + ', '.join(
        f'{k}={v}' for k, v in summary.items() if v
    ))

    if args.post_summary:
        return _post_summaries(repo, report)
    return 0


def _post_summaries(repo: str, report: dict) -> int:
    token = find_token()
    if not token:
        print('[verify] --post-summary needs a token in env', file=sys.stderr)
        return 2
    stamp = report.get('generated_at_utc', '')
    git_ref = report.get('git_ref', '')
    for item in report.get('issues', []):
        if item.get('verdict') == 'unmapped':
            continue
        body = (
            f"自動検証レポート: **{item['verdict']}** "
            f"({stamp}, ref `{git_ref}`) — "
            '詳細は Actions アーティファクトの '
            'issue_verification_report.json を参照。'
        )
        try:
            post_issue_comment(repo, item['issue'], body, token)
            print(f"[verify] posted summary on #{item['issue']}")
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            print(f"[verify] comment failed on #{item['issue']}: {exc}",
                  file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
