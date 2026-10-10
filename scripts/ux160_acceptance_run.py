#!/usr/bin/env python3
"""UX160 acceptance evidence runner (issue #880).

One command prepares and records one acceptance-matrix row end to end:

    python scripts/ux160_acceptance_run.py \
        --work-dir C:/path/to/ux160-evidence \
        --scale 1.25 --scenario seeded

It binds the exact producing build (#833 revision/dirty/toolchain
binding), applies the DPI/scaling row, resets and launches the real
workflow shell into the required initial state, executes the
machine-checkable golden-path checkpoints (reusing the
``ux160_driver.py`` check library), captures canonical screenshots,
window/layout geometry and critical-widget visibility, records the
Qt/VTK/GL runtime, optionally compares deterministic geometry metrics
against a baseline bundle, and emits ONE evidence bundle per run:

    <work-dir>/ux160/<row-id>/attempt-<NNN>/
        manifest.json         machine-readable bundle root
        report.md             human-facing summary + manual checklist
        driver_result.json    raw in-process checkpoint results
        driver.log            driver stdout/stderr capture
        shots/*.png           canonical checkpoint screenshots
    <work-dir>/ux160/<row-id>/latest.json    stable pointer (lifecycle ref)
    <work-dir>/ux-acceptance-evidence/cad-ux-evidence.sqlite3
        sealed ``cad_ux_acceptance_bundle_records`` authority rows

Honesty contract: a bundle is evidence, not acceptance. ``verdict`` is
``evidence_captured`` / ``capture_incomplete`` / ``capture_failed``;
``review_state`` stays ``manual_review_remaining`` while the manual
checklist has items. ``--profile offscreen-fixture`` produces harness
self-test bundles only — they can never pass as owned-Windows evidence.

Driver isolation: the Qt run happens in a child process (``--driver``
mode, internal) so a native crash becomes a ``capture_failed`` bundle,
never a lost run. ``--in-process`` runs the same driver inline for tests.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO / 'backend' / 'src'
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

BUNDLE_FORMAT = 'htdt-ux-acceptance-bundle-1'
DRIVER_RESULT_FORMAT = 'htdt-ux160-driver-result-1'
DEFAULT_MATRIX_ID = 'ux160'
EVIDENCE_DB_NAME = 'cad-ux-evidence.sqlite3'
WINDOW_SIZE = (1366, 860)

# Destinations swept on a real owned-Windows run — every registered
# workspace + application destination (same sweep as ux160_driver.py).
DEFAULT_DESTINATIONS = (
    'overview', 'room', 'measurement', 'optimization', 'presentation',
    'video',
    'projects', 'inbox', 'activity', 'library', 'support', 'acceptance',
    'verification',
)
# Conservative offscreen-fixture sweep: workspaces whose pages embed VTK
# viewports (room/presentation/optimization, video) are excluded — the
# fixture profile is a self-test lane, not owned-Windows evidence.
FIXTURE_DESTINATIONS = (
    'measurement', 'projects', 'inbox', 'library', 'support',
    'acceptance', 'verification',
)

#: Check phases the runner can execute; ``--checks`` subsets them.
ALL_CHECKS = (
    'launch', 'navigate', 'contexts', 'geometry', 'overflow',
    'focus', 'disabled_reasons', 'dynamic_a11y', 'palette', 'reopen',
)
#: Bounded default for the offscreen self-test lane: skips the long
#: keyboard-traversal sweep and the restart lane.
FIXTURE_CHECKS = (
    'launch', 'navigate', 'contexts', 'geometry', 'overflow',
    'disabled_reasons', 'dynamic_a11y',
)

#: Manual review vocabulary for UX160 rows — items only a human can
#: answer. Emitted verbatim into every bundle so the checklist itself is
#: retained evidence (and the sealed record pins it).
UX160_MANUAL_ITEMS: tuple[dict[str, Any], ...] = (
    {
        'item_id': 'manual:pointer-vtk-gestures',
        'description': (
            'Real pointer/mouse interaction and VTK camera/gizmo '
            'gestures at this DPI row — drag, orbit, pick feel'),
        'requires_human': True,
    },
    {
        'item_id': 'manual:readability-copy-polish',
        'description': (
            'Operator-judged readability and Japanese copy polish at '
            'this scale factor'),
        'requires_human': True,
    },
    {
        'item_id': 'manual:first-use-discoverability',
        'description': 'First-use discoverability impressions',
        'requires_human': True,
    },
    {
        'item_id': 'manual:visual-density-balance',
        'description': (
            'Layout density/aesthetic balance beyond the objective '
            'geometry invariants'),
        'requires_human': True,
    },
)

UX_ACCEPTANCE_NON_CLAIMS: tuple[str, ...] = (
    'automated checkpoints capture evidence only — they never accept a '
    'UX160 matrix row',
    'manual checklist items remain the acceptance gate for '
    'visual/subjective criteria',
    'pixel macros are never authoritative product verification',
)
FIXTURE_NON_CLAIMS: tuple[str, ...] = (
    'offscreen fixture run — GL/DPI rendering is emulated, never '
    'owned-Windows acceptance evidence',
)

EXIT_CAPTURED = 0
EXIT_INCOMPLETE = 3
EXIT_FAILED = 2
EXIT_USAGE = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict | None:
    try:
        payload = json.loads(io.open(path, encoding='utf-8').read())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    io.open(path, 'w', encoding='utf-8').write(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n')


# ---------------------------------------------------------------------------
# Environment binding (#833 revision/dirty/toolchain pattern)
# ---------------------------------------------------------------------------


def _verify_module():
    """Load sibling ``verify_open_issues.py`` by path (same idiom as
    ``run_release_verification.py``) for its env/git fingerprint
    machinery."""

    spec = importlib.util.spec_from_file_location(
        'verify_open_issues',
        Path(__file__).resolve().with_name('verify_open_issues.py'),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


def collect_environment() -> dict[str, Any]:
    """Recorder-side binding: build identity + toolchain + OS facts.

    Qt/screen/GL fields are merged from the driver result — only the
    in-process driver can observe them.
    """

    from htdt.build_info import get_build_info

    build = get_build_info()
    env: dict[str, Any] = {
        'build_version': build.version,
        'build_display_version': build.display_version,
        'build_commit_sha': build.commit_sha,
        'build_dirty': build.dirty,
        'build_source': build.source,
        'python_version': platform.python_version(),
        'python_implementation': platform.python_implementation(),
        'platform': platform.platform(),
        'machine': platform.machine(),
        'os_version': platform.version(),
        'environment_fingerprint': None,
        'lock_file_path': None,
        'lock_file_sha256': None,
        'qt_version': None,
        'pyside_version': None,
        'qpa_platform': None,
        'qt_scale_factor_env': os.environ.get('QT_SCALE_FACTOR'),
        'renderer_id': None,
        'screens': [],
    }
    lock_path = REPO / 'backend' / 'requirements-n05-windows.lock'
    if lock_path.is_file():
        env['lock_file_path'] = 'backend/requirements-n05-windows.lock'
        env['lock_file_sha256'] = _sha256_file(lock_path)
    try:
        env['environment_fingerprint'] = _verify_module()._env_fingerprint(
            sys.executable)
    except Exception:  # fingerprinting is best-effort provenance
        env['environment_fingerprint'] = None
    return env


# VTK/GL probing can hard-crash a process (access violation on a headless
# or mismatched GL stack). It therefore runs in a subprocess: a crash
# degrades to a fail-closed marker instead of taking the runner down.
_RENDERER_PROBE_CODE = """\
import sys
try:
    import pyvista as pv
    import vtkmodules.all as _vtk
    from vtkmodules.vtkCommonCore import vtkVersion
