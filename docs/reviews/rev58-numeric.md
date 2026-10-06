# REV58-NUMERIC — ソルバー数値忠実度 + ハイブリッド引継適格権威

スコープ: issue #683 (P0) / #685 (P0) / #687 (P0)
ブランチ: `devin/1791261109-rev58-numeric`（merge-test 経由マージ）
スキーマ: native schema v44 → v45（9 テーブル + インデックス追加 —
REV58-MEASCHAIN/DSPDECAY が v43/v44 を先取したため連番で v45 に着地）

## 実装範囲

### #683 Wave-solver numerical fidelity authority

新規 `backend/src/htdt/cad_wave_fidelity_authority.py` +
`cad_wave_fidelity_repository.py`（テーブル
`cad_wave_fidelity_profiles` / `cad_wave_numerical_convergences` /
`cad_wave_fidelity_qualifications`）。

- `WaveNumericalFidelityProfile`（wnfprof- 封印）— 宣言された
  ソルバー構成の固定スナップショット:
  - `formulation` (`WaveSolverFormulation`): solver_family
    （fdtd / fem / bem / fmbem / spectral / dgm / other_declared）、
    equation_formulation（helmholtz / time_domain_wave /
    weak_variational / boundary_integral / other_declared）、
    solver_domain（time / frequency / modal）、implementation +
    implementation_version、element_or_basis、precision、
    source_discretization、receiver_interpolation —
    「どのソルバーのどの式をどう離散化したか」を証拠として
    宣言しない限り「正確」を主張しない。
  - `discretization` (`WaveDiscretization`): mesh_identity +
    mesh_sha256（利用可能ならピン）、element_size/resolution
    記述、geometry_approximation、boundary_representation、
    points_per_wavelength_rule（宣言ルールのみ — HTDT は
    PPWL を計算しない）。
  - family 専用ブロック — fdtd なら `FdtdTimeStepping` 必須
    （dt_s、grid spacing、cfl_rule、cfl_number、
    time_integration、duration_s、stability_state =
    within_declared_stability / outside_declared_stability /
    unknown）、fem なら `FemNumerics`（element_family、
    element_order、mesh_density_descriptor、
    pollution_mitigation）、bem/fmbem なら `BemNumerics`
    （integration_scheme、singularity_treatment、
    nw_identity_condition 等）。異 family ブロックの混入は
    バリデータ拒否。
  - `artificial_boundary` (`ArtificialBoundarySpec`) —
    method（pml / absorbing_layer / sponge_layer /
    impedance_termination / none / other）+ `measured_reflection`
    （反射係数・戻りエネルギーの実測 fixture 証拠）。
    `AdjacentTerminationSpec` で計算用 PML と物理境界の主張を
    分離 — 「PML が暗黙に無響室である」主張は構造上作れない。
  - `linear_solve` (`LinearSolveEvidence`) — iterative solve の
    収束判定（残差・反復回数・収束状態）は離散収束とは別軸。
- `NumericalConvergenceRecord`（wnvconv- 封印）—
  `refinement`（≥2 level、各 level は profile を sha ピンした
  discretization + result 参照、`comparisons` は宣言済み level
  ペアのみ参照可）/ `cross_solver`（独立実装比較は
  `reference_independence_limitation` 必須 — 同一コードの
  再実行を独立検証と読まない）/ `declared_limitation`。
- `WaveFidelityQualification`（wnfqual- 封印）=
  `evaluate_wave_fidelity(document_id, profile, convergences)` —
  誤差クラス別 `ErrorClassState`（numerical_dispersion /
  fem_pollution / cfl_stability / artificial_boundary /
  algebraic_tolerance / geometry_discretization 各軸を
  qualified / limited / unresolved / not_applicable / failed で
  分離評価 — 誤差源を混同しない）。
  - fixture 不合格・宣言安定域外実行・非収束 solve →
    `not_qualified`
  - いずれかの適用可能クラスが unresolved、または収束証拠
    ゼロ → `insufficient_evidence`（欠落は dishonest な
    qualified ではなく証拠不足として正直に返す）
  - 測定済みだが数値指標なし、2-level のみの refinement、
    宣言 limitation のみ → `qualified_with_limitations`
  - 全適用クラス qualified + 強い収束証拠（≥3 level、
    analytic fixture 合格、または宣言済み cross-solver）→
    `qualified_for_declared_domain`

### #685 Geometrical-solver numerical fidelity authority

新規 `backend/src/htdt/cad_geometric_fidelity_authority.py` +
`cad_geometric_fidelity_repository.py`（テーブル
`cad_geometric_fidelity_profiles` /
`cad_ga_ray_sampling_convergences` /
`cad_ga_path_enumeration_qualifications` /
`cad_geometric_fidelity_qualifications`）。

