# N80a Windows acceptance — SearchSpec / candidate preview / apply

> 実行日: 2026-09-18  
> Tracking: Issue #65 / PR #66  
> Product-code head: `c6cc15e76edbc1ac263911ee084803ca1e32b42c`  
> Accepted gate head: `ff4dc8078eb9ca0b3effaed66b523cff175fea1a`

## Scope

N80aのnative SearchSpec authority、既存O10 deterministic search engineへのtransient adapter、candidate cloud/selection/preview、explicit apply + 1 Undo、A13 stale/cancel/document boundaryをowned Windowsで受け入れた。

この記録はN80全体の完了を意味しない。O20 batch predictionは使用model contract、O30 objective vector、O40 Pareto searchの各gateが未完了であり、Issue #65を継続する。

## Accepted code / CI

- Last product-code change: `c6cc15e76edbc1ac263911ee084803ca1e32b42c`
- Product CI: #321 / run `35280237062` — PASS
- Gate-only commits after product code:
  - `a4c51475605a7353099226f9817c28efd52e5034`
  - `ff4dc8078eb9ca0b3effaed66b523cff175fea1a`
- Final gate CI: #323 / run `35280664154` — PASS

The two commits after the product head only add/update CI and owned-Windows acceptance harnesses. Product runtime code did not change after `c6cc15e7...`.

## Owned-Windows environment

- Windows 11 Pro 10.0.26200 build 26200
- AMD Ryzen 7 8845HS with Radeon 780M Graphics
- RAM 31.31 GiB
- AMD Radeon 780M Graphics driver 32.0.13032.11
- 2880×1800, AppliedDPI 192 (200%)
- Python 3.12.10
- PySide6 6.11.2
- PyVista 0.49.0
- VTK 9.7.0
- PyQtGraph 0.14.0

## A13 result

```text
A13_N80_PRODUCT_COMPOSITION True
A13_N80_STALE_WORKER_STARTED True
A13_N80_MOUSE_SCENE_SELECT True
A13_N80_MOUSE_DRAG True
A13_N80_SAVE_CHANGED_REVISION True
A13_N80_EDIT_MAKES_RESULT_STALE True
A13_N80_UI_RESPONSIVE True 114
A13_N80_CANCEL_WORKER_STARTED True
A13_N80_CANCELLED_RESULT_NOT_APPLIED True
A13_N80_DOCUMENT_WORKER_STARTED True
A13_N80_DOCUMENT_SWITCH_RESULT_NOT_APPLIED True
A13_N80_CLEAN_EXIT_NO_WORKER True 0
A13_N80_RESULT PASS
```

This verifies that delayed generation cannot silently apply after source-revision edits, explicit cancellation, document change, or close.

## A14 result

```text
A14_N80_PRODUCT_COMPOSITION True
A14_N80_SEARCHSPEC_CREATED True
A14_N80_FAST_GENERATION_OBSERVED True
A14_N80_CANDIDATES_GENERATED True
A14_N80_DETERMINISTIC_COUNTS True
A14_N80_MOUSE_SELECT_CANDIDATE True
A14_N80_PREVIEW_NON_AUTHORITATIVE True
A14_N80_APPLY_ONE_COMMAND True
A14_N80_UNDO_RESTORES_EXACT_SCENE True
A14_N80_RESULT PASS
```

Candidate preview remains view-only. Explicit apply changes all candidate-touched entities as one history command, and one Undo restores the exact pre-apply SceneDocument.

## Runner cleanup

```text
N80_A13_A14_RESULT PASS
N80_GATE_A13_A14_EXIT=0
N80_RESIDUAL_PROCESSES=none
N80_RESTORED_SHA=5ede848e8e0b0967a50c04c83ff679a649ca439b
N80_POST_STATUS_COUNT=0
N80_RESTORE_OK=True
N80_HARDWARE_GATE_RESULT=PASS
```

The local repository was restored to the exact original detached SHA and clean state.

## Authority / semantics accepted

- Native SceneRevision remains source authority.
- Native CadConstraintSet remains constraint authority.
- Legacy Context-shaped data is transient O10 algorithm input only.
- SearchSpec is immutable and bound to exact scene revision/content hash/constraint-workspace hash.
- Candidate identity/order/set hash is deterministic for the same bound input.
- Candidate cloud reuses bulk analysis rendering rather than one actor per candidate.
- Candidate means feasible geometry candidate only; no ranking, recommendation, or acoustic quality claim is introduced.

## Remaining N80 gates

N80b/c remain open under Issue #65.

- O20 Batch Prediction requires a verified model contract. REW Room Simulator requires S01-equivalent evidence; a polygon predictor requires S03-equivalent evidence before becoming authority.
- O30 must define/version independent objective vectors and validate them on synthetic data.
- O40 must implement deterministic Pareto dominance/search without fabricating a single quality score.
- O50 measurement-loop integration remains later work.

N80a merged through PR #66 as `7473bb3efdbc511369c9a023b0b210eb5cde3553`. Issue #65 remains open for N80b/c.
