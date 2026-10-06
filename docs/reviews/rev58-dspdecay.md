# REV58-DSPDECAY — DSP フィルタ実現適格 + 減衰曲線ノイズ/切断処理 + 音響インピーダンス物理実現性

スコープ: issue #679 / #676 / #705 (全て P0)
ブランチ: `devin/rev58-dspdecay`（merge-test 経由 main マージ）
スキーマ: native schema v43 → v44（13 テーブル + インデックス追加）

## 実装範囲

### #679 DSP フィルタ実現適格権威

新規 `backend/src/htdt/cad_dsp_realization.py` +
`cad_dsp_realization_repository.py`（テーブル
`cad_dsp_realization_profiles` / `cad_dsp_stage_records` /
`cad_dsp_parameter_mappings` / `cad_dsp_realization_qualifications`）。

- `CadDspRealizationProfile`（dsppro- 封印）— デバイス実現モデル:
  宣言処理サンプルレート集合・フィルタバンク（family, max_filter_count,
  max_fir_taps）・係数フォーマット（float32/float64/
  fixed_point_documented/…/proprietary_hidden）・露出度
  （exact_coefficients_exposed → nominal_parameters_only → hidden）・
  処理順序・内部リサンプリング・プロバイダ来歴・#592 スナップショット
  ピン。**非ゼロサンプルレート宣言と ≥1 バンクが必須**。
- `CadDspParameterGrid` + `map_value()` — 決定論的マッピング:
  範囲外 → `unsupported_rejected`（クランプしない）、範囲内 off-step →
  `rounded_within_step`（delta 記録）、グリッド未宣言 →
  `unmappable_convention`、丸め規則不明 → `unknown`。
  `build_dsp_parameter_mapping` は任意の rejected/unmappable/unknown
  エントリを `unsupported`/`partially_unsupported` に倒す — 部分失敗は
  「declared deltas でマップ済」とは見なさない。
- `CadDspStageRecord`（dspstg- 封印）— 7 ステージ taxonomy
  （ideal_design → device_target_parameterization → encoded_artifact →
  requested_device_state → observed_readback_state →
  predicted_realized_transfer → measured_realized_transfer）。
  `observed_readback_state` は #592 スナップショット ref または
  content sha を必須化。
- `CadDspRealizationQualification`（dspqual- 封印）+
  `evaluate_dsp_realization` — fail-closed ラダー:
  宣言外レートの係数再利用 → `incompatible` →
  リジェクト/部分リジェクト → `reoptimization_required` →
  実測/予測ステージあり + エラーなし → `realized_within_declared_model` →
  実測/予測 + 宣言済みエラー成分 → `realized_with_declared_approximation` →
  モデル限界（係数量化 + 隠れ処理、または unmappable convention）→
  `realization_model_limited` → `nominal_state_match_only` →
  `unqualified`。`readback_status`（device_readback_match 等）は
  `transfer_verification`（transfer_realization_verified 等）と
  **分離保持** — 名目一致は伝達関数検証にならない。5 エラー成分
  （dsp_parameter_rounding / dsp_coefficient_quantization /
  dsp_implementation_unknown / sample_rate_mapping /
  device_hidden_processing）を全件報告。

### #676 減衰曲線ノイズ/切断処理権威

新規 `backend/src/htdt/cad_decay_processing.py` +
`cad_decay_processing_repository.py`（テーブル
`cad_decay_processing_profiles` / `cad_decay_noise_estimates` /
`cad_decay_truncation_decisions` / `cad_decay_edc_artifacts` /
`cad_decay_fit_records`）。issue 命名のモデル:
`EnergyDecayProcessingProfile`→`CadDecayProcessingProfile`、
`NoiseFloorEstimate`→`CadDecayNoiseEstimate`、
`RIRTruncationDecision`→`CadRirTruncationDecision`、
`DecayFitRecord`→`CadDecayFitRecord`（Cad 接頭辞はリポジトリ規約）。

- `CadDecayProcessingProfile`（decpro- 封印）— 処理同一性: バンド仕様
  （banded は中心または端周波数必須 — 「125 Hz RT」の無個体値を拒否）、
  Schroeder 後方積分バリアント（生/雑音補正/末尾補正/declare 外部 —
  補正系は noise_floor_method 宣言を強制）、雑音床推定法
  （lundeby_style_intersection / nonlinear_decay_plus_noise_model /…）、
  尾補正、指標ごとの評価窓（EDT/T20/T30 — 降順窓のみ）、定常性仮定、
  measured/simulated 来歴（simulated は solver_truncation_declared を
  保持）、アルゴリズムバージョン。
- `CadDecayNoiseEstimate`（decnse- 封印）— 推定器・区間・定常性・
  レベル・不確かさ。
- `CadRirTruncationDecision`（dectrn- 封印）— `capture_truncated` と
  `reason='noise_intersection'` の組み合わせをモデル拒否 —
  有限長測定は雑音限界ではない。`DecayTruncationCause` で
  `measurement_noise_limit` と `solver_time_or_order_truncation` を
  明示分離。
