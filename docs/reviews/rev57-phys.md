# REV57-PHYS レビュー — 幾何測量権威 + 設置スピーカー境界条件 + 多孔質吸収体物理モデル

対象 issue: #613 (geometry survey / scan metrology), #614 (installed loudspeaker boundary condition), #615 (porous-absorber physical model)。
スキーマ: NATIVE_SCHEMA_VERSION 37 (`_migrate_36_to_37` — 17 テーブル + 10 インデックスを baseline DDL から冪等作成)。REV57-METRO が v36 を先行取得したため本件は v37 へ再番号。authority_version は `geometry-survey-1` / `installed-source-boundary-1` / `porous-absorber-authority-1`。

## 実装範囲

### cad_geometry_survey.py (#613) — 7 封印レコード

- `GeometrySurveyInstrument` (`gsi:`) — `kind` (TLS / total station / モバイル LiDAR / 写真測量 / 手測り / sonar / 不明) と `capability_class` (`traceable_survey_instrument` / `consumer_depth` / `nominal_reference` / `unknown`) の分離。メーカー/モデルは証拠的メタデータであり精度主張ではない。
- `SurveyCampaign` (`gsc:`) — 測量セッション実体。`RegistrationTransform` (method + **honest** `uncertainty_mm`: None = 未特性化) を持つ。`MobileCaptureConditions` はモバイル捕捉の動線・環境を記録 (LiDAR ドリフトの証拠)。
- `GeometricElementEvidence` (`gee:`) — 部屋要素への証拠結合: `evidence_classes` 14 種 (design_bim / design_drawing / terrestrial_laser_scan / total_station_survey / mobile_lidar / photogrammetry / manual_dimension / hybrid_reconciled / as_built_verified / …)、`observation_state` (observed_surface / design_source_only / hidden_unknown / partially_observed)、`derivation_stage` (direct_observation / derived_registration / derived_inference / assumed)、`uncertainty` (kind別寄与量リスト)、`stale_after_change`。
- `GeometryControlMeasurement` (`gcm:`) — 独立計測 (wall_to_wall_distance / diagonal / ceiling_height / known_target / scale_bar / repeat_scan / manual_dimension)。`declared_value_mm` との残差、`tolerance_mm` 判定、`passed` 導出。**`used_for_registration` フラグ**: registration に消費されたコントロールはそのキャンペーンを検証できない (同じ観測は二度使えない — leakage guard)。
- `AsBuiltReconciliation` (`gar:`) — CAD-vs-実測の差異記録。`approved_change` 未承認は `RECONCILIATION_UNRESOLVED` でタスク降格。
- `GeometryTaskRequirement` (`gtr:`) — タスク別要件 (`sbir_early_reflection` / `low_frequency_wave_model` / `prediction_measurement_registration` / `high_precision_validation` / …) と `required_element_keys`、任意の `tolerance_mm`。
- `evaluate_geometry_qualification` → `AsBuiltGeometryQualification` (`gaq:`) — 要素状態を優先順位で fail-closed 決定: stale_after_change → insufficient_evidence (hidden) → design_only → control_check_failed → registration_limited → field_checked → observed_unqualified。タスク判定は `min(tier)` + tier≥2 でコントロール必須 + tolerance 照合。

**中核規則**: CAD 宣言精度は as-built 証拠を絶対に上回らない。design_only 要素は高精度タスクでは `insufficient_evidence`。登録不確かさを特性化していないスキャンは `registration_limited`。複数キャンペーンにまたがる要素は共通 registration 宣言なしに登録済み扱い不可。

### cad_installed_source_boundary.py (#614) — 5 封印レコード + 能力階梯

