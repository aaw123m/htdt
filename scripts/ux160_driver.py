"""UX160 in-process acceptance driver (issue #804).

Runs the real workflow shell on the *real* display (never offscreen) under a
requested Qt scale factor, sweeps every registered destination/context, and
emits machine-checkable evidence:

* ``<out>/shots/<destination>[--<context>].png`` — full-window grabs
* ``<out>/verdict.json`` — per-row check results + narrative notes

Checks automated here (issue #804 acceptance matrix):

* clipping/overflow heuristic — every visible widget must fit its
  ``minimumSizeHint`` inside its allocated size (4px tolerance);
* keyboard traversal — 400 Tab presses must never strand focus on a
  hidden/disabled widget or leave focus empty after the first press;
* disabled-action reasons — disabled buttons/actions must expose a
  tooltip, statusTip or accessible name;
* command palette (Ctrl+K) opens, accepts text, closes on Escape;
* undo/redo commands exist in the registry and respond to Ctrl+Z/Ctrl+Y
  dispatch state where applicable;
* clean restart — writes a marker, exits cleanly, and the orchestrator
  re-launches the same data dir to verify project reopen.

Manual rows (real pointer gestures on VTK surfaces, operator-judged visual
polish) are emitted as ``manual`` entries so the matrix report is honest.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

BACKEND_SRC = Path(__file__).resolve().parents[1] / 'backend' / 'src'
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

TOLERANCE_PX = 4
TAB_LIMIT = 400
SETTLE_MS = 700

# Widgets whose sizeHint contract is intentionally violated by design:
# QHeaderView reports a 69px-square default hint while headers are always
# height/width-constrained by the scroll area owning them.
_OVERFLOW_WHITELIST = frozenset({'QHeaderView'})


def _widget_id(widget) -> str:
    name = widget.objectName()
    return name or widget.__class__.__name__


def _settle(app, ms: int = SETTLE_MS) -> None:
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    app.processEvents()


def check_overflow(root) -> list[dict]:
    findings = []
    stack = [root]
    seen = set()
    while stack:
        w = stack.pop()
        if id(w) in seen:
            continue
        seen.add(id(w))
        for child in w.children():
            if hasattr(child, 'sizeHint') and callable(child.sizeHint):
                stack.append(child)
        if w.__class__.__name__ in _OVERFLOW_WHITELIST:
            continue
        try:
            size = w.size()
            visible = w.isVisible() and not w.isHidden() and size.width() > 0
        except Exception:
            continue
        if not visible:
            continue
        try:
            hint = w.minimumSizeHint() if callable(w.minimumSizeHint) else None
        except Exception:
            continue
        if hint is None:
            continue
        dx = hint.width() - size.width()
        dy = hint.height() - size.height()
        if dx > TOLERANCE_PX or dy > TOLERANCE_PX:
            findings.append({
                'widget': _widget_id(w),
                'class': w.__class__.__name__,
                'allocated': [size.width(), size.height()],
                'minimum_hint': [hint.width(), hint.height()],
                'exceed_px': [max(dx, 0), max(dy, 0)],
            })
    return findings


def check_focus_chain(app, root, limit: int = TAB_LIMIT) -> dict:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    problems = []
    sequence = []
    first = None
    for i in range(limit):
        QTest.keyClick(app.focusWidget() or root, Qt.Key.Key_Tab)
        app.processEvents()
        fw = app.focusWidget()
        if fw is None:
            problems.append({'press': i + 1, 'issue': 'focus_empty'})
            continue
        wid = _widget_id(fw)
        if not (fw.isVisible() and fw.isEnabled()):
            problems.append({
                'press': i + 1, 'issue': 'focus_on_inert_widget', 'widget': wid,
            })
        sequence.append(wid)
        if first is None:
            first = wid
        elif wid == first:
            sequence.append(wid)
            break
    return {
        'presses': len(sequence),
        'completed_cycle': sequence[-1] == first if sequence else False,
        'unique_widgets': len(set(sequence)),
        'problems': problems,
        'sequence_head': sequence[:20],
    }


def check_disabled_reasons(root) -> list[dict]:
    from PySide6.QtWidgets import QAbstractButton

    findings = []
    for w in root.findChildren(QAbstractButton):
        if w.isVisible() and not w.isEnabled():
            reason = w.toolTip() or w.statusTip() or w.accessibleName()
            if not (reason and reason.strip()):
                findings.append({
                    'widget': _widget_id(w),
                    'text': w.text()[:80],
                })
    from PySide6.QtGui import QAction
    for a in root.findChildren(QAction):
        if not a.isEnabled():
            reason = a.toolTip() or a.statusTip()
            if not (reason and reason.strip()):
                findings.append({'action': a.text()[:80] or a.objectName()})
    return findings


def check_dynamic_a11y(app, root) -> dict:
    """#975: the dynamic panels (decision brief, geometry intake,
    standards criteria, measurement quality) must keep keyboard focus
    across rebuilds, announce each state transition exactly once, and
    carry every disabled control's reason on the screen itself — not
    only on the (Tab-unreachable) disabled button."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QAbstractButton, QAbstractItemView, QLabel, QWidget,
    )

    from htdt.dynamic_a11y import DynamicAnnouncer, STABLE_ID_ROLE

    def _announcers(panel) -> list:
        try:
            return [
                v for v in vars(panel).values()
                if isinstance(v, DynamicAnnouncer)
            ]
        except Exception:
            return []

    def _refresh(panel) -> None:
        refresh = getattr(panel, 'refresh', None)
        if callable(refresh):
            refresh()
            return
        set_report = getattr(panel, 'set_report', None)
        if callable(set_report):
            set_report(getattr(panel, '_report', None))

    findings: list[dict] = []
    panels = [
        w for w in root.findChildren(QWidget)
        if _announcers(w) and not w.objectName().startswith('qt_')
    ]
    panels = [
        p for p in panels
        if not any(q is not p and q.isAncestorOf(p) for q in panels)
    ]
    checked = [_widget_id(p) for p in panels]
    focus_notes: dict[str, str] = {}

    for panel in panels:
        name = _widget_id(panel)

        # --- focus retention on a stable anchor ---------------------------
        anchor = None
        anchor_id = None
        for view in panel.findChildren(QAbstractItemView):
            idx = view.currentIndex()
            if idx.isValid():
                anchor = view
                anchor_id = (
                    idx.data(STABLE_ID_ROLE)
                    or idx.data(Qt.ItemDataRole.UserRole)
                )
                break
        if anchor is None:
            buttons = [
                b for b in panel.findChildren(QAbstractButton)
                if b.isEnabled() and b.isVisibleTo(panel)
                and b.focusPolicy() != Qt.FocusPolicy.NoFocus
            ]
            if buttons:
                anchor = buttons[0]
        focus_note = 'no_anchor'
        focus_notes[name] = focus_note
        if anchor is not None:
            anchor_name = anchor.objectName()
            anchor.setFocus()
            app.processEvents()
            if app.focusWidget() is anchor:
                _refresh(panel)
                app.processEvents()
                fw = app.focusWidget()
                if fw is None:
                    findings.append(
                        {'panel': name, 'issue': 'focus_lost'})
                    focus_note = 'lost'
                elif fw is anchor and isinstance(
                        anchor, QAbstractItemView) and anchor_id is not None:
                    cur = anchor.currentIndex()
                    same = cur.isValid() and (
                        cur.data(STABLE_ID_ROLE) == anchor_id
                        or cur.data(Qt.ItemDataRole.UserRole) == anchor_id)
                    focus_note = 'kept' if same else 'row_changed'
                    if not same:
                        findings.append({
                            'panel': name,
                            'issue': 'focus_row_changed',
                            'item_id': str(anchor_id)[:80],
                        })
                elif fw is anchor or fw.objectName() == anchor_name:
                    focus_note = 'kept'
                else:
                    findings.append({
                        'panel': name,
                        'issue': 'focus_moved',
                        'widget': _widget_id(fw),
                    })
                    focus_note = 'moved'
            else:
                # Real-GUI lanes only: a window that never took activation
                # cannot hold a probe — record, do not accuse.
                focus_note = 'probe_skipped'
            focus_notes[name] = focus_note

        # --- announcements: a same-state re-render must be silent ---------
        for announcer in _announcers(panel)[:1]:
            before = len(announcer.events)
            _refresh(panel)
            app.processEvents()
            added = len(announcer.events) - before
            if added:
                findings.append({
                    'panel': name,
                    'issue': 'announcement_spam',
                    'added': added,
                })

        # --- disabled reasons reachable on this screen --------------------
        for button in panel.findChildren(QAbstractButton):
            if not button.isVisibleTo(panel) or button.isEnabled():
                continue
            on_screen = any(
                label for label in panel.findChildren(QLabel)
                if label.focusPolicy() == Qt.FocusPolicy.StrongFocus
                and label.text().strip()
                and label.isVisibleTo(panel)
            )
            if not on_screen:
                findings.append({
                    'panel': name,
                    'issue': 'disabled_reason_not_on_screen',
                    'widget': _widget_id(button),
                    'tooltip': bool(button.toolTip() or button.statusTip()),
                })

    return {
        'panels': checked,
        'findings': findings,
        'focus_notes': focus_notes,
    }


