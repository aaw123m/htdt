"""Context collectors for the #884 support/diagnostic bundle.

Each collector returns a JSON-shaped payload for one
:class:`~htdt.support_diagnostics.PackageCategory`. They are deliberately
honest: when a probe cannot run (no GL context, stub backend, missing
artifact) the payload says ``status: unprobed`` / ``unavailable`` with a
reason — never a fabricated success. A collector that raises is caught by
the builder and recorded as a collection error.

Nothing here reads project content: identities, counts and status only.
"""

from __future__ import annotations

import importlib.metadata
import locale
import os
import platform
import sys
from pathlib import Path
from typing import Any, Mapping

from . import __version__
from .diagnostics_support import build_identity

# Dependency pins whose identity a support engineer needs to reproduce a
# failure. Only distribution names — never installed-path details.
_DEPENDENCY_PIN_NAMES = (
    'PySide6',
    'numpy',
    'scipy',
    'pydantic',
    'vtk',
    'sounddevice',
    'pyyaml',
)


def collect_runtime_context() -> dict[str, Any]:
    """OS build, locale, display scaling, Python + dependency identity."""
    dependencies: dict[str, str | None] = {}
    for name in _DEPENDENCY_PIN_NAMES:
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = None
    return {
        'status': 'collected',
        'os': {
            'system': platform.system(),
            'release': platform.release(),
            'version': platform.version(),
            'machine': platform.machine(),
        },
        'locale': {
            'encoding': locale.getpreferredencoding(False),
            'ui_language': os.environ.get('LANG') or '<unset>',
        },
        'display': {
            # Scaling is read as env hints only — probing real DPI needs a
            # live screen, which headless/support runs may not have.
            'scaling_env': {
                'QT_SCALE_FACTOR': os.environ.get('QT_SCALE_FACTOR'),
                'QT_AUTO_SCREEN_SCALE_FACTOR': os.environ.get(
                    'QT_AUTO_SCREEN_SCALE_FACTOR'
                ),
            },
        },
        'python': {
            'version': platform.python_version(),
            'implementation': platform.python_implementation(),
            'executable_basename': Path(sys.executable).name,
        },
        'htdt': {
            'version': __version__,
            'build': build_identity().describe(),
        },
        'dependencies': dependencies,
    }


def collect_gpu_context() -> dict[str, Any]:
    """GPU/driver/VTK/OpenGL identity — probed when possible.

    Creating a GL context just for diagnostics can crash on machines whose
    graphics stack is exactly what is broken, so the collector reports
    capability *presence* (module availability, env hints) and marks the
    renderer string ``unprobed`` rather than forcing a context.
    """
    import importlib.util

    vtk_available = importlib.util.find_spec('vtkmodules') is not None
    qt_offscreen = os.environ.get('QT_QPA_PLATFORM') == 'offscreen'
    return {
        'status': 'collected',
        'vtk_module_present': vtk_available,
        'qt_platform': os.environ.get('QT_QPA_PLATFORM') or 'native',
        'renderer': 'unprobed',
        'renderer_reason': (
            'GL context probing is not performed during export — the '
            'graphics stack may be the fault being diagnosed.'
        ),
        'mesa_libgl': os.environ.get('LIBGL_ALWAYS_SOFTWARE'),
        'headless': qt_offscreen,
    }


