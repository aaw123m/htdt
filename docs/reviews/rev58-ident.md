# REV58-IDENT — 校正パラメータ同定性 + 検証サンプル依存/ベンチリーク + 型付き対数量/dB 権威

スコープ: issue #689 (P0) / #698 (P0) / #691 (P0)
ブランチ: `devin/rev58-ident`（merge-test 経由マージ）
スキーマ: native schema v45 → v46（13 テーブル + インデックス追加 —
REV58-NUMERIC が v45 を先取したため連番で v46 に着地）

## 実装範囲

### #691 Typed logarithmic quantity / dB-reference authority

新規 `backend/src/htdt/cad_logarithmic_quantity.py` +
`cad_logarithmic_quantity_repository.py`（テーブル
`cad_log_quantities` / `cad_log_calibration_bridges` /
`cad_log_operations`）。

- `CadLogQuantity`（logqty- 封印）— すべての対数量を型化記録:
  - `quantity_class` (`QuantityClass`): `absolute_log_level` /
    `relative_gain_loss` / `device_relative_setting` /
    `linear_value` — 「dB」裸の値は絶対レベルか比かデバイス
    設定かを宣言なしに交えられない。
  - `domain` (`QuantityDomain`) × `quantity` (`QuantityKind`):
    acoustic（SPL / SWL / SIL / SEL / Leq / band level …）/
    digital（dBFS sample-peak / true-peak / RMS — AES17-2020）/
    electrical（dBu / dBV / declared voltage level）/
    dimensionless（gain_ratio / power_ratio / level_difference /
    TL / IL）。domain↔quantity の不一致はバリデータ拒否。
  - `CadLogReference`: 基準は value+unit+label+standard profile
    （ISO 80000-8:2020 / AES17-2020 / IEC 61672 / IEC 61260 /
    SMPTE RP 200 / vendor/custom/undeclared）+ weighting +
    band（`CadBandSpec`: octave/third-octave/fractional/FFT-bin/
    custom/broadband）+ time integration を束ねる。
    音響絶対レベルは weighting と time integration の明示必須 —
    「74 dB」だけでは物理的に未定義。
  - `RatioBasis`: 20log（field/amplitude 系）か 10log
    （power/energy 系）かは物理量の定義から決まる — UI の表示
    文字列からは決して導出しない（#691 §5）。
  - dBFS 系は +0 dBFS 超をバリデータ拒否（フルスケール上限）。
  - `CadDeviceScale`: デバイス相対設定は provider/device/scale
    marker を持つプロバイダ相対量として別型。
- `CadCalibrationBridge`（logbrg- 封印）— 2 領域間の版管理付き
  関係: `0 dBFS = full_scale_v_rms` は単位換算ではなく証拠 —
  chain_refs / device_state_ref の sha ピン必須、
  `active`/`superseded`/`withdrawn` 状態遷移つき。
- `CadLogOperation`（logop- 封印）=
  `evaluate_log_operation(...)`: fail-closed 演算判定 —
  - `level_difference`/`compare`: 量 identity（domain+quantity+
    reference identity）一致のみ `compatible` — 違えば
    `incompatible_quantities`
  - `apply_gain`: gain は relative/dimensionless 量 + 対象は絶対
    レベル + `linear_chain_declared` 必須; limiter/compressor/
    スピーカ圧縮（#649）→ `nonlinear_chain`、未宣言 →
    `unverified`（外挿値を作らない）
  - `energetic_sum`: 同一 identity + `sum_kind` 宣言 →
    `10·log10(Σ10^(L_i/10))`（線形域正準計算）; コヒーレントは
    位相データが無いため `coherent_requires_phase_data`（#690）
  - `arithmetic_mean`: 宣言済み `mean_method` 必須 —
    dB の裸算術平均は既定で正当化しない
  - `convert`: 一致する active ブリッジ必須 → なければ
    `requires_calibration_bridge`、非定数 transfer → `unverified`

### #689 Calibration-parameter identifiability authority

新規 `backend/src/htdt/cad_parameter_identifiability.py` +
`cad_parameter_identifiability_repository.py`（テーブル
`cad_calib_parameter_records` / `cad_ident_sensitivity_evidence` /
`cad_ident_correlation_evidence` / `cad_ident_equivalent_sets` /
`cad_identifiability_assessments`）。

- `CadCalibrationParameter`（calprm- 封印）—
  - `role` (`CalibrationParameterRole`): geometry / source /
    receiver / boundary absorption / boundary complex impedance /
    scattering / porous material / environment / device DSP /
    nuisance registration / model discrepancy。
  - `provenance` (`ParameterProvenance`): directly_measured /
    manufacturer_lab_evidence は証拠参照を sha ピン必須、
    calibration_adjusted / inverse_estimated /
    posterior_profile_estimate は校正ラン参照必須 —
    「調整済み」と「実測」が同一 store に混ざらない。
  - bounds・prior strength・regularization・effective domain
    （周波数帯 + 制約 source/receiver/observable 参照）を記録。
