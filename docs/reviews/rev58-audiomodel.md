# REV58-AUDIOMODEL — 音源原点 / 音場適用域 / 指向性分解能 / 多源相関 / 散乱・回折モデル適格権威

スコープ: issue #654 (P1) / #655 (P1) / #656 (P1) / #690 (P1) /
#684 (P1) / #681 (P1)
ブランチ: `devin/1791269165-rev58-audiomodel`（merge-test 経由マージ）
スキーマ: native schema v46 → v47（14 テーブル + インデックス追加）

## 実装範囲

### #654 Acoustic-reference origin / phase-center authority

新規 `backend/src/htdt/cad_source_origin_authority.py` +
`cad_source_origin_repository.py`（テーブル
`cad_source_origin_profiles` / `cad_source_origin_qualifications`）。

- `SourceReferenceOriginProfile`（sorprof- 封印）—
  宣言された基準点の固定スナップショット:
  - `acoustic_center_estimates`（`AcousticCenterEstimate`）:
    `model_kind`（physical_reference / acoustic_reference /
    band_limited_effective_acoustic_center /
    no_single_center_model）× `evidence_class`
    （directly_measured / manufacturer_data / inferred_from_
    geometry / assumed_at_driver_aperture / unknown 等）×
    `method`（実測系は必須）× `position_m` +
    `valid_band_hz`。band_limited_center は帯域必須、
    no_single_center_model は位置を主張不可。
  - `dataset_frame`（`DatasetReferenceFrame`）:
    データセット自体の座標原点宣言（測定距離の定義等）。
  - `capability`: unknown / location_only /
    signed_phase_reference / absolute_phase_reference —
    absolute は `phase_time_reference` か測定遅延証拠
    （directly_measured / derived_from_time_delay）なしに
    封印不可。
- `SourceOriginQualification`（sorqual- 封印）=
  `evaluate_source_origin(document_id, profile, ...)` —
  - 要求帯域を覆う実測・帯域別位相中心がある →
    `origin_qualified`
  - 幾何推定・開口部仮定・装着境界の不一致 →
    `origin_limited`（制限を列挙）
  - dataset_frame だけで推定ゼロ → `origin_unverified`
  - 推定も枠組みもなし → `insufficient_evidence`
- **核心規約**: キャビネット CAD ポーズは伝搬原点として
  読まれない — 原点は必ず宣言と証拠クラスから導出される。

### #655 Source near-field / far-field applicability authority

新規 `backend/src/htdt/cad_source_field_applicability_authority.py`
+ `cad_source_field_applicability_repository.py`（テーブル
`cad_source_field_profiles` / `cad_source_field_qualifications`）。

- `SourceFieldProfile`（sfldprof- 封印）—
  - `measurement_geometry`（`MeasurementGeometrySpec`）:
    mic_distance_m、distance_reference_kind（測定点が何に
    対する距離か）、environment（anechoic_far_field /
    near_field_scan / quasi_anechoic_gated / in_situ 等）、
    gate_window_s（quasi_anechoic_gated では必須）。
  - `band_regimes`（`BandFieldRegime`）: 帯域別に regime
    （near_field_scan / transition_field / anechoic_far_field /
    diffuse_field …）+ `applicable_distance_m`（適用距離域）+
    `transition_distance_m`（宣言時は basis 必須 — D²/λ 等）。
  - `origin_ref`: kind='source_reference_origin_profile' に
    限定 — #654 権威への sha ピン結合。
  - `near_far_derivation`（`NearFarDerivation`）:
    近接場スキャンから遠場を導出する宣言（NAH 等）—
    ソース測定を sha ピン。
- `SourceFieldQualification`（sfldqual- 封印）=
  `evaluate_source_field_applicability(document_id, profile, …)` —
  - capability 不一致（polar のみで multi_radiator_time_
    domain 要求等）→ `requires_explicit_multi_radiator_model`
  - 要求距離 < regime の min 距離（遠場モデルに対し）→
    `distance_too_close_for_selected_far_field_model`
  - near_field_scan に遠場距離で導出宣言なし →
    `nearfield_only`、宣言あり →
    `applicable_with_approximation`
  - ゲート窓分解能を割る低域要求 → `insufficient_evidence`
  - 宣言帯域外・遷移場のみ → `transition_field_limited` /
    `insufficient_evidence`
  - 全域カバー → `directly_applicable`

### #656 Directivity angular-resolution / interpolation authority

新規 `backend/src/htdt/cad_directivity_resolution_authority.py` +
`cad_directivity_resolution_repository.py`（テーブル
`cad_directivity_sampling_profiles` /
`cad_directivity_interpolation_records` /
`cad_directivity_direction_qualifications`）。