- `SourceMeasurementCondition` (`smc:`) — データセットの捕捉条件: `environment` (free_field_anechoic / quasi_anechoic / reverberant / in_situ / …)、`standard_profile` (`cta_2034_b` / `iec_60268_5` / `iec_60268_21` / manufacturer / custom / none / unknown) + **`profile_revision` は profile が declared なら必須** (バージョンなしの "standard" は証拠でない)、`baffle_condition`、`mounting_condition_at_capture`、**`includes_installed_boundary`** — データセットが設置境界を既に含むか。
- `InstalledMountingCondition` (`imc:`) — 設置実体: `kind` 12 種 (free_standing / stand_mounted / near_wall / on_wall / in_wall / flush_mounted / baffle_wall / ceiling_recessed / soffit_mounted / corner_multi_boundary / custom_cavity / unknown)、`MountedGeometry` (有限バッフル寸法・壁開口・キャビティ幅 — `finite_baffle_declared` プロパティ)、`RearCavityState` (none/sealed_volume/vented/backbox/open_stud_bay/unknown)、**`dsp_boundary_preset_ref`** (DSP 境界補正プリセット参照)、`declared_by` (design_intent / as_built_verified / user_declared / unknown)。
- `SourceBoundaryCorrection` (`sbc:`) — 境界補正宣言: `kind` (half/quarter/eighth-space boundary gain / finite_baffle_diffraction / cavity_loading / manufacturer_install_preset / screen_transfer / custom_declared)。**`corrects='path_effect'` は構築時に拒否** — SBIR 経路効果は #129 権威が保持。manufacturer preset は product/preset バインディング必須。`applicable_mountings` で適用域を限定。
- `InstalledSourceMeasurement` (`ism:`) — 設置状態計測。`fit_position_ids` と `holdout_position_ids` は **disjoint 強制** (同じ位置で fit も検証もできない)。`InstalledMeasurementObservation` は `observable` 分類 (seat_transfer / off_axis_transfer / sbir_notch / early_reflection_pattern / output_headroom / band_levels) + 残差 dB。
- `evaluate_installed_source` → `InstalledSourceQualification` (`isq:`) — 能力階梯 `_CAPABILITY_RANK`: unsupported(0) → reference_source_only(1) → reference_plus_geometric_sbir(2) → half_space_approximation(3) → boundary_corrected_empirical(4) → explicit_installed_source_model(5) → coupled_wave_model(6) → installed_measured_transfer(7)。`_MOUNTING_REQUIRED_CAPABILITY` で設置種別ごとの要求階梯。

**中核規則**:
- **Double-apply guard** — `includes_installed_boundary` なデータセット + boundary gain 補正 → `conflicting_corrections` (BOUNDARY_GAIN_DOUBLE_APPLY); DSP preset + model gain → `DSP_DOUBLE_COMPENSATION_RISK`。
- **測定条件一致**: `mounting_condition_at_capture` vs `mounting.kind` — free-field 捕捉で in-wall 設置は `MEASUREMENT_INCOMPATIBLE_WITH_MOUNTING` → unqualified_mounting_effect。
- **holdout 検証**: 設置計測が holdout 全点合格 → `installed_measured_transfer` (最高位); fit のみ → `fit_only` 警告。
- **caveat**: 有限バッフル宣言 + 半空間/明示モデル達成 → `FINITE_BAFFLE_NOT_HALF_SPACE`; キャビティ設置で rear_cavity=unknown/open_stud_bay → `REAR_CAVITY_UNCHARACTERIZED`; 非 as-built 宣言設置 → `AS_BUILT_MOUNTING_UNVERIFIED`。

### cad_porous_absorber.py (#615) — 5 封印レコード + 3 実装モデル

- `PorousParameterEvidence` (`ppe:`) — 材料パラメータ (airflow_resistivity / porosity / tortuosity / viscous_characteristic_length / thermal_characteristic_length / …/ thickness)。`evidence_class` (measured / manufacturer_declared / literature_assumed / inverse_estimated / user_assumed / unknown) と `method` (iso_9053_1_2026_static / iso_9053_2_2020_alternating / historical_iso_9053_1_2018 / manufacturer_method / independent_lab_other / in_situ_estimate / inverse_estimated / unknown) を分離。**`measured` クラスは実測メソッド必須、逆推定は `inverse_estimated` メソッド + `fit_record_ref` 必須で再ラベル不可能**。
- `PorousMaterialModel` (`pmm:`) — family (delany_bazley / miki_empirical / delany_bazley_lf_corrected / johnson_champoux_allard / other_equivalent_fluid / poroelastic_biot / measured_complex_impedance / custom_validated)、`ModelValidityDomain` (周波数範囲 + 無次元パラメータ範囲)、`required_parameters`、材料クラス許可、`requires_isotropy`、`compute_capable`、係数テーブル。組み込み: `delany_bazley_model()` / `miki_model()` / `johnson_champoux_allard_model()` / `measured_impedance_model(reference)`。
- `PorousBuildUp` (`pbu:`) — `PorousLayer` リスト (material_ref + thickness_mm + air_gap_behind_mm + facing)、`backing` (rigid / finite_absorbing / free_air / unknown)、`anisotropy` (isotropic_assumed / directional_available / declared_anisotropic / anisotropy_unknown)。
- `evaluate_porous_model_eligibility` — fail-closed: required params 欠落 → `missing_parameters`; `compute_capable=False` → `unsupported`; 材料クラス不一致 → `incompatible_material`; **isotropic モデルを declared/directional anisotropic に適用 → `unsupported`**; 妥当域外全帯域 → `outside_validated_domain`; 部分域外/ limitation 群 → `eligible_with_limitations`。
- `predict_porous_boundary` → `PorousBoundaryPrediction` (`pbp:`) — DB/Miki (正規化 x=ρ₀f/σ べき乗則) と JCA (5 パラメータ剛体骨格等価流体) の特性インピーダンス+波数から層変換行列で面インピーダンス算出: rigid → `-j·Zc·cot(kd)`; free_air → 空気 Zc·k₀; `finite_absorbing`/`unknown` は **exclude** (暗黙の rigid 仮定を決してしない)。域外周波数は `excluded_bands_hz` に収集 — **決して外挿・クリップしない**。`evidence_class` は `parametric_model_prediction` に封印。
- `compare_prediction_to_measured` → `PorousFitComparison` (`pfc:`) — `fit_band_hz`/`holdout_band_hz` 不変式 disjoint。holdout で declared_tolerance 合格 → `validated_on_holdout`; holdout なし → `fit_only` (未検証); 超過 → `residual_exceeds_declared`。
- **#570 連携**: `prediction_as_boundary_evidence()` → `MaterialBoundaryEvidence` (`method_class='derived_conversion'`, `quantity='surface_impedance'`, `incidence='normal'`, `phase='derived_complex_model'`, source_refs=prediction_id) — 予測は決して measured と偽らない。`porous_buildup_as_material_buildup()` で #570 比較形へ投影。

