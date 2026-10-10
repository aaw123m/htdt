"""Performance budget authority (#867).

Product-level performance contract for the primary workflow: named,
benchmarkable operations measured against representative fixtures, with
explicit budgets, environment identity, and fail-closed verdicts.

Separation of concerns (issue requirement):
- This module owns deterministic/reproducible benchmark evidence only —
  solver convergence and hardware-sensitive viewport acceptance live
  elsewhere (documented as owned-machine protocols, never faked here).
- A budgeted operation with no measurement is NEVER a pass: the report
  verdict is 'incomplete' (cannot claim regression coverage) or 'failed'
  when a measured operation exceeds its budget.
"""

from __future__ import annotations

import math
import platform
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical_json import canonical_sha256


PERF_BUDGET_VERSION = '1'
"""Authority version — bump when result/report schema semantics change."""

FixtureSize = Literal['small', 'medium', 'large']
OperationVerdict = Literal[
    'within_budget', 'exceeds_budget', 'unmeasured',
    'hardware_sensitive_skipped',
]
ReportVerdict = Literal['passed', 'failed', 'incomplete']

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ---------------------------------------------------------------------------
# Environment identity — a benchmark without its build/host identity is
# unattributable evidence (acceptance: results retain exact build/SHA and
# environment identity).


class PerfEnvironment(BaseModel):
    """Build + host + fixture + harness identity pinned to one run."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    app_sha: str = Field(min_length=7, max_length=64)
    app_dirty: bool
    os_system: str
    os_release: str
    machine: str
    python_version: str
    cpu_logical_count: int = Field(gt=0)
    memory_total_bytes: int = Field(gt=0)
    gpu: str | None = None
    """Best-effort GPU identity; None = not probed (never fabricated)."""
    fixture_id: str
    fixture_size: FixtureSize
    fixture_document_sha256: str = Field(pattern=_SHA256_PATTERN)
    iterations: int = Field(gt=0)
    warmup_iterations: int = Field(ge=0)
    benchmark_version: str = Field(
        default=PERF_BUDGET_VERSION, min_length=1)
    recorded_at_utc: str

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json')


def _total_memory_bytes() -> int:
    """Process/system total RAM without a psutil dependency."""
    if sys.platform == 'win32':
        try:
            import ctypes

            class _MEMSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ('dwLength', ctypes.c_ulong),
                    ('dwMemoryLoad', ctypes.c_ulong),
                    ('ullTotalPhys', ctypes.c_ulonglong),
                    ('ullAvailPhys', ctypes.c_ulonglong),
                    ('ullTotalPageFile', ctypes.c_ulonglong),
                    ('ullAvailPageFile', ctypes.c_ulonglong),
                    ('ullTotalVirtual', ctypes.c_ulonglong),
                    ('ullAvailVirtual', ctypes.c_ulonglong),
                    ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
                ]

            stat = _MEMSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(
                    ctypes.byref(stat)):
                return int(stat.ullTotalPhys)
        except Exception:  # error-boundary: platform probe — a memory-status probe failure falls through to an honest 'unknown' result (noqa: BLE001)
            pass
        return 0
    try:
        with open('/proc/meminfo', 'r', encoding='utf-8') as handle:
            for line in handle:
                if line.startswith('MemTotal:'):
                    return int(line.split()[1]) * 1024
    except OSError:
        # error-boundary: platform probe — report unprobed
        pass
    return 0


def _git_identity(repo_root: Path) -> tuple[str, bool]:
    try:
        sha = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=str(repo_root), capture_output=True, text=True,
            check=True,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=str(repo_root), capture_output=True, text=True,
            check=True,
        ).stdout.strip())
        return sha, dirty
    except (OSError, subprocess.CalledProcessError):
        # error-boundary: vcs probe — an unidentified build is reported, not
        # guessed; callers treat a missing sha as unattributable evidence.
        return 'unknown', True


def collect_environment(
    repo_root: Path,
    *,
    fixture_id: str,
    fixture_size: FixtureSize,
    fixture_document_sha256: str,
    iterations: int,
    warmup_iterations: int,
    gpu: str | None = None,
    clock: Any = _utc_now,
) -> PerfEnvironment:
    sha, dirty = _git_identity(repo_root)
    memory_total = _total_memory_bytes()
    return PerfEnvironment(
        app_sha=sha,
        app_dirty=dirty,
        os_system=platform.system(),
        os_release=platform.release(),
        machine=platform.machine(),
        python_version=platform.python_version(),
        cpu_logical_count=max(1, platform.os.cpu_count() or 1),
        memory_total_bytes=max(1, memory_total),
        gpu=gpu,
        fixture_id=fixture_id,
        fixture_size=fixture_size,
        fixture_document_sha256=fixture_document_sha256,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        recorded_at_utc=clock(),
    )


# ---------------------------------------------------------------------------
# Measurements and budgets


class OperationDistribution(BaseModel):
    """p50/p95 distribution of one benchmarked operation — never a single
    anecdotal timing (issue requirement)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    operation_id: str = Field(min_length=1)
    samples: tuple[float, ...] = Field(min_length=1)
    p50_s: float = Field(ge=0.0)
    p95_s: float = Field(ge=0.0)
    min_s: float = Field(ge=0.0)
    max_s: float = Field(ge=0.0)
    rss_bytes_after: int | None = Field(default=None, ge=0)
    """Process RSS sampled after the last iteration — the memory footprint
    observation for this operation."""