- `DirectivitySamplingProfile`（drsprof- 封印）—
  - `sampling`（`AngularSamplingSpec`）: coverage_class
    （full_sphere_sampled / hv_cuts / frontal_hemisphere /
    sparse_declared / unknown …）、measured_direction_count、
    nominal_step_deg。
  - `dataset_kind`: magnitude_only / complex_tf /
    spherical_harmonic_coefficients / other_declared。
  - `interpolation`（`AngularInterpolationSpec`）:
    method（linear_angular / nearest_measured /
    spherical_harmonic_reconstruction / complex_tf_
    interpolation / custom_validated …）と domain
    （magnitude_db / complex_tf / sh_coefficients …）の
    適合は validator 強制; SH 法は `ShRepresentation`
    （supported_order + convention）必須、complex_tf 法は
    complex データ必須、custom_validated は validation 宣言
    必須。
  - `presentation_step_deg`: 表示グリッド刻み —
    実測刻みより細かいグリッドは情報を増やさないので、
    これを超える分解能は claim できない。
  - `band_capabilities`（`BandAngularCapability`）:
    adequate / marginal / spatial_aliasing_risk /
    unmeasured 等の帯域別能力状態。
- `DirectivityInterpolationRecord`（dinterp- 封印）—
  実際に実行した補間の記録（method/domain/output_grid を
  プロファイルに sha ピン）。
- `DirectionQueryQualification`（drqual- 封印）=
  `evaluate_direction_query(document_id, profile, az, el, …)` —
  - 実測グリッドに一致 → `measured_direction`
  - 補間内挿・カバレッジ内 → `interpolated_eligible`、
    帯域能力限定 → `interpolated_limited`
  - カバレッジ外（hv_cuts のオフプレーン等）→
    `outside_coverage`、外挿方向 → `extrapolated`
  - 要求 SH 次数 > 宣言次数 → `sh_order_unsupported`

### #690 Multi-source correlation / coherence authority

新規 `backend/src/htdt/cad_source_coherence_authority.py` +
`cad_source_coherence_repository.py`（テーブル
`cad_source_coherence_profiles` /
`cad_source_combination_qualifications`）。

- `SourceCoherenceProfile`（mscprof- 封印）—
  - `members`（`SourceSignalPin`, ≥2）: 各メンバーは自身の
    ソースプロファイルを sha ピン、stimulus_ref・filter_refs
    もピン。
  - `relations`（`CorrelationRelation`）: member ペア別に
    relation（deterministic_identical / deterministic_delay_
    shifted / correlated_filtered / uncorrelated_independent /
    partially_coherent / content_dependent / time_varying /
    unknown …）+ `basis`（measured_cross_spectrum /
    measured_coherence_function / analytic_signal_path /
    bounded_scenarios_declared / unknown …）。
    partially_coherent は CSD 証拠または measured 系 basis
    必須; non-unknown relation は unknown basis を拒否。
  - `csd_evidence`（`CsdEvidence`）: 周波数グリッド昇順
    正値、hermitian_declared 必須。
  - `default_relation`: non-unknown は関係カバレッジ必須、
    deterministic 系は全メンバー stimulus_ref 必須。
- `SourceCombinationQualification`（mscqual- 封印）=
  `evaluate_source_combination(document_id, profile,
  requested_mode, …)` —
  - deterministic/coherent 群への非相関パワー和要求 →
    `incompatible_combination`（コヒーレント源を独立騒音源
    のようにエネルギー加算しない）
  - partial coherence → partial_coherence_csd または
    statistical モードのみ適格
  - content_dependent / time_varying → statistical のみ
  - unknown/undeclared → bounded_scenarios が宣言されていれば
    `qualified_with_scenario_bounds`、要求モードを拒否して
    `insufficient_evidence`

### #684 Geometric surface-scattering model qualification authority

新規 `backend/src/htdt/cad_scattering_model_authority.py` +
`cad_scattering_model_repository.py`（テーブル
`cad_scattering_model_profiles` /
`cad_scattering_model_qualifications`）。

- `SurfaceReflectionModelProfile`（scatprof- 封印）—
  - `solver_model`: specular_only / specular_plus_lambert_
    diffuse / specular_plus_measured_directional /
    pure_lambert / random_incidence_scalar / custom_validated
    …。
  - `coefficient_mapping`
    （`CoefficientDistributionMapping`）: 入力係数種別 ×
    分布法則（lambert / uniform_hemisphere /
    measured_kernel / none …）× specular/diffuse 分数 —
    **係数→分布の明示写像なしに散乱係数が黙って Lambert
    分布にならない**。
    - ISO 17497-2 directional_diffusion_coefficient 等の
      非分数種は solver_input として拒否
      （display_documentation ロールでのみ許可）
    - unit_energy マッピングは分数和 = 1 必須
    - measured_kernel 分布は random_incidence_scalar 入力を
      拒否（多次入射実測が前提）
  - 係数消費モデルは mapping 必須、measured_directional は
    measured_kernel 法則必須、custom_validated は実
    validation tier（measured/simulated/literature +
    evidence_refs）必須。
  - `early_late_applicability`、`directional_redirection`
    （幾何学的再配分能力）、`incidence_domain`。
