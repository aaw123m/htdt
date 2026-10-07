# Issue #804 — UX160 owned-Windows acceptance matrix

Generated: 2026-10-07T02:09:44Z
Driver: `scripts/ux160_acceptance.py` + `scripts/ux160_driver.py`
Display: real Windows desktop (never offscreen); `QT_SCALE_FACTOR` drives the DPI rows.

| scale | scenario | run | nav | overflow | contexts | focus | disabled-reasons | palette | clean exit |
|---|---|---|---|---|---|---|---|---|---|
| 1.0 | seeded | RAN | ok | ok | ok | cycle=yes problems=0 | 0 | True | True |
| 1.0 | fresh | RAN | ok | ok | ok | cycle=yes problems=0 | 0 | True | True |
| 1.25 | seeded | RAN | ok | ok | ok | cycle=yes problems=0 | 0 | True | True |
| 1.25 | fresh | RAN | ok | ok | ok | cycle=yes problems=0 | 0 | True | True |
| 1.5 | seeded | RAN | ok | ok | ok | cycle=yes problems=0 | 0 | True | True |
| 1.5 | fresh | RAN | ok | ok | ok | cycle=yes problems=0 | 0 | True | True |
| 2.0 | seeded | RAN | ok | ok | ok | cycle=yes problems=0 | 0 | True | True |
| 2.0 | fresh | RAN | ok | ok | ok | cycle=yes problems=0 | 0 | True | True |

## Evidence

Per-cell screenshots: `<work-dir>/dpi-<pct>-<scenario>/shots/*.png`
Per-cell machine verdicts: `<work-dir>/dpi-*/verdict.json`

## Manual rows (not automatable)

- Real pointer/mouse + VTK camera/gizmo gestures at each DPI row
- Operator-judged readability/Japanese copy polish
- First-use discoverability impressions
