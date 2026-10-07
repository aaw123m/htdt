# Issue #868 — クローズドループ・コミッショニングオーケストレータ

「機械が制御し、人が承認し、機械が検証する」コミッショニングループを
HTDT 権威として実装する。測定プロバイダへの委託 (REW API 等) と
デバイスアダプタ (CamillaDSP 等) の間にある既存の権威を串刺しにする
ステートマシンであり、新しい測定・デプロイ機能は追加しない。

## スコープ

ステージ階段:

```
PRECHECK → BASELINE_MEASUREMENT → INGEST/QUALITY_GATE → ANALYZE →
OPTIMIZE → OPERATOR_APPROVAL → COMPILE_FOR_TARGET → DEPLOY →
READBACK_VERIFY → POST_MEASUREMENT → BEFORE/AFTER → ACCEPTANCE_DECISION
(+ completed / rolled_back / aborted の終端)
```

- `cad_commissioning_orchestrator.py` — シール済みレコード、純粋な遷移
  関数、無効化評価器、サービス面。
- `cad_commissioning_orchestrator_repository.py` — 6 テーブルの
  `_SealedStore` (`cad_commissioning_orch_*`)、監査配線済み
  (`_ROW_BINDINGS` / `_RepositoryChain` / `_ReplayProbe` /
  `_LIFECYCLE_TABLE_LABELS`)。
- `commissioning_panel.py` — 読み取り専用ワークフローパネル
  (デバイス変異経路なし)。

## ステート・結果語彙

イベント (`CommissioningEventKind`): `run_created`,
`precheck_evaluated`, `baseline_acquisition_recorded`,
`quality_gate_evaluated`, `analysis_bound`, `optimization_committed`,
`operator_authorized`, `compile_completed`, `deploy_acked`,
`readback_evaluated`, `runtime_observed`,
`post_measurement_recorded`, `before_after_evaluated`,
`acceptance_decided`, `rollback_completed`, `provider_disconnected`,
`device_disconnected`, `connectivity_restored`,
`authority_invalidated`, `abort` — 全て JA ラベル登録済み。

遷移結果 (`outcome`): `advanced` / `rejected` / `blocked` /
`informational` / `invalidated` / `completed` / `rolled_back` /
`aborted`。拒否もブロックも全部シール済みで残る — 暗黙の遷移はない。

`stage_transition(state, event)` は純粋・全関数:
- 終端ステージは全イベントを拒否。
- 切断フラグ中は `connectivity_restored`/`abort` 以外を拒否
  (切断イベント自体は同ステージ `blocked`)。
- `succeeded=False` の試行は同ステージ `blocked` で残り、再試行も
  abort も合法。
- `rollback_completed` は DEPLOY 以降でのみ受理、かつ
  `succeeded` 真のときだけ終端 `rolled_back`。失敗したロールバックは
  `blocked` (検証なしに「戻った」とは言わない)。
- `authority_invalidated` は後退のみ、かつ `succeeded=False` =
  「ピン再確認したが陳腐化なし」の informational。
- `runtime_observed` は `readback_matched is True` の後のみ受理 —
  `CONFIG_READBACK_MATCHED` / `RUNTIME_OBSERVED` /
  `POST_MEASUREMENT_VERIFIED` は別段階のまま (`derive_evidence_strength`)。

## 証拠モデル

シール済みレコード (id プレフィックス):
- `CommissioningOrchestrationRun` (cor-): ランのピン —
  scene revision + 内容ハッシュ、プロバイダ manifest、測定能力、
  デバイス binding、ルーティングプロファイル、必須 capability claim、
  受け入れ基準。
- `CommissioningStageTransition` (cot-): 全遷移の台帳 —
  `seq` + `event_kind` + `outcome` + `from_stage`/`to_stage` +
  `reason` + `evidence_refs`。`run_created` は `from_stage=None`。
- `CommissioningOperatorAuthorization` (coa-): 一発承認。
  `deploy_apply` は承認した校正候補 (`plan_semantic_sha256`) をピンし、
  DEPLOY で export→materialization の系譜を機械検証する。
  `rollback_apply` は対象 deployment をピン。期限切れ・スコープ違い・
  消費済みは常に拒否。
- `CommissioningRollback` (crl-): `restored_verified` にはアダプタ証跡が
  必須、`failed` には理由が必須。
- `CommissioningBeforeAfter` (cba-): 配置済み・読戻し済み config にピン
  された before/after 比較 (baseline + post 測定証跡、deltas、verdict)。
- `CommissioningAcceptanceVerdict` (cav-): 全基準 satisfied + 陳腐化なし
  でのみ `accepted`。1 件 failed で `rejected`。それ以外は
  `indeterminate` — 最終判定は陳腐・欠落証拠で常に fail-closed。

## 無効化評価器

`evaluate_invalidation(run, **pins)` — 生のピン値 (scene 内容ハッシュ、
manifest sha、binding sha、ルーティング sha、読戻し期待/観測 config
sha) をランのピンと比較し、陳腐化するピン・理由・最も早い陳腐ステージを
決定的に返す。`apply_invalidation` がその結果を
`authority_invalidated` イベントとして台帳に載せ、以後の再証拠化までは
当該ステージが `stale_stages` に残る。

## 統合点

- 委託プロバイダ: `DelegatedProviderManifest` +
  `ProviderAcquisitionRecord` (`record_acquisition` が `acquire` 呼出を
  包んで結果を正直に記録 — 例外や証跡ゼロは `blocked`)。
- 品質ゲート: `CadMeasurementQualityReport` の 8 チェック + capability
  claim をランの required claims と照合。
- 最適化: `CadCalibrationPlan` の baseline ピン (scene ハッシュ、品質
  レポート sha、ソース測定 sha) をランの受理済み証拠と照合 —
  無効な測定を有効な証拠へロンダリングしない。
- デプロイ: `adapter.materialize` → `validate_materialization_result`
  → `evaluate_deployment_gate` → `adapter.apply` →
  `CalibrationDeployment` (`deployment_unverified`) →
  `read_back` + `build_observation` → 再シールされた deployment +
  CamillaDSP deployment session (readback sha + matched)。
- ロールバック: `rollback_previous` (CamillaDSP) を優先。無い
  アダプタは `not_attempted` + `blocked` — 手動復旧を機械検証済みとは
  書かない。

## デバイス専用に残るもの

- 実機への `SetConfigJson` 適用・前回 config の実復元・ランタイム
  テレメトリ取得 — パネルには物理経路を置かず、サービス層 +
  オペレーター承認シール経由でのみ行われる。
- 非 CamillaDSP アダプタは deployment session / rollback evidence /
  runtime observation を出さない (fail-closed: 証拠の少なさをそのまま
  出す、捏造しない)。
- 音響検証は委託プロバイダ測定の再取得でのみ — `deploy_acked` は常に
  「まだ未検証」。