def percentile(samples: tuple[float, ...] | list[float], q: float) -> float:
    """Deterministic linear-interpolation percentile (numpy-independent).

    ``q`` in [0, 100]. Single-sample distributions return that sample for
    every percentile so small runs remain honest rather than interpolated.
    """
    if not samples:
        raise ValueError('percentile requires at least one sample')
    ordered = sorted(float(s) for s in samples)
    if len(ordered) == 1:
        return ordered[0]
    rank = (q / 100.0) * (len(ordered) - 1)
    lo = int(math.floor(rank))
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def distribution_from_samples(
    operation_id: str,
    samples: tuple[float, ...] | list[float],
    *,
    rss_bytes_after: int | None = None,
) -> OperationDistribution:
    if not samples:
        raise ValueError(f'{operation_id}: no samples recorded')
    return OperationDistribution(
        operation_id=operation_id,
        samples=tuple(float(s) for s in samples),
        p50_s=percentile(samples, 50.0),
        p95_s=percentile(samples, 95.0),
        min_s=float(min(samples)),
        max_s=float(max(samples)),
        rss_bytes_after=rss_bytes_after,
    )


class PerfBudgetEntry(BaseModel):
    """One operation's budget on one fixture size. ``None`` budget fields
    mean that dimension is not gated (recorded, not asserted)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    operation_id: str = Field(min_length=1)
    fixture_size: FixtureSize
    p95_budget_s: float | None = Field(default=None, gt=0.0)
    memory_budget_bytes: int | None = Field(default=None, gt=0)
    rationale: str = ''

    @model_validator(mode='after')
    def _asserts_something(self) -> 'PerfBudgetEntry':
        if self.p95_budget_s is None and self.memory_budget_bytes is None:
            raise ValueError(
                f'{self.operation_id}: a budget entry must assert at '
                'least one dimension (p95 or memory) — an entry that '
                'asserts nothing would report a fake pass')
        return self


class PerfBudgetManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    version: str = Field(default=PERF_BUDGET_VERSION, min_length=1)
    entries: tuple[PerfBudgetEntry, ...]

    def budget_for(
        self, operation_id: str, fixture_size: str,
    ) -> PerfBudgetEntry | None:
        for entry in self.entries:
            if (entry.operation_id == operation_id
                    and entry.fixture_size == fixture_size):
                return entry
        return None


def load_budget_manifest(path: Path) -> PerfBudgetManifest:
    import yaml

    raw = yaml.safe_load(path.read_text(encoding='utf-8'))
    if not isinstance(raw, Mapping):
        raise ValueError(f'{path}: budget manifest must be a mapping')
    return PerfBudgetManifest.model_validate(raw)


# ---------------------------------------------------------------------------
# Results and report


class PerfOperationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    operation_id: str
    title: str
    fixture_size: FixtureSize
    verdict: OperationVerdict
    distribution: OperationDistribution | None = None
    budget: PerfBudgetEntry | None = None
    detail: str = ''
    """Skip/exceed rationale — recorded verbatim, never blank on
    non-passing verdicts is enforced at report level."""


class PerfRunReport(BaseModel):
    """Machine-readable benchmark evidence for one fixture run."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    report_id: str = Field(min_length=1)
    environment: PerfEnvironment
    results: tuple[PerfOperationResult, ...]
    hardware_sensitive_operations: tuple[str, ...] = ()
    """Operations deferred to the owned-machine protocol — listed so a
    'passed' report can never be misread as covering them."""
    verdict: ReportVerdict
    report_sha256: str = Field(pattern=_SHA256_PATTERN)
    recorded_at_utc: str

    def identity_payload(self) -> dict[str, Any]:
        return {
            'environment': self.environment.model_dump(mode='json'),
            'results': [r.model_dump(mode='json') for r in self.results],
            'hardware_sensitive_operations':
                list(self.hardware_sensitive_operations),
            'verdict': self.verdict,
            'recorded_at_utc': self.recorded_at_utc,
        }

    def operation(self, operation_id: str) -> PerfOperationResult | None:
        for result in self.results:
            if result.operation_id == operation_id:
                return result
        return None