- `CadDecayEdcArtifact`（decedc- 封印）— raw_backward_integral /
  noise_compensated_edc / tail_corrected_edc / declared_external を
  別アーティファクトとして保持。補正系は `derived_from_ref`（raw EDC
  のハッシュピン）を必須化 — 処理済みは「上書き」ではなく派生。
- `CadDecayFitRecord`（decfit- 封印）+ `evaluate_decay_fit` —
  fail-closed eligibility ラダー: modal_method_required →
  multi_slope_model_mismatch → capture_truncated →
  non_stationary_noise → insufficient_decay_range（指標窓に対する
  有効減衰域不足）→ noise_floor_too_high（ノイズマージン不足）→
  noise 推定未資格（measured 必須だが欠落）→ `indeterminate` →
  証拠一部欠落 → `eligible_with_limitations` → `eligible`。
  eligible 系は `value_s` 必須 — 不適格で値を提供しない（逆に値が
  あれば理由列挙と共に記録される）。

### #705 音響インピーダンス物理実現性ゲート

新規 `backend/src/htdt/cad_boundary_realizability.py` +
`cad_boundary_realizability_repository.py`（テーブル
`cad_boundary_evidence_records` / `cad_boundary_rational_fits` /
`cad_td_impedance_realizations` /
`cad_boundary_realizability_assessments`）。issue 命名:
`AcousticBoundaryRealizabilityAssessment`→`CadBoundaryRealizabilityAssessment`、
`PassiveBoundaryQualification`→証拠/評価レコードとして分離、
`TimeDomainImpedanceRealization`→`CadTdImpedanceRealization`。

- `CadBoundaryEvidenceRecord`（bdevi- 封印）— 証拠クラス 9 種
  （measured_frequency_domain_impedance / parametric_physical_model /
  rational_frequency_domain_fit / spline_interpolated_table /
  delany_bazley_family / miki_model / komatsu_family /
  active_control_boundary / imported_unknown）、quantity convention
  （impedance_z / admittance / reflection_factor /…）、法線方向、
  宣言受動クラス（passive_boundary / active_boundary_explicit /
  nonpassive_unexpected / unknown）、観測帯域、**最小実部値**
  （保存時に clip しない — `min_resistive_value=-0.02` はそのまま
  封印される）、|R| 上限、不確かさ分解、補間/外挿/対称性宣言、
  因果チェック宣言。`active_control_boundary` は
  `passivity_class='active_boundary_explicit'` 宣言を強制 —
  アクティブ境界は passive 経路に入れない。
- `CadRationalPole.is_stable(domain, margin)` — 連続時間: re<−margin
  安定 / re>margin 不安定、離散: |p|<1−margin。`CadBoundaryRationalFit`
  （bdrat- 封印）は pole_count と長さの一致を強制、
  `pole_stability(margin)` で集約。
- `CadTdImpedanceRealization`（bdtim- 封印）— ソルファミリ・タイム
  ステップ・境界更新スキーム・積分法・境界安定性余裕/規準
  （Toyoda 系）、solver 帯域、FD↔TD 残差 vs 宣言許容、エネルギー
  増大観察、対称性。
- `evaluate_boundary_realizability`（bdass- 封印）— issue §19 の
  10 state を verbatim 出力: `active_control_boundary` →
  ハード違反（resistive depth > 不確かさ、|R| depth > bound+不確かさ、
  causality_violation_detected、宣言 nonpassive_unexpected）→
  `nonpassive_input` → `unstable_fit`/`unstable_realization` →
  `time_domain_realization_mismatch`/`symmetry_violated` →
  軟違反（不確かさ内の負実部）→ `passivity_unresolved_with_uncertainty` →
  `insufficient_evidence` → `causality_unresolved_finite_band`
  （有限帯域はグローバル因果 PASS にしない）→
  `stable_numerical_realization`（TD 実現が健全）→
  `passive_causal_validated` → `passive_with_limitations`（solver 帯域が
  証拠帯域外、独立スプライン、境界極、端外挿）。
  §18 エラー成分 7 種（resistive_violation / reflection_bound /
  causality_underspecification / interpolation_artifact /
  fit_stability / solver_discretization / out_of_band_assumption /
  passivity_repair_delta）を全件保持。

## 統合

- `measurement_evidence_display.py`: JA 表示行 3 関数
  （`dsp_realization_line` / `decay_fit_line` / `boundary_realizability_line`）
  — 層状 verdict（readback vs transfer、eligibility、
  passivity/causality/stability 成分）を潰さない。
- `application_pages.py` `_LIFECYCLE_TABLE_LABELS` に JA ラベル 13 件。

## スキーマ登録

- `cad_schema_ddl.py`: 13 CREATE TABLE + インデックスを
  `NATIVE_BASELINE_DDL` 末尾に、`NATIVE_SCHEMA_TABLES` に 13 テーブル登録。
