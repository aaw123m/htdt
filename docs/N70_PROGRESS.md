# N70 implementation progress

Tracking: Issue #63  
PR: #64  
Branch: `feat/n70-prediction-visualization`  
Base main: `86354dd58200738b33ae27261a5308e3551bcbac`

## Scope and evidence boundary

N70 adds the native prediction/visualization workspace without promoting geometry-only calculations to validated acoustic FR/SPL prediction.

The existing rectangular-room algorithms are used only for what they actually compute:

- rectangular room-mode frequency/class candidates;
- first-order image-source reflection geometry;
- direct/reflection path visualization.

They do not model reflection amplitude/phase, material absorption, loudspeaker directivity, modal damping/amplitude, or a spatial SPL field. Therefore heatmap/slice/volume controls stay disabled until a real model produces a scalar field.

## Product implementation

`305a05c238e0a01b4faecbda6e3c826d8cbffde5`:

- immutable `CadPredictionResult` bound to exact native SceneRevision/content hash;
- canonical model ID/version/parameters/input snapshot and SHA-256 identity;
- prediction repository in the native CAD database;
- exact axis-aligned rectangular compatibility detection;
- explicit `unsupported` result for non-rectangular polygon rooms;
- no silent bounding-box/rectangular approximation;
- prediction-specific stale/cancel/document/constraint job guard.

`332ec93803b3bec981a605ca19f483051d02a86c`:

- Japanese `予測` dock integrated into the native right-side CAD tab stack;
- saved prediction history and model/input/revision metadata;
- async worker outside the GUI thread;
- stale/cancel/document mismatch rejection before persistence/application;
- direct/reflection overlays as non-pickable analysis actors;
- scalar-field UI gated by actual result payload;
- native launcher composes `PredictionWorkspaceWindow` over N60.

CI #307 / run `35231396558` passed completely.

`32cc06be125a054416df4c6ebf9c0bf12560f7df` added the original A13/A14 owned-Windows harness. CI #308 / run `35231929534` passed.

`76c21eed7d7efcff23905e8af977854669df2752` is the **last N70 product-code change**. It adds:

- reusable bulk analysis-marker rendering;
- one `PyVista.PolyData` / one mesh actor for many non-editable markers;
- non-pickable marker cloud;
- focused 10,000-marker structural test;
- F5 owned-Windows benchmark with 50 editable objects + 10,000 markers.

CI #309 / run `35270706491` passed completely.

## Hardware-gate preparation

`bdd7267630d82b4cd58821a65c83b32fb17a7359` added `run-n70-hardware-gate.ps1`. It refuses dirty state, records environment, executes A13/A14/F5, cleans residual processes, and restores the exact pre-gate checkout. CI #310 / run `35271098365` passed including gate preflight.

The first owned-Windows run exposed a timing-dependent acceptance-harness race in the A13 fixed-delay worker. F5 already passed with 10,000 markers in one actor. The local checkout was restored cleanly.

`7ae853f9d44ba55614c2287c42326e5177634daa` changed only the acceptance harness to deterministic worker latches. CI #311 passed. The second owned-Windows run then passed all A13 stale/cancel/document/close assertions; A14 failed only because the unsupported calculation completed and cleared its transient token before the harness observed it. F5 passed again.

`2a6eaae351beb8b2cbbef23a07e3054bb4fb1c93` made the A14 harness tolerate that legitimate fast completion only when the persisted unsupported-result assertions subsequently pass. Product runtime code remained unchanged. CI #312 / run `35272332747` passed completely.

## Final owned-Windows acceptance — PASS

Detailed record: [N70 Windows acceptance](N70_ACCEPTANCE_2026-09-18.md).

Environment:

- Windows 11 Pro build 26200
- Ryzen 7 8845HS / Radeon 780M / 31.31 GiB
- Radeon driver 32.0.13032.11
- 2880×1800 / AppliedDPI 192 (200%)
- Python 3.12.10
- PySide6 6.11.2
- PyVista 0.49.0
- VTK 9.7.0
- PyQtGraph 0.14.0

A13 final result: **PASS**

- real mouse scene select/drag/save while prediction worker is held;
- edit makes pending result stale;
- stale result not persisted/applied;
- UI remains responsive;
- explicit cancel registered and cancelled result not applied;
- document-switch result not applied;
- clean close leaves zero live worker threads.

A14 final result: **PASS**

- 8-vertex L-room remains exact polygon authority;
- rectangular-only model persists `unsupported`;
- no silent rectangular approximation rule;
- no mode/reflection payload and no prediction overlay;
- scalar-field controls remain disabled.

F5 final result: **PASS**

- 50 editable furniture objects;
- 10,000 analysis markers;
- marker actor delta `1`;
- marker actor non-pickable;
- first render `11.406 ms`;
- orbit p50 `22.993 ms` / p95 `27.963 ms` / max `31.205 ms`.

The 64^3 scalar-grid fixture remains conditional on a validated model producing a scalar field. N70 deliberately does not generate synthetic field data just to satisfy a visualization path.

Gate cleanup:

- restored exact pre-gate detached SHA `5ede848e8e0b0967a50c04c83ff679a649ca439b`;
- post-status count `0`;
- `N70_HARDWARE_GATE_RESULT=PASS`.

## Merge completion

Acceptance documentation commit `bff12d4a379a5458f7dcf6cfa7e2b55b570ea4ec` added the final acceptance record and updated status. CI #313 / run `35276546531` passed completely.

PR #64 was marked ready with no review comments and merged into main as:

`2ca8755b66af5521c2ed4fc98d39ce1aeb732c64`

Issue #63 closed automatically with state reason `completed`.

**N70 is complete and merged.** The roadmap next milestone is **N80 — 最適化workspace**, which will connect SearchSpec/O10 and applicable O20–O40 gates for candidate preview/application, objective vectors and Pareto comparison.