def collect_audio_context(backend: Any | None = None) -> dict[str, Any]:
    """Audio backend identity + capability — never device payload.

    ``backend`` may be an ``AudioIOBackend`` (live or stub). Without one,
    reports the known backend registry states (wasapi stub, fake marked
    simulated) so support can see which path was in play.
    """
    backends: list[dict[str, Any]] = []
    try:
        from .cad_sweep_acquisition import (
            FakeAudioBackend,
            WasapiAudioBackend,
        )

        candidates = [WasapiAudioBackend(), FakeAudioBackend()]
        if backend is not None:
            candidates.insert(0, backend)
        for candidate in candidates:
            entry: dict[str, Any] = {
                'backend_id': getattr(candidate, 'backend_id', 'unknown'),
                'backend_version': getattr(
                    candidate, 'backend_version', 'unknown'
                ),
                'available': bool(candidate.available()),
            }
            if not entry['available']:
                reason = getattr(candidate, 'unavailable_reason', None)
                entry['unavailable_reason'] = (
                    reason() if callable(reason) else 'unknown'
                )
            if isinstance(candidate, FakeAudioBackend):
                entry['simulated'] = True
            backends.append(entry)
    except Exception as exc:  # error-boundary: probe isolation
        return {'status': 'unprobed', 'reason': type(exc).__name__}
    return {'status': 'collected', 'backends': backends}


def collect_release_evidence(data_dir: Path) -> dict[str, Any]:
    """Verification/release evidence identity — sha + verdict, not payload."""
    from hashlib import sha256

    artifacts = Path(data_dir) / 'artifacts'
    reports = sorted(
        list(artifacts.glob('release-verification-*/report.json'))
        + list(artifacts.glob('release-verification-*/verification-report.json'))
    ) if artifacts.is_dir() else []
    entries: list[dict[str, Any]] = []
    for report in reports[-5:]:  # bounded tail
        try:
            blob = report.read_bytes()
        except OSError:
            continue
        import json

        verdict = None
        try:
            verdict = json.loads(blob).get('verdict')
        except Exception:  # error-boundary: verdict is informational
            pass
        entries.append(
            {
                'name': report.parent.name,
                'sha256': sha256(blob).hexdigest(),
                'bytes': len(blob),
                'verdict': verdict,
            }
        )
    return {
        'status': 'collected' if entries else 'unprobed',
        'reason': None if entries else 'no release-verification reports found',
        'reports': entries,
    }


def collect_workflow_state(
    *, current_workspace_id: str | None = None,
    workflow_stage: str | None = None,
    journey_progress: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Current workspace/workflow stage — provided by the shell."""
    return {
        'status': 'collected',
        'current_workspace_id': current_workspace_id,
        'workflow_stage': workflow_stage,
        'journey_progress': dict(journey_progress or {}),
    }


def collect_measurement_engine_status(engine: Any | None = None) -> dict[str, Any]:
    """Measurement/acquisition engine status — state + last outcome only."""
    if engine is None:
        return {'status': 'unprobed', 'reason': 'no engine instance'}
    try:
        snapshot = engine.status() if hasattr(engine, 'status') else None
    except Exception as exc:  # error-boundary: probe isolation
        return {'status': 'unprobed', 'reason': type(exc).__name__}
    return {'status': 'collected', 'engine': snapshot}


def collect_device_transaction_status(
    transactions: Any | None = None,
) -> dict[str, Any]:
    """Device transaction/readback status — ids + status + sha refs only."""
    if transactions is None:
        return {'status': 'unprobed', 'reason': 'no transaction source'}
    try:
        items = [
            {
                'transaction_id': getattr(t, 'transaction_id', None)
                or (t.get('transaction_id') if isinstance(t, Mapping) else None),
                'status': getattr(t, 'status', None)
                or (t.get('status') if isinstance(t, Mapping) else None),
                'readback_status': getattr(t, 'readback_status', None)
                or (
                    t.get('readback_status')
                    if isinstance(t, Mapping)
                    else None
                ),
            }
            for t in list(transactions)[-50:]
        ]
    except Exception as exc:  # error-boundary: probe isolation
        return {'status': 'unprobed', 'reason': type(exc).__name__}
    return {'status': 'collected', 'transactions': items}


__all__ = [
    'collect_audio_context',
    'collect_device_transaction_status',
    'collect_gpu_context',
    'collect_measurement_engine_status',
    'collect_release_evidence',
    'collect_runtime_context',
    'collect_workflow_state',
]