- `GeometricalNumericalFidelityProfile`（gnfprof- 封印）—
  - `algorithm` (`GaAlgorithmIdentity`): family（image_source /
    ray_tracing / beam_tracing / tree_tracing /
    acoustic_radiance_transfer / hybrid_early_deterministic_
    late_stochastic / other_declared）+ path_class 宣言
    （specular / scattered / diffracted / transmitted、
    deterministic 系は経路列挙・ビーム可視性の扱いを宣言）。
  - deterministic 可能 family は `PathTruncationSpec`（最大
    反射回数・最小エネルギー等 ≥1 ルール必須）+ `VisibilitySpec`
    + `GaVisibilityCaseResult`（tested は fixture 参照または
    詳細必須）必須。
  - stochastic 可能 family は `RayLaunchSpec`（ray count・
    発射スキーム、seed 指定には rng_name 必須）+
    `ReceiverEstimatorSpec`（spherical/adaptive は radius_m
    必須、path_intersection_exact は半径禁止、`radius_
    sensitivity` で半径感度掃引を保持可）必須。
- `RaySamplingConvergence`（raysconv- 封印）— ray-count 掃引、
  distinct-seed 分散（SeedSpreadStudy は互いに異なる seed
  必須）、energy 収支記録。status='converged' は sweep/seed/
  detail のいずれかの証拠なしに宣言不可。
- `PathEnumerationQualification`（pathqual- 封印）—
  image-source 系の「全経路を網羅した」主張の適格評価:
  covered ∩ untested のケース重複は封印時拒否、
  `exact_ga_path_eligible` = 'qualified' は
  deterministic_state='qualified' の時のみ（#677 の
  「厳密経路」ゲート）。
- `GeometricFidelityQualification`（gnfqual- 封印）=
  `evaluate_geometric_fidelity()` — deterministic / stochastic
  両軸を分離評価。未検証軸があれば `insufficient_evidence`、
  受信球半径未評価なら `qualified_with_limitations` 上限。

### #687 Wave ↔ geometrical hybrid handoff qualification authority

新規 `backend/src/htdt/cad_hybrid_handoff_authority.py` +
`cad_hybrid_handoff_repository.py`（テーブル
`cad_hybrid_composition_profiles` /
`cad_hybrid_transition_qualifications`）。

- `HybridCompositionProfile`（hybprof- 封印）—
  - `wave_component` / `geometric_component`
    (`HybridComponentPin`): 予測を sha ピン +
    `fidelity_profile_ref`（kind は wave_fidelity_profile /
    geometric_fidelity_profile に限定 — #683/#685 権威との
    結合点）+ `qualified_band_hz`。
  - `transition_band` (`TransitionBandSpec`): low < nominal <
    high を validator で強制、`selection_basis` 必須
    （schroeder_estimate_hz は参考情報として保持可、自動
    決定はしない）。
  - `kind`（filtered_overlap / overlap_stitch /
    confidence_blend / sequential_split）— filtered/overlap/
    confidence-blend は `CrossoverFilterSpec` 必須。
  - `normalization` (`SourceNormalizationSpec`): quantity =
    pressure / energy / power / mixed_declared +
    `normalization_authority_ref` — pressure 成分と energy
    成分の混合は正規化権威または mixed_declared 明示が必須。
  - `time_alignment` (`TimeAlignmentSpec`):
    filter_group_delay_policy = zero_phase /
    group_delay_compensated / uncompensated / unknown。
  - `phenomena` (`PhenomenonDeclaration`): 各現象を wave |
    geometric の担当ブランチへ割当 + 任意の
    `decomposition_note`（共有現象の分解根拠）。
  - `composition_spec_ref`: kind='numerical_hybrid_composition_
    spec' の sha ピン — R160 ハイブリッド合成 spec 権威への
    結合点（置き換えではなく束縛）。
- `HybridTransitionQualification`（hybqual- 封印）=
  `evaluate_hybrid_handoff()` —
  - 宣言済み qualified band の比較から `gap_in_capability`
    （wave_high < ga_low またはその逆）を機械的に導出、
    `gap_band_hz` 必須。
  - 同一現象が両ブランチに割当かつ `decomposition_note` なし
    → `double_count_risk`（重複検出は叙述ではなく宣言の
    交差から導出）。
  - `ContinuityEvidence`（magnitude_step_db / ripple /
    phase_discontinuity_rad / group_delay_artifact_s の
    per-metric state）なし → `transition_unqualified`。
  - 全軸 continuous → `overlap_qualified`、一部 →
    `qualified_with_limitations`、holdout tuning /
    uncompensated group delay / phase capability 不一致で
    qualified 系は limited へ降格。

### 統合・配線

- `cad_schema_ddl.py`: 9 テーブル + 18 インデックスを
  `NATIVE_BASELINE_DDL` に追加、`NATIVE_SCHEMA_TABLES` 台帳登録。
- `cad_schema.py`: `NATIVE_SCHEMA_VERSION = 45`、
  `_migrate_44_to_45`（idempotent `CREATE TABLE IF NOT EXISTS`
  再生）、`_MIGRATIONS` 登録。`test_cad_schema.py` 台帳に
  `(45, …)` 追加。
- `native_authority_audit.py`: `wave_fidelity` /
  `geometric_fidelity` / `hybrid_handoff` factory 分岐 + 9
  `_ReplayProbe`（sha 再計算による改竄検出を replay 監査が
  検証）。