except Exception:
    print('unavailable')
    sys.exit(0)
gl = 'unverified'
try:
    probe = pv.Plotter(off_screen=True, window_size=(64, 64))
    probe.add_mesh(pv.Sphere(radius=0.05))
    probe.render()
    caps = probe.ren_win.ReportCapabilities() or ''
    for line in caps.splitlines():
        line = line.strip()
        if line.startswith('OpenGL version string:'):
            gl = line.split(':', 1)[1].strip()
            break
    probe.close()
except Exception:
    pass
print('pyvista-%s/vtk-%s/%s' % (
    pv.__version__, vtkVersion().GetVTKVersion(), gl))
"""


def _renderer_probe(timeout: float = 120.0) -> str:
    """VTK/GL identity probe — fail-closed string, never an exception,
    never a native crash in this process."""

    env = dict(os.environ)
    env['QT_QPA_PLATFORM'] = 'offscreen'
    try:
        proc = subprocess.run(
            [sys.executable, '-c', _RENDERER_PROBE_CODE],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except Exception:
        return 'probe_failed'
    lines = (proc.stdout or '').strip().splitlines()
    if proc.returncode != 0 or not lines:
        return f'probe_crashed(rc={proc.returncode})'
    return lines[-1]


def _qt_environment() -> dict[str, Any]:
    """Qt/screen/GL facts — only valid inside the driver process."""

    from PySide6.QtCore import qVersion
    from PySide6.QtGui import QGuiApplication
    from PySide6 import __version__ as pyside_version

    screens = []
    for screen in QGuiApplication.screens():
        geometry = screen.geometry()
        available = screen.availableGeometry()
        screens.append({
            'name': screen.name(),
            'geometry': [geometry.x(), geometry.y(),
                         geometry.width(), geometry.height()],
            'available_geometry': [
                available.x(), available.y(),
                available.width(), available.height()],
            'logical_dpi': screen.logicalDotsPerInch(),
            'device_pixel_ratio': screen.devicePixelRatio(),
        })
    return {
        'qt_version': qVersion(),
        'pyside_version': pyside_version,
        'qpa_platform': QGuiApplication.platformName(),
        'qt_scale_factor_env': os.environ.get('QT_SCALE_FACTOR'),
        'renderer_id': _renderer_probe(),
        'screens': screens,
    }


# ---------------------------------------------------------------------------
# In-process driver (--driver / run_driver_phase) — reuses ux160_driver
# check functions; never pixel macros.
# ---------------------------------------------------------------------------


def _driver_checks_module():
    """Import the existing UX160 check library (same scripts dir)."""

    import ux160_driver  # noqa: PLC0415

    return ux160_driver


def _shot(out_dir: Path, window: Any, name: str) -> dict[str, Any] | None:
    shots = out_dir / 'shots'
    shots.mkdir(parents=True, exist_ok=True)
    path = shots / f'{name}.png'
    if not window.grab().save(str(path)):
        return None
    return {
        'name': f'shots/{name}.png',
        'kind': 'screenshot',
        'sha256': _sha256_file(path),
        'byte_count': path.stat().st_size,
    }


def _checkpoint(checkpoint_id: str, kind: str, status: str,
                target: str | None = None, detail: str | None = None,
                evidence: list[dict] | None = None,
                t0: float | None = None) -> dict[str, Any]:
    return {
        'checkpoint_id': checkpoint_id,
        'kind': kind,
        'target': target,
        'status': status,
        'detail': detail,
        'evidence': evidence or [],
        'elapsed_ms': (
            int((time.monotonic() - t0) * 1000)
            if t0 is not None else None),
    }


def run_driver_phase(data_dir: Path, out_dir: Path, scenario: str,
                     destinations: tuple[str, ...], checks: tuple[str, ...],
                     phase: str = 'main') -> dict[str, Any]:
    """Drive the real workflow shell in-process and emit checkpoint
    results. Writes ``driver_result.json`` + ``shots/`` under ``out_dir``
    and a ``clean_exit.marker`` after a clean shutdown."""

    from PySide6.QtWidgets import QApplication

    driver = _driver_checks_module()
    app = QApplication.instance() or QApplication([sys.argv[0]])

    from htdt.cad_repository import SceneRepository
    from htdt.workflow_application import WorkflowApplicationComposition
    from htdt.workflow_navigation import (
        ApplicationDestinationId,
        WorkspaceId,
        normalize_destination_id,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / 'cad-scenes.sqlite3'
    repository = SceneRepository(db_path)

    document_id = 'document-1'
    if scenario == 'seeded':
        from htdt.cad_synthetic_demo import (
            SYNTHETIC_DEMO_DOCUMENT_ID,
            seed_synthetic_optimization_demo,
        )
        if repository.current_head(SYNTHETIC_DEMO_DOCUMENT_ID) is None:
            seed_synthetic_optimization_demo(repository)
        document_id = SYNTHETIC_DEMO_DOCUMENT_ID

    checkpoints: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {'destinations': {}}
    result: dict[str, Any] = {
        'format': DRIVER_RESULT_FORMAT,
        'phase': phase,
        'scenario': scenario,
        'capture_mode': os.environ.get(
            'HTDT_UX_CAPTURE_MODE', 'owned_windows'),
        'environment': _qt_environment(),
        'document_id': document_id,
        'checkpoints': checkpoints,
        'metrics': metrics,
        'finished_utc': None,
        'clean_exit': False,
    }

    from htdt.project_library_repository import ProjectLibraryRepository
    composition = WorkflowApplicationComposition(
        repository,
        document_id,
        project_library=ProjectLibraryRepository(repository),
    )
    window = composition.shell
    window.resize(*WINDOW_SIZE)
    window.show()
    driver._settle(app, 1500)

    if 'launch' in checks:
        t0 = time.monotonic()
        visible = window.isVisible()
        resolved = getattr(composition, 'document_id', None) or document_id
        checkpoints.append(_checkpoint(
            'launch:initial_state', 'launch',
            'pass' if visible else 'blocked',
            target=str(resolved),
            detail=(
                f'window {"shown" if visible else "not visible"}; '
                f'document={resolved}; size={window.width()}x'
                f'{window.height()}'),
            t0=t0))
        metrics['window_allocated'] = [window.width(), window.height()]

    if phase == 'reopen':
        # Restart/reopen lane: the store already holds the project — the
        # checkpoint is that startup resolution lands on the same
        # document and the shell becomes usable again.
        t0 = time.monotonic()
        head = repository.current_head(document_id)
        ok = head is not None and window.isVisible()
        shot = _shot(out_dir, window, 'reopen--home')
        checkpoints.append(_checkpoint(
            'reopen:startup_resolution', 'reopen',
            'pass' if ok else 'finding',
            target=document_id,
            detail=(
                f'head={head.revision_id if head else None}; '
                f'window_visible={window.isVisible()}'),
            evidence=[shot] if shot else [],
            t0=t0))
    else:
        for dest_text in destinations:
            dest = normalize_destination_id(dest_text)
            row_shots: list[dict[str, Any]] = []
            t0 = time.monotonic()
            ok = window.navigate(dest)
            driver._settle(app)
            if not ok:
                checkpoints.append(_checkpoint(
                    f'navigate:{dest_text}', 'navigate', 'blocked',
                    target=dest_text,
                    detail=window.router.last_block_reason,
                    t0=t0))
                metrics['destinations'][dest_text] = {
                    'navigated': False}
                continue
            checkpoints.append(_checkpoint(
                f'navigate:{dest_text}', 'navigate', 'pass',
                target=dest_text, t0=t0))
            shot = _shot(out_dir, window, dest_text)
            if shot:
                row_shots.append(shot)
            page = window.centralWidget()
            size = page.size() if page is not None else None
            metrics['destinations'][dest_text] = {
                'navigated': True,
                'page_class': (
                    page.__class__.__name__ if page is not None else None),
                'page_size': (
                    [size.width(), size.height()] if size is not None
                    else None),
                'page_visible': (
                    page.isVisible() if page is not None else False),
            }
            if 'geometry' in checks:
                t0 = time.monotonic()
                geom_ok = bool(
                    size is not None and size.width() > 0
                    and size.height() > 0
                    and page is not None and page.isVisible())
                checkpoints.append(_checkpoint(
                    f'geometry:{dest_text}', 'geometry',
                    'pass' if geom_ok else 'finding',
                    target=dest_text,
                    detail=json.dumps(
                        metrics['destinations'][dest_text],
                        ensure_ascii=False),
                    evidence=row_shots, t0=t0))
            if 'overflow' in checks:
                t0 = time.monotonic()
                findings = driver.check_overflow(window)
                metrics['destinations'][dest_text]['overflow_count'] = (
                    len(findings))
                checkpoints.append(_checkpoint(
                    f'overflow:{dest_text}', 'overflow',
                    'pass' if not findings else 'finding',
                    target=dest_text,
                    detail=f'{len(findings)} overflow findings',
                    t0=t0))
            if 'contexts' in checks:
                registration = window.router.registration(dest)
                for ctx in registration.contexts:
                    t0 = time.monotonic()
                    try:
                        window.router.select_context(dest, ctx.context_id)
                        driver._settle(app, 500)
                        shot = _shot(
                            out_dir, window,
                            f'{dest_text}--{ctx.context_id}')
                        checkpoints.append(_checkpoint(
                            f'context:{dest_text}/{ctx.context_id}',
                            'context', 'pass',
                            target=ctx.context_id,
                            evidence=[shot] if shot else [],
                            t0=t0))
                    except Exception as exc:
                        checkpoints.append(_checkpoint(
                            f'context:{dest_text}/{ctx.context_id}',
                            'context', 'finding',
                            target=ctx.context_id,
                            detail=str(exc)[:200],
                            t0=t0))

        if 'focus' in checks:
            for dest_text in destinations[:2]:
                dest = normalize_destination_id(dest_text)
                t0 = time.monotonic()
                if window.navigate(dest):
                    driver._settle(app)
                    focus = driver.check_focus_chain(app, window)
                    metrics['focus'] = {
                        'completed_cycle': focus['completed_cycle'],
                        'unique_widgets': focus['unique_widgets'],
                        'problems': len(focus['problems']),
                    }
                    checkpoints.append(_checkpoint(
                        f'focus:{dest_text}', 'focus',
                        'pass' if not focus['problems'] else 'finding',
                        target=dest_text,
                        detail=(
                            f"cycle={focus['completed_cycle']} "
                            f"unique={focus['unique_widgets']} "
                            f"problems={len(focus['problems'])}"),
                        t0=t0))

        if 'disabled_reasons' in checks:
            t0 = time.monotonic()
            findings = driver.check_disabled_reasons(window)
            metrics['disabled_reasons_missing'] = len(findings)
            checkpoints.append(_checkpoint(
                'global:disabled_reasons', 'disabled_reasons',
                'pass' if not findings else 'finding',
                detail=f'{len(findings)} disabled widgets without '
                       'a recorded reason',
                t0=t0))

        if 'dynamic_a11y' in checks:
            # #975: dynamic panels must retain keyboard focus through
            # rebuilds, announce each state transition exactly once,
            # and keep every disabled control's reason on screen.
            t0 = time.monotonic()
            a11y = driver.check_dynamic_a11y(app, window)
            a11y_findings = a11y['findings']
            metrics['dynamic_a11y'] = {
                'panels': len(a11y['panels']),
                'focus_notes': a11y['focus_notes'],
                'findings': len(a11y_findings),
            }
            checkpoints.append(_checkpoint(
                'global:dynamic_a11y', 'dynamic_a11y',
                'pass' if not a11y_findings else 'finding',
                detail=(
                    f"panels={len(a11y['panels'])} "
                    f'findings={len(a11y_findings)} '
                    f'focus={a11y["focus_notes"]}'
                )[:300],
                t0=t0))

        if 'palette' in checks:
            t0 = time.monotonic()
            palette = driver.check_palette(app, window, out_dir / 'shots')
            checkpoints.append(_checkpoint(
                'global:command_palette', 'palette',
                'pass' if palette.get('opened') else 'finding',
                detail='opened' if palette.get('opened') else
                'palette did not open', t0=t0))

    result['finished_utc'] = _utc_now()
    _write_json(out_dir / 'driver_result.json', result)

    # Clean-shutdown marker last-but-one like ux160_driver: a teardown
    # crash still leaves the result on disk.
    (out_dir / 'clean_exit.marker').write_text('clean\n', encoding='utf-8')
    window.close()
    driver._settle(app, 400)
    result['clean_exit'] = True
    _write_json(out_dir / 'driver_result.json', result)
    return result


def _run_driver_subprocess(python: Path, data_dir: Path, out_dir: Path,
                           scenario: str, destinations: tuple[str, ...],
                           checks: tuple[str, ...], phase: str,
                           scale: str, capture_mode: str,
                           log_path: Path, timeout: int) -> int | None:
    env = dict(os.environ)
    env['QT_SCALE_FACTOR'] = scale
    env['PYTHONIOENCODING'] = 'utf-8'
    env['HTDT_UX_CAPTURE_MODE'] = capture_mode
    if capture_mode == 'offscreen_fixture':
        env['QT_QPA_PLATFORM'] = 'offscreen'
    else:
        env.pop('QT_QPA_PLATFORM', None)
    cmd = [
        str(python), '-u', str(Path(__file__).resolve()),
        '--driver',
        '--data-dir', str(data_dir), '--out', str(out_dir),
        '--scenario', scenario,
        '--destinations', ','.join(destinations),
        '--checks', ','.join(checks),
        '--phase', phase,
    ]
    with io.open(log_path, 'w', encoding='utf-8') as log:
        try:
            proc = subprocess.run(cmd, env=env, stdout=log, stderr=log,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
    return proc.returncode


# ---------------------------------------------------------------------------
# Baseline comparison — deterministic scalars only.
# ---------------------------------------------------------------------------


def _metrics_from_manifest(manifest: dict) -> dict[str, Any]:
    return manifest.get('metrics') or {}


def compare_baseline(baseline_metrics: dict[str, Any],
                     current_metrics: dict[str, Any]) -> tuple[str, list[str]]:
    """Compare deterministic geometry/focus scalars. Returns
    ('matched'|'diverged', divergences). 'not_run' is produced by the
    caller when no baseline exists."""

    divergences: list[str] = []
    keys = sorted(set(baseline_metrics) | set(current_metrics))
    for key in keys:
        if key == 'destinations':
            dests = sorted(
                set(baseline_metrics.get('destinations') or {})
                | set(current_metrics.get('destinations') or {}))
            for dest in dests:
                base = (baseline_metrics.get('destinations') or {}).get(dest)
                cur = (current_metrics.get('destinations') or {}).get(dest)
                if base is None or cur is None:
                    divergences.append(
                        f'destinations.{dest}: baseline={base} run={cur}')
                    continue
                for field in ('page_size', 'overflow_count',
                              'page_visible', 'navigated'):
                    if base.get(field) != cur.get(field):
                        divergences.append(
                            f'destinations.{dest}.{field}: '
                            f'baseline={base.get(field)} '
                            f'run={cur.get(field)}')
            continue
        if baseline_metrics.get(key) != current_metrics.get(key):
            divergences.append(
                f'{key}: baseline={baseline_metrics.get(key)} '
                f'run={current_metrics.get(key)}')
    return ('diverged' if divergences else 'matched', divergences)


# ---------------------------------------------------------------------------
# Bundle assembly + sealed record persistence
# ---------------------------------------------------------------------------


def _attempt_number(name: str) -> int | None:
    match = re.fullmatch(r'attempt-(\d+)', name)
    return int(match.group(1)) if match else None


def _next_attempt(row_dir: Path, repository=None,
                  matrix_id: str = DEFAULT_MATRIX_ID,
                  row_id: str = '') -> int:
    on_disk = [n for n in (
        _attempt_number(p.name) for p in row_dir.glob('attempt-*')
        if p.is_dir()) if n is not None]
    attempt = (max(on_disk) + 1) if on_disk else 1
    if repository is not None:
        try:
            attempt = max(
                attempt,
                repository.next_run_attempt(matrix_id, row_id))
        except Exception:
            pass
    return attempt


def _evidence_repository(work_dir: Path):
    from htdt.cad_repository import SceneRepository
    from htdt.cad_ux_acceptance_evidence_repository import (
        CadUxAcceptanceEvidenceRepository,
    )

    db_dir = work_dir / 'ux-acceptance-evidence'
    db_dir.mkdir(parents=True, exist_ok=True)
    scene = SceneRepository(db_dir / EVIDENCE_DB_NAME)
    return CadUxAcceptanceEvidenceRepository(scene)


def _artifact(path: Path, bundle_dir: Path, kind: str) -> dict[str, Any]:
    return {
        'name': path.relative_to(bundle_dir).as_posix(),
        'kind': kind,
        'sha256': _sha256_file(path),
        'byte_count': path.stat().st_size,
    }


def _collect_artifacts(bundle_dir: Path) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for path in sorted(bundle_dir.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(bundle_dir).as_posix()
        if rel == 'manifest.json':
            continue  # bound separately by manifest_sha256
        if rel.endswith('.png'):
            kind = 'screenshot'
        elif rel == 'report.md':
            kind = 'report'
        elif rel.endswith('driver_result.json'):
            kind = 'driver_result'
        elif rel.endswith('.log'):
            kind = 'driver_log'
        else:
            kind = 'other'
        artifacts.append(_artifact(path, bundle_dir, kind))
    return artifacts


def render_report(manifest: dict[str, Any]) -> str:
    env = manifest['environment']
    lines = [
        f"# UX160 acceptance evidence — {manifest['row_id']} "
        f"attempt {manifest['run_attempt']}",
        '',
        f"- verdict: **{manifest['verdict']}** "
        f"(evidence only — not row acceptance)",
        f"- review_state: `{manifest['review_state']}`",
        f"- capture_mode: `{manifest['capture_mode']}`",
        f"- scenario: `{manifest['scenario']}` "
        f"scale={manifest['scale_factor']}",
        f"- build: `{env.get('build_display_version')}` "
        f"(sha {env.get('build_commit_sha')}, "
        f"dirty={env.get('build_dirty')}, "
        f"source={env.get('build_source')})",
        f"- toolchain: python {env.get('python_version')} "
        f"{env.get('machine')} / {env.get('platform')}",
        f"- qt {env.get('qt_version')} pyside "
        f"{env.get('pyside_version')} qpa={env.get('qpa_platform')} "
        f"scale_env={env.get('qt_scale_factor_env')}",
        f"- renderer: `{env.get('renderer_id')}`",
        f"- baseline: `{manifest['baseline']['verdict']}`",
        '',
        '## Checkpoints',
        '',
        '| checkpoint | kind | status | detail |',
        '|---|---|---|---|',
    ]
    for cp in manifest['checkpoints']:
        detail = (cp.get('detail') or '').replace('|', '\\|').replace(
            '\n', ' ')
        lines.append(
            f"| `{cp['checkpoint_id']}` | {cp['kind']} | "
            f"{cp['status']} | {detail[:160]} |")
    lines += ['', '## Artifacts', '']
    for artifact in manifest['artifacts']:
        lines.append(
            f"- `{artifact['name']}` ({artifact['kind']}, "
            f"{artifact['byte_count']} B, "
            f"sha256 `{str(artifact['sha256'])[:16]}…`)")
    if manifest['baseline']['divergences']:
        lines += ['', '## Baseline divergences', '']
        lines += [f'- {d}' for d in manifest['baseline']['divergences']]
    lines += [
        '',
        '## Manual review items — human judgment required',
        '',
        'These criteria are NOT automated. The row is not accepted until '
        'a human answers them on the owned-Windows surface:',
        '',
    ]
    for item in manifest['manual_items']:
        lines.append(f"- [ ] `{item['item_id']}` — {item['description']}")
    lines += ['', '## Non-claims', '']
    lines += [f'- {claim}' for claim in manifest['non_claims']]
    lines.append('')
    return '\n'.join(lines)


def assemble_bundle(
    *,
    work_dir: Path,
    matrix_id: str,
    row_id: str,
    scenario: str,
    scale_factor: str,
    capture_mode: str,
    document_id: str,
    checkpoints: list[dict[str, Any]],
    metrics: dict[str, Any],
    environment: dict[str, Any],
    driver_results: list[dict[str, Any]],
    driver_completed: bool,
    failure_detail: str | None,
    baseline_manifest: dict | None,
    attempt_dir: Path | None = None,
    run_attempt: int | None = None,
    persist: bool = True,
    started_at_utc: str,
) -> dict[str, Any]:
    """Write the bundle directory, seal the authority record, and update
    the stable ``latest.json`` pointer. Returns the manifest dict."""

    from htdt.cad_authority_resolver import AuthorityRef
    from htdt.cad_ux_acceptance_evidence import (
        UxArtifactRef,
        UxCheckpointOutcome,
        UxEnvironmentBinding,
        UxManualChecklistItem,
        build_ux_acceptance_bundle_record,
        derive_bundle_verdict,
        derive_review_state,
    )

    repository = _evidence_repository(work_dir) if persist else None
    if run_attempt is None:
        run_attempt = _next_attempt(
            work_dir / matrix_id / row_id, repository,
            matrix_id, row_id)
    bundle_ref = f'{matrix_id}/{row_id}/attempt-{run_attempt:03d}'
    bundle_dir = attempt_dir or (work_dir / bundle_ref)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    outcome_models = tuple(
        UxCheckpointOutcome.model_validate(c) for c in checkpoints)
    verdict = derive_bundle_verdict(
        outcome_models,
        driver_completed=driver_completed,
        failure_detail=failure_detail,
    )
    manual_items = tuple(
        UxManualChecklistItem.model_validate(item)
        for item in UX160_MANUAL_ITEMS)
    non_claims = list(UX_ACCEPTANCE_NON_CLAIMS)
    if capture_mode == 'offscreen_fixture':
        non_claims += FIXTURE_NON_CLAIMS

    baseline: dict[str, Any] = {'verdict': 'not_run', 'divergences': [],
                                'ref': None}
    if baseline_manifest is not None:
        base_verdict, divergences = compare_baseline(
            _metrics_from_manifest(baseline_manifest), metrics)
        baseline = {
            'verdict': base_verdict,
            'divergences': divergences,
            'ref': baseline_manifest.get('bundle_ref'),
        }

    manifest: dict[str, Any] = {
        'format': BUNDLE_FORMAT,
        'matrix_id': matrix_id,
        'row_id': row_id,
        'run_attempt': run_attempt,
        'bundle_ref': bundle_ref,
        'scenario': scenario,
        'scale_factor': scale_factor,
        'capture_mode': capture_mode,
        'document_id': document_id,
        'verdict': verdict,
        'review_state': derive_review_state(manual_items),
        'checkpoints': checkpoints,
        'metrics': metrics,
        'environment': environment,
        'driver_results': [
            {
                'phase': r.get('phase'),
                'clean_exit': r.get('clean_exit'),
                'finished_utc': r.get('finished_utc'),
            }
            for r in driver_results
        ],
        'baseline': baseline,
        'manual_items': [m.model_dump(mode='json') for m in manual_items],
        'non_claims': non_claims,
        'failure_detail': failure_detail,
        'started_at_utc': started_at_utc,
        'finished_at_utc': _utc_now(),
        'lifecycle_ref': {
            'pointer': f'{matrix_id}/{row_id}/latest.json',
            'authority_table': 'cad_ux_acceptance_bundle_records',
            'evidence_db': f'ux-acceptance-evidence/{EVIDENCE_DB_NAME}',
        },
    }

    # Manifest first (no record_ref — the sealed record binds the file's
    # final bytes; the id lives in latest.json to avoid a hash cycle).
    manifest_path = bundle_dir / 'manifest.json'
    _write_json(manifest_path, {**manifest, 'artifacts': []})
    report_path = bundle_dir / 'report.md'
    report_path.write_text(render_report(
        {**manifest, 'artifacts': []}), encoding='utf-8')

    artifacts = _collect_artifacts(bundle_dir)
    manifest['artifacts'] = artifacts
    _write_json(manifest_path, manifest)

    finished = manifest['finished_at_utc']
    record = build_ux_acceptance_bundle_record(
        document_id=document_id,
        matrix_id=matrix_id,
        row_id=row_id,
        run_attempt=run_attempt,
        scenario=scenario,
        scale_factor=scale_factor,
        capture_mode=capture_mode,  # type: ignore[arg-type]
        environment=UxEnvironmentBinding.model_validate(environment),
        verdict=verdict,
        checkpoints=list(outcome_models),
        bundle_ref=bundle_ref,
        manifest_sha256=_sha256_file(manifest_path),
        report_sha256=_sha256_file(report_path),
        artifacts=[UxArtifactRef.model_validate(a) for a in artifacts],
        manual_items=manual_items,
        baseline_ref=(
            AuthorityRef(
                kind='ux_acceptance_bundle',
                ref_id=str(baseline['ref']),
            ) if baseline['ref'] else None),
        baseline_verdict=baseline['verdict'],
        baseline_divergences=baseline['divergences'],
        failure_detail=failure_detail,
        non_claims=non_claims,
        started_at_utc=started_at_utc,
        finished_at_utc=finished,
    )
    if repository is not None:
        repository.save_bundle(record)

    latest = {
        'row_id': row_id,
        'matrix_id': matrix_id,
        'latest_attempt': run_attempt,
        'bundle_ref': bundle_ref,
        'record': {
            'table': 'cad_ux_acceptance_bundle_records',
            'bundle_id': record.bundle_id,
            'bundle_sha256': record.bundle_sha256,
        },
    }
    _write_json(bundle_dir.parent / 'latest.json', latest)
    return manifest


# ---------------------------------------------------------------------------
# Recorder (default CLI mode)
# ---------------------------------------------------------------------------


def _row_id(scale: str, scenario: str) -> str:
    pct = str(int(float(scale) * 100))
    return f'dpi-{pct}-{scenario}'


def run_row(args: argparse.Namespace) -> int:
    started = _utc_now()
    work_dir: Path = args.work_dir
    row_id = args.row_id or _row_id(args.scale, args.scenario)
    capture_mode = (
        'offscreen_fixture' if args.profile == 'offscreen-fixture'
        else 'owned_windows')
    destinations = tuple(
        d.strip() for d in args.destinations.split(',') if d.strip())
    checks = tuple(
        c.strip() for c in args.checks.split(',') if c.strip())
    unknown_checks = sorted(set(checks) - set(ALL_CHECKS))
    if unknown_checks:
        print(f"[ux880] error: unknown --checks entries: "
              f"{', '.join(unknown_checks)} (known: "
              f"{', '.join(ALL_CHECKS)})", file=sys.stderr)
        return EXIT_USAGE

    data_dir = args.data_dir or (work_dir / f'data-{args.scenario}')
    if args.reset_data and data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    document_id = (
        'htdt-synthetic-o70-o80-demo-v1' if args.scenario == 'seeded'
        else 'document-1')

    environment = collect_environment()
    environment['qt_scale_factor_env'] = args.scale

    # Baseline: explicit --baseline manifest, else the row's previous
    # attempt's manifest when present.
    baseline_manifest: dict | None = None
    if args.baseline:
        baseline_manifest = _read_json(
            Path(args.baseline) / 'manifest.json')
        if baseline_manifest is None:
            baseline_manifest = _read_json(Path(args.baseline))
        if baseline_manifest is None:
            # An explicit-but-unreadable baseline is operator error —
            # never degrade it to a silent not_run.
            print(f"[ux880] error: --baseline {args.baseline} is not a "
                  "readable bundle dir or manifest.json", file=sys.stderr)
            return EXIT_USAGE
    else:
        latest_ptr = _read_json(
            work_dir / args.matrix_id / row_id / 'latest.json')
        if latest_ptr and latest_ptr.get('bundle_ref'):
            baseline_manifest = _read_json(
                work_dir / latest_ptr['bundle_ref'] / 'manifest.json')

    row_dir = work_dir / args.matrix_id / row_id
    attempt = _next_attempt(row_dir)
    attempt_dir = row_dir / f'attempt-{attempt:03d}'
    attempt_dir.mkdir(parents=True, exist_ok=True)

    checkpoints: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {}
    driver_results: list[dict[str, Any]] = []
    failure_detail: str | None = None
    driver_completed = True

    phases = ['main'] + (['reopen'] if 'reopen' in checks else [])
    for phase in phases:
        phase_out = attempt_dir / ('driver' if phase == 'main'
                                   else f'driver_{phase}')
        log_path = attempt_dir / (
            'driver.log' if phase == 'main' else f'driver_{phase}.log')
        if args.in_process:
            env_backup = os.environ.get('QT_SCALE_FACTOR')
            os.environ['QT_SCALE_FACTOR'] = args.scale
            os.environ['HTDT_UX_CAPTURE_MODE'] = capture_mode
            try:
                result = run_driver_phase(
                    data_dir, phase_out, args.scenario,
                    destinations, checks, phase=phase)
            except Exception:
                result = None
                rc = None
                failure_detail = (
                    f'{phase} driver raised in-process: '
                    + traceback.format_exc()[-400:])
            else:
                rc = 0
            finally:
                if env_backup is None:
                    os.environ.pop('QT_SCALE_FACTOR', None)
                else:
                    os.environ['QT_SCALE_FACTOR'] = env_backup
        else:
            rc = _run_driver_subprocess(
                args.python, data_dir, phase_out, args.scenario,
                destinations, checks, phase, args.scale, capture_mode,
                log_path, args.timeout)
            result = _read_json(phase_out / 'driver_result.json')
        if result is None:
            driver_completed = False
            tail = ''
            if log_path.is_file():
                tail = io.open(log_path, encoding='utf-8',
                               errors='replace').read()[-2000:]
            if not failure_detail:
                failure_detail = (
                    f'{phase} driver did not produce a result '
                    f'(rc={rc}); stderr tail: {tail[-400:]}')
            break
        driver_results.append(result)
        if result.get('environment'):
            for key, value in result['environment'].items():
                if value is not None:
                    environment[key] = value
        checkpoints.extend(result.get('checkpoints') or [])
        if phase == 'main':
            metrics = result.get('metrics') or {}
            document_id = result.get('document_id') or document_id
        if not result.get('clean_exit'):
            driver_completed = False
            failure_detail = (
                f'{phase} driver exited uncleanly (rc={rc})')
            break

    manifest = assemble_bundle(
        work_dir=work_dir,
        matrix_id=args.matrix_id,
        row_id=row_id,
        scenario=args.scenario,
        scale_factor=args.scale,
        capture_mode=capture_mode,
        document_id=document_id,
        checkpoints=checkpoints,
        metrics=metrics,
        environment=environment,
        driver_results=driver_results,
        driver_completed=driver_completed,
        failure_detail=failure_detail,
        baseline_manifest=baseline_manifest,
        attempt_dir=attempt_dir,
        run_attempt=attempt,
        started_at_utc=started,
    )
    verdict = manifest['verdict']
    print(f"[ux880] row {row_id} attempt {attempt:03d} -> {verdict} "
          f"({manifest['capture_mode']})", flush=True)
    print(f"[ux880] bundle -> {attempt_dir}", flush=True)
    if verdict == 'evidence_captured':
        return EXIT_CAPTURED
    if verdict == 'capture_incomplete':
        return EXIT_INCOMPLETE
    return EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--driver', action='store_true',
                        help='internal: run the in-process Qt driver')
    parser.add_argument('--phase', choices=('main', 'reopen'),
                        default='main')
    parser.add_argument('--work-dir', type=Path)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--out', type=Path, help='driver output dir')
    parser.add_argument('--matrix-id', default=DEFAULT_MATRIX_ID)
    parser.add_argument('--row-id')
    parser.add_argument('--scale', default='1.0')
    parser.add_argument('--scenario', choices=('fresh', 'seeded'),
                        default='seeded')
    parser.add_argument('--profile',
                        choices=('owned-windows', 'offscreen-fixture'),
                        default='owned-windows')
    parser.add_argument('--destinations')
    parser.add_argument('--checks')
    parser.add_argument('--baseline',
                        help='baseline bundle dir or manifest.json path')
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--reset-data', action='store_true')
    parser.add_argument('--in-process', action='store_true',
                        help='run the driver inline (tests/debugging)')
    args = parser.parse_args(argv)

    if args.driver:
        if args.data_dir is None or args.out is None:
            parser.error('--driver requires --data-dir and --out')
        profile = os.environ.get('HTDT_UX_CAPTURE_MODE', 'owned_windows')
        destinations = tuple(
            d.strip() for d in (args.destinations or '').split(',')
            if d.strip()) or tuple(
                FIXTURE_DESTINATIONS
                if profile == 'offscreen_fixture'
                else DEFAULT_DESTINATIONS)
        checks = tuple(
            c.strip() for c in (args.checks or '').split(',')
            if c.strip()) or tuple(
                FIXTURE_CHECKS
                if profile == 'offscreen_fixture'
                else ALL_CHECKS)
        run_driver_phase(args.data_dir, args.out, args.scenario,
                         destinations, checks, phase=args.phase)
        return 0

    if args.work_dir is None:
        parser.error('--work-dir is required')
    if not args.destinations:
        args.destinations = ','.join(
            FIXTURE_DESTINATIONS
            if args.profile == 'offscreen-fixture'
            else DEFAULT_DESTINATIONS)
    if not args.checks:
        args.checks = ','.join(
            FIXTURE_CHECKS
            if args.profile == 'offscreen-fixture'
            else ALL_CHECKS)
    return run_row(args)


if __name__ == '__main__':
    raise SystemExit(main())
