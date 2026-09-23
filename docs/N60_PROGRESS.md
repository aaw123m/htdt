# N60 implementation progress

Tracking: Issue #61 / PR #62  
Branch: `feat/n60-measurement-workspace`  
Final technical gate: **PASS**  
Merge: `f1694eb879a250835efa026ac5dacdec18362788`

## 2026-09-17 — implementation summary

N60 connects the existing REW/measurement assets to the native CAD without making legacy `Context` authoritative.

Implemented boundaries and invariants:

- immutable measurement binding to exact native `SceneRevision`, document ID, content hash and measurement/acoustic-reference entity;
- native `CadMeasurementRepository` in `cad-scenes.sqlite3` with exact source-revision validation;
- content-addressed raw measurement assets and immutable local FR datasets;
- REW text import plus read-only REW API snapshot normalization;
- external REW UUID retained as provenance only, never used as the HTDT primary key;
- unknown calibration/reference/capture metadata remains unknown rather than inferred;
- PyQtGraph-based native FR display, saved-measurement tree and provenance display;
- saved FR remains available while REW is stopped;
- exact source-revision historical placement ghost, non-pickable and visually distinct from the current scene;
- saved A/B comparison retaining exact dataset IDs and source revision IDs;
- QThread-based REW reads with submission-time document/revision/hash/point snapshot;
- `MeasurementJobGuard` rejects cancelled, superseded and stale completions;
- browser UI remains frozen; no duplicate N60 implementation was added there.

## Focused verification

The N60 test set covers the invariants that materially protect measurement evidence:

- measurement round-trip remains bound to the original revision after later scene edits;
- mismatched content hash, source position or missing measurement entity is rejected;
- REW external UUID is provenance rather than internal identity;
- A/B persistence retains exact dataset/revision IDs;
- job guard rejects cancellation, supersession, revision staleness and document mismatch;
- measurement scroll content can exceed its viewport without publishing a large dock minimum;
- all inherited right-side context docks are normalized into one tab stack.

Pixel snapshots and other low-value visual tests were not added.

## Real-hardware findings and fixes

Owned-Windows testing at 2880×1800 / 200% DPI found issues that CI compilation could not expose.

1. Acceptance actor-center clicks became unreliable after the right-side workspace grew. The harness selects scene entities through the visible scene tree while still using real Win32 mouse input.
2. Lower measurement controls were initially outside the usable desktop. The product gained `_MeasurementScrollArea`; its tall content remains internally scrollable while the form height no longer becomes the dock minimum.
3. That alone was insufficient because inherited right-side docks still formed multiple vertical rows. Partial tabification missed `Room` and `壁・開口`.
4. Final product code dynamically rebuilds every `RightDockWidgetArea` dock into one tab stack. Known surfaces are `Inspector`, `Room`, `壁・開口`, `オブジェクト詳細`, `制約`, `実測`; later unknown right-side docks are included as well.
5. Product head `8551963c4dc3e5eb5a22bd0683edbaea6e891cdb` is the first version on which A12 passed in full.

## GitHub verification

Last product-code head:

- `8551963c4dc3e5eb5a22bd0683edbaea6e891cdb`
- CI #300 / run `35223222374`: full PASS.

Final acceptance-harness head:

- `73b77866ebdc42f70a016e2ebd13ad99858b01b8`
- only change after `8551963c...`: `scripts/validate_n60_a13_windows.py`
- CI #301 / run `35225682111`, Windows job `105216542198`: full PASS.

Final acceptance-document head:

- `5cd8c3f02e31e6e46af0a81b8a213364bd055c94`
- CI #302 / run `35226382149`: full PASS.

The final A13 harness change replaced a fixed 0.65 s fake-REW delay with a controlled latch. The earlier delay was shorter than or comparable to the real 200%-DPI foreground/tab/scroll/mouse path, so the fake response could complete just before the cancel click. The deterministic harness now proves the worker is running, performs the real cancel click, confirms the guard recorded cancellation, and only then releases the delayed response. No product runtime code changed for this correction.

## Final owned-Windows acceptance

Environment:

- Windows 11 Pro build 26200
- Ryzen 7 8845HS / Radeon 780M / driver 32.0.13032.11
- 2880×1800 / AppliedDPI 192 (200%)
- Python 3.12.10
- PySide6 6.11.2
- PyVista 0.49.0
- VTK 9.7.0
- PyQtGraph 0.14.0

Final hardware-gate checkout/head: `73b77866ebdc42f70a016e2ebd13ad99858b01b8`.

A12:

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

A13:

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

Cleanup:

```text
N60_RESTORED_SHA=5ede848e8e0b0967a50c04c83ff679a649ca439b
N60_POST_STATUS_COUNT=0
N60_RESTORE_OK=True
N60_HARDWARE_GATE_RESULT=PASS
```

Full record: [N60 Windows acceptance](N60_ACCEPTANCE_2026-09-17.md).

## N60 completion

A12/A13 technical acceptance is complete. PR #62 was merged to `main` as `f1694eb879a250835efa026ac5dacdec18362788`, and Issue #61 closed automatically with state reason `completed`.

N60 is complete on `main`. Next roadmap milestone: **N70 — 予測・可視化**.

## Invariants carried forward to N70+

- Never attach old measurement evidence to whichever scene happens to be current.
- Do not use REW UUID/filename as internal identity.
- Measurement position means explicit acoustic reference/capsule position, not seat-body center.
- Do not infer calibration, capture time, phase validity, routing or SPL reference when unknown.
- A long-running calculation must capture all authoritative input versions at submission and reject stale completion.
- Saved historical evidence remains valid after edits; only its relationship to the current scene changes.
- Prediction/optimization outputs must remain distinguishable from measured evidence.