- `native_row_integrity.py`: 9 テーブル分の `_ROW_BINDINGS`
  （id/sha + 全バインド列の fail-closed 比較。ネストした
  payload パス経由で solver_family / qualified_band /
  transition edges 等をミラー）。
- `application_pages.py` `_LIFECYCLE_TABLE_LABELS`: 9 テーブルの
  JA ラベル（波動忠実度プロファイル / 波動収束記録 /
  波動忠実度適格評価 / 幾何忠実度プロファイル /
  レイサンプリング収束記録 / 経路列挙適格評価 /
  幾何忠実度適格評価 / ハイブリッド合成プロファイル /
  ハイブリッド引継適格評価）。
- `measurement_evidence_display.py`: `fidelity_state_label` /
  `handoff_state_label`（verdict の JA 表示）+
  `wave_fidelity_line` / `geometric_fidelity_line` /
  `hybrid_handoff_line`（1 行サマリ — verdict + 誤差クラス /
  軸 / handoff state を明示）。
- `backend/tests/test_rev58_numeric.py`: 42 テスト — 封印往復、
  family-block 整合、sha ピン必須、PML≠無響（unresolved →
  insufficient_evidence）、全証拠 → qualified_for_declared_
  domain、収束なし → insufficient_evidence、2-level →
  limitations、安定域外/fixture 不合格 → not_qualified、
  cross-solver 独立性必須、GA deterministic/stochastic 軸分離、
  半径未評価 cap、exact-path ゲート、gap/double-count 検出、
  continuity 必須、holdout tuning cap、append-only・改竄検出。

## 文献根拠

- **数値分散（wave）**— Ihlenburg & Babuška, *Finite Element
  Solution of the Helmholtz Equation* (1995); Marburg, "Six
  pollution errors…" (2002): Helmholtz 型離散化は k³h² 項の
  汚染誤差で波数増大と共に位相精度が劣化 → dispersion と
  fem_pollution を別 error class として分離し、PPWL は宣言
  ルールのみとした。
- **CFL 条件** — Courant-Friedrichs-Lewy (1928); Taflove &
  Hagness *Computational Electrodynamics*: 安定数と時間
  積分を `FdtdTimeStepping` で宣言、`stability_state` =
  outside_declared_stability は validator 強制ではなく
  evaluator が `not_qualified` に落とす（記録は残し判定で
  fail）。
- **PML** — Berenger (1994), Gedney (1996): 完全整合層の
  漏れは実測（反射係数/戻りエネルギー）でのみ主張可 →
  `measured_reflection` なしでは artificial_boundary は
  unresolved に留まり、全体判定を insufficient_evidence へ。
- **収束次数** — IEEE/CAM 慣行の refinement study:
  ≥2 level + level 間比較を構造必須、2-level のみは
  limitations、独立実装比較は `reference_independence_
  limitation` 宣言必須。
- **GA 収束・受信球** — Vorländer, *Auralization* (2008);
  Kuttruff *Room Acoustics*: ray 数の統計収束は掃引または
  多 seed 分散で証拠化、受信球半径は空間分解へ直接効く
  → `radius_sensitivity` 未提供なら limited 上限。
- **影像法 vs 追跡** — Allen & Berkley (1979); Savioja &
  Svensson (2015): deterministic 経路列挙は可視性 spec +
  ケース網羅が前提 → PathEnumerationQualification で
  「厳密経路」適格を独立判定。
- **Schroeder 周波数・ハイブリッド分割** — Schroeder (1962);
  Aretz & Vorländer (2014), hybrid wave-GA 連携研究:
  帯域遷移は nominal + 遷移帯で宣言、連続性は
  magnitude/ripple/phase/group-delay の観測可能量で評価、
  圧力↔エネルギーの正規化権威と位相ポリシーを分離。
- **二重計上** — 両ブランチに割当てられた現象は
  `decomposition_note` なしで double_count_risk。

## 残存事項

- **実ソルバー連携**: profile/convergence は宣言証拠。
  PFFDTD/GA 実行ハーネスから自動供給する ingest 配線は
  別タスク（既存 prediction 権威への pin 経路は整備済み）。
- **PPWL 自動推定**: `points_per_wavelength_rule` は宣言値。
  メッシュ経由の最小波長・実 PPWL の導出器は未実装。
- **fixture ライブラリ**: analytic fixture 合格証拠は
  fixture_ref ピンのみ。fixture 自体の整備は別 REV スコープ。
- **受信球半径の自動掃引**: `ReceiverRadiusStudy` は宣言保持。
  エンジン側の掃引実行は実 GA エンジン連携待ち。
- **R160 側の自動供給**: `composition_spec_ref` で
  numerical_hybrid_composition_spec に束縛可能だが、R160
  実行が spec/連続性メトリクスを自動生成する配線は未接続。
- **Schroeder 自動推定**: `schroeder_estimate_hz` は参考
  情報。room volume + RT60 からの推定器は別タスク。
- **UI 入力フォーム**: 9 テーブルの証拠入力は repository API
  経由のみ。GUI フォームは既存 authority 入力パターンに従う
  別タスク。
