"""HTDT performance benchmark harness (issue #867).

Runs the named benchmarkable operations of ``htdt.perf_harness`` against a
deterministic representative fixture and emits machine-readable evidence
(report JSON + human-readable Markdown). With ``--check`` the report is
evaluated against ``scripts/perf_budgets.yaml`` and a regression exits 2.

Usage:
    python scripts/perf_benchmark.py --fixture-size small
    python scripts/perf_benchmark.py --fixture-size medium --iterations 9 \\
        --check --out artifacts/perf
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'backend' / 'src'))

from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.perf_budget import (  # noqa: E402
    collect_environment,
    evaluate_report,
    load_budget_manifest,
)
from htdt.perf_fixtures import ensure_fixture  # noqa: E402
from htdt.perf_harness import (  # noqa: E402
    OPERATIONS,
    BenchmarkContext,
    run_benchmark,
)

BUDGETS_PATH = REPO_ROOT / 'scripts' / 'perf_budgets.yaml'
DEFAULT_OUT = REPO_ROOT / 'artifacts' / 'perf'
FIXTURES_ROOT = DEFAULT_OUT / 'fixtures'


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--fixture-size', choices=('small', 'medium', 'large'),
        default='small')
    parser.add_argument('--iterations', type=int, default=7)
    parser.add_argument('--warmup', type=int, default=1)
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT)
    parser.add_argument('--fixtures-root', type=Path, default=FIXTURES_ROOT)
    parser.add_argument('--budgets', type=Path, default=BUDGETS_PATH)
    parser.add_argument(
        '--check', action='store_true',
        help='evaluate against the budget manifest; regressions exit 2')
    parser.add_argument(
        '--operations', nargs='*', default=None,
        help='restrict to named operations (debugging only)')
    args = parser.parse_args()

    fixture = ensure_fixture(args.fixtures_root, args.fixture_size)
    database_path = Path(fixture['fixture_dir']) / fixture['database']
    document_id = str(fixture['document_id'])
    repository = SceneRepository(database_path)
    head = repository.current_head(document_id)
    first_revision_id = head.revision_id
    while True:
        revision = repository.get(first_revision_id)
        if revision.parent_revision_id is None:
            break
        first_revision_id = revision.parent_revision_id
    head_revision_id = head.revision_id

    errors: list[tuple[str, str]] = []

    def _on_error(operation_id: str, exc: BaseException) -> None:
        errors.append((operation_id, f'{type(exc).__name__}: {exc}'))

    context = BenchmarkContext(
        repo_root=REPO_ROOT,
        fixture_dir=Path(fixture['fixture_dir']),
        database_path=database_path,
        document_id=document_id,
        head_revision_id=head_revision_id,
        first_revision_id=first_revision_id,
    )
    distributions = run_benchmark(
        context,
        iterations=args.iterations,
        warmup=args.warmup,
        only=tuple(args.operations) if args.operations else None,
        on_error=_on_error,
    )

    environment = collect_environment(
        REPO_ROOT,
        fixture_id=str(fixture['fixture_id']),
        fixture_size=args.fixture_size,
        fixture_document_sha256=str(fixture['document_sha256']),
        iterations=args.iterations,
        warmup_iterations=args.warmup,
    )
    if args.budgets.exists():
        budgets = load_budget_manifest(args.budgets)
    elif args.check:
        print(f'error: budget manifest missing: {args.budgets}',
              file=sys.stderr)
        return 2
    else:
        from htdt.perf_budget import PerfBudgetManifest
        budgets = PerfBudgetManifest(entries=())
    report = evaluate_report(
        environment, OPERATIONS, distributions, budgets)

    for operation_id, detail in errors:
        print(f'  [unmeasured] {operation_id}: {detail}', file=sys.stderr)

    args.out.mkdir(parents=True, exist_ok=True)
    report_path = args.out / f'perf-report-{args.fixture_size}.json'
    report_path.write_text(
        json.dumps(report.model_dump(mode='json'), indent=2),
        encoding='utf-8')
    md_path = args.out / f'perf-report-{args.fixture_size}.md'
    md_path.write_text(_markdown(report), encoding='utf-8')
    print(f'report: {report_path}')
    print(f'markdown: {md_path}')
    print(f'verdict: {report.verdict}')
    for result in report.results:
        distribution = result.distribution
        line = (
            f'  {result.operation_id}: {result.verdict}'
            + (f' p50={distribution.p50_s:.3f}s p95={distribution.p95_s:.3f}s'
               f' n={len(distribution.samples)}'
               + (f' rss={distribution.rss_bytes_after // 1024}KiB'
                  if distribution.rss_bytes_after else '')
               if distribution else '')
            + (f' — {result.detail}' if result.detail else ''))
        print(line)

    if args.check:
        return 0 if report.verdict == 'passed' else 2
    return 0


def _markdown(report) -> str:  # noqa: ANN001 — pydantic model
    env = report.environment
    lines = [
        f'# Performance report — {env.fixture_id} ({env.fixture_size})',
        '',
        f'- verdict: **{report.verdict}**',
        f'- app sha: `{env.app_sha}` (dirty={env.app_dirty})',
        f'- fixture document sha: `{env.fixture_document_sha256}`',
        f'- host: {env.os_system} {env.os_release} {env.machine}, '
        f'{env.cpu_logical_count} cpus, '
        f'{env.memory_total_bytes // (1024**3)} GiB RAM, '
        f'gpu={env.gpu or "unprobed"}',
        f'- python: {env.python_version}; iterations={env.iterations} '
        f'(+{env.warmup_iterations} warmup)',
        '',
        '| operation | verdict | p50 (s) | p95 (s) | n | rss (KiB) | detail |',
        '|---|---|---|---|---|---|---|',
    ]
    for result in report.results:
        distribution = result.distribution
        lines.append(
            '| {op} | {verdict} | {p50} | {p95} | {n} | {rss} | {detail} |'
            .format(
                op=result.operation_id,
                verdict=result.verdict,
                p50=f'{distribution.p50_s:.3f}' if distribution else '—',
                p95=f'{distribution.p95_s:.3f}' if distribution else '—',
                n=len(distribution.samples) if distribution else '—',
                rss=(distribution.rss_bytes_after // 1024
                     if distribution and distribution.rss_bytes_after
                     else '—'),
                detail=result.detail or '',
            ))
    if report.hardware_sensitive_operations:
        lines += [
            '',
            '## Hardware-sensitive operations (owned-machine protocol)',
            '',
        ]
        for operation_id in report.hardware_sensitive_operations:
            lines.append(f'- `{operation_id}` — see docs/issues/'
                         'issue-867-performance-budget.md')
    lines += ['', f'report sha256: `{report.report_sha256}`', '']
    return '\n'.join(lines)


if __name__ == '__main__':
    raise SystemExit(_main())
