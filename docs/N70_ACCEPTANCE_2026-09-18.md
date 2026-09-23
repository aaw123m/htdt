# N70 Windows acceptance — 2026-09-18

Tracking: Issue #63 / PR #64  
Branch: `feat/n70-prediction-visualization`  
Result: **PASS**

## Accepted revisions

- Last N70 product-code change: `76c21eed7d7efcff23905e8af977854669df2752`
- Final accepted gate/head: `2a6eaae351beb8b2cbbef23a07e3054bb4fb1c93`
- Product CI: #309 / run `35270706491` — PASS
- Final harness CI: #312 / run `35272332747` — PASS

The commits after `76c21eed...` only harden the Windows acceptance harness and hardware-gate runner. They do not change N70 runtime product semantics.

## Owned Windows environment

- Windows 11 Pro 10.0.26200 build 26200
- AMD Ryzen 7 8845HS w/ Radeon 780M Graphics
- RAM 31.31 GiB
- AMD Radeon 780M Graphics, driver 32.0.13032.11
- Display 2880×1800
- AppliedDPI 192 (200%)
- Python 3.12.10
- PySide6 6.11.2
- PyVista 0.49.0
- VTK 9.7.0
- PyQtGraph 0.14.0

## A13 — async stale/cancel/document boundary

Final real-Windows result:

```text
A13_N70_PRODUCT_COMPOSITION True
A13_N70_STALE_WORKER_STARTED True
A13_N70_MOUSE_SCENE_SELECT True
A13_N70_MOUSE_DRAG True
A13_N70_SAVE_CHANGED_REVISION True
A13_N70_EDIT_MAKES_RESULT_STALE True
A13_N70_UI_RESPONSIVE True 57
A13_N70_CANCEL_WORKER_STARTED True
A13_N70_CANCEL_REGISTERED True
A13_N70_CANCELLED_RESULT_NOT_APPLIED True
A13_N70_DOCUMENT_WORKER_STARTED True
A13_N70_DOCUMENT_SWITCH_RESULT_NOT_APPLIED True
A13_N70_CLEAN_EXIT_NO_WORKER True 0
A13_N70_RESULT PASS
```

Acceptance meaning:

- prediction jobs capture immutable submission input;
- a later SceneRevision is not overwritten by stale completion;
- explicit cancellation prevents result application/persistence;
- document switch rejects the old completion;
- GUI event processing remains responsive while the worker is pending;
- closing leaves no live prediction worker.

## A14 — non-rectangular room / rectangular-only model

Fixture: 8-vertex concave L-room from F2.

```text
A14_PRODUCT_COMPOSITION True
A14_FAST_COMPLETION_OBSERVED True
A14_NONRECT_UNSUPPORTED True
A14_NO_SILENT_RECTANGULAR_APPROXIMATION True
A14_UNSUPPORTED_HAS_NO_OVERLAY True
A14_SCALAR_FIELD_CONTROL_GATED True
A14_RESULT PASS
```

The exact rectangular geometry model does not silently substitute the L-room bounding rectangle. It persists `unsupported`, retains the exact 8-vertex polygon in the input snapshot, stores no mode/reflection payload for that unsupported run, draws no prediction overlay, and keeps scalar-field controls disabled.

A14 in `CAD_EDITOR_ACCEPTANCE.md` spans N70–N80. In this N70 unsupported branch there is deliberately no candidate to preview/apply, so an apply/Undo command is not generated. Candidate preview/application remains an N80 gate when O10/O20-backed candidates exist. This is preferable to fabricating a rectangular approximation merely to exercise an apply path.

## F5 — 50 editable objects + 10,000 analysis markers

Final owned-PC result:

```text
F5_PRODUCT_COMPOSITION True
F5_EDITABLE_OBJECT_COUNT 50
F5_MARKER_COUNT 10000
F5_MARKER_ACTOR_DELTA 1
F5_MARKER_NON_PICKABLE True
F5_FIRST_RENDER_MS 11.406
F5_ORBIT_FRAME_P50_MS 22.993
F5_ORBIT_FRAME_P95_MS 27.963
F5_ORBIT_FRAME_MAX_MS 31.205
F5_PERFORMANCE_BUDGET_STATUS MEASURE_ONLY_NO_PRESET_BUDGET
F5_RESULT PASS
```

The 10,000 analysis markers are represented as one `PyVista.PolyData` / one non-pickable mesh actor, not 10,000 editable actors. F5 intentionally records the first owned-PC measurements before defining a regression budget, as required by the acceptance plan.

The F5 `64^3 scalar grid` is explicitly a later/conditional part of the fixture. N70 has no validated model that produces a spatial scalar SPL field, so HTDT does not synthesize one. Heatmap/slice/volume controls remain gated until a real model result contains such a field.

## Gate cleanup

```text
N70_GATE_A13_A14_EXIT=0
N70_GATE_F5_EXIT=0
N70_RESTORED_SHA=5ede848e8e0b0967a50c04c83ff679a649ca439b
N70_POST_STATUS_COUNT=0
N70_RESTORE_OK=True
N70_HARDWARE_GATE_RESULT=PASS
```

The local validation checkout was restored to the exact pre-gate detached SHA with no tracked or untracked residue.

## Diagnostic history

Two earlier gate attempts exposed acceptance-harness races rather than product-contract failures:

1. A fixed-delay A13 worker could finish while the 200% DPI real-mouse drag was still in progress, creating a VTK interaction race. The gate was changed to deterministic latches.
2. The intentionally unsupported A14 calculation can complete and clear its transient token before the harness observes it. The harness now treats that fast completion as valid only if the persisted `unsupported` contract assertions all pass.

Both harness-only fixes were CI-validated before the final hardware gate.

## N70 conclusion

N70 technical acceptance is complete for the implemented model boundary:

- immutable native prediction authority and exact input identity;
- rectangular geometry candidates without semantic promotion to validated FR/SPL;
- explicit `unsupported` handling for non-rectangular rooms;
- stale/cancel/document-safe async execution;
- non-pickable prediction overlays and truthful scalar-field gating;
- bulk 10,000-marker rendering with measured owned-PC performance.

Next roadmap milestone after merge: **N80 — 最適化workspace**.