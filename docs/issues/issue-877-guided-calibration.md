# Issue #877 — ガイド付き自動測定チェーン校正 (REV67)

再開可能な封緘済み状態機械が、必要な*物理*操作のみを明示的な
オペレータプロンプトに変換し、ソフトウェア制御の作業を全て自動化する
ガイド付き校正ウィザード。#869 掃引エンジン上にのみ取得を実行し、
#611/#699 の校正権威に証跡を封緘する。

## 3 レーン (`cad_calibration_wizard.py`)

- `interface_loopback` (レーン A): 厳密な I/O パスを束縛 → ループバック
  ケーブル配線を促す → #869 エンジンでプリフライト → ネイティブ掃引 →
  伝達導出 → 品質ゲート → 結合 DAC+ADC インタフェース校正 (#699) を
  封緘しパスに束縛。補正評価 (#699 `evaluate_interface_correction`)
  まで自動で通る。
- `spl_reference_check` (レーン B): 計器インスタンス束縛 + 封緘済み
  受理プロファイル (`CadSplCheckAcceptanceProfile`) → 校正器配置を
  促す → 記録専用窓を取得 → レベル/周波数/安定性/SNR/持続時間を
  評価 (`evaluate_reference_check`) → #611 検証チェック
  (`CadInstrumentVerificationCheck`) を封緘 → 適性状態
  (`InstrumentFitnessState`) を封緘更新。不測定/偏差/不安定/
  クリップ/低 SNR は全て不合格判定で記録される — 黙っての合否
  ごまかしなし。
- `campaign_checks` (レーン C): 封緘済みチェック計画
  (`CadCampaignCheckPlan` + `RequiredCheckSpec`) → 必須の使用前
  フィールドチェックを記録 → キャンペーン窓の経過を待つ →
  使用後チェックを記録 → ゲート評価
  (`evaluate_campaign_check_gate`)。ポリシー上必要なとき、
  欠落したチェックは従属証拠をブロックする。

## 機械 — 封緘ログが唯一の再開権威

- `CadCalibrationWizardRun` — レーン・結合参照 (io_path/instrument/
  plan/profile)・stimulus・routing を不変に保持するラン定義。
  `wizard_run_binding` が AuthorityRef を発行。
- `CadCalibrationWizardTransition` — 追記専用封緘イベント
  (`event_kind`・`outcome: advanced/blocked/regressed/completed/
  failed/cancelled/informational`・`actor`・`from/to_stage`・
  `recorded_at_utc`・`reason`・`evidence_refs`・`result_tag`)。
  `regressed` は必ずステージを後退させる — 同一ステージへの
  「リグレッション」はモデルが拒否する。
- `wizard_transition` — 純粋な遷移判定。レーン×ステージ×イベント
  の許可表 (`_ADVANCE_RULES`) にないイベントは
  `WizardTransitionRejection`。`succeeded=False` は
  `_FAILING_EVENTS` → `failed`、それ以外 → `blocked`。
  `device_lost`/`run_cancelled`/`run_failed` は全ステージで受理、
  `connectivity_restored` は未切断時に拒否。
- `derive_wizard_state` — 同一ログを同じ状態へ fold:
  現在ステージ・furthest・blocked_reason・device_disconnected・
  seen_kinds・証跡 (`evidence` dict)・最終 result_tag。
  アプリ再起動後に同じ結論が出る — 証跡未封緘のステージが
  完了と報告されることはない。
- `next_permitted_events` / `pending_physical_instruction` —
  UI が「いま何ができるか」「何の物理操作を求められているか」を
  機械から直接導出する。指示語彙
  (`connect_loopback_cable` / `position_calibrator` /
  `record_pre_use_checks` / `await_campaign_window` /
  `record_post_use_checks` / `restore_device_connection` /
  `resolve_blocked_step`) は封緘済み語彙のみ。
- `retry_step` — オペレータ要求による明示的後退
  (`_RETRY_TARGET`: lane → 再入場ステージ)。同一/前方へのリトライは
  `no_regression` で拒否。デバイス切断中のリトライは拒否 —
  ハード未復旧での再実行は証拠捏造になり得るため fail closed。

## 正直性ルール

- キャプチャは #869 `MeasurementAcquisitionEngine` 経由のみ:
  デバイス・チャンネル・サンプルレートの暗黙代替なし。
- クリッピング・低 SNR・不安定リファレンス・古い校正状態は
  fail closed — ランがブロックするか、チェックが正直な不合格を記録。
- 結合 DAC+ADC ループバック校正は常に
  `combined_dac_analog_adc_loopback` — 成分真実を主張しない。
- `run_automatic` は `advanced` 以外の outcome でループを止める —
  ブロック時に自動でスピンしない。ブロックされたソフトウェア
  ステージの回復は同じイベントの再評価 (機械が許可) か
  明示的 `retry_step` のみ。
- `tick()` は `blocked_reason` で実行を拒否しない — ブロックは
  「理由つきで止まった」記録であり、再試行可否は機械が決める。
  `device_disconnected` 中は実行しない。

## シーン封緘 (`cad_calibration_wizard_repository.py`)

`CadCalibrationWizardRepository` — runs/profiles/plans/transitions を
シーン DB の v101 テーブルへ書く。遷移行は `run_ref` バインド +
`seq` の一意性で保護され、読み出し時に全バインド列を再検証する
(改ざん → integrity error)。プロファイル/計画は内容ハッシュの
コンフリクト検出つき。スキーマは `NATIVE_SCHEMA_VERSION = 101`
(`cad_calibration_wizard_runs` / `cad_calibration_wizard_transitions` /
`cad_spl_acceptance_profiles` / `cad_campaign_check_plans`)、
`cad_schema_ddl`・`native_row_integrity`・`native_authority_audit` に
配線済み。

## UI — `calibration_wizard` コンテキスト

校正ページのガイドカードから「ガイド付き校正を開く」で遷移。
コンテキストレールには出さない (#869 acquisition と同じパターン)。

- レーン選択 (日本語ラベル) → 束縛 (I/O パス・計器・プロファイル・
  キャンペーン) → 実行ボタン → 物理プロンプト
  (`pending_physical_instruction`) のみを表示 → 確認/再試行/中止。
- デバイス列挙は正直に — WASAPI スタブでは空、シーン DB 未接続時は
  「このセッション内でのみ保持」注記。永続化がないのに証跡がある
  ように見せる表示はしない。
- 状態表示は `derive_wizard_state` の fold に完全一致:
  現在ステージ・ブロック理由・デバイス切断・終端状態・
  遷移数・最終 verdict を表示。証跡参照 (stimulus・sweep run・
  observation・calibration・check・assessment) を列挙。
- 実機実走はワーカースレッドではなく同期実行 — #869 と同じ
  フェイクバックエンドが既定で、実 WASAPI は未実装を正直に報告。

## デバイス専用に残るもの

実 WASAPI 経路でのワイヤードループバック実行、実校正器
(カリブレータ/SPL リファレンス) でのレーン B 実行、実機
キャンペーン中のレーン C 前後チェック、実デバイスでの
device_lost/connectivity_restored 観測。

Refs #877