- `ScatteringModelQualification`（scatqual- 封印）=
  `evaluate_scattering_model(document_id, profile, …)` —
  - 再配分を要する幾何に非再配分モデル →
    `directional_redirection_unsupported`
  - early_reflection 要求に late-only モデル →
    `qualified_with_limitations`
  - 宣言領域内 → `qualified_for_declared_domain`

### #681 Edge-diffraction model qualification authority

新規 `backend/src/htdt/cad_edge_diffraction_authority.py` +
`cad_edge_diffraction_repository.py`（テーブル
`cad_diffraction_model_profiles` /
`cad_diffraction_benchmark_results` /
`cad_diffraction_qualifications`）。

- `EdgeDiffractionProfile`（edfprof- 封印）—
  - `model_family`: btm_finite_edge / utd / det_rational /
    keller / iir_reduced_approximation /
    numerical_wave_reference / custom_validated —
    `unknown` は封印時拒否（モデル選択は必ず宣言）。
  - `edge_geometry`（`EdgeGeometryPin`）: edge_kind
    （finite_straight_edge / infinite_wedge / curved_edge /
    corner_vertex …）、finite edge は edge_length_m 必須、
    wedge_angle_deg。
  - `wedge_material`（`WedgeMaterialSemantics`）:
    rigid_reference / impedance_boundary_supported（要
    impedance_refs）等。
  - `orders`（`DiffractionOrderSpec`）、`numerics`
    （`DiffractionNumericsSpec`: series_summation /
    numerical_integration / iir_filter /
    keller_asymptotic 等 + `convergence_evidence_ref`）、
    `visibility`（`DiffractionVisibilitySpec`）。
- `DiffractionBenchmarkResult`（difbench- 封印）—
  profile を sha ピン、fixture_id/kind（finite_edge /
  infinite_wedge_reference / double_wedge / screen …）、
  reference_class（bras / analytic_btm / measured /
  cross_solver / literature 等）、observables + result —
  pass は mismatch を含められない。
- `EdgeDiffractionQualification`（difqual- 封印）=
  `evaluate_diffraction_model(document_id, profile,
  benchmarks, …)` —
  - fail fixture がひとつでも → `unqualified`
  - fixture ゼロ → `insufficient_evidence`
    （numerical_wave_reference のみ limitation 付き
    `physical_reference_capability`）
  - 強い fixture（finite_edge / double_wedge / screen 等）
    全合格 → `physical_reference_capability`、弱い fixture
    のみ → `physical_approximation_capability`
  - iir_reduced_approximation は強い fixture なしで
    `perceptual_approximation_capability` に cap
  - 剛体モデルを非剛体楔に適用 → 降格 + boundary_limited
  - 収束証拠なし → physical_reference は approximation に
    cap

## 統合・配線

- `cad_schema_ddl.py`: 14 テーブル + インデックスを
  `NATIVE_BASELINE_DDL` に追加、`NATIVE_SCHEMA_TABLES` 台帳登録。
- `cad_schema.py`: `NATIVE_SCHEMA_VERSION = 47`、
  `_migrate_46_to_47`（idempotent `CREATE TABLE IF NOT EXISTS`
  再生）、`_MIGRATIONS` 登録。`test_cad_schema.py` 台帳に
  `(47, …)` 追加。main 側で REV58-IDENT が v46 を先取したため
  連番で v47 に着地。
- `native_authority_audit.py`: `source_origin` /
  `source_field_applicability` / `directivity_resolution` /
  `source_coherence` / `scattering_model` / `edge_diffraction`
  factory 分岐 + 14 `_ReplayProbe`（sha 再計算による改竄検出を
  replay 監査が検証）。
- `native_row_integrity.py`: 14 テーブル分の `_ROW_BINDINGS`
  （id/sha + バインド列の fail-closed 比較。bool→INTEGER
  ミラー列も直接比較。len() を要する count 列は repo
  `get_*` で検証）。
- `application_pages.py` `_LIFECYCLE_TABLE_LABELS`: 14 テーブルの
  JA ラベル（音源基準原点プロファイル / 音源原点適格評価 /
  音場適用プロファイル / 音場適用適格評価 / 指向性サンプリング
  プロファイル / 指向性補間記録 / 方向問合せ適格評価 /
  音源相関プロファイル / 音源合成適格評価 / 散乱モデル
  プロファイル / 散乱モデル適格評価 / 回折モデルプロファイル /
  回折ベンチマーク結果 / 回折適格評価）。
