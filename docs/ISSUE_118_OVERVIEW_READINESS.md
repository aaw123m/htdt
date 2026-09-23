# Issue #118 — Overview readiness contract

Issue #118 の「概要」は、機能一覧ではなく、保存済み project state と既存の evidence / validation authority から「次に何をするか」を導出する read-only presentation layer とする。

## Scope

readiness ruleは `backend/src/htdt/overview_readiness.py` に閉じ、描画だけを `backend/src/htdt/overview_workspace.py` が担当する。

- Scene / DB を変更しない。
- repository を Overview widget/service 内部で construct しない。application composition root が既存 repository class を所有し、read protocol として注入する。
- domain gate を再計算しない。
- readiness serviceはQt/shell/routerを知らない。Qt widgetはview modelを描画し、navigationはshared deep-linkをshellへ委譲する。
- UUID / hash / schema / job ID を user-facing copy に埋め込まない。
- entity ID は navigation metadata として保持できるが、通常表示文字列には含めない。

`OverviewWorkspace` は `OverviewReadinessService.read(document_id)` が返す `OverviewReadinessViewModel` を描画する。Overview layer 自体は navigation authorityを持たず、shared `WorkspaceDeepLink` をshellへ渡す。

## View model

`OverviewReadinessViewModel` は以下を返す。

- `summary`: 現在の最重要状態を短い日本語で表現
- `blockers`: 解消しないと該当 workflow を進められない状態
- `warnings`: optional evidence / capability / stale state
- `next_action`: 1 個の主要 action
- `optimization_ready`: candidate/optimization setup へ進めるか
- 各 action の `OverviewNavigationTarget`: `workspace / subsection / entity_id`

navigation target は router implementation ではなく transport data である。`workflow_navigation.py` のcanonical contextと合わせて次を使用する。

| 状態 | workspace | subsection |
| --- | --- | --- |
| 部屋形状 | `room` | `geometry` |
| スピーカー / 座席 | `room` | `placement` |
| 予測 | `room` | `acoustics` |
| REW 読み込み | `measurement` | `import` |
| 測定品質 | `measurement` | `quality` |
| 最適化開始 | `optimization` | `setup` |
| validation | `optimization` | `validation` |

entity selection が必要な action は `entity_id` を metadata として返す。現行 Agent A router は workspace/context selection までを責務としており、entity selection callback は workspace integration 側で接続する。

## Readiness rules and authority

| Rule | Overview 表示 | Authority source | 判定 |
| --- | --- | --- | --- |
| 保存済み Scene がない | blocker | `SceneRepository.current_head()` | revision がない |
| 部屋形状未完成 | blocker | `SceneRevision.document.room` | `room is None` |
| speaker がない | blocker | `SceneRevision.document.entities` | `kind == "speaker"` が 0 件 |
| speaker role 未設定 | blocker | `SceneEntity.speaker_role` | falsey role を defensive に検出 |
| 測定なし | warning | `CadMeasurementRepository.list_measurements()` | record が 0 件 |
| timing/phase capability 不足 | warning | `CadFrequencyResponseDataset.phase_status` | `valid` の dataset が 0 件 |
| 予測なし | warning / next action | `CadPredictionRepository.list_results()` | completed result がない |
| prediction 要再計算 | warning / next action | prediction の immutable revision/content binding | completed result はあるが current revision/content と一致しない |
| validation gate blocked | blocker for automatic recommendation | `CadModelValidationRecord.recommendation_gate` と `gate_reasons` | latest relevant validation が `disabled` |
| optimization 準備完了 | ready | 上記 Scene readiness + current prediction | room/speaker blocker がなく current prediction がある |

### Speaker role

現行 `SceneEntity` validator は speaker に `speaker_role` を必須とする。そのため通常保存経路では role 未設定 Scene は成立しない。Overview の role rule は legacy / malformed read state に対する defensive presentation であり、Scene authority を緩めない。

### Measurement capability

phase array の存在だけでは capability を昇格しない。REW API import は phase data が存在しても verification 前は `phase_status="unknown"` とする既存 semantics を持つ。Overview は `phase_status == "valid"` のみを timing/phase 比較に利用可能とみなし、`unknown` / `absent` は warning にする。

「測定なし」は repository record がないことを意味する。measurement evidence type が `unknown` でも、REW から取り込まれた record を「測定なし」と扱わない。

### Prediction stale

prediction workspace と同じ immutable identity を使う。

- `scene_revision_id`
- `scene_content_hash`
- `status == "completed"`

Overview 独自の freshness flag は保存しない。

### Validation gate

Overview は O60 validation を再実装しない。`CadModelValidationRecord` が authority であり、Overview は `recommendation_gate` を読むだけである。

`gate_reasons` は internal objective/candidate ID を含み得るため、通常表示には raw reason を出さず、以下の user-facing category へ縮約する。

- owned-room evidence
- calibration / holdout evidence
- residual
- objective trend
- placement sensitivity
- repeatability
- candidate separation
- model applicability

`constraint_workspace_hash` を caller が渡した場合は exact constraint identity まで一致する SearchSpec だけを validation 対象にする。未指定時は latest SceneRevision/content に binding された最新 SearchSpec の validation を表示する。Overview は constraint hash を自前で計算しない。caller は既存 authority `cad_search_models.constraint_workspace_snapshot(current_constraint_set)` が返す hash を利用し、hash algorithm を重複実装しない。

## Next-action precedence

Overview は複数状態を同時に表示しても primary action は 1 個にする。

1. 部屋形状
2. speaker 追加 / role
3. prediction 実行または再計算
4. blocked validation の確認
5. 最適化開始

測定は Issue #118 の guided workflow で optional なので、測定なしや timing/phase capability 不足は warning として表示するが、room/prediction が揃った場合の最適化開始を塞がない。

validation の `disabled` は「自動推薦」を block する authority であり、candidate exploration 自体を禁止する意味には使わない。このため `optimization_ready=True` と validation blocker は同時に成立し得る。

## Shell integration

`native_cad.py` のapplication composition rootが既存repository classを所有し、serviceへ注入する。

```python
overview = OverviewReadinessService(
    scene_repository,
    measurement_repository,
    prediction_repository,
    search_repository,
    validation_repository,
)
view_model = overview.read(
    document_id,
    constraint_workspace_hash=current_constraint_workspace_hash,
)
```

重要事項:

- Overview widget/service自身はrepositoryをconstructしない。composition rootでのみ既存repository classを組み立てる。
- `next_action.target` はshared `WorkspaceDeepLink` として `WorkflowShellWindow.handle_deep_link()` へ渡す。
- `subsection` はshellのcanonical context selectionへ渡す。
- `entity_id` は destination workspace の selection API が受け取る。shell/router は domain entity を解釈しない。
- view refresh は save/import/prediction/validation 更新後に呼ぶ。Overview は event source や job lifecycle を所有しない。

## Navigation contract

Shell / command / Overview は `backend/src/htdt/workflow_navigation.py` の
`WorkspaceId` / `WorkspaceDeepLink` を共有する。canonical workspace ID は
`overview / room / measurement / optimization` であり、aliasや第二authorityは作らない。

## Verification

readiness rule の precedence、entity deep-link metadata、phase capability、prediction stale、validation reason sanitization、constraint hash filtering、optional measurement と optimization readiness を `backend/tests/test_overview_readiness.py` で検証する。

RDC は使用しない。Windows visual acceptance は本 slice の対象外。