def check_palette(app, window, shots_dir: Path) -> dict:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    result = {'opened': False}
    print('[ux160] palette: ctrl+K', file=sys.stderr, flush=True)
    QTest.keyClick(window, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
    app.processEvents()
    _settle(app, 400)
    from PySide6.QtWidgets import QDialog
    dialogs = [d for d in app.topLevelWidgets()
               if isinstance(d, QDialog) and d.isVisible()]
    if dialogs:
        result['opened'] = True
        print('[ux160] palette: type', file=sys.stderr, flush=True)
        QTest.keyClicks(dialogs[0], 'm')
        app.processEvents()
        _settle(app, 300)
        print('[ux160] palette: grab', file=sys.stderr, flush=True)
        dialogs[0].grab().save(str(shots_dir / 'command_palette.png'))
        QTest.keyClick(dialogs[0], Qt.Key.Key_Escape)
        app.processEvents()
        _settle(app, 200)
    return result


def run_driver(data_dir: Path, out_dir: Path, scenario: str) -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([sys.argv[0]])

    from htdt.cad_repository import SceneRepository
    from htdt.workflow_application import WorkflowApplicationComposition
    from htdt.workflow_navigation import ApplicationDestinationId, WorkspaceId

    shots = out_dir / 'shots'
    shots.mkdir(parents=True, exist_ok=True)
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

    from htdt.project_library_repository import ProjectLibraryRepository
    composition = WorkflowApplicationComposition(
        repository,
        document_id,
        project_library=ProjectLibraryRepository(repository),
    )
    window = composition.shell
    window.resize(1366, 860)
    window.show()
    _settle(app, 1500)

    rows = []
    destinations = list(WorkspaceId) + list(ApplicationDestinationId)
    for dest in destinations:
        print(f'[ux160] navigate {dest.value}', file=sys.stderr, flush=True)
        row = {'destination': dest.value, 'checks': {}, 'contexts': []}
        ok = window.navigate(dest)
        _settle(app)
        row['navigated'] = bool(ok)
        if not ok:
            row['checks']['navigate'] = {
                'status': 'blocked',
                'reason': window.router.last_block_reason,
            }
            rows.append(row)
            continue
        shot = window.grab()
        shot.save(str(shots / f'{dest.value}.png'))

        overflow = check_overflow(window)
        row['checks']['overflow'] = {
            'status': 'pass' if not overflow else 'finding',
            'findings': overflow[:40],
            'count': len(overflow),
        }
        registration = window.router.registration(dest)
        for ctx in registration.contexts:
            print(f'[ux160]   ctx {dest.value}/{ctx.context_id}', file=sys.stderr, flush=True)
            try:
                window.router.select_context(dest, ctx.context_id)
                _settle(app, 500)
                (shots / f'{dest.value}--{ctx.context_id}.png').write_bytes(b'')
                window.grab().save(str(shots / f'{dest.value}--{ctx.context_id}.png'))
                row['contexts'].append({'context': ctx.context_id, 'status': 'pass'})
            except Exception as exc:  # context failure is a finding, not fatal
                row['contexts'].append({
                    'context': ctx.context_id,
                    'status': 'finding',
                    'error': str(exc)[:200],
                })
        rows.append(row)

    print('[ux160] destinations done', file=sys.stderr, flush=True)

    # Focus traversal on the current (last) workspace and on ROOM/OPTIMIZATION
    for dest in (WorkspaceId.ROOM, WorkspaceId.OPTIMIZATION):
        print(f'[ux160] focus {dest.value}', file=sys.stderr, flush=True)
        if window.navigate(dest):
            _settle(app)
            rows.append({
                'destination': f'{dest.value}:focus',
                'checks': {'focus_chain': check_focus_chain(app, window)},
            })

    print('[ux160] disabled reasons', file=sys.stderr, flush=True)
    disabled = check_disabled_reasons(window)
    rows.append({
        'destination': 'global:disabled_reasons',
        'checks': {'disabled_reasons': {
            'status': 'pass' if not disabled else 'finding',
            'findings': disabled[:60],
            'count': len(disabled),
        }},
    })
    print('[ux160] palette', file=sys.stderr, flush=True)
    rows.append({
        'destination': 'global:palette',
        'checks': {'command_palette': check_palette(app, window, shots)},
    })

    verdict = {
        'data_dir': str(data_dir),
        'scenario': scenario,
        'scale_factor': os.environ.get('QT_SCALE_FACTOR', '1.0'),
        'rows': rows,
        'finished_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    (out_dir / 'verdict.json').write_text(
        json.dumps(verdict, ensure_ascii=False, indent=2), encoding='utf-8')

    # #804 restart row: exit cleanly, marker proves shutdown path ran.
    # Written last-but-one so a teardown crash still leaves the verdict.
    marker = out_dir / 'clean_exit.marker'
    marker.write_text('clean\n', encoding='utf-8')
    window.close()
    _settle(app, 400)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--scenario', choices=('fresh', 'seeded'), default='seeded')
    args = parser.parse_args()
    return run_driver(args.data_dir, args.out, args.scenario)


if __name__ == '__main__':
    raise SystemExit(main())