- `measurement_evidence_display.py`: 6 組の verdict ラベル +
  `source_origin_line` / `source_field_line` /
  `directivity_direction_line` / `source_combination_line` /
  `scattering_model_line` / `edge_diffraction_line`（1 行サマリ）。
- `backend/tests/test_rev58_audiomodel.py`: 71 テスト — 封印
  往復、sha ピン必須、capability/適合性 validator、全
  verdict 経路、append-only・改竄検出。

## 文献根拠

- **位相中心** — Keele, "The Acoustic Center of a Loudspeaker"
  (JAES 1977); Vanderkooy, "The Acoustic Centre: A Small-
  Driver Approximation" (JAES 1983): 位相中心は帯域・幾何
  依存で単一点とは限らない → `band_limited_center` と
  `no_single_center_model` を別 model_kind で表現し、
  evidence_class（実測/メーカー/幾何推定/仮定）を必須化、
  未宣言は origin_unverified/insufficient_evidence に落とす。
- **近接/遠距離場遷移** — Fraunhofer 距離 d > 2D²/λ の
  古典的基準; AES/CEA-2010 測定距離宣言慣行:
  `transition_distance_m` は basis 宣言必須、測定距離・
  基準点・環境を `MeasurementGeometrySpec` で pin。
  ゲート窓測定の低域分解能限界（Δf ≈ 1/T_gate）を
  `gate_window_s` から機械的に導出して低域 claim を
  insufficient_evidence に落とす。
- **空間サンプリングと補間** — Rafaely, *Fundamentals of
  Spherical Array Processing* (2015); 球面サンプリング定理
  （有効 SH 次数 N ≈ kr と刻み間隔の関係）: `nominal_step_deg`
  と `ShRepresentation.supported_order` を宣言し、補間
  グリッドが密でも claim は実測サンプリングの情報上限に
  留まる。補間法と表現 domain の適合（complex_tf 法は
  complex データ必須等）を validator で強制。
- **多源相関** — Beranek & Mellow *Acoustics: Sound Fields
  and Transducers*; ISO 3745 等のパワー和前提:
  独立非相関源のみ energy sum が正当。関係を
  `CorrelationRelation`（deterministic / correlated /
  partial / uncorrelated / content-dependent / time-varying /
  unknown）+ basis で宣言し、証拠なし独立源仮定と
  deterministic 群へのパワー和を `incompatible_combination` で
  拒否。サブ/LCR 群の位相関係は member 単位の
  stimulus_ref ピンで固定。
- **散乱係数** — ISO 17497-1/-2; Vorländer *Auralization*
  (2008) 4.3 散乱モデル; Christensen & Rindel:
  random-incidence 散乱係数 s は「鏡面以外へのエネルギ
  分数」であり角度分布を定めない → 係数→分布の写像を
  `CoefficientDistributionMapping` で必須化し、ISO 17497-2
  方向拡散係数を solver scalar input から構造的に排除、
  Lambert 既定は `specular_plus_lambert_diffuse` の明示宣言
  に限定。
- **回折モデル** — Biot & Tolstoy (1957), Medwin (1981)
  BTM; Keller (1962) GTD; Kouyoumjian & Pathak (1974) UTD;
  Svensson et al. (1999) 二次源法の誤差特性: モデル族を
  必須宣言（unknown 不可）、有限 edge 長・楔角・境界材を
  `EdgeGeometryPin`/`WedgeMaterialSemantics` で pin、
  収束証拠を `convergence_evidence_ref` で宣言、
  benchmark fixture（BRAS 実測、解析 BTM、cross-solver）
  の pass/fail で capability を段階評価 — 未検証モデルは
  insufficient_evidence、IIR 近似は perceptual cap。

## 残存事項

- **実測パイプライン連携**: 位相中心自動推定、遷移距離
  自動推定（D²/λ 計算器）、SH 次数推定、CSD 実測取り込み、
  BTM/UTD fixture 実行ハーネス — いずれも権威は宣言を
  受理する構造のみで、自動供給側は別タスク。
- **live 結合**: #654 origin_ref は source_field_profile の
  `origin_ref` で結合済みだが、excitation authority（#668 系）
  ・balloon 権威・material scattering（#570 系）・geometric
  fidelity（#685）への実行時伝搬は未接続 — solver が
  実際に origin/距離/分布を読む箇所の配線は別 REV。
- **UI 入力フォーム**: 14 テーブルの証拠入力は repository
  API 経由のみ。ライフサイクル画面にラベルと 1 行サマリは
  追加済み、フォーム UI は別タスク。
- **部分相関の定量評価**: `bounded_scenarios` は宣言保持
  のみ。correlation coefficient ρ(f) の数値的上下限からの
  合成誤差範囲計算は未実装。
- **回折 fixture ライブラリ**: fixture_id + reference_class
  で証拠を pin するが、fixture データ自体（BRAS 計測値等）
  の整備は別 REV スコープ。
