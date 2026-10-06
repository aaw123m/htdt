"""Commit manifest-verification results into the sealed gate ledger.

Reads ``issue_verification_report.json`` produced by
``scripts/verify_open_issues.py``, resolves each reported check to the
sealed ``ManifestGate`` that ``cad_manifest_verification`` derives from
the manifest, and appends a ``GateRunResult`` per reported check into the
scene's append-only store — turning a script run into evidence-bound
records instead of a transient report file.

Usage:

    python scripts/commit_manifest_verification.py
        --scene <path/to/project.htdtscene>
        --document-id <document id>
        [--manifest scripts/issue_verification_manifest.yaml]
        [--report <report-dir>/issue_verification_report.json]
        [--dry-run]

``--dry-run`` evaluates without committing. Exit status: 0 ok,
2 bad args / unreadable inputs, 3 integrity/commit failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# The backend is a src-layout package; scripts import it by adding the
# src directory — same convention as the other repo scripts.
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / 'backend' / 'src')
)

from htdt.cad_authority_resolver import AuthorityRef  # noqa: E402
from htdt.cad_manifest_gate_repository import (  # noqa: E402
    CadManifestGateRepository,
)
from htdt.cad_manifest_verification import (  # noqa: E402
    GateRunResult,
    evaluate_issue_verdict,
    load_manifest_gates,
)
from htdt.cad_repository import SceneRepository  # noqa: E402

_OUTCOME_MAP = {
    'passed': 'passed',
    'failed': 'failed',
    'timeout': 'timeout',
    'error': 'error',
}

_MANIFEST_DEFAULT = Path(
    __file__).resolve().parent / 'issue_verification_manifest.yaml'


def _fail(message: str) -> 'NoReturn':
    print(f'[commit] error: {message}', file=sys.stderr)
    raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Commit manifest-verification results into the '
        'sealed gate ledger.'
    )
    parser.add_argument('--scene', required=True, help='.htdtscene path')
    parser.add_argument(
        '--document-id', required=True,
        help='document id the gates belong to')
    parser.add_argument(
        '--manifest', default=str(_MANIFEST_DEFAULT),
        help='manifest path (must match the bytes the gates pin)')
    parser.add_argument(
        '--report', required=True,
        help='issue_verification_report.json path')
    parser.add_argument(
        '--dry-run', action='store_true',
        help='evaluate only; commit nothing')
    args = parser.parse_args(argv)

    manifest = Path(args.manifest)
    report_path = Path(args.report)
    if not manifest.is_file():
        _fail(f'manifest not found: {manifest}')
    if not report_path.is_file():
        _fail(f'report not found: {report_path}')

    try:
        report = json.loads(report_path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        _fail(f'report is not valid JSON: {exc}')

    gates = load_manifest_gates(args.document_id, manifest)
    by_key = {
        (g.issue_ref, g.check_id): g for g in gates
    }

    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    results = []
    skipped_manual = 0
    unknown_checks = []
    for issue in report.get('issues') or []:
        issue_ref = f"issue-{issue.get('issue')}"
        for check in issue.get('checks') or []:
            outcome = _OUTCOME_MAP.get(check.get('status'))
            if outcome is None:
                skipped_manual += 1
                continue
            gate = by_key.get((issue_ref, check.get('id')))
            if gate is None:
                unknown_checks.append(
                    f"{issue_ref}/{check.get('id')}")
                continue
            results.append(GateRunResult.create({
                'document_id': args.document_id,
                'gate_ref': AuthorityRef(
                    kind='manifest_gate',
                    ref_id=gate.gate_id,
                    ref_sha256=gate.gate_sha256,
                ),
                'outcome': outcome,
                'finished_at_utc': now,
                'detail': (check.get('detail') or '')[:500],
            }))

    if args.dry_run:
        repo = None
    else:
        repo = CadManifestGateRepository(
            SceneRepository(Path(args.scene)))
        for gate in gates:
            repo.save_manifest_gate(gate)
        for result in results:
            repo.save_gate_run_result(result)

    # verdict summary per issue
    for issue in report.get('issues') or []:
        issue_ref = f"issue-{issue.get('issue')}"
        issue_gates = tuple(
            g for g in gates if g.issue_ref == issue_ref
        )
        if not issue_gates:
            continue
        if repo is not None:
            latest = {}
            for g in issue_gates:
                rows = [
                    r for r in repo.list_gate_run_results()
                    if r.gate_ref.ref_id == g.gate_id
                ]
                if rows:
                    latest[g.gate_id] = rows[-1]
        else:
            latest = {
                r.gate_ref.ref_id: r for r in results
                if r.gate_ref.ref_id in
                {g.gate_id for g in issue_gates}
            }
        verdict, reason = evaluate_issue_verdict(
            issue_gates, latest
        )
        print(
            f"[commit] {issue_ref}: {verdict} — {reason}"
        )

    if unknown_checks:
        print(
            '[commit] unmapped checks (no sealed gate): '
            + ', '.join(unknown_checks),
            file=sys.stderr,
        )
    print(
        f'[commit] committed {len(results)} gate results'
        f'{" (dry-run)" if args.dry_run else ""}; '
        f'{skipped_manual} manual checks left for the wizard'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
