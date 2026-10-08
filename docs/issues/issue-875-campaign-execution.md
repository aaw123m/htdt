# Issue #875 — 複数測定位置の自動キャンペーン実行 (REV67)

複数の測定位置 (席) × チャンネル × ロール × リピート回数で構成される測定
キャンペーンを、HTDT が自動で順次実行する権威。#869 の取得エンジン
(`MeasurementAcquisitionEngine`) を素材とし、「実行計画 → 位置確認ゲート
→ 測定実行 → 品質ゲート → リトライ/次エントリ → 完了」をジャーナル
記録付きで駆動する。

非目標: マイクの物理的移動を自動化した振りをしない。位置が存在しない
ことを黙って発明しない。キャンペーン設計権威 (#813) を単純なバッチループに
置き換えない。

## ドメインコア (`cad_campaign_execution.py`)

- `CampaignQueueEntry` — 実行キューの1要素。`entry_key` は `e0001` 形式。
  position-major → チャンネル (ソート順) → ロール → リピート順に
  `materialize_run_queue` が展開し、計画封緘でキューの正確性を再検証する。
  同じ入力は常に同じ `mcplan-` ID を持つ。
- `CampaignPositionSpec` — 測定位置の要求。`declared_position` は計画側の
  宣言値; `roles` は測定ロール集合 (calibration / screening / holdout 等);
  `required` は必須フラグ; `channel_entity_ids` は位置限定チャンネル;
  `orientation_ref` はマイク方位への封緘参照。
- `CampaignAutomationPolicy` — 自動化の明示的ポリシー。デフォルトは全て
  保守的: `max_attempts_per_entry=2`、`retry_clipping_once_at_reduced_level
  =False`、`retry_transient_device_failure=False`、
  `holdout_retry_on_state_drift=False`、
  `stop_after_consecutive_quality_failures=3`、
  `require_human_on_unexpected_routing_change=True`、
  `allow_waived_required=False` (必須ランを waived で済ませるには明示的
  opt-in が必要)、`position_reconfirm_each_run=False`。
- `PositionConfirmationMethod` — 確認の証拠階梯:
  `operator_attest` < `coordinate_entry` / `survey_import` <
  `tracked_fixture` / `position_tracker`。
  「クリックして確認した」は測定証拠と同等ではない —
  `CONFIRMATION_EVIDENCE_QUALITY` で attested / entered / measured に
  マップされ、reported_position の座標は `position_tolerance_m` (既定
  0.05m) との距離検証を通る。座標を持たない確認 (attest) は
  attested のまま残り、measured に読み替えられることはない。
- `evaluate_retry_decision` — 決定論的リトライ判定表。優先順位:
  試行回数上限 → holdout ドリフト拒否 → 連続品質失敗によるキャンペーン
  停止 → キャンセル済み → レベル低下クリッピングリトライ → 一時的デバイス
  失敗リトライ → 失敗確定。ポリシーにない失敗はリトライしない。
- `derive_campaign_state(plan, events, run_records)` — ジャーナル + 実行
  レコードからの純粋な fold。計画・イベント・レコードのみから再現可能な
  導出状態 (エントリ状態・位置ゲート・進捗カウント・アウトカム) を返す。
  in-memory 状態は存在しない — 再起動後も同じジャーナルから同じ状態が
  復元される。

## イベントジャーナル / 証跡モデル (`cad_campaign_execution_evidence.py`)

- `CadCampaignExecutionEvent` (mcevt-) — 計画 + `journal_seq` で封緘された
  実行イベント。`kind` 毎に必須フィールドをバリデータで強制
  (例: `position_confirmed` は method/evidence_quality を要求、
  `run_started`/`run_recorded` は attempt + outcome/verdict を要求)。
- `CadCampaignRunRecord` (mcrun-) — エントリ毎の測定試行レコード。
  entry identity (key/ordinal/position/channel/role/run_index/required) を
  コピーし、計画側値と不一致は整合エラー。routing スナップショット、
  level_dbfs、`stimulus_ref` / `acquisition_ref` / `position_ref` /
  `orientation_ref`、engine_run_id、backend_is_simulated、
  outcome/failure_kind/quality_verdict/reasons、時刻を保持。
  `notes` に `simulated_backend` が必ず記される。

アウトカム語彙: `in_progress` / `awaiting_position` / `paused` /
`blocked` / `cancelled` / `completed` / `completed_with_failures` /
`failed`。必須ランが allowed terminal state に達しなければ completed
にはならない — ただし `allow_waived_required` が明示的な場合のみ waived
を含む。`completed_with_failures` は「必須は全て完了、非必須に失敗あり」
を正確に表し、終端マーカーが失敗を折り畳むことはない。

エントリ状態語彙: `pending` / `awaiting_position` / `in_progress` /
`interrupted` / `blocked` / `completed` / `failed` / `waived`。

## 実行ランナー (`cad_campaign_execution_runner.py`)

- `MeasurementCampaignRunner(plan, engine_factory, event_sink,
  run_record_sink, acquisition_sink=None, arm_confirmation_provider=None,
  calibration_state_provider=None, position_ref_for=None, clock, events=(),
  run_records=())` — ジャーナル駆動実行。
  `step()` が1単位 (次エントリ) を進め、`run_until_blocked()` が
  人間判断/終端まで走り続ける。`RunnerStepResult.action` ∈
  `ran | awaiting_position | paused | blocked | cancelled | terminal`。
- `plan_registered` — 空ジャーナルでの初回イベント (計画登録証跡)。
- 位置確認ゲート: `requires_position_confirmation` のエントリは
  `position_gate_opened` を発行して `awaiting_position` で止まる。
  `confirm_position` が宣言座標との距離を検証し
  `position_confirmed` をジャーナル — 「物理的移動の確認なしに進まない」
  が機械的に強制される。`position_reconfirm_each_run` は再確認強制。
- `_execute_entry`: run_started → configure (precheck_blocked で記録) →
  arm provider (不在 → `arm_confirmation_unavailable`、ArmBlockedError
  → `arm_blocked` で failure path) → start → acquisition_sink →
  `_classify` (cancelled→cancelled / completed+valid|limited→completed /
  completed+invalid→quality_invalid / else→capture_failed) →
  `_record_attempt` (sink → journal の順で二重記録のずれを防ぐ) →
  `_decide` (completed→entry_completed / else evaluate_retry_decision →
  retry_scheduled (reduced_level_dbfs 保持) / policy_stop /
  entry_failed)。
- 連続失敗ストリークは時系列 fold (成功でリセット、キャンセルは中立)。
  `policy_stop` はトリガしたエントリを failed にしてキャンペーンを停止 —
  両方のイベントが記録される。
- `report_routing_change` — 実行中の予期せぬルーティング/デバイス変化を
  ジャーナル。`require_human_on_unexpected_routing_change` で campaign を
  blocked へ — 人間が `resume` するまで進まない。holdout ランの
  ドリフトは `holdout_retry_on_state_drift=False` (既定) で拒否。
- `calibration_state_provider` — #813 の cal/holdout 分離を実行時に強制。
  `drift` (非成立) を返した場合、holdout エントリは自動リトライされない。
- `pause`/`resume`/`cancel`/`waive_entry`/`report_routing_change` は全て
  ジャーナルイベントとして記録される — サイレントなスキップは存在しない。
- 再起動復旧: `events=()` / `run_records=()` 既存ジャーナルを注入すると
  `restart_recovered` が発行され、in_progress エントリは pending に戻る。
  `_level_for` はジャーナル上の retry_scheduled から reduced level を
  読むため、再起動後もリトライレベルが正しく引き継がれる。
  異なる計画/SHA のジャーナル、journal_seq の非連続は整合エラー。

## 永続化 (`cad_campaign_execution_repository.py`)

`CadCampaignExecutionRepository(SceneRepository, sweep_repository=None)` —
スキーマ v100 の3テーブル:

- `cad_campaign_execution_plans` — 封緘計画 (plan_id/plan_sha256 UNIQUE)。
- `cad_campaign_execution_events` — ジャーナル (event_id/sha256 UNIQUE、
  plan_id + journal_seq で順序保証、kind/entry_key/position_id 等で索引)。
- `cad_campaign_execution_runs` — 測定試行レコード (run_record_id/sha256
  UNIQUE、entry_ordinal/attempt で索引)。

`_SealedStore` の append-only: 同一 sha の再保存は黙り、異なる sha は
`CampaignExecutionConflictError`。`runner_for(plan, ...)` は永続ジャーナル
から中断後の残作業を正確に再構成する。
`acquisition_sink(...)` は swstim + swrun をスイープリポジトリ経由で
`campaign_ref=plan_ref` 付きで記録し、実行と取得証跡を連鎖させる。
`native_authority_audit` の `campaign_execution` ブランチが計画/イベント/
レコードをリプレイ検証する。

## 統合点

- #869 `MeasurementAcquisitionEngine`: configure/arm/start の全結果が
  品質ゲートと precheck を通る。engine の `stage` と `result.quality` が
  キャンペーン分類の根拠。
- #813 キャンペーン権威: cal/holdout ロール分離は plan 側 `roles` と
  `calibration_state_provider` で保持。holdout のドリフト後自動リトライは
  既定で拒否。
- #868 出力消費: `CadCampaignRunRecord.acquisition_ref` が #869 の
  `swrun-` を指し、`acquisition_sink` が計画に封緘された参照で記録する —
  計画 → 実行 → 取得の連鎖が改ざん検知可能。

## 残るデバイス限定領域

- 実オーディオI/O (`WasapiAudioBackend` は本ビルドで未実装 — Fake で
  決定論検証済みの経路のみ)。`backend_is_simulated` が記録に必須で残る。
- マイクの物理的位置推定 — `position_tracker` / `tracked_fixture` は
  将来のハードウェア統合に予約。現在は attest/entered/measured いずれも
  人間操作または輸入データに依存する。
- UI 表面 (キャンペーン進捗表示・確認ダイアログ) は別イシュー —
  `application_pages` のライフサイクルラベルのみ日本語化済み。

## バリデーション

`backend/tests/test_issue_875_campaign_execution.py` (68 tests):
キュー展開順序/封緘整合、確認階梯 (tolerance 検証、attested≠measured)、
リトライ決定表全パス、クリッピング低レベルリトライ、policy stop、
holdout ドリフト拒否、ルーティング変化ブロック、pause/resume/cancel、
必須/任意 waive ポリシー、再起動復旧 (fold 再現・残作業の正確性・
in-flight 再キュー)、append-only/tamper 検知、監査リプレイ、
完了-with-失敗の誠実なアウトカム。
