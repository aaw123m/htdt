# REV56-MEASEV — 測定不確かさ予算・測定状態安定性ゲート・変換権威

スコープ: issues #572 (P0), #573 (P0), #575 (P1)
ブランチ: `devin/1791196327-measev`
スキーマ: native schema v21 → v22（6 テーブル追加）

## 実装範囲

### #572 測定不確かさ予算 + 計量トレーサビリティ

新規 `backend/src/htdt/cad_measurement_uncertainty.py`。

- `MeasurementUncertaintyBudget`（凍結・`mub:<sha>` 封印）: 測定対象に
  結び付く不確かさ予算。寄与要素 `UncertaintyContributor` は
  16 種の寄与種別と 8 種の出所クラス（`calibration_certificate` /
  `manufacturer_spec` / `repeatability_empirical` /
  `standard_typical_value` / `geometry_tolerance` /
  `model_propagation` / `assumed_bound` / `unknown`）を持ち、
  `assumed_bound` は `bounded_interval` 表現しか許さない — 仮定の
  境界を校正証拠へ昇格させることはバリデータで拒否される。
- GUM 評価種別: Type A は実測標本列を要求、Type B は証明書/仕様/
  標準代表値根拠、UNKNOWN は UNKNOWN 表現を要求。
- 合成: `first_order_linear`（#979 `propagate_uncertainty_budget` へ
  委譲、宣言済み相関グループは線形加算）、`bounded_worst_case`
  （半幅線形加算）、`monte_carlo`（GUM-S1 的割当: std→正規,
  区間→一様, 標本→ブートストラップ; seed/回数/アルゴリズム
  バージョンを記録）、`declared_only`（合成を捏造しない —
  `significance_threshold` は None、判定は insufficient）。
- スペクトル不確かさ: 同一周波数グリッド宣言時のみ合成、不整合
  グリッドは limitations へ記録して拒否。
- トレーサビリティ: `CalibrationChainLink`（instrument →
  calibrator → intermediate → reference/si_traceable）、各リンクは
  `linked/broken/unknown` + 破断理由必須。`traceable_documented` は
  全リンク linked + 末端が reference_standard/si_traceable_reference
  の時だけ claim 可能。`relative_only` は absolute_spl を拒否。
- `CalibratorCheck`（pre/post）→ `evaluate_calibrator_drift` で
  `calibrator_stable / calibrator_drift_detected /
  insufficient_calibrator_evidence`。
- 有意性判定: `classify_residual`（残差 vs 拡張不確かさ →
  `residual_clearly_above_measurement_uncertainty` /
  `residual_comparable_to_measurement_uncertainty` /
  `residual_below_resolution_of_evidence`（u_c/2 未満）/
  `insufficient_uncertainty_information`）。`assess_residual_report`
  は #564 `PredictionMeasurementResidualReport` の全 observable を
  走査して封印済み `ResidualSignificanceAssessment`（`msa:<sha>`）を
  発行。`evaluate_delta_significance` は u_delta = sqrt(ua²+ub²)
  で before/after 差を判定 — 証拠不確かさより小さい改善は
  `delta_inconclusive_within_evidence_uncertainty` のまま。
- `uncertainty_display_decimals`: UI 表示桁は証拠で上限化。
- 測定/モデル/ソルバ不確かさは別枠: budget は measurement 面
  （`engine_surface='measurement'` を宣言）に限定され、
  `model_propagation` 寄与は別予算として分離可能な記録。

### #573 測定状態安定性ゲート

新規 `backend/src/htdt/cad_measurement_state.py`。

- `MeasurementStateSnapshot`（凍結・`mss:<sha>`）: 観測時刻・
  sequence index・シーン内容ハッシュ・環境観測（T/RH/気圧/音速 +
  source+authority ref）・開口部観測・占有
  （unoccupied/occupied_as_designed/partial_occupancy/
  temporary_obstacle_present/unknown）・デバイス動的状態（8 機能:
  adaptive_room_correction/loudness/night_mode/compressor_limiter/
  tone_controls/auto_delay_trim/dynamic_eq/dynamic_bass — 各
  enabled/disabled/engaged/unknown、リミッタ発動観測フラグ、
  熱状態・再生レベル）・騒音体制（5 源 + フロア）。全項目は
  「明示値または UNKNOWN」— 消灯前提の自動仮定はしない。