- `CadSensitivityEvidence`（idsens- 封印）— Jacobian/感度証拠は
  method + parameter scaling + observable normalization +
  perturbation detail を必須化（別メソッドの尺度と較べられない
  生数は受け取らない）。
- `CadParameterCorrelationEvidence`（idcorr- 封印）— 相関/
  トレードオフは method + assumptions 必須で定性クラス or 数値
  相関（[-1,1] 内）を保持。
- `CadEquivalentSolutionSet`（ideqset- 封印）— tolerance 内の
  近最適解を保持、物理的に別 claim を立てる member を
  `physically_distinct_claims` で明示（multimodal 宣言付き）。
- `CadIdentifiabilityAssessment`（idassess- 封印）=
  `evaluate_identifiability(...)`: fail-closed ラダー —
  - 物理的に別 claim の等価解集合 → `structurally_non_unique` /
    `parameter_not_identifiable` + 追加証拠要求（別 source 位置・
    追加帯域）
  - `confirmed` モデル形式乖離 → `model_discrepancy_limited` /
    `parameter_model_dependent`（#689 §8 — 誤指定モデルでも
    高尤度パラメータを返しうる）
  - directly_measured/manufacturer_lab_evidence → 逆同定ではない
    ため domain 内 identified（domain note 必須・自動導出）
  - prior_informative / prior_assumed → `prior_dominated` /
    not_identifiable（事前分布が安定化したが測定同定ではない）
  - 上下限に張り付き → `bound_dominated` / not_identifiable
  - 感度・相関・等価解の証拠ゼロ → `insufficient_evidence`
  - insensitive entry または強相関（定性 `strong` または
    |r|≥threshold=0.9）/ 近共線 → `weakly_identifiable` /
    `parameter_weakly_identified` + 診断 receiver/帯域の要証拠
  - `plausible` 乖離 → `model_discrepancy_limited`
  - `not_evaluated` 乖離 → クラスは identified でも claim は
    `parameter_weakly_identified` に留める（§8: モデル形式の
    問いを立てない物理パラメータ主張はしない）
  - クリーン証拠のみ `parameter_identified_within_domain` —
    domain note 必須（自動導出 or 呼出側供給）。グローバル同定は
    構造上作れない。
  - nuisance/registration パラメータが物理量と相関 → §9 の
    「タイミング/位置ずれが材料に化ける」警告を reasons に付記。
  - `evidence_needs`（§18）に型化診断出力を保持。

### #698 Validation sample-dependence / benchmark-leakage authority

新規 `backend/src/htdt/cad_validation_statistics.py` +
`cad_validation_statistics_repository.py`（テーブル
`cad_validation_statistical_designs` / `cad_dependence_models` /
`cad_dataset_role_assignments` / `cad_benchmark_exposures` /
`cad_challenge_qualifications`）。

- `CadValidationStatisticalDesign`（vsdes- 封印）—
  - `generalization_claim`（unseen_rooms / unseen_room_configs /
    unseen_sources/receivers_within_room / unseen_sessions /
    unseen_installations / across_benchmark_scenes /
    within_pair_repeatability / other_declared）。
  - `independent_unit` — room / room_config / source_position /
    receiver_position / source_receiver_pair / measurement_session /
    physical_device_instance / installation_project /
    benchmark_scene / other_explicit。**frequency_bin・repeat は
    型として存在しない**（bins/repeats は独立単位にならない）。
  - `CadHierarchyLevel` で room→source→receiver→repeat→bin の
    明示的ネスト構造。`independent_cap()` は単位レベル以上の
    積で防御可能上限を機械計算。
  - raw_observation_count / independent_unit_count /
    effective_sample_size を分離。effective size 推定は method
    必須 — 裸の数値は正当化されない（§2）。
- `CadDependenceModel`（vsdep- 封印）— 依存構造の宣言:
  spatial_correlation / frequency_bin_coupling / repeat /
  common_calibration / common_processing / common_model_inputs +
  spatial model class（Kuster reverberant family / measured
  covariance / declared / none / unknown）+ frequency 結合原因
  （finite IR window / FFT leak / smoothing / resampling / modal
  bandwidth / common calibration / deconvolution）+ bootstrap の
  resampling unit。
- `CadDatasetRoleAssignment`（vsrole- 封印）— corpus の認識論的
  役割: calibration_parameter_fit / model_selection_development /
  threshold_policy_tuning / development_validation /
  locked_challenge_test / repeatability_only。
- `CadBenchmarkExposureRecord`（vsexp- 封印）— holdout 露出の
  append-only 台帳: 何を公開・いつ・どの solver 版に・どの開発
  判断に続いたか（solver_change / threshold_change /
  metric_selection / model_selection / debugging /
  published_result）。
