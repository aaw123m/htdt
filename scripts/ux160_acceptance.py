"""UX160 acceptance-matrix orchestrator (issue #804).

Runs ``scripts/ux160_driver.py`` once per (scale factor, scenario) cell on the
real display, then merges the per-cell ``verdict.json`` files into a single
matrix report at ``docs/issues/issue-804-ux160-matrix.md``.

Usage::

    python scripts/ux160_acceptance.py \
        --work-dir C:/path/to/ux160 \
        --report   C:/path/to/repo/docs/issues/issue-804-ux160-matrix.md \
        [--scales 1.0,1.25,1.5,2.0] [--scenarios seeded,fresh]

Each cell is an independent subprocess so a native crash in one cell never
poisons the matrix: a crashed cell is recorded BLOCKED with its stderr tail.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / 'scripts' / 'ux160_driver.py'
PYTHON = Path(sys.executable)


def run_cell(python: Path, scale: str, scenario: str, data_dir: Path,
             out_dir: Path, log_path: Path) -> dict:
    env = dict(os.environ)
    env['QT_SCALE_FACTOR'] = scale
    env['PYTHONIOENCODING'] = 'utf-8'
    env.pop('QT_QPA_PLATFORM', None)
    cmd = [str(python), '-u', str(DRIVER),
           '--data-dir', str(data_dir), '--out', str(out_dir),
           '--scenario', scenario]
    with open(log_path, 'w', encoding='utf-8') as log:
        try:
            proc = subprocess.run(cmd, env=env, stdout=log, stderr=log,
                                  timeout=600)
        except subprocess.TimeoutExpired:
            # subprocess.run already killed the child; record the cell
            # BLOCKED like any other crashed cell instead of aborting the
            # whole matrix run.
            proc = None
    verdict_path = out_dir / 'verdict.json'
    cell = {
        'scale': scale, 'scenario': scenario,
        'returncode': proc.returncode if proc is not None else None,
        'clean_exit': (out_dir / 'clean_exit.marker').exists(),
        'log': str(log_path),
    }
    if proc is not None and verdict_path.exists():
        cell['verdict'] = json.loads(
            verdict_path.read_text(encoding='utf-8'))
        cell['status'] = 'ran'
    else:
        cell['status'] = 'blocked'
        if proc is None:
            cell['detail'] = 'timeout'
        tail = log_path.read_text(encoding='utf-8', errors='replace')[-2000:]
        cell['stderr_tail'] = tail
    return cell


def summarize_cell(cell: dict) -> dict:
    if cell['status'] == 'blocked':
        return {
            'status': 'BLOCKED',
            'detail': cell.get('detail') or f"rc={cell['returncode']}",
        }
    rows = cell['verdict'].get('rows', [])
    nav_fail = [r['destination'] for r in rows if r.get('navigated') is False]
    overflow = {
        r['destination']: r['checks']['overflow']['count']
        for r in rows
        if isinstance(r.get('checks', {}).get('overflow'), dict)
        and r['checks']['overflow'].get('count')
    }
    ctx_fail = [
        f"{r['destination']}/{c['context']}"
        for r in rows for c in r.get('contexts', [])
        if c.get('status') != 'pass'
    ]
    focus = [
        r['checks']['focus_chain']
        for r in rows if 'focus_chain' in r.get('checks', {})
    ]
    focus_problems = sum(len(f.get('problems', [])) for f in focus)
    focus_cycles = all(f.get('completed_cycle') for f in focus) if focus else None
    disabled = next((r['checks']['disabled_reasons']['count'] for r in rows
                     if 'disabled_reasons' in r.get('checks', {})), 0)
    palette = next((r['checks']['command_palette'].get('opened')
                    for r in rows if 'command_palette' in r.get('checks', {})),
                   None)
    return {
        'status': 'RAN',
        'nav_failures': nav_fail,
        'overflow': overflow,
        'context_failures': ctx_fail,
        'focus_cycles_complete': focus_cycles,
        'focus_problems': focus_problems,
        'disabled_missing_reasons': disabled,
        'palette_opened': palette,
        'clean_exit': cell['clean_exit'],
    }


def write_report(cells: list[dict], report_path: Path) -> None:
    lines = [
        '# Issue #804 — UX160 owned-Windows acceptance matrix',
        '',
        f'Generated: {time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}',
        'Driver: `scripts/ux160_acceptance.py` + `scripts/ux160_driver.py`',
        'Display: real Windows desktop (never offscreen); `QT_SCALE_FACTOR` '
        'drives the DPI rows.',
        '',
        '| scale | scenario | run | nav | overflow | contexts | focus | '
        'disabled-reasons | palette | clean exit |',
        '|---|---|---|---|---|---|---|---|---|---|',
    ]
    for cell in cells:
        s = summarize_cell(cell)
        if s['status'] == 'BLOCKED':
            lines.append(
                f"| {cell['scale']} | {cell['scenario']} | **BLOCKED** "
                f"({s['detail']}) | - | - | - | - | - | - | - |")
            continue
        nav = 'ok' if not s['nav_failures'] else ','.join(s['nav_failures'])
        ov = ('ok' if not s['overflow'] else
              '; '.join(f"{k}={v}" for k, v in s['overflow'].items()))
        ctx = 'ok' if not s['context_failures'] else ','.join(s['context_failures'])
        focus = (f"cycle={'yes' if s['focus_cycles_complete'] else 'NO'} "
                 f"problems={s['focus_problems']}")
        lines.append(
            f"| {cell['scale']} | {cell['scenario']} | RAN | {nav} | {ov} | "
            f"{ctx} | {focus} | {s['disabled_missing_reasons']} | "
            f"{s['palette_opened']} | {s['clean_exit']} |")
    lines += [
        '',
        '## Evidence',
        '',
        'Per-cell screenshots: `<work-dir>/dpi-<pct>-<scenario>/shots/*.png`',
        'Per-cell machine verdicts: `<work-dir>/dpi-*/verdict.json`',
        '',
        '## Manual rows (not automatable)',
        '',
        '- Real pointer/mouse + VTK camera/gizmo gestures at each DPI row',
        '- Operator-judged readability/Japanese copy polish',
        '- First-use discoverability impressions',
        '',
    ]
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text('\n'.join(lines), encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--work-dir', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--python', type=Path, default=PYTHON)
    parser.add_argument('--scales', default='1.0,1.25,1.5,2.0')
    parser.add_argument('--scenarios', default='seeded,fresh')
    args = parser.parse_args()

    cells = []
    for scale in args.scales.split(','):
        pct = str(int(float(scale) * 100))
        for scenario in args.scenarios.split(','):
            tag = f'dpi-{pct}-{scenario}'
            data_dir = args.work_dir / f'data-{scenario}'
            out_dir = args.work_dir / tag
            out_dir.mkdir(parents=True, exist_ok=True)
            print(f'[ux160] cell {tag} ...', flush=True)
            cell = run_cell(
                args.python, scale, scenario,
                data_dir, out_dir,
                args.work_dir / f'{tag}.log',
            )
            print(f'[ux160] cell {tag} -> {cell["status"]} '
                  f'rc={cell["returncode"]}', flush=True)
            cells.append(cell)

    matrix_path = args.work_dir / 'matrix.json'
    matrix_path.write_text(
        json.dumps(cells, ensure_ascii=False, indent=2), encoding='utf-8')
    write_report(cells, args.report)
    print(f'[ux160] matrix -> {matrix_path}')
    print(f'[ux160] report -> {args.report}')
    blocked = [c for c in cells if c['status'] == 'blocked']
    return 0 if not blocked else 1


if __name__ == '__main__':
    raise SystemExit(main())
