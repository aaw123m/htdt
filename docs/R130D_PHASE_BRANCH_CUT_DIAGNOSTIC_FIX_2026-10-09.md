# R130D #938 — circular phase separation in non-convergence diagnostics (2026-10-09)

## Defect and scope

The diagnostic worsening-bin classifier in `scripts/run_r130d_reproduction_isolation_diagnostic.py` calculated `abs(angle(b) - angle(a))`. The individual principal angles live on `[-180°, +180°]`, so a genuine `+179° -> -179°` difference of **2°** could be misclassified as **358°**. That is a branch-cut error in the **diagnostic**, not evidence of an FDTD physics defect.

The diagnosis now uses `wrapped_phase_separation_deg(a, b)`, the shortest unsigned angular separation on the circle (bounded 0–180°). Non-finite samples and exact-zero complex inputs are rejected rather than claiming a defined phase at a null.

Tests in `backend/tests/test_issue_938_reproduction_isolation.py` pin branch crossings, reversal symmetry, exactly opposite phasors, larger real phase changes, and undefined inputs.

## Preserved evidence and unresolved numerical gate

The committed #938 reproduction evidence has **18 worsening-bin rows**, with maximum previously recorded `phase_delta_deg = 150.31629523935882` and **no values >180°**. Thus this fix prevents a real, falsifiable classification defect in future runs; it does **not** reclassify those 18 historical rows or change the canonical 40/80 Hz transfer, 8/10/12 PPW schedule, phase mask, acceptance thresholds, or hashes.

The existing #947 boundary/absorbing-halo correction is already merged, and it does not clear the original #938 self-convergence failure. The R130D physical issue remains a likely pre-asymptotic geometry/stencil/position-dependent discretization limitation. It still requires:

1. An independently built pinned MFEM reference and additional numerical-resolution experiments, not simply replay of the original coarse 8/10/12 PPW.
2. Predeclared, versioned numerical experiments testing denser spatial resolutions and potentially an independently qualified boundary representation. Never promote a diagnostic variant under the frozen #938 acceptance plan.
3. Fresh numerical self-convergence for both solvers before any cross-solver or production recommendation claim. BRAS and owned-room physical validation remain separate gates.

**Verdicts unchanged:** `SELF_CONVERGENCE_FAILED`, `CROSS_SOLVER_BLOCKED`, `NOT_VALIDATED`; no production solver selected.

## Local tests

On an isolated Windows Python 3.12 virtual environment with pinned numerical Python dependencies, a scoped copy of the two test modules (outside the repo-wide GUI conftest) yielded **21 passed**: `test_issue_938_reproduction_isolation.py` and `test_issue947_boundary_halo.py`. The default full-suite invocation on this environment was not usable without `PySide6`, and full pinned MFEM/PFFDTD numerical replay was not run.
