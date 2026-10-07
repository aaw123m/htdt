"""Issue #848 — evidence-aware issue lifecycle classification + drift check.

GitHub open/closed is binary; HTDT's completion model is not. This module
reads the canonical lifecycle metadata in
``scripts/issue_lifecycle_manifest.yaml`` plus the open-issue verification
manifest, and produces:

* a per-issue classification — implementation_missing /
  implementation_present_checks_red / gate_remaining / closeable /
  research_only / unclassified — answering "what landed" and "what exact
  gate remains" without reading commit history;
* drift findings — contradictions between GitHub state, lifecycle
  metadata and the verification manifest (report-only; nothing is
  mutated on GitHub).

Deterministic regeneration: repository files plus optionally a
pre-fetched GitHub issues JSON (``--issues-json``); when omitted the
GitHub-dependent checks are skipped and reported as such.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_LIFECYCLE = Path('scripts') / 'issue_lifecycle_manifest.yaml'
DEFAULT_VERIFICATION = Path('scripts') / 'issue_verification_manifest.yaml'

LIFECYCLE_STATES = {
    'planned',
    'in_progress',
    'software_landed',
    'acceptance_remaining',
    'physical_evidence_remaining',
    'structural_followup_remaining',
    'complete',
    'withdrawn',
    'superseded',
}
#: States that assert implementation already landed.
LANDED_STATES = {
    'software_landed',
    'acceptance_remaining',
    'physical_evidence_remaining',
    'structural_followup_remaining',
    'complete',
}
#: States still describing live work (a closed GitHub issue must not be one).
ACTIVE_STATES = LIFECYCLE_STATES - {'complete', 'withdrawn', 'superseded'}

GATE_KINDS = {'manual', 'physical', 'structural', 'verification'}


@dataclass(frozen=True)
class LifecycleEntry:
    issue: int
    lifecycle: str
    prs: tuple[int, ...] = ()
    commits: tuple[str, ...] = ()
    remaining_gates: tuple[tuple[str, str], ...] = ()
    evidence_refs: tuple[str, ...] = ()
    close_when_gates_clear: bool = True
    superseded_by: int | None = None

    @property
    def has_landed_refs(self) -> bool:
        return bool(self.prs or self.commits)


@dataclass(frozen=True)
class Finding:
    severity: str  # error | warning
    issue: int
    code: str
    message: str


@dataclass(frozen=True)
class Classification:
    issue: int
    lifecycle: str | None
    bucket: str
    detail: str


def _fail(message: str) -> None:
    raise SystemExit(f'issue_lifecycle: {message}')


def _load_yaml(path: Path) -> Any:
    try:
        import yaml
    except ImportError:  # pragma: no cover - dev env always has PyYAML
        _fail('PyYAML is required to read the lifecycle manifest')
    try:
        return yaml.safe_load(path.read_text(encoding='utf-8'))
    except yaml.YAMLError as exc:
        _fail(f'{path}: YAML parse error: {exc}')


def load_lifecycle_manifest(path: Path) -> dict[int, LifecycleEntry]:
    raw = _load_yaml(path)
    if not isinstance(raw, dict):
        _fail(f'{path}: top-level must be a mapping')
    entries: dict[int, LifecycleEntry] = {}
    for item in raw.get('entries') or []:
        where = f"{path} issue {item.get('issue', '?')}"
        issue = item.get('issue')
        if not isinstance(issue, int) or issue <= 0:
            _fail(f'{where}: issue must be a positive integer')
        if issue in entries:
            _fail(f'{where}: duplicate lifecycle entry')
        lifecycle = item.get('lifecycle')
        if lifecycle not in LIFECYCLE_STATES:
            _fail(f'{where}: unknown lifecycle {lifecycle!r}')
        landed = item.get('landed') or {}
        prs = landed.get('prs') or []
        commits = landed.get('commits') or []
        if not all(isinstance(p, int) and p > 0 for p in prs):
            _fail(f'{where}: landed.prs must be positive integers')
        if not all(isinstance(c, str) and 7 <= len(c) <= 64 for c in commits):
            _fail(f'{where}: landed.commits must be sha strings (7-64 chars)')
        gates = []
        for gate in item.get('remaining_gates') or []:
            kind = gate.get('kind')
            if kind not in GATE_KINDS:
                _fail(f'{where}: gate kind must be one of {sorted(GATE_KINDS)}')
            description = (gate.get('description') or '').strip()
            if not description:
                _fail(f'{where}: gate needs a description')
            gates.append((kind, description))
        evidence = item.get('evidence_refs') or []
        if not all(isinstance(e, str) and e.strip() for e in evidence):
            _fail(f'{where}: evidence_refs must be non-empty paths')
        entries[issue] = LifecycleEntry(
            issue=issue,
            lifecycle=lifecycle,
            prs=tuple(prs),
            commits=tuple(str(c) for c in commits),
            remaining_gates=tuple(gates),
            evidence_refs=tuple(evidence),
            close_when_gates_clear=bool(item.get('close_when_gates_clear', True)),
            superseded_by=item.get('superseded_by'),
        )
    return entries


def load_verification_issues(path: Path) -> dict[int, dict]:
    """Issue numbers declared in the open-issue verification manifest."""
    raw = _load_yaml(path)
    issues: dict[int, dict] = {}
    for item in (raw or {}).get('issues') or []:
        issue = item.get('issue')
        if isinstance(issue, int):
            issues[issue] = item
    return issues


def drift_findings(
    entries: dict[int, LifecycleEntry],
    verification_issues: dict[int, dict],
    github_states: dict[int, str] | None = None,
) -> list[Finding]:
    """Contradictions across lifecycle metadata, verification manifest and
    GitHub state. ``github_states`` maps issue -> 'open'|'closed'; None means
    GitHub metadata was not supplied and those checks are skipped."""
    findings: list[Finding] = []
    for entry in entries.values():
        if entry.lifecycle in (
            LANDED_STATES - {'complete'}
        ) and not entry.has_landed_refs:
            findings.append(Finding(
                'error', entry.issue, 'landed_without_refs',
                f'lifecycle={entry.lifecycle} requires landed.prs/commits refs',
            ))
        if entry.lifecycle == 'complete' and entry.remaining_gates:
            findings.append(Finding(
                'error', entry.issue, 'complete_with_gates',
                'lifecycle=complete cannot declare remaining_gates',
            ))
        if entry.lifecycle == 'superseded' and entry.superseded_by is None:
            findings.append(Finding(
                'error', entry.issue, 'superseded_without_target',
                'lifecycle=superseded requires superseded_by',
            ))
        if (
            entry.lifecycle in ACTIVE_STATES
            and entry.has_landed_refs
            and not entry.remaining_gates
            and entry.close_when_gates_clear
        ):
            findings.append(Finding(
                'warning', entry.issue, 'landed_no_gate_not_complete',
                'implementation landed with no remaining gate — '
                'classify complete, or record the outstanding gate',
            ))
        if github_states is not None:
            state = github_states.get(entry.issue)
            if state == 'closed' and entry.lifecycle in ACTIVE_STATES:
                findings.append(Finding(
                    'error', entry.issue, 'closed_but_active',
                    'GitHub issue closed while lifecycle is still active',
                ))
            if state is None:
                findings.append(Finding(
                    'warning', entry.issue, 'lifecycle_for_unknown_issue',
                    'lifecycle entry for an issue not in the supplied GitHub set',
                ))
    # Tracked by the verification manifest but no lifecycle classification.
    # GitHub-closed issues are settled — they do not need a lifecycle entry.
    for issue in sorted(set(verification_issues) - set(entries)):
        if github_states is not None and github_states.get(issue) == 'closed':
            continue
        findings.append(Finding(
            'warning', issue, 'unclassified',
            'issue has verification checks but no lifecycle entry',
        ))
    return findings


def classify_issue(
    entry: LifecycleEntry | None,
    *,
    check_statuses: tuple[str, ...] = (),
    github_state: str | None = None,
) -> Classification:
    """Bucket an issue for the backlog view.

    ``check_statuses`` is the verify-open-issues per-check statuses for the
    issue (passed/failed/manual/...). A green software check never implies
    issue completion: with gates remaining the bucket stays
    ``gate_remaining``.
    """
    if entry is None:
        return Classification(-1, None, 'unclassified', 'no lifecycle entry')
    issue = entry.issue
    lc = entry.lifecycle
    if lc in {'withdrawn', 'superseded'}:
        return Classification(issue, lc, 'closed', lc)
    if github_state == 'closed':
        return Classification(issue, lc, 'closed', 'GitHub closed')
    if lc in {'planned', 'in_progress'} and not entry.has_landed_refs:
        return Classification(
            issue, lc, 'implementation_missing', f'lifecycle={lc}',
        )
    if lc in {'planned', 'in_progress'} and entry.has_landed_refs:
        return Classification(
            issue, lc, 'implementation_present_checks_red',
            'landed refs recorded but lifecycle still pre-landed',
        )
    # Landed states.
    automated_failed = any(s in {'failed', 'error', 'timeout'} for s in check_statuses)
    if lc == 'complete':
        return Classification(issue, lc, 'closeable', 'all gates cleared')
    if automated_failed:
        return Classification(
            issue, lc, 'implementation_present_checks_red',
            'automated checks failing on landed implementation',
        )
    if entry.remaining_gates:
        kinds = ', '.join(sorted({k for k, _ in entry.remaining_gates}))
        return Classification(
            issue, lc, 'gate_remaining', f'remaining: {kinds}',
        )
    return Classification(issue, lc, 'closeable', 'no remaining gates')


def load_verification_report(
    path: Path,
) -> dict[int, tuple[str, ...]]:
    """Per-issue automated check statuses from an
    ``issue_verification_report.json`` written by
    ``scripts/verify_open_issues.py``."""
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f'{path}: report unreadable: {exc}')
    if not isinstance(raw, dict):
        _fail(f'{path}: report must be a JSON object')
    statuses: dict[int, tuple[str, ...]] = {}
    for item in raw.get('issues') or []:
        if not isinstance(item, dict):
            continue
        issue = item.get('issue')
        if not isinstance(issue, int):
            continue
        statuses[issue] = tuple(
            str(check.get('status'))
            for check in item.get('checks') or []
            if isinstance(check, dict) and check.get('status')
        )
    return statuses


def render_report(
    entries: dict[int, LifecycleEntry],
    classifications: list[Classification],
    findings: list[Finding],
    *,
    github_supplied: bool,
    checks_supplied: bool = False,
) -> str:
    lines = [
        '# Issue lifecycle report (#848)',
        '',
        'Canonical lifecycle metadata — separate from GitHub open/closed.',
        f'GitHub metadata: {"supplied" if github_supplied else "not supplied (offline)"}',
        f'Verification check statuses: {"supplied" if checks_supplied else "not supplied (checks-red bucket cannot trigger)"}',
        '',
        '## Buckets',
        '',
    ]
    buckets: dict[str, list[Classification]] = {}
    for cls in classifications:
        buckets.setdefault(cls.bucket, []).append(cls)
    order = (
        'implementation_missing', 'implementation_present_checks_red',
        'gate_remaining', 'closeable', 'research_only', 'unclassified',
        'closed',
    )
    for bucket in order:
        items = buckets.get(bucket) or []
        lines.append(f'### {bucket} ({len(items)})')
        lines.append('')
        for cls in sorted(items, key=lambda c: c.issue):
            lines.append(f'- #{cls.issue} — {cls.detail}')
        lines.append('')
    lines.append('## Drift findings')
    lines.append('')
    if not findings:
        lines.append('- none')
    for f in sorted(findings, key=lambda f: (f.severity, f.issue, f.code)):
        lines.append(f'- [{f.severity}] #{f.issue} {f.code}: {f.message}')
    lines.append('')
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lifecycle-manifest', type=Path,
                        default=DEFAULT_LIFECYCLE)
    parser.add_argument('--verification-manifest', type=Path,
                        default=DEFAULT_VERIFICATION)
    parser.add_argument('--issues-json', type=Path, default=None,
                        help='pre-fetched GitHub issues JSON (offline input)')
    parser.add_argument('--verification-report', type=Path, default=None,
                        help='issue_verification_report.json from '
                        'verify_open_issues.py — supplies per-issue '
                        'automated check statuses for the checks-red '
                        'bucket')
    parser.add_argument('--report', type=Path, default=None)
    parser.add_argument('--json', dest='json_out', type=Path, default=None)
    args = parser.parse_args(argv)

    entries = load_lifecycle_manifest(args.lifecycle_manifest)
    verification = load_verification_issues(args.verification_manifest)

    github_states = None
    if args.issues_json is not None:
        raw_issues = json.loads(args.issues_json.read_text(encoding='utf-8'))
        github_states = {
            item['number']: item.get('state', 'open')
            for item in raw_issues if isinstance(item.get('number'), int)
        }

    check_statuses: dict[int, tuple[str, ...]] = {}
    if args.verification_report is not None:
        check_statuses = load_verification_report(args.verification_report)

    findings = drift_findings(entries, verification, github_states)
    classifications = []
    for issue in sorted(set(entries) | set(verification)):
        entry = entries.get(issue)
        state = (github_states or {}).get(issue)
        if entry is None:
            bucket = 'closed' if state == 'closed' else 'unclassified'
            detail = 'GitHub closed' if state == 'closed' else 'no lifecycle entry'
            classifications.append(Classification(issue, None, bucket, detail))
            continue
        classifications.append(classify_issue(
            entry,
            check_statuses=check_statuses.get(issue, ()),
            github_state=state,
        ))
    report = render_report(
        entries, classifications, findings,
        github_supplied=github_states is not None,
        checks_supplied=args.verification_report is not None,
    )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding='utf-8')
    else:
        print(report)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps({
            'classifications': [
                {'issue': c.issue, 'lifecycle': c.lifecycle,
                 'bucket': c.bucket, 'detail': c.detail}
                for c in classifications
            ],
            'findings': [
                {'severity': f.severity, 'issue': f.issue,
                 'code': f.code, 'message': f.message}
                for f in findings
            ],
        }, ensure_ascii=False, indent=2), encoding='utf-8')
    errors = [f for f in findings if f.severity == 'error']
    if errors:
        print(f'{len(errors)} error finding(s); see report', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
