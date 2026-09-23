# UX120 Room prediction integration — 2026-09-19

Issue #118 の UX120 Room workspaceへ、既存N70矩形幾何予測authorityを接続した記録。

## Goal

新Room画面から予測を実行・確認できるようにする。ただしUI都合でN70のrequest identity、
stale/cancel判定、constraint binding、immutable persistenceを再実装しない。

## Reused authority

- `rectangular_geometry_request_identity()`
- `analyze_native_rectangular_geometry()`
- `PredictionJobGuard / PredictionJobToken / PredictionJobApplyContext`
- `CadPredictionRepository`
- `CadPredictionResult`
- `CadConstraintRepository`
- `constraint_workspace_snapshot()`
- exact `SceneRevision.revision_id / content_hash`

`RoomPredictionController` はapplication adapterであり、新しいprediction semanticsを所有しない。

## Run prerequisites

予測開始時に以下をfail closedする。

- formal SceneRevisionが存在しない
- WorkingDocumentにactive previewがある
- WorkingDocumentがdirty
- receiverにacoustic referenceがない
- current revisionとcommitted document hashが一致しない
- すでにprediction workerが実行中

receiver、mode上限、音速からexisting N70 request identityを生成し、現在のconstraint setは
`constraint_workspace_snapshot()` の既存hashへbindingする。

## Async / stale / cancel

計算はQThread workerで実行する。

completion時は既存JobGuardとresult identityを使い、以下が完全一致する場合だけ保存する。

- document
- SceneRevision ID
- Scene content hash
- model ID/version
- parameters/input hash
- constraint workspace hash

Scene editまたはconstraint変更後に戻った遅延結果は保存しない。
cancelled tokenも保存しない。

Cancel後もworker threadが実際に `finished` をemitするまでRoom workspaceのdeactivation、
restore、新規predictionをblockする。thread cleanupはGUI thread所属のcontroller slotで行う。

## Room UI

「部屋 > 音響」contextの右側surfaceにprediction panelを表示する。

- 受音点
- mode上限
- 音速
- 予測実行
- キャンセル
- 保存済みrun一覧
- 現在 / 要再計算
- model/version
- room mode件数
- 一次反射候補件数
- warning

内部run UUIDやinput hashを通常表示の主要情報にはしない。

central command `prediction.run` は既存deep-linkでRoom/acousticsへ移動した後、
このpanelの実行authorityへbindする。

## 3D overlay

current Scene + current constraintに一致するrunだけを3D overlayへ渡す。

- direct path
- first reflection path
- reflection point
- 「予測幾何 · 実測ではありません」label

stale/history runは一覧・detailでは確認できるが、current geometry overlayとしては表示しない。
scientific visualization tokenを使用し、UI accent/warning色をprediction scaleへ流用しない。

## Tests

`backend/tests/test_room_prediction.py`:

- existing N70 identity / JobGuard / repository reuse
- exact revision / input / constraint binding
- local Scene edit後のdelayed result reject
- saved resultのstale判定
- cancelled tokenのpersistence reject
- constraint workspace変更後のresult reject
- worker thread teardown完了までdeactivationをblock

full repository CI / Windows Release Artifactを最終回帰authorityとする。

## Remaining Room work

- midpoint insert/delete
- live numeric edge dimension
- wall/opening topology editingの新component移植
- UX150 lighting/layout/language polish
- UX160 owned-Windows first-use / DPI / visual acceptance

このintegrationはR130以降の新しいwave solver authorityを意味しない。現在のN70 rectangular geometry
modelのcapability/limitationをそのまま表示する。

RDCは使用しない。
