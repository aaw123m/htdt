# REV56-TARGETS — CEDIA/CTA-RP22 StandardsProfile + スペクトルバランス/目標プロファイル権威

スコープ: issues #579 (P1), #588 (P1)
ブランチ: `devin/1791208414-rev56-targets`
スキーマ: native schema v25 → v26（4 テーブル + 5 インデックス追加）

## 実装範囲

### #579 RP22 21パラメータ標準プロファイル権威

新規 `backend/src/htdt/cad_rp22_profile.py` +
`cad_rp22_profile_repository.py`（テーブル `cad_rp22_profiles` /
`cad_rp22_evaluations`）。

- `RP22ParameterSpec` — Appendix A の全 21 パラメータを宣言レコード化:
  定義・測定セマンティクス・HTDT 権威マッピング・mapping_status・必要
  入力・出典参照・レベル別限界（Min. と Rec. を別タプルで保持。N/A セル
  は `None` で再現 — `applies_at_level` が偽のとき評価は
  `not_applicable` であって暗黙の合格ではない）。
- 厳密な限界表現: P1 は strict `>`（1.5 m ちょうどは未達）、P8 は
  `boolean_allowed`（L1/L2 は「許可」= 必須ではない、L3/L4 のみ禁止と
  評価）、P21 は `maximum`（-8/-10/-12 dB 以下で合格 — より深い抑制は
  より良い）。P2 の `15/13*` は `RP22AlternativeLimit(condition_tag=
  'auro3d_room_design')` として符号化 — コンテキスト宣言なしの 13 台は
  L3/L4 では未達。
- `RP22StandardsProfile`（`cedia-cta-rp22-v1.2-full`、sha256 封印）は
  `standard_id='cedia-cta-rp22'`、`edition='v1.2'`、registry_key
  `cedia-cta-rp22@v1.2` に pin — #599 外部規格レジストリに登録済みの
  正確な版と一致。境界値の検証のみを行い、規格本文や CEDIA 認定を
  主張しないことは `interpretation_notes` に宣言済み。
- `evaluate_rp22_parameter`: fail-closed 判定語彙 `met` / `not_met` /
  `insufficient_evidence` / `not_applicable` / `unsupported`。
  - 証拠クラス強度ゲート（`design_prediction` < `as_built` <
    `measured_commissioning`）— 設計予測はコミッショニング評価を満た
    せない。
  - ダイナミクス（P12–14）は `RP22DynamicsBasis` 必須 — nominal spec
    のみや小信号モデルは十分な根拠にならず（圧縮/ヘッドルーム証拠が
    未検証の SPL 主張は不採用）。P12–14 以外の basis は拒否。
  - seat スコープの観測で `seat_scope` 未宣言 → limitation 注記
    （部屋全体を暗黙に代表させない）。
  - `achieved_level` は要求レベルとは独立に保持 — L4 要求が失敗して
    も L3 達成は残る。
- `evaluate_rp22_profile` → `RP22Evaluation`（`rp22ev-<sha24>`、sha256
  封印）: 全体適合は `met` / `not_met` / `indeterminate` — 不合格が
  1 件でもあれば not_met、不明しかなければ indeterminate。平均スコアは
  生成しない（`limiting_parameter_ids` / `insufficient_parameter_ids`
  で列挙）。重複・未宣言パラメータの観測は拒否。
- `rp22_verification_plan` — 未達・不明の各パラメータに必要な証拠を
  列挙（unsupported → 外部証拠経路、measurement_required → 実測定義、
  not_met → 要求値との差）。評価の `profile_sha256` pin を検証。
- `rp22_report_rows` — 21 行の証拠行列（観測値・証拠クラス・basis・
  判定・達成レベル・limitations を行ごとに保持）。
- レイアウト派生ブリッジ `derive_rp22_design_observations` — 既存の
  `rp22_spatial_profile` クライテリオンレーン
  （`scene-layout-derivation-v1`、predicted basis）を L4 基準セットで
  走らせ、P01/P05/P09 を `design_prediction` 証拠の
  `RP22ParameterObservation` へ再バインド。派生不能なパラメータは
  捏造しない。

### #588 応答目標 / スペクトルバランス権威

新規 `backend/src/htdt/cad_response_target.py` +
`cad_response_target_repository.py`（テーブル `cad_response_targets` /
`cad_spectral_balance_evaluations`）。

- `ResponseTargetProfile`（`rtgt-<sha24>`、sha256 封印）— 目標カーブ
  （#508 `CadTargetCurve` 再利用）・補間則・有効帯域・チャネルスコープ
  （screen/wide/surround/upper/lfe/subwoofer/redirected_bass/system_sum/
  custom）・seat スコープ・比較セマンティクス・許容差・由来を宣言。
  「カーブ名」だけでは再現可能な目標ではない。