- `cad_schema.py`: `NATIVE_SCHEMA_VERSION = 44`、`_migrate_43_to_44`、
  `_MIGRATIONS[44]`。
- `native_row_integrity.py`: 13 テーブルの `_ROW_BINDINGS`（ネスト
  `AuthorityRef` は `ref_id` パス、`value_s` 等の nullable カラム対応）。
- `native_authority_audit.py`: `_RepositoryChain` ファクトリ 3 件
  （dsp_realization / decay_processing / boundary_realizability）+
  `_ReplayProbe` 13 件 — `audit_table_modes` 経由で replay_canonical
  カバレッジ自動登録。
- `backend/tests/test_cad_schema.py`: 移行台帳 `(44, 'migrate native
  schema to v44')`。
- `scripts/issue_verification_manifest.yaml`: #679/#676/#705 エントリ
  （pytest チェック + manual 残件）。

## 文献根拠

### DSP 実現（#679）

- Bristow-Johnson, R.W., "Cookbook formulae for audio EQ biquad filter
  coefficients", AES 1994 — PEQ の Q/octave 帯域幅規約と biquad 係数
  マッピング。`DspParameterConvention` に `bristow_johnson_q` /
  `octave_bandwidth` / `hz_bandwidth` / `shelf_slope_db_per_oct` /
  `vendor_defined` を分離した根拠。
- Agrawal, K. et al., AES 2021 — 実デバイス DSP の係数量化/語長効果:
  fixed_point_undocumented / proprietary_hidden を独立クラスとして
  扱い、word length 未文書化はエラー成分として評価を bounds。
- minidsp/AVR ベンダー実装観察 — フィルタバンク容量（max_filter_count/
  max_fir_taps）は出口で宣言され、超過要求は silent clamp ではなく
  reject/再最適化が行業標準。

### 減衰曲線（#676）

- Schroeder, M.R., "Backward-integrated impulse..." 1965 / Liu et al.
  1979 — reverse-time EDC の基本式と誤差伝播。
- Lundeby, A. et al., "Uncertainties of measurements in room acoustics",
  Acustica 81 (1995) — 雑音床交点推定、フィット区間選定、不確かさ。
- Bodlund, K., J. Sound Vib. 1978 — 減衰特性の統計評価。
- Dragonetti, R. et al., 2009 / Janković, M. et al. 2016 — 雑音床下の
  減衰推定と非線形同時フィット。
- ISO 3382-1 / ISO 3382-2 — T20/T30/EDT の評価区間とエンド端差分離
  （measurement_noise_limit vs solver_time_or_order_truncation）。

### 音響インピーダンス（#705）

- Toyoda, T. 2018 — FDTD 境界セルの安定条件（boundary_stability_
  criterion の型付き保留）。
- Jang & Ih 2012 / Zhong, Zhang & Huang 2016 — 時間領域インピーダンス
  実現・線形時不変境界の因果/安定要求。
- Wang & Hornikx 2020 — 有理フィットの受動性 enforcement。
- Rodio, Hu & Nark 2022 — 受動境界実現の FD↔TD 検証。
- Srivastava 2021 — 音響材料モデル適用域。
- 受動条件 Re(Z)≥0・因果性（KK 関係は帯域有限では未解決）・
  安定極（連続 re<0 / 離散 |z|<1）を機械検査として実装。

## テスト

- `backend/tests/test_rev58_dspdecay.py` — DSR10/20/40/60/70/80、
  DEC10/30/40/50/60/70/80/90、ABI10/30/40/50/60/70/80/90/100
  フィクスチャ + 3 リポジトリ往復 + 封印改竄 + 表示行。
- scoped pytest: test_rev58_dspdecay + test_cad_schema +
  test_cad_schema_ddl_contract + test_native_row_integrity +
  test_authority_audit_coverage + test_native_authority_audit +
  test_native_authority_audit_hardening + test_rev56_measev グリーン。
- `assert_row_integrity_registry_complete()` パス。

## 残存事項

- #679: 実デバイス係数読み戻しパイプライン、ベンダー係数バイナリ
  デコード、フィルタバンク容量データベース係留、DSP 再最適化
  エンジン、UI 入力フォーム。
- #676: Lundeby 自動交点推定器・非線形同時フィット実装、測定位置
  依存 sensitivity propagation、複数 HEM 分散推定、IEC 61672 不確かさ
  予算、UI 入力フォーム。
- #705: 実測管データからの evidence 自動生成、vector-fitting 統合
  （pole extraction / passivity enforcement 実装）、#683 CFL 権威との
  組成、solver-specific 境界安定条件データベース、UI 入力フォーム。
- 共通: 本 REV58 は権威モデル+永続化+評価器の着地点。既存 ingest /
  solver / 測定 UI への live 配線は次の REV で行う — 本権威が
  ゲート用の型を提供する。
