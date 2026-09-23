# N50 Windows acceptance — A11

Date: 2026-09-17  
Tracking: Issue #59 / PR #60  
Acceptance head: `a8ba8d0507c404a9058c0ffe12475fce4dca968c`  
Last product-code change before acceptance: `8762c5e15c1f8c9442ce6ef6378f24881e0f5236`

## Result

**A11 PASS.** The normal native product composition was exercised on the owned Windows PC with real Win32 mouse input. Hard wall-clearance and walkway violations were rejected without silently changing the committed pose, the corresponding reason could be selected, and the CAD scene exposed the constrained subject plus wall/region and numeric distance evidence.

## Environment

- Windows 11 Pro 10.0.26200 / build 26200
- AMD Ryzen 7 8845HS w/ Radeon 780M Graphics
- AMD Radeon 780M Graphics driver 32.0.13032.11
- RAM: 31.3 GiB
- Display: 2880×1800
- AppliedDPI: 192 (200%)
- Python 3.12.10
- PySide6 6.11.2
- PyVista 0.49.0
- VTK 9.7.0
- Runtime: existing `C:\Users\ka092\Desktop\HTDT\repo\.venv\Scripts\python.exe`

The machine's `py -3.12` launcher pointed to a separate bare Python installation without PySide6, so that preflight invocation stopped before product startup. No environment was created or modified for acceptance; the existing HTDT acceptance venv above was used for the final gate.

## GitHub verification before hardware gate

GitHub Actions CI #238, run `35201888464`, passed on product-code head `8762c5e15c1f8c9442ce6ef6378f24881e0f5236`.

That run passed:

- backend tests, including CAD↔G10 adapter, persistence workspace, commit policy, and hide/lock feasibility invariants
- backend and native CAD launcher CLI checks
- Windows CAD acceptance harness compilation, including `scripts/validate_n50_windows.py`
- PowerShell syntax validation
- frontend build and built-app smoke test

Later commits before the hardware gate changed documentation only.

## Fixture and interaction

The A11 fixture starts in a valid state and contains:

- a normal N50 `ConstraintEditorWindow` product composition
- stable wall topology
- a speaker with a right-wall clearance constraint
- a seat with a walkway/exclusion-region constraint

The gate uses OS mouse input to move the speaker and seat into violating candidates. The candidate is evaluated through the existing G10 placement-constraint authority via the N50 adapter before commit.

For a newly introduced hard violation, the candidate is rejected, the previous committed pose remains authoritative, and no silent placement is accepted. The rejection result remains available so the user can select and inspect the concrete reason.

## Final output

```text
A11_PRODUCT_COMPOSITION True
A11_INITIAL_FEASIBLE True
A11_MOUSE_WALL_REJECT True
A11_MOUSE_WALL_REASON True
A11_MOUSE_WALKWAY_REJECT True
A11_MOUSE_WALKWAY_REASON True
A11_EDIT_AFTER_REJECT True
A11_CONSTRAINT_PERSISTENCE True
A11_QUALITY_WORDING_ABSENT True
A11_RESULT PASS
A11_EXIT=0
PRE_STATUS_COUNT=0
POST_STATUS_COUNT=0
```

## What A11 establishes

- The normal native launcher resolves to the N50 composition.
- A wall-clearance violating mouse move is not silently committed.
- Selecting the wall-clearance reason exposes the subject, selected wall, and actual/required distance evidence in the native workspace.
- A walkway/exclusion-region violating mouse move is not silently committed.
- Selecting the walkway reason exposes the subject and region overlay.
- After a rejected move, ordinary editing still works.
- Constraint definitions reopen from the N50 document-scoped workspace unchanged.
- UI wording describes constraint satisfaction/violation only; feasibility is not presented as sound quality, recommendation, rank, or score.
- The local checkout is clean before and after the acceptance run.

## Persistence and topology boundary

N50 constraint definitions are document-scoped authoring state stored beside `SceneRepository` in the same SQLite database, not inside `SceneRevision`. Evaluation results are derived and are never persisted as authoritative scene state.

A full N50 wall-clearance constraint stores stable N30b `wall_id`. The adapter regenerates the transient G10 endpoint edge ID at evaluation time. When a referenced wall is split/merged/deleted, N50 does not guess the intended successor scope; the topology edit is explicitly blocked until the user removes or redefines the full constraint.

For later asynchronous measurement/prediction jobs, mutable latest constraint workspace state must not substitute for captured job input. N60/N70 work must snapshot/hash the relevant constraint workspace together with the exact SceneRevision when submitting a job.

## Known intentional limits

- Constraint-definition authoring itself does not yet participate in Scene command Undo/history.
- Rectangular physical bodies are represented to current G10 footprint constraints by a conservative circumscribed horizontal radius, not exact oriented-box collision.
- N50 does not automatically migrate full wall-clearance semantics across wall topology changes.
- Optimization, ranking, prediction quality, and acoustic recommendation remain outside N50.