### 統合

- `cad_schema_ddl.py`: 17 `CREATE TABLE` + 10 `CREATE INDEX` を `NATIVE_BASELINE_DDL` 末尾へ。`NATIVE_SCHEMA_TABLES` に 17 登録。
- `cad_schema.py`: `NATIVE_SCHEMA_VERSION = 36`、`_migrate_35_to_36` (baseline DDL 全実行)、`_MIGRATIONS[36]`。
- `native_row_integrity.py`: `_ROW_BINDINGS` に 17 エントリ (id/sha/document_id/mirrored 列)。
- `native_authority_audit.py`: `_RepositoryChain._build` に `geometry_survey` / `installed_source` / `porous_absorber` の遅延ファクトリ + `_ReplayProbe` ×17。
- `application_pages.py`: `_LIFECYCLE_TABLE_LABELS` JA ラベル ×17。
- `measurement_evidence_display.py`: JA label 関数 (`geometry_*`/`installed_source_*`/`porous_*`) + `geometry_qualification_line` / `installed_source_line` / `porous_prediction_line` / `porous_fit_line` の 4 行サマリ。
- `test_cad_schema.py`: ledger に `(37, 'migrate native schema to v37')`。
- `scripts/issue_verification_manifest.yaml`: #613/#614/#615 に pytest + manual 残件登録。
- 3 append-only repository: seal 検証 (sha/id 再計算拒否)、冪等再保存、親参照 (campaign→instrument, element→campaign, control→campaign/instrument, qualification→全親 sha 一致)、prediction→model+buildup sha pin、comparison→prediction sha pin、行読み出し時の payload-vs-mirrored 再検証。

## 文献根拠

### #613 — 測量計量学
- **ISO 17123-5:2018** (total stations — 野外部試験手順、2023 確認) — traceable instrument クラスの能力評価基準。
- **ISO 17123-9:2018** (TLS) — stage 90.92 "to be revised" + **ISO/CD 17123-9 Ed.2** 起草中: TLS 現場精度評価の不安定な規格状況を反映 — TLS キャンペーンは control measurement で独立検証しないと self-claim 不可。
- **ISPRS Archives XLIII-B4-2022 / XLVIII-2-W8-2024** — iPad/iPhone LiDAR: 局所平面 RMSE ~5 mm もアプリ依存ドリフトで ~10 cm まで悪化 → `mobile_lidar` は tier 2 (control 必須) に位置づけ。
- **Leica/FARO テクニカル文書** — 点群 registration のターゲット/ICP 手順; registration 不確かさは宣言を要求 (`RegistrationTransform.uncertainty_mm` None=未特性化)。

### #614 — 境界設置音響
- **Allison, R.F., "The influence of room boundaries on loudspeaker output power" JAES 22(5) 1974** — 半空間設置で +6 dB (放射抵抗 +3 dB パワー + 圧力 +3 dB)。half/quarter/eighth-space boundary gain 係数の根拠。
- **Waterhouse, R.V. (JASA)** — 境界近傍の放射インピーダンス変動、干渉パターン — LF ローディングは宣言するが構造化計算ではなくキャパビリティ階梯で表現。
- **IEC 60268-21:2018** — 境界考慮型出力測定手順 (boundary-aware measurement)。`standard_profile` に登録。
- **IEC 60268-5** — 標準バッフル条件 (測定環境の基準面分離)。
- **ANSI/CTA-2034-B (2024-07)** — スピーカー測定標準: free-field reference condition として分類。
- **Salmensaari 1992 Helsinki thesis** (in-wall/baffle 解析) / issue 引用 AES preprint 3571 — 壁埋込み境界問題の学術参照 (AES 3571 の正確な designation は一次資料未確認のため issue 引用として扱う)。

