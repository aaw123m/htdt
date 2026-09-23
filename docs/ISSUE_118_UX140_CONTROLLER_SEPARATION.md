# Issue #118 — UX140 controller/window separation

Date: 2026-09-19

## Purpose

UX140のpage-first UIは先行実装で成立していたが、内部では
`OptimizationWorkflowWorkspace(OptimizationWorkspaceWindow)` としてlegacy
`QMainWindow -> dock/widget` compositionを継承し、hidden dock controlsをpageへreparentしていた。

この文書は、そのtransitional adapterを除去した後のauthority境界を記録する。

## New composition

`OptimizationWorkflowWorkspace`
- plain `QWidget`
- 4 canonical pages: 探索設定 / 候補 / 比較 / 測定・検証
- shared dark `RoomViewport3D`
- page composition / visual hierarchyのみを所有

`OptimizationWorkflowController`
- `QObject`
- existing O10-O80 mixinsをhost
- repository/service authorityを再利用
- Scene/WorkingDocument lifecycleは既存 `RoomWorkspaceController` を再利用
- worker / status / view-state / recovery boundaryを所有

Viewport overlay adapter:
- `add_mesh/remove_actor/add_text/render` のみ
- optimization semanticsをRoomViewport3Dへ移さない
- base Scene描画はRoomViewport3D、candidate cloud/previewだけO-series mixinがoverlayする

## Authority preserved

以下は変更しない。

- `CadSearchRepository` / `generate_cad_candidates()`
- `CadObjectiveRepository` / existing Pareto authority
- `CadMeasurementRepository` / Measurement Plan
- `CadValidationCampaignRepository/Service`
- `CadModelValidationRepository/Service`
- `CadAdaptivePlanRepository/Service`
- `CadExtendedSearchRepository`
- `CadAdaptiveExtendedRepository/Service`

新しいtotal score、recommendation authority、validation semanticsは作らない。

## REW campaign import

legacy構成ではOptimization pageの「選択REW→Campaign実測」が、
hidden MeasurementEditor dockの `rew_combo` / selected measurement point /
channel roleへ暗黙依存していた。

新UIではValidation pageに以下を明示表示する。

- REW測定
- 測定点
- channel role

async apply fenceは既存:
- `MeasurementJobGuard`
- exact document/revision/content hash
- `normalize_rew_api_snapshot()`
- `CadMeasurementRepository.save()`

をそのまま使用する。

Measurement PlanとCampaignのsearch authority/current Scene gateは
`ValidationControllerMixin.read_selected_rew_for_campaign_async()` の既存条件を維持する。

## Lifecycle

workspace switchは次の場合fail closed:

- search worker active
- extended-search worker active
- REW worker active
- WorkingDocument preview active
- unsaved WorkingDocument
- recovery candidate pending

clean activation時だけrepository latestへreloadし、constraint setと各O-series
repository表示を再取得する。

## Legacy boundary

新UX140 runtimeから以下を除去する。

- `OptimizationWorkspaceWindow` inheritance
- `PredictionWorkspaceWindow` inheritance chain
- hidden optimization dock
- hidden measurement/prediction/constraint controls as UX140 state authority
- `workflow_legacy_bridge` activation/deactivation

legacy `optimization_workspace.py` は旧GUI compatibility / regression sourceとして残せるが、
workflow shellのproduction UX140 compositionには使用しない。

## Validation

Focused:
- real UX140 workspace is not `QMainWindow`
- no `QDockWidget` child
- canonical page/deep-link contract unchanged
- dirty state blocks workspace deactivation
- explicit campaign REW controls exist

Repository CI remains the regression authority for O10-O80 semantics.

## Next

UX150:
- spacing/control rhythm
- Japanese-first copy cleanup
- disabled/hover/focus state consistency
- plot/viewport readability
- 1280x800 / 1440x900 software layout checks
- 100/150/200% DPI software checks where deterministic

UX160:
- owned-Windows first-use/visual acceptance
- final workflow-shell default promotion gate

RDC is not used in this implementation.
