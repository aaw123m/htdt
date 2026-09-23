# N60 Windows acceptance — 2026-09-17

Tracking: Issue #61 / PR #62  
Milestone: N60 — 実測workspace  
Result: **PASS**

## Accepted revisions

- Final hardware-gate branch/head: `73b77866ebdc42f70a016e2ebd13ad99858b01b8`
- Last product-code head: `8551963c4dc3e5eb5a22bd0683edbaea6e891cdb`
- Difference after the last product-code head: only `scripts/validate_n60_a13_windows.py`
- The A13-only change makes the explicit-cancel acceptance deterministic; native runtime/product code is unchanged after `8551963c...`.
- Base main before N60: `160c62e77ca9e1a32510795bf93604a6e7a3fd44`

## GitHub verification

Product head `8551963c4dc3e5eb5a22bd0683edbaea6e891cdb` passed:

- CI #300
- run `35223222374`
- backend tests: PASS
- backend/native launcher checks: PASS
- A07–A13 Windows harness compile: PASS
- PowerShell syntax: PASS
- N60 hardware-gate preflight: PASS
- frontend install/build and smoke: PASS

Final gate/head `73b77866ebdc42f70a016e2ebd13ad99858b01b8` passed:

- CI #301
- run `35225682111`
- Windows job `105216542198`
- all steps PASS, including backend tests, native launcher, acceptance-harness compile, PowerShell syntax, N60 preflight, frontend build and smoke.

Final acceptance-document head `5cd8c3f02e31e6e46af0a81b8a213364bd055c94` passed CI #302 / run `35226382149` before merge.

## Owned-Windows environment

The final A12/A13 gate ran on the owned Windows machine with the repository initially clean.

- OS: Microsoft Windows 11 Pro 10.0.26200 build 26200
- CPU: AMD Ryzen 7 8845HS w/ Radeon 780M Graphics
- GPU: AMD Radeon 780M Graphics
- GPU driver: 32.0.13032.11
- display: 2880×1800
- Windows AppliedDPI: 192 = 200%
- Python: 3.12.10
- PySide6: 6.11.2
- PyVista: 0.49.0
- VTK: 9.7.0
- PyQtGraph: 0.14.0

Repository-state evidence:

```text
N60_ORIGINAL_SHA=5ede848e8e0b0967a50c04c83ff679a649ca439b
N60_ORIGINAL_BRANCH=
N60_PRE_STATUS_COUNT=0
N60_RESIDUAL_PROCESSES=none
N60_BRANCH_HEAD=73b77866ebdc42f70a016e2ebd13ad99858b01b8
N60_PRODUCT_HEAD=73b77866ebdc42f70a016e2ebd13ad99858b01b8
N60_GATE_SHA=73b77866ebdc42f70a016e2ebd13ad99858b01b8
```

The gate runner's `N60_PRODUCT_HEAD` label means its guarded checkout SHA. The actual last product-code change is separately recorded above as `8551963c...`.

## A12 — measurement/revision workflow

Final result:

```text
A12_PRODUCT_COMPOSITION True
A12_MOUSE_SELECT_SAVED_A True
A12_OFFLINE_FR True
A12_MOUSE_MOVE_SAVE_B True
A12_A_BINDING_IMMUTABLE True
A12_HISTORICAL_GHOST True
A12_AB_REVISION_BINDING True
A12_RESULT PASS
N60_GATE_A12_EXIT=0
```

This proves on the real GUI that:

- a saved measurement can be selected through the product UI;
- its local FR remains displayable without REW;
- a later scene revision can be produced by real mouse editing and save;
- the original measurement remains bound to its exact SceneRevision/content/measurement point;
- historical placement is shown as the exact source-revision ghost rather than rebound to the current scene;
- saved A/B comparison retains the exact A/B datasets and source revision IDs.

## A13 — stale/cancel/document-change/close

Final result:

```text
A13_PRODUCT_COMPOSITION True
A13_EDIT_MAKES_RESULT_STALE True
A13_UI_RESPONSIVE True 85
A13_CANCEL_WORKER_STARTED True
A13_CANCEL_REGISTERED True
A13_CANCELLED_RESULT_NOT_APPLIED True
A13_DOCUMENT_SWITCH_RESULT_NOT_APPLIED True
A13_CLEAN_EXIT_NO_WORKER True 0
A13_RESULT PASS
N60_GATE_A13_EXIT=0
```

The explicit-cancel harness uses a controlled fake REW response latch. The worker is first confirmed running, the real Win32 mouse path clicks `読込キャンセル`, cancellation is confirmed in the job guard, and only then is the delayed fake response released. This verifies the intended race directly: a completion occurring **after cancellation** must not save or apply its result.

An earlier harness used a fixed 0.65 s fake response delay. At 200% DPI, the real tab/scroll/foreground/mouse sequence could itself consume roughly that interval, allowing the fake result to finish just before the cancel click reached the button. That was a test-timing race, not evidence that `MeasurementJobGuard` accepted a cancelled token. Commit `73b77866...` replaced that timing assumption with the latch above; product runtime code remained at `8551963c...`.

A13 also proves that:

- a result captured for revision A is stale after a real edit/save to revision B and is not applied;
- blocking REW I/O remains off the GUI thread while the UI heartbeat continues;
- document-state change invalidates a delayed result;
- closing with a pending read leaves no running `QThread`.

## 200% DPI defect and final fix

Real-hardware acceptance exposed a layout defect that compilation/headless tests could not reveal. The measurement form had a valid internal scroll range, but lower controls still landed near global `Y=1046` because the right side of `QMainWindow` retained multiple vertically split dock rows.

Two structural corrections were required:

1. The tall measurement form is hosted by `_MeasurementScrollArea`, whose content extent is scrollable but does not propagate the full form height as the dock minimum/size hint.
2. Every inherited dock currently in `RightDockWidgetArea` is removed, re-added, and tabified into one CAD-style context stack. The set includes `Inspector`, `Room`, `壁・開口`, `オブジェクト詳細`, `制約`, and `実測` (plus any later right-area dock discovered dynamically).

The earlier partial unification only handled the newest context docks and missed inherited `Room` and `壁・開口`, so the vertical split topology survived. Product head `8551963c...` contains the complete normalization and is the first head on which A12 passed in full.

## Cleanup / restoration

Final runner evidence:

```text
N60_GATE_A12_EXIT=0
N60_GATE_A13_EXIT=0
N60_RESIDUAL_PROCESSES=none
N60_RESTORED_SHA=5ede848e8e0b0967a50c04c83ff679a649ca439b
N60_POST_STATUS_COUNT=0
N60_RESTORE_OK=True
N60_HARDWARE_GATE_RESULT=PASS
```

No acceptance harness process remained, the owned machine returned to its original detached SHA, and the worktree remained clean.

## Acceptance decision

N60 satisfies its A12/A13 technical gate:

- native measurement evidence is immutable with respect to exact SceneRevision and acoustic reference point;
- saved FR is locally available independent of REW;
- historical evidence is not silently rebound to current geometry;
- REW reads are asynchronous and guarded against stale, superseded and cancelled application;
- A/B comparison preserves exact datasets and revision identity;
- the native measurement workspace is usable at the owned machine's 200% DPI through real Win32 mouse interaction;
- close/cleanup leaves no worker behind.

## Merge completion

PR #62 was merged to `main` as `f1694eb879a250835efa026ac5dacdec18362788`. Issue #61 closed automatically with state reason `completed`.

N60 is complete on `main`. The next roadmap milestone is **N70 — 予測・可視化**.