### #615 — 多孔質吸収体
- **Delany & Bazley 1970** (AC 1373-80) — 正規化パラメータ `x = ρ₀f/σ` べき乗則 (特性インピーダンス 1+0.0571x⁻⁰·⁷⁵⁴ −j·0.0870x⁻⁰·⁷³²、波数 1+0.0978x⁻⁰·⁷⁰⁰ −j·0.189x⁻⁰·⁵⁹⁵)、**有効域 0.01 ≤ x ≤ 1.0、σ ∈ [10³, 6×10⁴] Pa·s/m²** — 域外帯域は `excluded_bands_hz` へ。
- **Miki 1990** (Acustica 69) — DB の係数改良 (正値実数化: {0.0699, −0.632, 0.107, −0.632 | 0.109, −0.618, 0.160, −0.618})。
- **Johnson-Champoux-Allard** (Allard & Atalla, *Propagation of Sound in Porous Media*, 2nd ed., Wiley 2009 ch. 5) — 剛体骨格等価流体 5 パラメータ (σ, φ, α∞, Λ, Λ′) + 空気定数 (ρ₀=1.204, c₀=343, η=1.85e-5, Pr=0.71, γ=1.4, P₀=101325)。
- **ISO 9053-1:2026 (Ed.2)** — 静的気流抵抗率測定 (:2018 を置換 — `historical_iso_9053_1_2018` で宣言可能だが明確に "歴史的" と分類)。
- **ISO 9053-2:2020** (2025 確認) — 交流気流法。
- **ISO 10534-2:2023 (+Cor.2025-08)** — インピーダンス管 法線入射 α: `measured_complex_impedance` family の参照; **法線入射 ≠ ISO 354 散野** の非等価性を assumptions に明記。
- 伝達線形積層 — `Z_in = Zc·(ZL+jZc·tan(kd)) / (Zc+jZL·tan(kd))`、空気層は `Zc_air=ρ₀c₀`, k₀。

## 検証

- `test_rev57_phys.py` **46 件グリーン**: seal/identity (prefix+sha 派生)、冪等再保存、forge 拒否 (`model_copy` 改ざん検出)、親参照強制、evaluator 全パス (design_only → insufficient_evidence タスク降格、control_check_failed、registration_limited、control leakage、stale、reconciliation demotion、free-field→in-wall 不一致、double-apply 両方向、DSP 二重補正リスク、path-effect 拒否、manufacturer preset バインディング、fit/holdout disjoint、holdout 検証昇格、missing_parameters、material veto、anisotropy fail-closed、finite-absorbing backing honesty、multilayer limitation、DB 物理値域、Miki-JCA 球場一致、#570 エクスポート)。
- `test_cad_schema.py` 25 件グリーン (v37 ledger 含む)。
- `test_native_authority_audit.py` / `test_native_row_integrity.py` / `test_cad_display_labels.py` / `test_authority_audit_coverage.py` — populated DB の 17 テーブルリプレイで `assert_native_authority_graph` 合格。
- 既存 `test_application_pages.py` 2 件は main でも FAIL (Qt shell フレーク、既知) — 本件非起因。

## 残存事項

- **#613**: TLS/スキャン実データ取込 (PLY/IFC/E57)、計測機器台帳 UI、校正証明書バインディング、element_key の部屋エディタ結合 UI。
- **#614**: 実スピーカーデータセットへの condition 付与、設置計測の取込、#129 SBIR / #282 screen-transfer 権威との双方向リンク、予測パイプラインからの capability 問い合わせ。
- **#615**: 材料カタログへの σ 証跡登録 UI、ISO 10534-2 実測データ取込、material build-up 側 (#570/#631) への prediction 差し込み、斜め入射・散野拡張、Biot/poroelastic 実装。
- **#599 連携**: ISO/IEC/ANSI 標準文書は `cad_external_standards` への登録はテスト側で構築; 中央 seed は本件対象外。
- **AES 3571 designation 未確認** — issue 文中の "AES preprint 3571" は二次確認できなかったため、文献根拠では Salmensaari 1992 学位論文に代替記載。
- **v36 衝突解決済み**: REV57-METRO (#609/#610/#611) が v36 を取得 — 本件は v37 へ再番号し、双方の DDL/_ROW_BINDINGS/_ReplayProbe/ラベルブロックを保持。
