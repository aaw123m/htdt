"""Round 14 (MEM): measured startup / idle / session-memory probe.

Two modes, both run fully offscreen on Windows:

  boot     Drive the REAL ``htdt.native_cad.main`` launch path on a fresh
           data dir. Records wall-clock milestones (import, QApplication,
           upgrade plan, repository, shell build, window show, exec entry)
           and samples RSS + CPU while the app sits idle, then quits.
  session  Build ``WorkflowApplicationComposition`` directly (same
           construction as the GUI path) and drive realistic operations —
           workspace navigation cycles, settings dialog open/close,
           command palette open/close, project switches — reporting RSS
           and tracemalloc growth per operation.

Usage (from repo root):

    C:/devin/python/python.exe scripts/round14_resource_probe.py boot --data-dir C:\\t\\r14-boot
    C:/devin/python/python.exe scripts/round14_resource_probe.py session --data-dir C:\\t\\r14-session

Numbers printed here are the BEFORE/AFTER evidence in
``docs/reviews/round14-mem.md``.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
from pathlib import Path
import sys
import threading
import time
import tracemalloc

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / 'backend' / 'src'))

import psutil  # noqa: E402

_PROC = psutil.Process()
_MARKS: list[tuple[str, float, float]] = []


def _rss_mb() -> float:
    return _PROC.memory_info().rss / (1024 * 1024)


def _mark(label: str) -> None:
    _MARKS.append((label, time.perf_counter(), _rss_mb()))


def _report() -> None:
    t0 = _MARKS[0][1] if _MARKS else 0.0
    for label, t, rss in _MARKS:
        print(f'{t - t0:8.3f}s  {rss:9.1f}MB  {label}')


# ---------------------------------------------------------------- boot mode


def run_boot(data_dir: Path, idle_seconds: float = 6.0) -> int:
    _mark('probe start')
    from htdt import native_cad

    _mark('import htdt.native_cad')

    # pydantic must be fully imported before PySide6: shiboken's signature
    # hook inspects __wrapped__ chains during later imports and trips a
    # circular import inside pydantic._migration otherwise.
    import pydantic  # noqa: F401
    from htdt import data_relocation  # noqa: F401

    _mark('import pydantic + data_relocation')

    from PySide6.QtWidgets import QApplication, QMessageBox

    # Offscreen there is no operator: modal notices/choice dialogs would
    # block the loop forever. Record and auto-accept each one — the
    # equivalent of a user clicking the default button instantly.
    real_box_exec = QMessageBox.exec
    dialogs_seen: list[str] = []

    def auto_box_exec(self):  # noqa: ANN001
        dialogs_seen.append(f'{self.windowTitle()}: {self.text()[:80]}')
        for button in self.buttons():
            if self.buttonRole(button) == QMessageBox.ButtonRole.AcceptRole:
                self.done(QMessageBox.StandardButton.Ok)
                self._clicked_btn = button  # noqa: SLF001
                return 0
        self.done(0)
        return 0

    QMessageBox.exec = auto_box_exec
    real_clicked = QMessageBox.clickedButton
    QMessageBox.clickedButton = lambda self: getattr(  # noqa: ANN001
        self, '_clicked_btn', real_clicked(self)
    )

    samples: list[tuple[float, float, float]] = []
    stop_sampling = threading.Event()

    def _sample() -> None:
        t_start = time.perf_counter()
        while not stop_sampling.wait(0.25):
            cpu = _PROC.cpu_times()
            samples.append(
                (
                    time.perf_counter() - t_start,
                    cpu.user + cpu.system,
                    _rss_mb(),
                )
            )

    real_exec = QApplication.exec
    boot_done_at: list[float] = []

    def timed_exec(self):  # noqa: ANN001
        _mark('QApplication.exec (interactive)')
        boot_done_at.append(time.perf_counter())
        threading.Thread(target=_sample, daemon=True).start()
        from PySide6.QtCore import QTimer

        QTimer.singleShot(int(idle_seconds * 1000), self.quit)
        return real_exec()

    QApplication.exec = timed_exec

    real_build = native_cad.build_workflow_shell

    def timed_build(*args, **kwargs):  # noqa: ANN002, ANN003
        window = real_build(*args, **kwargs)
        _mark('workflow shell built')
        real_show = window.show

        def timed_show():  # noqa: ANN001
            real_show()
            _mark('window.show()')

        window.show = timed_show
        return window

    native_cad.build_workflow_shell = timed_build

    # Phase timers around the heavyweight steps inside _run_gui: each
    # collaborator is a lazy attribute on native_cad, so wrapping the name
    # on the module object intercepts every construction.
    for attr in ('SceneRepository', 'ProjectLibraryRepository'):
        real_cls = getattr(native_cad, attr)

        def _timed(*args, _cls=real_cls, _attr=attr, **kwargs):  # noqa: ANN002, ANN003
            obj = _cls(*args, **kwargs)
            _mark(f'{_attr} ready')
            return obj

        setattr(native_cad, attr, _timed)

    real_upgrade = native_cad.execute_native_upgrade

    def timed_upgrade(*args, **kwargs):  # noqa: ANN002, ANN003
        _mark('upgrade plan start')
        result = real_upgrade(*args, **kwargs)
        _mark('execute_native_upgrade done')
        return result

    native_cad.execute_native_upgrade = timed_upgrade

    argv = ['--data-dir', str(data_dir)]
    code = native_cad.main(argv)
    stop_sampling.set()
    for text in dialogs_seen:
        print(f'modal shown: {text}')
    _report()
    if len(samples) > 2:
        cpu0, cpu1 = samples[1][1], samples[-1][1]
        span = samples[-1][0] - samples[1][0]
        print(
            f'idle cpu: {(cpu1 - cpu0) * 1000:.0f}ms over {span:.1f}s '
            f'({(cpu1 - cpu0) / span * 100:.2f}% of one core)'
        )
        print(f'idle rss drift: {samples[-1][2] - samples[1][2]:+.1f}MB')
    print(f'boot-to-interactive: {boot_done_at[0] - _MARKS[0][1]:.3f}s')
    return code


# ------------------------------------------------------------- session mode


def _gc_rss() -> float:
    gc.collect()
    return _rss_mb()


def run_session(data_dir: Path) -> int:
    _mark('probe start')
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([sys.argv[0]])

    from htdt.cad_repository import SceneRepository
    from htdt.project_library_repository import ProjectLibraryRepository
    from htdt.workflow_application import WorkflowApplicationComposition
    from htdt.workflow_navigation import WorkspaceId

    repository = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    library = ProjectLibraryRepository(repository)
    first = library.ensure_document_registered('probe-document-1')
    second = library.create_project('計測対象ルーム2')
    _mark('composition prerequisites')

    composition = WorkflowApplicationComposition(
        repository, first.document_id, project_library=library
    )
    composition.shell.show()
    app.processEvents()
    _mark('composition built + shown')

    tracemalloc.start(25)
    snap0 = tracemalloc.take_snapshot()
    base_rss = _gc_rss()
    print(f'\n{"operation":<38} {"dRSS_MB":>9} {"RSS_MB":>9}')

    def _op(label: str, count: int, fn) -> None:  # noqa: ANN001
        before = _gc_rss()
        for _ in range(count):
            fn()
            app.processEvents()
        after = _gc_rss()
        print(f'{label:<38} {after - before:>9.1f} {after:>9.1f}')

    destinations = [
        WorkspaceId.OVERVIEW,
        WorkspaceId.ROOM,
        WorkspaceId.MEASUREMENT,
        WorkspaceId.OPTIMIZATION,
    ]

    _op(
        'workspace cycle x10',
        10,
        lambda: [composition.shell.navigate(d) for d in destinations],
    )
    _op(
        'settings open/close x15',
        15,
        lambda: (
            composition.settings_dialog.open_settings(),
            composition.settings_dialog.close(),
        ),
    )
    _op(
        'palette open/close x15',
        15,
        lambda: (
            composition.command_palette.open(),
            composition.command_palette.palette.close(),
        ),
    )
    _op(
        'project switch x6',
        6,
        lambda: composition._switch_project(
            second.document_id
            if composition.document_id == first.document_id
            else first.document_id
        ),
    )

    snap1 = tracemalloc.take_snapshot()
    growth = _gc_rss() - base_rss
    print(f'\ntotal session growth: {growth:+.1f}MB (rss {base_rss:.1f} -> {_gc_rss():.1f})')
    print('top tracemalloc diffs (ops window):')
    for stat in snap1.compare_to(snap0, 'filename')[:15]:
        if stat.size_diff > 64 * 1024:
            print(f'  +{stat.size_diff / 1048576:6.2f}MB  {stat.traceback}')

    widgets = app.topLevelWidgets()
    print(f'top-level widgets at end: {len(widgets)}')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('boot', 'session'))
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--idle-seconds', type=float, default=6.0)
    parser.add_argument(
        '--profile',
        action='store_true',
        help='wrap boot mode in cProfile and dump the top frames',
    )
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    if args.mode == 'boot':
        if args.profile:
            import cProfile
            import pstats

            code = 0

            def _target() -> None:
                nonlocal code
                code = run_boot(args.data_dir, idle_seconds=args.idle_seconds)

            with cProfile.Profile() as prof:
                _target()
            pstats.Stats(prof).sort_stats('cumulative').print_stats(30)
            pstats.Stats(prof).sort_stats('tottime').print_stats(20)
            return code
        return run_boot(args.data_dir, idle_seconds=args.idle_seconds)
    return run_session(args.data_dir)


if __name__ == '__main__':
    raise SystemExit(main())
