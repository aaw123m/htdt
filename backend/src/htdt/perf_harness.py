"""Benchmarkable operations for the #867 performance budget.

Each operation is a named, deterministic measurement over a representative
fixture — distributions (p50/p95), never a single timing. Operations the
host cannot run raise; the runner records them as harness errors so the
report stays 'incomplete' rather than silently green.
"""

from __future__ import annotations

import gc
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .cad_repository import SceneRepository
from .perf_budget import OperationSpec, OperationDistribution, distribution_from_samples


# ---------------------------------------------------------------------------
# Process memory probe (psutil-free)


def process_rss_bytes() -> int | None:
    """Resident set size of this process, or None when unprobed."""
    if sys.platform == 'win32':
        try:
            import ctypes
            import ctypes.wintypes as wintypes

            class _MEM_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ('cb', wintypes.DWORD),
                    ('PageFaultCount', wintypes.DWORD),
                    ('PeakWorkingSetSize', ctypes.c_size_t),
                    ('WorkingSetSize', ctypes.c_size_t),
                    ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                    ('PagefileUsage', ctypes.c_size_t),
                    ('PeakPagefileUsage', ctypes.c_size_t),
                ]

            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.GetCurrentProcess.argtypes = []
            psapi = ctypes.windll.psapi
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(_MEM_COUNTERS),
                wintypes.DWORD,
            ]
            counters = _MEM_COUNTERS()
            counters.cb = ctypes.sizeof(_MEM_COUNTERS)
            if psapi.GetProcessMemoryInfo(
                    kernel32.GetCurrentProcess(),
                    ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
        except Exception:
            # error-boundary: platform probe — return unprobed
            return None
        return None
    try:
        with open('/proc/self/statm', 'r', encoding='utf-8') as handle:
            return int(handle.read().split()[1]) * os.sysconf('SC_PAGE_SIZE')
    except (OSError, IndexError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Operation registry


@dataclass
class BenchmarkContext:
    """Everything an operation may use across iterations. Mutable so a
    single-shot op can hand back an RSS observation via ``rss_sample``."""

    repo_root: Path
    fixture_dir: Path
    database_path: Path
    document_id: str
    head_revision_id: str
    first_revision_id: str
    iteration: int = 0


OperationFn = Callable[[BenchmarkContext], Any]


def _mutate_document(document: Any, dx: float) -> Any:
    """Move the first entity by dx — a minimal real document mutation."""
    entity = document.entities[0].model_copy(
        update={'position': document.entities[0].position.model_copy(
            update={'x_m': document.entities[0].position.x_m + dx})})
    return document.model_copy(
        update={'entities': (entity,) + document.entities[1:]})


# --- individual operations -------------------------------------------------


def _startup_subprocess(repo_root: Path) -> float:
    env = dict(os.environ)
    env['PYTHONPATH'] = str(repo_root / 'backend' / 'src')
    env['PYTHONIOENCODING'] = 'utf-8'
    env.setdefault('QT_QPA_PLATFORM', 'offscreen')
    start = time.perf_counter()
    subprocess.run(
        [
            sys.executable, '-X', 'utf8', '-c',
            'import htdt.workflow_application',
        ],
        cwd=str(repo_root), env=env, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return time.perf_counter() - start


def op_application_startup_cold(ctx: BenchmarkContext) -> float:
    # First subprocess in this harness run — the coldest observation the
    # harness can honestly report (OS page cache may already be warm).
    return _startup_subprocess(ctx.repo_root)


def op_application_startup_warm(ctx: BenchmarkContext) -> float:
    return _startup_subprocess(ctx.repo_root)


def op_project_open(ctx: BenchmarkContext) -> float:
    start = time.perf_counter()
    repository = SceneRepository(ctx.database_path)
    head = repository.current_head(ctx.document_id)
    if head is not None:
        repository.get(head.revision_id)
    release = getattr(repository, 'release_read_handles_under', None)
    if release is not None:
        release()
    return time.perf_counter() - start


def op_project_save(ctx: BenchmarkContext) -> float:
    repository = SceneRepository(ctx.database_path)
    start = time.perf_counter()
    head = repository.current_head(ctx.document_id)
    document = repository.get(head.revision_id).document
    repository.save(
        _mutate_document(document, 0.001 * (ctx.iteration + 1)),
        parent_revision_id=head.revision_id,
    )
    return time.perf_counter() - start


def op_project_reopen(ctx: BenchmarkContext) -> float:
    return op_project_open(ctx)


def op_scene_revision_apply(ctx: BenchmarkContext) -> float:
    # Cold-cache revision application: fresh repository handle, load the
    # first revision, walk every entity — the scene-apply data path.
    start = time.perf_counter()
    repository = SceneRepository(ctx.database_path)
    revision = repository.get(ctx.first_revision_id)
    count = sum(1 for _ in revision.document.entities)
    release = getattr(repository, 'release_read_handles_under', None)
    if release is not None:
        release()
    assert count > 0
    return time.perf_counter() - start


_NON_VTK_WORKSPACES = ('overview', 'measurement', 'optimization',
                       'presentation')
"""Room/video workspaces own VTK surfaces — they are excluded here and
covered by the hardware-sensitive viewport protocol, not silently
skipped: the excluded ids are recorded in the operation detail."""


def _composition(ctx: BenchmarkContext):
    from .workflow_application import WorkflowApplicationComposition

    repository = SceneRepository(ctx.database_path)
    return WorkflowApplicationComposition(repository, ctx.document_id)


def _close_composition(composition, app) -> None:
    composition.shell.router.shutdown()
    composition.shell.close()
    app.processEvents()


def op_workspace_first_mount(ctx: BenchmarkContext) -> float:
    """First lazy-mount of every non-VTK workspace (one composition,
    untimed setup apart from the mounts themselves)."""
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    composition = _composition(ctx)
    start = time.perf_counter()
    mounted = 0
    for workspace_id in _NON_VTK_WORKSPACES:
        if composition.shell.router.navigate(workspace_id) is not None:
            mounted += 1
    elapsed = time.perf_counter() - start
    assert mounted == len(_NON_VTK_WORKSPACES)
    _close_composition(composition, app)
    return elapsed


def op_workspace_transition(ctx: BenchmarkContext) -> float:
    """Steady-state workspace transition — navigate a cycle across
    workspaces that are already mounted (what an operator pays per
    switch, not the one-time mount cost)."""
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    composition = _composition(ctx)
    router = composition.shell.router
    for workspace_id in _NON_VTK_WORKSPACES:
        router.navigate(workspace_id)
    start = time.perf_counter()
    for workspace_id in _NON_VTK_WORKSPACES:
        router.navigate(workspace_id)
    elapsed = time.perf_counter() - start
    _close_composition(composition, app)
    return elapsed


def op_measurement_plot_load(ctx: BenchmarkContext) -> float:
    """Measurement-workspace model load — controller + view materialization
    (data layer; rasterization is hardware-sensitive)."""
    from .measurement_workflow import MeasurementWorkflowController

    repository = SceneRepository(ctx.database_path)
    start = time.perf_counter()
    controller = MeasurementWorkflowController(
        repository, ctx.document_id)
    controller.measurement_views()
    return time.perf_counter() - start


def op_comparison_view_prep(ctx: BenchmarkContext) -> float:
    """Candidate comparison prep — entity-level diff of head vs first
    revision (the comparison view's data path)."""
    repository = SceneRepository(ctx.database_path)
    start = time.perf_counter()
    head = repository.get(ctx.head_revision_id)
    first = repository.get(ctx.first_revision_id)
    first_map = {e.entity_id: e for e in first.document.entities}
    changed = [
        e.entity_id for e in head.document.entities
        if first_map.get(e.entity_id) != e
    ]
    release = getattr(repository, 'release_read_handles_under', None)
    if release is not None:
        release()
    assert changed or first.document.entities
    return time.perf_counter() - start


def op_memory_idle_rss(ctx: BenchmarkContext) -> float:
    # Single-shot RSS observation after imports + repository open.
    repository = SceneRepository(ctx.database_path)
    repository.current_head(ctx.document_id)
    start = time.perf_counter()
    rss = process_rss_bytes()
    del repository
    gc.collect()
    ctx.rss_sample = rss  # type: ignore[attr-defined]
    return time.perf_counter() - start


def op_memory_loaded_rss(ctx: BenchmarkContext) -> float:
    repository = SceneRepository(ctx.database_path)
    revision = repository.get(ctx.head_revision_id)
    for entity in revision.document.entities:
        _ = entity.entity_id
    start = time.perf_counter()
    rss = process_rss_bytes()
    del repository
    ctx.rss_sample = rss  # type: ignore[attr-defined]
    return time.perf_counter() - start


def op_shutdown_close(ctx: BenchmarkContext) -> float:
    repository = SceneRepository(ctx.database_path)
    repository.current_head(ctx.document_id)
    start = time.perf_counter()
    release = getattr(repository, 'release_read_handles_under', None)
    if release is not None:
        release()
    del repository
    gc.collect()
    return time.perf_counter() - start


OPERATIONS: tuple[OperationSpec, ...] = (
    OperationSpec(
        'application_startup_cold',
        'Cold application startup (first measured subprocess import)',
    ),
    OperationSpec(
        'application_startup_warm',
        'Warm application startup (subsequent subprocess import)',
    ),
    OperationSpec('project_open', 'Project open (repository init + head load)'),
    OperationSpec('project_save', 'Project save (head revision write)'),
    OperationSpec('project_reopen', 'Project reopen (second open + load)'),
    OperationSpec('scene_revision_apply',
                  'Scene/revision application (cold-cache load + entity walk)'),
    OperationSpec(
        'workspace_first_mount',
        'First lazy-mount of all non-VTK workspaces (one-time cost)',
    ),
    OperationSpec(
        'workspace_transition',
        'Workspace transition (steady-state navigation across '
        'already-mounted non-VTK workspaces; room/video covered by the '
        'viewport protocol)',
    ),
    OperationSpec('measurement_plot_load',
                  'Measurement plot data load (model layer)'),
    OperationSpec('comparison_view_prep',
                  'Candidate comparison view prep (revision diff)'),
    OperationSpec('memory_idle_rss',
                  'Memory footprint at idle (post-import/open RSS)'),
    OperationSpec('memory_loaded_rss',
                  'Memory footprint with a representative project loaded'),
    OperationSpec('shutdown_close',
                  'Shutdown/cleanup (handle release + collection)'),
    OperationSpec(
        'viewport_3d_interaction',
        '3D viewport interaction under representative scene sizes',
        hardware_sensitive=True,
        hardware_note=(
            'Hardware-sensitive: owned-machine protocol — run the '
            'viewport sweep on the reference machine per '
            'docs/issues/issue-867-performance-budget.md; synthetic CI '
            'timings are not claimed as viewport evidence.'),
    ),
)

_OPERATION_FNS: dict[str, OperationFn] = {
    'application_startup_cold': op_application_startup_cold,
    'application_startup_warm': op_application_startup_warm,
    'project_open': op_project_open,
    'project_save': op_project_save,
    'project_reopen': op_project_reopen,
    'scene_revision_apply': op_scene_revision_apply,
    'workspace_first_mount': op_workspace_first_mount,
    'workspace_transition': op_workspace_transition,
    'measurement_plot_load': op_measurement_plot_load,
    'comparison_view_prep': op_comparison_view_prep,
    'memory_idle_rss': op_memory_idle_rss,
    'memory_loaded_rss': op_memory_loaded_rss,
    'shutdown_close': op_shutdown_close,
}

# Single-observation operations — iterations past the first add no signal.
_SINGLE_SAMPLE_OPS = frozenset({
    'application_startup_cold', 'workspace_first_mount', 'memory_idle_rss',
    'memory_loaded_rss', 'shutdown_close',
})


class HarnessOperationError(RuntimeError):
    """An operation raised — recorded as unmeasured evidence, never a pass."""


def run_benchmark(
    context: BenchmarkContext,
    *,
    iterations: int = 7,
    warmup: int = 1,
    only: tuple[str, ...] | None = None,
    on_error: Callable[[str, BaseException], None] | None = None,
) -> Mapping[str, OperationDistribution]:
    """Run the benchmarkable operations and return distributions keyed by
    operation id. An operation that raises contributes no distribution —
    the report marks it 'unmeasured'."""
    wanted = set(only) if only else None
    distributions: dict[str, OperationDistribution] = {}
    for spec in OPERATIONS:
        if spec.hardware_sensitive:
            continue
        if wanted is not None and spec.operation_id not in wanted:
            continue
        fn = _OPERATION_FNS.get(spec.operation_id)
        if fn is None:
            continue
        reps = 1 if spec.operation_id in _SINGLE_SAMPLE_OPS else iterations
        warmup_reps = (
            0 if spec.operation_id in _SINGLE_SAMPLE_OPS else warmup)
        samples: list[float] = []
        last_rss: int | None = None
        try:
            for iteration in range(warmup_reps + reps):
                ctx = BenchmarkContext(
                    repo_root=context.repo_root,
                    fixture_dir=context.fixture_dir,
                    database_path=context.database_path,
                    document_id=context.document_id,
                    head_revision_id=context.head_revision_id,
                    first_revision_id=context.first_revision_id,
                    iteration=iteration,
                )
                elapsed = fn(ctx)
                if iteration >= warmup_reps:
                    samples.append(elapsed)
                    observed = getattr(ctx, 'rss_sample', None)
                    if observed is not None:
                        last_rss = observed
        except Exception as exc:  # noqa: BLE001 — evidence, not a crash
            # error-boundary: benchmark harness — an unmeasurable operation
            # is recorded as unmeasured evidence, never silently passed.
            if on_error is not None:
                on_error(spec.operation_id, exc)
            continue
        distributions[spec.operation_id] = distribution_from_samples(
            spec.operation_id, tuple(samples), rss_bytes_after=last_rss)
    return distributions
