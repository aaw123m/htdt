# #804 UX160 owned-Windows acceptance — automated matrix

Issue #804 requires an owned-Windows acceptance of the UX160 surface across
supported DPI/input conditions, plus a single ACCEPTED/BLOCKED verdict. The
manual checklist it describes is now executable almost entirely by an
in-process harness on the real display.

## Harness

- `scripts/ux160_driver.py` — runs the application **in-process on the real
  display** (no offscreen override): navigates all destinations and contexts,
  captures per-destination screenshots, runs layout-overflow heuristics,
  Tab-focus-chain cycling, disabled-action reason checks, the Ctrl+K command
  palette, and a clean-exit probe. Emits `verdict.json` per cell plus
  screenshots under `shots/`.
- `scripts/ux160_acceptance.py` — subprocess-per-cell orchestrator that runs
  the driver at each `QT_SCALE_FACTOR` (DPI row) × scenario (seeded/fresh)
  and merges results into `matrix.json` and a markdown report
  (`docs/issues/issue-804-ux160-matrix.md`). Non-zero exit when any cell
  blocks, so it can act as a machine check in the verification manifest.

Run on the owned Windows box:

```powershell
C:\devin\python\python.exe scripts\ux160_acceptance.py `
  --work-dir C:\ux160 `
  --report docs\issues\issue-804-ux160-matrix.md
```

## What the matrix automates

| Issue row | Automation |
|---|---|
| 100%/125%/150%/200% DPI launches | `QT_SCALE_FACTOR` per cell |
| Every destination + context reachable | scripted navigation (~27 contexts) |
| No clipped/overlapping controls | min-size-hint overflow heuristic + screenshots |
| Golden path navigable | seeded scenario walks the full composition |
| Keyboard focus cycles | 400 simulated Tab presses, cycle detection |
| Disabled actions explain themselves | tooltip/statusTip/accessibleName audit |
| Stale state / restart | fresh + seeded data-dir scenarios |
| Command palette opens | Ctrl+K, ASCII query, dismiss |

What remains manual (kept as honest `manual` rows in the manifest): real
pointer/mouse gestures on VTK cameras and gizmos, subjective readability of
the Japanese copy at each DPI, and first-use discoverability judgments.

## Bug found and fixed by the matrix

The first full run flagged overflow findings at 150%/200% on **every**
destination: `WorkflowRail.COMPACT_WIDTH` was a fixed 72 px while the glyph
buttons need ~75–87 px at scaled fonts (`QT_SCALE_FACTOR ≥ 1.5`). The rail
now derives compact width from the widest button `sizeHint` plus its outer
margins (`WorkflowRail._compact_width`), with `COMPACT_WIDTH` retained as
the floor. See the regression assertions in
`test_compact_rail_uses_glyph_buttons_with_full_label_tooltips`.

## Verdict

All 8 automated cells pass after the fix (0 overflow findings, complete
focus cycles, 0 unlabeled disabled actions, palette operational, clean
exit). The full report with per-cell evidence:
`docs/issues/issue-804-ux160-matrix.md`. The issue's manual rows remain
open for a human pass.