- 由来タクソノミー `ResponseTargetKind`: project_defined /
  user_preference / provider_device_profile / external_standard_profile /
  measured_reference_derived / system_capability_derived / research_profile
  / unknown。kind ごとに必須 provenance を validator が強制:
  - `measured_reference_derived` → `ResponseTargetDerivation`（元測定
    id・変換 DAG・平滑/フィット・アルゴリズム版）必須 — 平滑化した測定
    を「独立工業規格」と偽れない。
  - `external_standard_profile` → `ResponseTargetExternalBinding` 必須;
    `source_version_ambiguous` / `unregistered` /
    `license_profile_unavailable` は理由必須（AVIXA A103.01 のカタログ
    "2023" vs ストア "2022" 衝突が典型例 — 正確な版が取れるまで曖昧の
    まま）。
  - `provider_device_profile` → provider/product/version + acquisition
    （generated/imported/observed）必須 — プロバイダアルゴリズム等価を
    視覚的一致から主張しない。
  - `unknown` → rationale 必須。
- `TargetComparisonSemantics` — グリッド則（target_points /
  measurement_grid / explicit_grid=グリッド必須）、スムージング
  （none/1/1/1/3/1/6/1/12 oct/variable/custom=説明必須）、絶対/相対
  表現、重み付け、窓/ゲート、面積集約（unweighted/weighted=マップ必須/
  rms_energy/median/percentile=百分位必須/worst）。±dB の比較はこの
  宣言なしには不完全。
- `evaluate_response_target` → `SpectralBalanceEvaluation`
  （`sbev-<sha24>`、sha256 封印）:
  - 正規化オフセットはカーブの `normalization` 宣言から解決
    （reference_frequency / band_average / absolute_level）。アンカー
    未解決は `normalization_failed` カバレッジとして記録。
  - 補間は宣言則（linear_db_log_hz / linear_db_hz / nearest / step）で
    行い、系列範囲外は `None` — 外挿を証拠にしない。
  - 独立メトリクス: 席別 `rms/mean/max_abs` 目標乖離 + グリッド各点
    の seat-to-seat ばらつき（≥2 席がカバーする点のみ）+ 面積集約
    `mean_rms_target_deviation_db` / `spread_rms` / `spread_max`。
    「一様だが目標外」も「平均は合致だが 1 席だけ失敗」も別々に残る。
  - `control` / `holdout` / `evaluation` ロール — 同一 pin 目標に対し
    コントロール席とホールドアウト席を別グループ集約 +
    `holdout_vs_control_delta_db`。
  - チャネルスコープ外の応答は拒否（全帯域目標を LFE 応答に適用し
    ない）。席 ID 重複も拒否。
- `target_identity_differences(a, b)` — セマンティックフィールド名を
  列挙（id/hash/時刻/version を除く）— 最適化目標 vs デプロイ目標 vs
  コミッショニング目標が「同じ目標」かを確認可能。

### UI 最小配線

- `measurement_evidence_display.py`: RP22 判定/適合/証拠クラス/
  評価種別/マッピング状態/dynamics basis、目標 kind/バインド状態/
  カバレッジ/席ロールの JA ラベル + `rp22_evaluation_line` /
  `response_target_line` / `spectral_balance_line` サマリ行（要求
  レベルと達成を分離表示、目標乖離とばらつきを別数値で表示）。
- `application_pages.py`: 新テーブル 4 件の JA 表示名登録。

## 文献根拠

- CEDIA/CTA-RP22 v1.2 (Sep 2023) Appendix A — 21 パラメータの定義・
  スコープ（Seat/Room/System）・Min./Rec. 二層限界を直接転写。公式
  PDF から抽出（P12–14 は AES75-2022 / ANSI-CTA-2034-A §8 ベースの
  capability、P15 は NCB、P16–17 は 500 Hz–16 kHz 1-oct、P19–20 は
  transition frequency 以下 1/3-oct、P21 は 0–15 ms / 1–8 kHz）。
- SPL capability ≠ リスニングレベル: RP22 §11 は「システムの余裕」を
  規定し日常再生レベルを規定しない（CEDIA clarifications,
  2025-10/2026-01 — basis 階層で強制）。
- 応答目標 ≠ 面積均一性 ≠ ターゲット形状: Welti & Devantier 系の
  seat-to-seat variance 研究では「目標との乖離」と「座席間ばらつき」
  は別指標 — 一様系は目標を外れうる、平均合致でも席別失敗が隠れ
  うる。両方を独立メトリクスとして保持。
- 万能フラットカーブは存在しない（Harman room curve / Toole target の
  実務: 目標はマイク位置・平滑・帯域に依存）— kind タクソノミーと
  明示セマンティクスで「宣言済み」目標のみを pin。

## 残存事項

- SPL 差分系パラメータ（P4/P6/P10）の anechoic 伝播予測は
  `supported_with_limitations` — in-room 寄与は実測証拠が必要。
- measurement_required パラメータ（P15 NCB、P16/P17 座席間 FR、
  P19/P20 目標/LF、P21 早期反射）は入力観測が供給されるまで
  `insufficient_evidence` — 外部測定/ソルバー証拠の配線は別 issue。
- P3/P11 のゾーン評価はレイアウト派生レーンでは観測を生成しない
  （推奨ゾーン帰属は HTDT 非派生）— 宣言された証拠が来るまで正直な
  UNKNOWN。
- `test_application_pages.py` の 2 件（`test_shell_registers_…` /
  `test_shell_project_identity_visible`）は本変更前から
  `missing: presentation, video` で失敗 — 既存ロット、本スコープ外。
- 他 REV56 兄弟セッションがスキーマ版を更に進めた場合、マージ時に
  v26→vN の再採番が必要（ESTABLISHED 手順）。
