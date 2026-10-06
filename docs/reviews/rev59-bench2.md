# REV59-BENCH2 レビュー記録 — 帯域意味論・ミキシングタイム・音場補間・計算予算

対象 issue: #763 (P1), #764 (P1), #755 (P2), #770 (P1), admission #766/#768 (P1)
着地: schema v52、新規 10 テーブル、リポジトリ `cad_field_metric_repository`、回帰テスト `test_rev59_bench2.py` 45 件

## 実装権威

### #763 `cad_fractional_octave.py`
- `FractionalOctaveProfile` (foct-): band_kind・frequency_standard・filter_class・process_domain・integration_semantics を宣言。IEC 61260 系周波数標準は class 0/1/2 + 時間領域/アナログフィルタ処理必須（bin 統合・FFT後合成は IEC フィルタを名乗らない）、`custom_table` は中心周波数表 ref 必須
- `BandIntegrationRecord` (band-): 帯域統合結果を profile に pin
- `evaluate_band_semantics`: 全軸宣言 → semantics_pinned、非IEC/ビン統合 → partially_pinned、軸 unknown → unpinned
- `compare_band_semantics`: kind/標準/意味論/フィルタ有無が一致しなければ incomparable — IEC 通過帯域とビン統合の混在平均を fail-closed

### #764 `cad_mixing_time.py`
- `EchoDensityProfile` (edp-): 推定器 kind・window・threshold・方向性を pin（唯一の万能式なし → 定数ではなく推定器を pin）
- `MixingTimeEstimate` (mixt-): 位置・basis（measured/predicted/spatial/simulated）・不確かさ付きの推定
- `LateFieldTransitionAssessment` (lft-): 遷移評価の封印 verdict
- `evaluate_transition`: no_estimate/basis_unknown/estimator_unknown → insufficient_evidence、user_declared/threshold 未宣言 → in_transition（sufficiently_mixed を名乗れない）、profile sha 不一致 → incomparable
- `late_handoff_gate`: **RT 単独では後期遷移を推定しない** — assessment なしでは insufficient_evidence（`rt_alone_not_evidence`）

### #755 `cad_field_interpolation.py`
- `InterpolationProfile` (fint-): method/quantity pin。`method=unknown` は権威にできず、`model_assisted` は再構成系手法（compressed_sensing/equivalent_source/image_source/solver_assisted）のみ
- `FieldCell` + `FieldSurfaceRecord` (fsurf-): セル毎の支持種別（measured_support/interpolated/extrapolated/model_assisted）を保持 — 平滑な面が「実測連続場」と見えない構造
- `evaluate_field_claim`: measured_surface は全セル実測支持必須、複素量（IR/複素音圧/音圧）の単純補間による再構成 claim は overclaimed、model_aided/reconstructed claim は model_dependent ラベル（実測に見せない）

### #770 `cad_compute_budget.py`
- `PredictedCost`: 予測コストに証拠階層（observed_run は run ref 必須、vendor_documented/extrapolated/assumed/unknown）
- `SolverBudgetProfile` (cbp-): fidelity_axes → predicted_costs の宣言
- `ComputeObservation` (cobs-): 実測ランのコスト — 予測と分離して外挿が実測を装えない
- `AccuracyCostEnvelope` (cenv-): fidelity+cost を #566 accuracy 証拠にバインド
- `evaluate_budget`（observed vs limits、unobserved → budget_unknown）+ `fidelity_cost_gate`（コスト証拠なしの忠実度宣言を捕捉）

### Admission（#766/#768）
- `docs/validation/spatial-rir-benchmark-admission.md` — dEchorate + MeshRIR の役割・採録条件・fixture/holdout 方針
- `docs/validation/room-state-benchmark-admission.md` — Motus + Arni の状態変化感度メトリクス・holdout 分割・校正境界

## 文献根拠
- IEC 61260-1/3・ISO 266（帯域フィルタ・クラス・中心周波数）
- Abel & Huang 正規化エコー密度、Defrance & Polack 初期遷移研究
- Tsunokuni et al. (Appl. Acoust. 2021) 等価音源による早期 RIR 空間外挿、圧縮センシング再構成
- Treble DG ソルバー文書（容積/IR長/最大周波数でのコストスケーリング）、Melander et al. 2024、Okuzono et al. 2022
- dEchorate (EURASIP JASM 2021)、MeshRIR、Motus (DOI 10.5281/zenodo.4923187)、Arni (DOI 10.5281/zenodo.6985104) — いずれも CC BY

## 検証
- `test_rev59_bench2.py` 45 テスト: seal/validator/fail-closed verdict/ゲート/repo roundtrip・冪等・tamper 検出・fresh-migrate テーブル存在
- 登録面: NATIVE_SCHEMA_TABLES + DDL 10 テーブル + `_migrate_51_to_52` + `_ROW_BINDINGS` 10 + `_RepositoryChain._build` `field_metric` + `_ReplayProbe`×10 + `application_pages` ラベル + `measurement_evidence_display` JA 行 + manifest 6 issue

## 残件
- データセット実取込・fixture 化（#766/#768 の実装相）は後続 — admission 計画のみ着地
- UI 登録経路（プロファイル作成ウィザード等）は後続