- `CadChallengeQualification`（vsqual- 封印）=
  `evaluate_validation_claim(...)`: fail-closed —
  - 独立数未宣言 → `insufficient_evidence`
  - 独立数 > hierarchy cap（repeats/bins 計上）→
    `pseudoreplication_detected`
  - 分割粒度 < claim 粒度（例: receiver 単位で unseen_rooms
    主張）→ `split_mismatch`
  - claim corpus が model_selection/threshold_tuning 役割または
    selection 系露出あり → `winner_selection_biased`
  - claim corpus が development/fit 役割または露出あり →
    `development_validation_only`
  - spatial/frequency 構造があるのに依存モデル無し →
    `independence_unestablished`（XYZ が違うだけでは独立にならない）
  - untouched な locked_challenge corpus 存在 →
    `locked_challenge_eligible`
  - 残存 unknown（other_explicit unit、未確立 spatial model、
    error-distribution scope 未宣言）→ `qualified_with_limitations`
  - それ以外 → `qualified_generalization`
  - `corpus_states` に各 corpus の status（untouched/exposed/
    superseded_for_version）を保持 — 後の solver 版で role が
    変わっても履歴を消さない。

## 統合

- スキーマ v46: `NATIVE_BASELINE_DDL` に 13 CREATE TABLE + INDEX、
  `NATIVE_SCHEMA_TABLES` 登録、`_migrate_45_to_46`、
  `_MIGRATIONS[46]`、`test_cad_schema.py` 台帳に
  `(46, 'migrate native schema to v46')`。
- `native_row_integrity.py` `_ROW_BINDINGS` に 13 テーブル分の
  列↔payload バインド（ネスト AuthorityRef は `ref_id` パス、
  nullable は optional）。
- `native_authority_audit.py` に `_RepositoryChain` ファクトリ
  （logarithmic_quantity / parameter_identifiability /
  validation_statistics）+ 全 13 テーブルの `_ReplayProbe` —
  replay_canonical 監査対象に自動編入。
- `measurement_evidence_display.py` に JA 表示行:
  `log_operation_line` / `identifiability_line` /
  `validation_claim_line` + 状態ラベル辞書。
- `application_pages.py` `_LIFECYCLE_TABLE_LABELS` に 13 テーブルの
  JA ラベル。
- `scripts/issue_verification_manifest.yaml` に #689/#691/#698
  エントリ。

## テスト

`backend/tests/test_rev58_ident.py`（35 テスト）:
- DB 系: 参照 identity 必須・domain 一致・dBFS 上限・
  演算 fail-closed・ブリッジ要求・リポジトリ往復+改竄検出
- IDN 系: provenance ピン・等価解 → 非一意・強相関 → 弱同定・
  prior/bound 支配・モデル形式未評価キャップ・domain note 必須・
  往復+改竄
- VSD 系: hierarchy cap・疑似反復検出・分割粒度不一致・依存未宣言・
  locked challenge 適格・露出 bias・往復+改竄
- 横断: 偽造封印拒否・JA 表示行・数値相関閾値

scoped pytest:
`test_rev58_ident.py` 35 本 + `test_cad_schema.py` +
`test_cad_schema_ddl_contract.py` + `test_native_row_integrity.py` +
`test_authority_audit_coverage.py` + `test_native_authority_audit.py` +
`test_native_authority_audit_hardening.py` + `test_rev56_measev.py`
— 全グリーン。

## 文献根拠

- **同定性 (#689)**: Mondet et al. 2024 (同定不能パラメータでも
  適合するベイズ校正) / Wulbusch et al. 2024 (モデル形式乖離と
  物理パラメータ) / Raue et al. (profile likelihood, structural vs
  practical identifiability) / Morris & Sobol 感度解析 /
  ill-posed 逆問題の多解性。fit の良さ ≠ パラメータ一意性を
  evaluator が強制。
- **ベンチリーク (#698)**: Dwork et al. 2015 *The reusable holdout*
  （適応的参照は holdout を腐らせる → 露出台帳）/ Hurlbert 1984
  pseudoreplication（相関観測を独立単位と数えるな）/
  Kuster 2008（残響場の空間相関 — 別座席は独立にならない）/
  Johnson & Long 1999（平滑スペクトルの bin 依存性）。
- **dB 型 (#691)**: ISO 80000-8:2020 + Amd 1:2025（量次元・
  参照・weighting/time integration は identity の一部）/
  AES17-2020（dBFS 規約）/ IEC 61672 / IEC 61260 /
  SMPTE RP 200（-20 dBFS → 85 dBC シネマチェーンは型化
  bridge として記録）。

## 残存事項

- 実校正ラン (#564/#675) からの Jacobian/profile/multi-start 証拠の
  自動供給 — 現状は宣言インタフェースのみ。
- 実ベンチコーパス (#773/#793 locked holdout) への role 割当・露出
  台帳の自動配線。
- 既存 SPL/dBFS 計算パスの型付き量への段階移行、不確かさ伝搬
  (#572) との統合。
- UI の入力フォーム（表示行は配線済み、編集 UI は未着手）。
- ICC/AR(1) 等の残差相関推定器・群分割 CV の自動適用。