- `StateControlPolicy`（凍結・`spol:<sha>`）: 温度差・湿度差・
  気圧差・音速相対差・経過時間・占有規則・開口部要求・動的 DSP
  要求・騒音規則・シーン一致・測定距離帯域・反復閾値をすべて
  宣言的に保持 — 単一の万能閾値は存在しない。
- `evaluate_state_comparability`（凍結 verdict `msv:<sha>`、
  evaluator `measev-mss-gate-1`）:
  - スナップショット欠落 → `insufficient_state_evidence` +
    全ドメイン不可（fail-closed）
  - シーン内容ハッシュ不一致 → `state_changed`（ハード）
  - 温度・湿度超過 → `state_changed`（位相・時間・減衰不可）
  - 気圧・音速・経過時間・繰り返し不可解釈 →
    `comparable_with_declared_drift`（ドリフト、限定ドメイン不可）
  - 占有不一致/要件違反、開口部不一致/要件違反、動的 DSP
    engaged または非一致、リミッタ発動観測、騒音不一致/フロア
    超過 → `state_changed` + 当該ドメイン不可
  - 要求された状態が `unknown` → `insufficient_state_evidence`
  - 優先度: state_changed > insufficient > declared_drift > stable
- `evaluate_sequence_stationarity`: 有界反復列（同一グリッド
  必須）の最大連続 |Δlevel| とレベルドリフトを policy 閾値と比較
  → `non_stationary_during_capture` / `comparable_stable_state` /
  `insufficient_state_evidence`。

### #575 測定変換権威

新規 `backend/src/htdt/cad_measurement_transform.py`。

- `MeasurementTransform`（凍結・`mtr:<sha>`）: 21 種変換（RMS/dB/
  ベクトル/ハイブリッド平均、6 種算術、フル/バンド位相回転、
  時間/位相整合、レベル整合、時間シフト、IR 遅延除去、窓、ゲート、
  クロックレート補正、リサンプル、平滑、バンド集約、正規化、
  周波数結合）。入力は測定データセット / IR / 上位変換のいずれかに
  sha256 ピン — DAG を構成する。
- 能力フラグ（8 種: magnitude/absolute_spl/relative_phase/
  absolute_phase/relative_timing/absolute_timing/
  impulse_response_physical/distortion）は降格のみ —
  `output_capabilities ⊆ 全入力 ∩ 種別許可` をバリデータが強制。
- 平均クラス: `repeat_capture` / `spatial` / `operator_blend` /
  `declared_purpose`。空間平均は distinct positions 要求・
  位相/物理 IR 出力には alignment 宣言要求、繰り返し平均は
  位置混合を拒否。ベクトル/ハイブリッド位相平均は全入力
  `relative_phase_valid` 必須。
- アライメント権威: `measured_common_timing_reference`（
  timing_reference_type = loopback/acoustic_reference_speaker/
  known_hardware_reference/other 必須）・
  `estimated_cross_correlation`（アルゴリズム+バージョン+
  相関帯域+per-input 遅延/ゲイン）・`manual_declared`・
  `direct_arrival`・`unknown`。推定整合からの出力に
  `absolute_timing_valid` は許可しない。
- クロック補正: `ClockCorrectionSpec`（loopback/acoustic_reference/
  independent_estimated）+ 適用係数 + 残余誤差境界 +
  `raw_source_preserved` は常に True 必須。
- 算術は宣言済み quantity domain ペアのみ合法
  （`_ARITHMETIC_LEGAL` 表; dB×dB 乗算等は拒否）。
- `apply_transform`（executor `measev-mta-exec-1`）: 入力 identity
  を sha256 で照合し、共通グリッド未宣言時の暗黙リサンプルを拒否。
  RMS/dB/ベクトル（宣言遅延回転付き）/算術/位相回転/レベル整合/
  周波数結合/リサンプル/平滑/バンド集約/正規化を実行し、
  `DerivedFrequencyResponseOutput`（content_sha256 封印）を発行。
  時間領域種別は「記録のみ・実行不可」を正直に拒否。

### 統合

- `cad_measurement_evidence_repository.py`: 6 テーブルの
  append-only リポジトリ（payload_json + ミラー索引列、読み出しで
  モデル+ミラー列の一致を再検証、同 id 別内容 →
  `MeasurementEvidenceConflictError`）。