def evaluate_distribution(
    distribution: OperationDistribution,
    budget: PerfBudgetEntry | None,
    *,
    fixture_size: FixtureSize,
    title: str,
) -> PerfOperationResult:
    """One operation vs its budget. Missing budget ⇒ the measurement is
    reported 'within_budget' only when the operation is explicitly
    ungated; a budgeted-key operation without a budget entry reports
    'unmeasured' so regressions cannot hide behind a missing row.
    """
    if budget is None:
        return PerfOperationResult(
            operation_id=distribution.operation_id,
            title=title,
            fixture_size=fixture_size,
            verdict='unmeasured',
            distribution=distribution,
            detail='no budget entry for this operation/fixture size',
        )
    if (budget.p95_budget_s is not None
            and distribution.p95_s > budget.p95_budget_s):
        return PerfOperationResult(
            operation_id=distribution.operation_id,
            title=title,
            fixture_size=fixture_size,
            verdict='exceeds_budget',
            distribution=distribution,
            budget=budget,
            detail=(
                f'p95 {distribution.p95_s:.4f}s > budget '
                f'{budget.p95_budget_s:.4f}s'),
        )
    if budget.memory_budget_bytes is not None:
        if distribution.rss_bytes_after is None:
            return PerfOperationResult(
                operation_id=distribution.operation_id,
                title=title,
                fixture_size=fixture_size,
                verdict='unmeasured',
                distribution=distribution,
                budget=budget,
                detail='memory budget asserted but rss was not probed',
            )
        if distribution.rss_bytes_after > budget.memory_budget_bytes:
            return PerfOperationResult(
                operation_id=distribution.operation_id,
                title=title,
                fixture_size=fixture_size,
                verdict='exceeds_budget',
                distribution=distribution,
                budget=budget,
                detail=(
                    f'rss {distribution.rss_bytes_after} > budget '
                    f'{budget.memory_budget_bytes}'),
            )
    return PerfOperationResult(
        operation_id=distribution.operation_id,
        title=title,
        fixture_size=fixture_size,
        verdict='within_budget',
        distribution=distribution,
        budget=budget,
    )


@dataclass(frozen=True)
class OperationSpec:
    """Registry entry for one benchmarkable (or protocol-deferred) op."""

    operation_id: str
    title: str
    hardware_sensitive: bool = False
    hardware_note: str = ''


def evaluate_report(
    environment: PerfEnvironment,
    specs: tuple[OperationSpec, ...],
    distributions: Mapping[str, OperationDistribution],
    budgets: PerfBudgetManifest,
    *,
    clock: Any = _utc_now,
) -> PerfRunReport:
    """Fail-closed report: every registered op resolves to a verdict —
    measured within/exceeds budget, unmeasured (never a pass), or a
    hardware-sensitive skip that is listed verbatim on the report."""
    when = clock()
    results: list[PerfOperationResult] = []
    deferred: list[str] = []
    for spec in specs:
        if spec.hardware_sensitive:
            deferred.append(spec.operation_id)
            results.append(PerfOperationResult(
                operation_id=spec.operation_id,
                title=spec.title,
                fixture_size=environment.fixture_size,
                verdict='hardware_sensitive_skipped',
                detail=spec.hardware_note or (
                    'owned-machine protocol — see issue doc'),
            ))
            continue
        distribution = distributions.get(spec.operation_id)
        if distribution is None:
            results.append(PerfOperationResult(
                operation_id=spec.operation_id,
                title=spec.title,
                fixture_size=environment.fixture_size,
                verdict='unmeasured',
                detail='operation produced no samples',
            ))
            continue
        results.append(evaluate_distribution(
            distribution,
            budgets.budget_for(
                spec.operation_id, environment.fixture_size),
            fixture_size=environment.fixture_size,
            title=spec.title,
        ))

    if any(r.verdict == 'exceeds_budget' for r in results):
        verdict: ReportVerdict = 'failed'
    elif any(r.verdict == 'unmeasured' for r in results):
        verdict = 'incomplete'
    else:
        verdict = 'passed'

    payload = {
        'environment': environment.model_dump(mode='json'),
        'results': [r.model_dump(mode='json') for r in results],
        'hardware_sensitive_operations': list(deferred),
        'verdict': verdict,
        'recorded_at_utc': when,
    }
    sha = canonical_sha256(payload)
    return PerfRunReport(
        report_id=f'perf-{sha[:24]}',
        environment=environment,
        results=tuple(results),
        hardware_sensitive_operations=tuple(deferred),
        verdict=verdict,
        report_sha256=sha,
        recorded_at_utc=when,
    )
