# Round 22 — test rot on main

Scope: the full `backend/tests` suite run end-to-end on current main
(`34e6d8ff`, post REV21-EXPORT) under Qt-offscreen (`QT_QPA_PLATFORM=
offscreen`), Python 3.12.10 (NuGet `python` package at `C:/devin/python`),
`pip install -e backend[dev]` — i.e. every dev dependency present,
including scipy. Branch `devin/rev22-rot`.

Suite gate (local, no GitHub Actions):

    TMPDIR=C:\t PYTHONIOENCODING=utf-8 QT_QPA_PLATFORM=offscreen \
      C:/devin/python/python.exe -m pytest -p pydantic backend/tests \
      -q -n 4 -p no:warnings

`-p pydantic` pre-imports pydantic before any test module touches
PySide6 — without it the shiboken↔pydantic circular import can surface
as a collection error.

## Result

**6684 passed, 195 skipped, 0 failed, 0 errors** — 6879 test outcomes in
~75 min on this branch. (Under `-n 4` xdist, pytest on this box does not
emit the usual "N passed in Ns" terminal line; counts above are the
per-test marks in the run log, and the process exit code is 0.)

## Reported offenders — verified on current main

1. **`test_review_round3` + `test_round18_cancel_paths` — stale
   `_BusyFakeController` fixture (missing `operation_cancelled`).**
   Already fixed on main. `DataManagementController` grew the
   `operation_cancelled` signal in the R19 worker-contracts round, and
   commit `21237cdd` (REV21-EXPORT, merged as `34e6d8ff`) updated the
   fake to match — `backend/tests/test_round18_cancel_paths.py:291`
   declares `operation_cancelled = Signal(object)`, the full ten-signal
   contract of `data_management.py:739-748`. Both files pass: 8 + 7
   tests green. The earlier reports were against a pre-REV21-EXPORT
   main.

2. **`test_rev18_acoustic_truth` collection error when scipy is
   absent.** Real, latent rot — fixed this round. The module imported
   `scipy.signal` unconditionally at line 33, so a machine without the
   dev extra failed at *collection* (an `E`, not a skip). Guarded with
   `scipy_signal = pytest.importorskip("scipy.signal")` — the same
   convention the suite already uses for pyroomacoustics, PySide6,
   h5py, pymupdf and yaml — and the four `scipy.signal.*` call sites
   rebound. With scipy installed the module collects and all 29 tests
   pass unchanged; without it the module now skips cleanly.

## Full-suite sweep

The entire suite runs green on main with deps installed — **zero
failures, zero errors** beyond clean skips. Skips are the suite's own
environment gates doing their job: external-solver/hardware fixtures
(pyroomacoustics, MFEM/PFFDTD harness paths, GPU/audio devices) that are
designed to skip offscreen on a dependency-light box. They are honest
skips, not silenced failures — each gates on the missing capability and
runs on a machine that has it.

## Classification

| Item | Class | Disposition |
|------|-------|-------------|
| `_BusyFakeController` signal drift | test-side rot | already fixed on main (`21237cdd`) |
| `test_rev18_acoustic_truth` scipy collection error | test-side rot | fixed — `importorskip` guard |
| product regressions | — | **none found** — no test failure this round traced to product code |

Deferred: none.