- `cad_schema_ddl.py`: `cad_measurement_uncertainty_budgets` /
  `cad_measurement_significance_assessments` /
  `cad_measurement_state_policies` /
  `cad_measurement_state_snapshots` /
  `cad_measurement_state_verdicts` / `cad_measurement_transforms` +
  索引。`cad_schema.py`: v21→v22 マイグレーション + 台帳行。
- `native_row_integrity.py`: 6 テーブルの行束縛。
- `native_authority_audit.py`: `measurement_evidence` リポジトリ +
  6 リプレイプローブ（`replay_canonical` カバレッジ自動登録）。
- `application_pages.py`: ライフサイクル表 JA ラベル 6 件。
- `measurement_evidence_display.py` + `measurement_page_workspace.py`:
  品質ページの詳細行に不確かさ要約/トレーサビリティ/状態判定
  （未登録・未記録は正直に「未登録」「未記録」と表示、失敗時は
  「取得に失敗」行）。

## 文献根拠

- ISO/IEC Guide 98-3 (GUM): 合成標準不確かさ u_c(y)=sqrt(Σu_i²)
  （無相関）、拡張不確かさ U=k·u_c（k≈2 ≈95% 包含）、Type A
  （実測標本）/Type B（証明書・仕様・矩形区間）評価、GUM-S1
  Monte-Carlo（seed 宣言、割当分布を記録）。
- JCGM 200/VIM 語彙: traceability chain = 校正階層の各段階を
  「リンク + 状態（linked/broken/unknown）+ 破断理由」で記録、
  破断時は `traceable_documented` を claim 不可能。
- REW 測定セマンティクス（公式ヘルプ + Foz/FIR処理文献）:
  RMS 平均 = 非相干パワー平均、dB 平均 = デシベル算術平均、
  ベクトル/複素平均 = 位相情報を使う（位相コヒーレンス必須）、
  SPL 整合（level alignment, declared span）、時間整合
  （timing reference: loopback/acoustic 基準 vs 推定クロス
  コリレーション）、クロックドリフト補正（raw source を保持）。
- Prawda & Schlecht & Välimäki (2024, JAES): 室内 RIR は
  温度変動により非定常 — 位相/到達時刻は微小な音速変化で最先に
  劣化するため、環境ドリフトは phase/timing/decay を magnitude より
  先に invalidation。
- Cramer (1993): 音速温度依存 c≈331.3+0.606·T を
  音速観測値の整合性確認の物理的枠組として参照。

## 回帰テスト

`backend/tests/test_rev56_measev.py`（71 件）:

- 封印・id 一貫性・append-only 永続化・ミラー列整合・衝突拒否
- fail-closed: 破断リンク/unknown 状態/欠落スナップショット/
  グリッド不整合/入力差し替えをすべて拒否または UNKNOWN で
  返すことを検証
- 数値検証: RSS（0.3⊕0.4=0.5、相関グループ線形 0.6 vs 独立
  sqrt(0.18)）、worst-case 線形和、Monte-Carlo ≈0.5（±5%、
  seed 再現）、dB/RMS/ベクトル平均・算術・整合・正規化・
  集約・リサンプル・結合の解析値一致
- 「0.2 dB の改善が不確かさより小さい場合 inconclusive のまま」
  の検証

スコープ pytest:
`test_rev56_measev` + `test_cad_schema` + `test_cad_schema_ddl_contract`
+ `test_authority_audit_coverage` + `test_issue_979_uncertainty_budget`
+ `test_measurement_authorities` +
`test_cad_measurement_asset_contract` +
`test_measurement_workspace_composition` +
`test_measurement_quality_producer` +
`test_cad_measurement_quality` + `test_rev55_corrqual` — 全緑。

## 残存事項

- 不確かさ予算を実測定へ自動付与する取り込み経路（REW CSV import
  時の budget 付帯等）は未配線 — 権威と表示は完成、プロデューサー
  への自動生成は後続。
- 状態スナップショットの物理採取ワークフロー（環境計測・占有確認・
  DSP 状態確認 UI）は宣言的権威のみ — インポータ/UI 取得経路は残件。
- 時間領域変換の実行系（window/gate/time_shift/clock_rate による
  IR 変換）は権威レコードとして記録可能だが実行は後続。
- Monte-Carlo は正規/一様/ブートストラップの 3 割当のみ（その他
  分布は後続で拡張可）。
- `residual_below_resolution_of_evidence` の分解能床は
  u_c/2（保守的仮定）— 測定分解能の厳密モデル化は残件。
- issue_verification_manifest.yaml に #572/#573/#575 エントリ追加済み。
