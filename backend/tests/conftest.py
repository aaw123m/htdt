"""Suite-wide test isolation.

``htdt.main`` builds ``app = create_app()`` at module import time, and
``create_app()`` opens the *default* data dir. Under pytest-xdist every
worker imports test modules concurrently, so workers race on one shared
``cad.sqlite3`` — one worker sees the database mid-initialization (no
``schema_version``) and collection fails with ``MigrationOpenError``,
which xdist then reports as "different tests were collected between
workers".

Setting ``LOCALAPPDATA`` here — before any test module is imported —
gives each pytest process its own default data dir, removing the race
and isolating tests from the developer's real data. ``LOCALAPPDATA`` is
the only variable ``htdt.main._default_data_dir`` reads besides the home
directory, so this also applies on POSIX runners.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile


def _shorten_temp_root() -> None:
    # Windows MAX_PATH: the default temp root (~50 chars under
    # %LOCALAPPDATA%\Temp) plus pytest's per-test directories pushes deeply
    # nested fixture paths past 260 chars and flakes tests like
    # test_evidence_lifecycle_rejects_illegal_promotions. Conftests for the
    # collected testpaths load before pytest resolves basetemp, so fixing
    # the temp env here works for every worker.
    if os.name != 'nt':
        return
    # Git Bash exports like TMPDIR=/c/t never reach Windows: python.exe sees
    # the literal POSIX path, resolves it to a nonexistent '\c\t', and
    # tempfile silently falls back to the long default root.
    for var in ('TMP', 'TMPDIR', 'TEMP'):
        value = os.environ.get(var)
        if value:
            match = re.match(r'^/([a-zA-Z])/(.*)$', value)
            if match:
                drive = match.group(1).upper()
                tail = match.group(2).replace('/', os.sep)
                os.environ[var] = f'{drive}:\\{tail}'
    if not os.environ.get('TMPDIR'):
        short_root = Path(os.environ.get('SystemDrive', 'C:') + '\\t')
        try:
            short_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        for var in ('TMP', 'TMPDIR', 'TEMP'):
            os.environ[var] = str(short_root)
    tempfile.tempdir = None


_shorten_temp_root()


def _isolate_default_data_dir() -> None:
    worker = os.environ.get('PYTEST_XDIST_WORKER', 'main')
    root = Path(tempfile.gettempdir()) / 'htdt-test-data' / worker
    root.mkdir(parents=True, exist_ok=True)
    os.environ['LOCALAPPDATA'] = str(root)


_isolate_default_data_dir()


# Suite-wide Qt top-level leak guard: compositions and helper hosts are
# never parented, so without this they linger until an arbitrary GC —
# which segfaults offscreen xdist workers and lets one test's widgets
# bleed into the next. Imported lazily so non-Qt runs stay cheap.
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _destroy_created_toplevels():
    """Destroy top-level widgets each test creates.

    ``deleteLater`` (not ``close``): teardown must not fire closeEvent —
    tests legitimately swap collaborators for doubles that never modeled
    the close contract, and a dirty shell's closeEvent can open a modal
    resolve dialog, which hangs forever offscreen. Destroying the widget
    is the goal anyway; orphaned worker threads are cancelled on detach
    (``native_worker``/``data_management``) and drained at session finish.
    """
    yield
    from PySide6.QtCore import Qt, QEvent
    from PySide6.QtWidgets import QApplication
    from shiboken6 import isValid

    app = QApplication.instance()
    if app is None:
        return

    # Stop orphaned worker machinery first: a detached-but-running thread
    # keeps emitting (finished/metacall posts land on stray receivers).
    # Only detached threads are touched — live pools/controllers may
    # belong to fixtures shared across tests.
    from htdt import data_management, native_worker

    native_worker.cancel_detached_threads()
    data_management.cancel_detached_op_threads()

    def _stray(widget) -> bool:
        return isValid(widget) and not widget.objectName().startswith("qt_")

    def _killable(widget) -> bool:
        """True only when no live ancestor destroys the stray itself.

        Some top-level strays are private QObject children of a still-
        live widget — a combobox dropdown's QFrame popup lives under its
        QComboBox, a submenu under its parent menu. ``~QComboBox`` does
        ``delete container`` on a raw pointer the child's own ~ never
        clears, so delivering the child's DD early double-frees it the
        moment the owner's ~ runs — the abort observed in this drain.
        Parented strays are left to die inside their owner's cascade;
        only unparented ones (or children of already-dead parents) are
        destroyed here.
        """
        parent = widget.parent()
        return parent is None or not isValid(parent)

    # Strays are destroyed in dependency order. A popup's ~ walks its
    # logical owner (a ViewBox, a mount's action group) even though it
    # is unparented — popups go first so their ~ is delivered while the
    # windows owning those collaborators are still alive. Windows that
    # still own children come next: their ~ cascade touches satellite
    # top-levels they spawned (a workspace's pages each own an
    # unparented render/overlay window held by raw pointer, not QObject
    # parenting) — an owner dying on a dead satellite aborts inside
    # ~QObject. Parentless leaf widgets (no QObject children) go last:
    # their ~ is self-contained, safe on dead collaborators. Delivery must
    # be DeferredDelete-TYPED per receiver: on this Qt build neither
    # processEvents nor an unfiltered sendPostedEvents dispatches
    # DeferredDelete events — the DD sits queued forever (verified
    # empirically), accumulating a backlog of queued-but-alive strays
    # that some later typed flush delivers all at once, mid-test in a
    # foreign context — the xdist worker-crash class. removePostedEvents
    # is equally fatal: Qt posts a DD only on the first deleteLater()
    # (deleteLaterCalled stays set), so destroying the posted event makes
    # every later deleteLater a silent no-op. Synchronous delete() was
    # tried and rejected too: a stray ~ can fault on collaborators that
    # died earlier. The global typed sweep at the end of each pass picks
    # up queued DDs for non-top-level objects (finished-thread self-DDs,
    # child objects) that topLevelWidgets never lists.
    for _ in range(8):
        strays = sorted(
            (w for w in app.topLevelWidgets() if _stray(w) and _killable(w)),
            key=lambda w: (
                w.windowType() != Qt.WindowType.Popup,
                not w.children(),
            ),
        )
        for widget in strays:
            if isValid(widget):
                widget.deleteLater()
                app.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        if not any(_stray(widget) for widget in app.topLevelWidgets()):
            break


def pytest_sessionfinish(session, exitstatus):
    """Drain Qt worker machinery before the worker process exits.

    Runs per xdist worker: deferred deletes are pumped and leaked Python
    wrappers collected so ``destroyed`` handlers fire now (detaching any
    running threads into the module maps), then both drain helpers give
    the detached work a bounded cooperative stop. Threads left running
    past this point are torn down by ``~QThread`` during interpreter
    exit — terminated mid-operation, the nondeterministic ``worker 'gwN'
    crashed`` signature; the drain shrinks that window.
    """
    import gc

    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return
    for _ in range(2):
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        gc.collect()
    from htdt import data_management, native_worker

    native_worker.drain_worker_threads()
    data_management.drain_operation_threads()
    app.sendPostedEvents()
    app.processEvents()
