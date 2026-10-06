# REV58-VALIDMETH — 最適化アルゴリズム適格 + 固有モード検証 + 拡散場適用性 + 結合室マルチスロープ + 初期反射対応 + 時周波モーダル減衰 権威

スコープ: issue #675 / #674 / #673 / #671 / #677 / #706（全 P1）
ブランチ: `devin/1791271884-rev58-validmeth`（merge-test 経由マージ）
スキーマ: native schema v47 → v48（16 テーブル + インデックス追加 —
`NATIVE_SCHEMA_TABLES` 登録済み。REV58-AUDIOMODEL が作業中に v47 を
先取したため連番で v48 に着地 — 両側のテーブル・バインディング・
移行関数を併存）

設計原則は REV57/REV58 と同じ: すべての権威レコードは
content-addressed な frozen pydantic モデル（`_seal` + sha256 +
semantic id）、append-only リポジトリ（同 sha 再保存は no-op、
カラム改竄は IntegrityError で fail-closed）、評価器は純粋関数で
verdict を新規封印する。CLAIM > EVIDENCE を構造的に禁止する。

## 実装範囲

### #675 Optimizer algorithm qualification authority

`backend/src/htdt/cad_optimizer_qualification.py` +
`cad_optimizer_qualification_repository.py`（テーブル
`cad_optimization_problems` / `cad_optimizer_run_profiles` /
`cad_optimizer_qualifications` / `cad_pareto_assessments`）。

- `OptimizationProblemIdentity`（optprob- 封印）— 問題同一性:
  決定変数、目的関数（direction/unit/weighting 込み）、硬制約、
  scene/variant/search-spec pin、solver fidelity。目的の向きを
  変えた実行は別問題 — 同一リーダーボードで比較しない。
- `OptimizerRunProfile`（optprof- 封印）— ベンチマーク同一性:
  `OptimizerAlgorithmSpec`（family + implementation + version +
  正準 `name=value` パラメータ + RNG/seed policy — 確率的
  アルゴリズムの seed policy 未宣言はバリデータ拒否）、宣言済み
  `SearchBudget`（fidelity 別の評価数を区別 — surrogate 評価を
  solver 評価として計上しない）、`IndependentRunRecord` の集合
  （確率系は seed 重複拒否）、等予算ベースライン、
  `KnownOptimumFixturePin`、マルチフィデリティ昇格契約。
- `evaluate_optimizer_qualification` → `OptimizationRunQualification`
  （optqual-）: fail-closed 規則 —
  - 予算未宣言 → `insufficient_evidence`（予算は一次情報）。
  - 確率系を単一実行で評価 → `qualified_with_limitations` 上限
    （seed 分散を測っていない）。
  - 収束証跡なし / 予算到達時点で改善中 → 上限
    `qualified_with_limitations`（プラトーは経験的証拠、数学的大域
    収束ではない）。
  - `global_optimum_proven` は `exhaustive_enumeration` 等の厳密系
    + 回収済み既知最適フィクスチャが必須 — 不十分なら claim は
    `best_found_under_budget` へ縮退し理由を残す。
  - 既知最適フィクスチャ未回収 → `unqualified` + regression flag。
  - ベースラインも既知最適も無い探索は `qualified_with_limitations`
    上限 — 未ベンチの「最適」を拒否。
  - マルチフィデリティで finalists を共通 fidelity で再評価してい
    ない / surrogate 誤差証拠なし → 上限。
- `evaluate_pareto_approximation` → `ParetoApproximationAssessment`
  （parassess-）— 収束・多様性・実行可能率を分離:
  `no_reference_front` → 最高でも `assessed_empirical_only`
  （attainment/coverage 挙動のみ、フロント品質スカラーは出さない）;
  `best_known_aggregate_front` → `assessed_with_limitations`
  （アルゴリズム出力の和集合は真のフロントではない）;
  GD/IGD/epsilon は pinned reference front をバリデータ必須、
  hypervolume は reference point 必須 — 基準のない数値は比較不能。

### #674 Acoustic eigenmode / mode-shape validation authority

`cad_eigenmode_validation.py` + `cad_eigenmode_validation_repository.py`
（`cad_mode_pairings` / `cad_eigenmode_verdicts`）。

- `PredictedEigenmodePin` / `MeasuredModalEvidencePin` — solver 側と
  計測側のモーダル同一性を分離（複素固有値/減衰、モード形状場 ref、
  識別アルゴリズム + 版、不確かさ、holdout 位置）。reconstructed
  field は派生証拠であり voxel 全点の直接計測に昇格しない。
- `ModePairingRecord`（modpair- 封印）— 対応付けを証拠次元で宣言:
  `frequency_proximity` 単独では `paired_high_confidence` に到達
  できない（レコード層で拒否）。近縮退モードは
  `degenerate_subspace_match` + `subspace_members`≥2 を要求。
- `evaluate_eigenmode_validation` → `EigenmodeValidationVerdict`
  （eigval-）: 周波数・形状・減衰を別観測量として評価 —
  - freq-only 証拠 → `frequency_only_match_insufficient` 上限。
  - 周波数不一致 → `frequency_mismatch`; 宣言済み形状 acceptance
    band 未達 → `shape_mismatch`; 宣言済み減衰公差超過 →
    `damping_mismatch`（`declared_damping_tolerance_s` 未宣言の残差は
    報告のみ）。
  - `degenerate_subspace_match` は honest —
    `eigenmode_validated_with_limitations`（部分空間を評価、基底回転は
    失敗ではない）。
  - `validation_role='calibration'` → `calibration_contaminated` +
    上限（校正に消費された証拠は独立検証にならない）。
  - `source_participation_low`/`receiver_observability_low` は確度を
    下げるだけでモードを消さない。

### #673 Diffuseness / statistical-model applicability authority

`cad_diffuseness_applicability.py` +
`cad_diffuseness_applicability_repository.py`（
`cad_diffuseness_assessments` /
`cad_statistical_applicability_declarations`）。

- `DiffuseFieldDomainPin` — 評価領域を identity 化（空間 extent・
  帯域・時間領域・位置クラス・送信/受信条件）。
- `FieldUniformityProfile` — 推定器ごとのプロファイル:
  `covered_property`（energy_uniformity / directional_isotropy /
  incoherent_tail / ensemble_decay_statistics / position_invariance）
  を宣言 — エネルギー分散推定器は方向等方性を代理しない。
  evidence の provenance は `measured` / `model_derived_indicator` /
  `theory_assumption` を区別。
- `SoundFieldDiffusenessAssessment`（diffassess- 封印）— eligibility
  を封印（`eligible_within_declared_domain` … `non_diffuse_field` /
  `directionally_biased` / `transition_region` /
  `estimator_incompatible` / `insufficient_evidence`）。estimator
  も証拠も無いアセスメントは `insufficient_evidence` 系のみ許容。
- `evaluate_diffuseness_applicability` →
  `StatisticalModelApplicabilityDeclaration`（diffdec-）— 用途別に
  fail-closed:
  - assessment 未提示 → `insufficient_evidence`/`unsupported`。
  - `non_diffuse_field` / `estimator_incompatible` / 要
    `directional_isotropy` に対する `directionally_biased` →
    `not_applicable`。
  - 必要特性のカバレッジ欠落 → `applicable_with_limitations` で
    欠落特性を明示。
  - `theory_assumption`/`model_derived_indicator` のみの basis →
    理論限界を伴う `applicable_with_limitations` — 実測主張に
    昇格しない。
  - 推定器間の競合は平均せず保持。

### #671 Coupled-room multi-slope decay authority

`cad_coupled_decay.py` + `cad_coupled_decay_repository.py`（
`cad_multi_slope_fits` / `cad_single_slope_assessments` /
`cad_coupled_decay_qualifications`）。

- `CoupledDecayProfile` — 結合シーンの同一性: member
  `AcousticRegion` refs、portal refs（#1029 coupling、#573 ドア
  状態を含む）、solver pin、`solver_supports_energy_exchange`。
  multi-region は portal pin 必須。
- `evaluate_single_slope_adequacy` → `SingleSlopeAdequacyAssessment`
  （cplgate-）— 位置ごとのゲート:
  - noise-floor limited（#676 処理証拠）→ `noise_limited`。
  - dynamic range 欠落/不足（既定 45 dB）→ `insufficient_range` —
    第2スロープを見せる尾が観測されない。
  - curvature/multi-slope 証拠宣言 → `multi_slope_supported`。
  - 平坦宣言 + 十分レンジのみ `single_slope_adequate`。
- `MultiSlopeDecayFit`（cplfit- 封印）—
  double_slope/coupled_exponential/non_exponential モデルを raw
  証拠へ束縛（components は early/late/intermediate、decay_rate_s
  を時間として保持 — T30 に畳まない）。
- `evaluate_coupled_decay` → `InterRegionEnergyDecayQualification`
  （cplqual-）:
  - 複数 region + solver がエネルギー交換を表現不能 →
    `single_slope_collapse_rejected` — 結合シーンの単一スロープ
    主張を却下。
  - adequacy 証拠なし → `insufficient_evidence`。
  - multi-slope 支持 + 束縛 fit なし → `qualified_with_limitations`
    （suspected のまま保持、潰さない）。
  - 全位置 adequate → `qualified` + `single_exponential_decay` —
    ただし宣言位置/帯域限定の注記付き（室レベル RT 主張はしない）。
  - 弱証拠（noise/insufficient_range）位置は列挙して保持。

### #677 Predicted↔measured early-reflection correspondence authority

`cad_reflection_correspondence.py` +
`cad_reflection_correspondence_repository.py`（
`cad_reflection_pairings` /
`cad_reflection_correspondence_sets` /
`cad_reflection_correspondence_verdicts`）。

- `PredictedReflectionPath` — solver 側経路 identity:
  **順序付き** `interaction_refs`（面/稜線）+ path class
  （specular/edge_diffraction/surface_scattering/mixed — 散乱事象は
  失敗した鏡面経路ではない）+ 到達時間/次数/レベル/DOA。
- `ObservedReflectionEvent` — 抽出アルゴリズム + 版を identity に
  含める（同じ RIR でも抽出器が違えば別事象）。
- `ReflectionCorrespondencePairing`（rfxpair- 封印）— 対応付けは
  複合証拠次元（time_alignment / direction_of_arrival /
  geometric_path_consistency / reflection_order /
  level_compatibility / spectral_signature /
  cross_position_consistency）で宣言 — 隠れた最近傍スコアは存在
  しない。`unresolved_cluster`/`many_*` は cluster メンバー必須。
- `ReflectionCorrespondenceSet`（rfxset-）— direct-path
  registration ref が必須前提。
- `evaluate_reflection_correspondence` →
  `ReflectionCorrespondenceVerdict`（rfxverdict-）:
  - registration 無効 → `registration_prerequisite_missing`。
  - `time_alignment` 単独の対応付けは ambiguous に計上 —
    最近傍ピークは壁を校正しない。
  - ambiguous/unresolved 存在 → `ambiguous_unresolved`。
  - 全対応付けが校正に消費済み →
    `calibration_only_no_independent_validation`。
  - 未対応の予測/観測は計数保持（隠さない）→
    `qualified_with_limitations`。

### #706 Time-frequency modal-decay authority

`cad_modal_decay_view.py` + `cad_modal_decay_view_repository.py`（
`cad_modal_decay_observations` / `cad_modal_decay_qualifications`）。

- `TimeFrequencyDecayTransform` — 変換同一性: kind（STFT 固定窓 /
  moving-IR-window CSD / CWT / Morlet / S-transform /
  filtered-modal-decay …）、窓・hop・周波数グリッド・wavelet
  パラメータ・正規化・時間/周波数分解能・信頼範囲・edge 宣言。
  `transform_binding` は content-addressed — パラメータ変更は別
  変換。
- `ModalDecayObservation`（mdtobs- 封印）— モード候補減衰を変換 +
  raw 証拠 + #676 処理 refs に束縛。overlap state（isolated /
  partially_overlapped / unresolved_multiple_modes /
  ridge_crossing_ambiguous / insufficient_frequency_resolution）、
  envelope semantic（energy/magnitude/log-energy は別物）、
  ridge range、fit model、fitted_decay_s + uncertainty。
  未解決重畳での single_exponential fit はレコード層で拒否。
- `evaluate_modal_decay_qualification` → `ModalDecayQualification`
  （mdtqual-）:
  - fit なし → `insufficient_evidence`（表示は数字ではない）。
  - 未解決重畳/ridge 交叉/分解能不足 → `overlap_unresolved` —
    観測減衰は合成カーブであり単一モード減衰ではない。
  - noise-floor/truncation limited → `noise_or_truncation_limited`。
  - 部分重畳または uncertainty 未宣言 →
    `qualified_with_limitations`。
  - `multi_exponential_declared` fit は #671 結合減衰権威へ引継ぎを
    注記。

## 統合・UI 最小配線

- `_LIFECYCLE_TABLE_LABELS`（application_pages.py）に 16 テーブルの
  JA ラベルを追加。
- `measurement_evidence_display.py` に VERDICT 系 9 関数 +
  状態ラベル辞書を追加: `最適化適格:` / `Pareto近似評価:` /
  `固有モード検証:` / `拡散場評価:` / `統計モデル適用性:` /
  `単一スロープ適性:` / `結合室減衰適格:` / `初期反射対応:` /
  `時周波モーダル減衰:` — すべて fail-closed 状態語彙をそのまま
  表示。
- `native_row_integrity._ROW_BINDINGS` に 16 テーブルのミラー列
  バインディング、`native_authority_audit` に 6 リポジトリの
  `_ReplayProbe` を追加（監査リプレイ対象）。
- `NATIVE_SCHEMA_VERSION = 47`、`_migrate_46_to_47`、
  `test_cad_schema.py` の移行ステップ断言に `(47, ...)` を追加。
- `scripts/issue_verification_manifest.yaml` に 6 issue 分の
  pytest/残件エントリを追加。

## テスト

`backend/tests/test_rev58_validmeth.py`（50 テスト）:
- OPT 系: 単一実行 seed キャップ・大域 claim 縮退・厳密列挙証明・
  フィクスチャ未回収 → unqualified・未ベンチ上限・
  multi-fidelity 上限・Pareto 参照フロント規則・往復+改竄
- MOD 系: 完全ペア検証・周波数単独上限・形状不一致・周波数不一致・
  縮退部分空間・校正混入・減衰不一致・往復+改竄
- DFF 系: assessment 無し・実測フルカバー・理論のみキャップ・
  未カバー特性・非拡散/偏り → not_applicable・往復+改竄
- CPL 系: noise/短レンジ/曲率/平坦各ゲート・交換不能 solver 却下・
  double-slope 適格・単一指数位置限定・証拠なし・往復+改竄
- RPA 系: registration 前提・time-only 曖昧・複合証拠適格・
  校正消費ブロック・未対応保持・往復+改竄
- MDT 系: 孤立モード適格・fit 無し・重畳未解決・ノイズ制限・
  部分重畳・uncertainty 未宣言・transform content-address・
  往復+改竄
- 横断: v47 移行後の 16 テーブル存在・JA 表示行

scoped pytest:
`test_rev58_validmeth.py` 50 本 + `test_cad_schema.py` +
`test_cad_schema_ddl_contract.py` + `test_native_row_integrity.py` +
`test_native_authority_audit.py` +
`test_native_authority_audit_hardening.py` +
`test_authority_audit_coverage.py` + `test_cad_display_labels.py` +
`test_authority_lifecycle_integrity.py` + `test_schema_wire_key.py` +
`test_rev58_ident.py` + `test_rev57_*.py` — 全グリーン。

## 文献根拠

- **最適化適格 (#675)**: Zitzler/Deb/Thiele 2000 (SPEA 比較と
  benchmark suite 規約) / Deb & Jain 2014 (NSGA-II) / ZDT & DTLZ
  テスト群（既知 Pareto フロントを持つフィクスチャ）/
  Auger & Hansen 2005 (CMA-ES の確率的収束と restart) /
  Knowles & Corne 2002 (attainment surface — 参照なしでは
  empirical coverage のみ) / Hansen et al. BBOB 規約（予算・seed・
  独立実行が評価の一次情報）/ surrogate-assisted optimization の
  誤差証拠分離（Jones, Schonlau & Welch 1998 EGO）。
- **固有モード検証 (#674)**: Allemang & Brown 1982 (MAC — 形状一致
  は周波数一致と別観測量) / Ewins *Modal Testing*（縮退モードは
  部分空間として評価）/ modal participation/observability
  （節上の励振・観測は弱証拠、モードの不存在ではない）/
  calibration-consumed データは独立検証にならない（Dwork 的な
  holdout 腐敗の類比）。
- **拡散場適用性 (#673)**: Kuttruff *Room Acoustics*（拡散場条件
  の定義 — エネルギー密度一様・等方・非コヒーレント）/
  Jacobsen 1979 *The Diffuse Sound Field* / Schultz 1971 /
  ISO 3741 Annex A（拡散場適性判定の指針 — RT だけでは拡散は
  推論できない）/ Bradley & Sabine diffuseness 測定系 / SEA
  適用域（Lyon — モード密度と結合条件が前提）。
- **結合室マルチスロープ (#671)**: Eyring 1930 （並列エネルギー
  経路は複数減衰率を生む）/ Cremer & Müller *Principles and
  Applications of Room Acoustics*（結合室の二重スロープ）/
  Hirata 1982（結合室の幾何音響解釈 — 早期=吸収室、後期=残響室）/
  Davis 1925 / Billon et al. 2010（結合体積の低域挙動）—
  単一 T30 は結合シーンの物理を潰す。
- **初期反射対応 (#677)**: Defrance & Polack 2009（ETC ピークと
  幾何経路の対応付けは時間・方向・次数の複合照合）/
  Begault *3-D Sound* / Vorländer *Auralization*（auralization
  の反射帰属検証）/ ISD/RIR の時間位置だけでは最近傍は壁を
  校正しない — 校正消費されたペアは独立検証証拠にならない。
- **時周波モーダル減衰 (#706)**: Gabor 1946 / STFT の時間-周波数
  分解能トレードオフ / Mallat *A Wavelet Tour*（CWT/S-transform
  の分解能意味）/ Schroeder 1965 integrated-impulse（帯域減衰は
  処理帯域で定義される）/ #676 ノイズフロア・切断ゲート（fit
  範囲が真の尾に届かない場合、傾斜は artifact）/ Loutridis 2005
  （wavelet リッジからの減衰推定限界）。waterfall の視覚的減衰は
  変換パラメータの関数であり、宣言なしに「真の低域減衰」に
  昇格しない。

## 残存事項

- 各権威への実データ自動供給: 実探索ランからの `OptimizerRunProfile`
  書き出し、#972 MeasuredModalModel → measured pin、ETC 抽出器 →
  `ObservedReflectionEvent`、waterfall レンダラ → transform 宣言、
  #676 処理レコード → `processing_refs` 自動 pin は未配線
  （宣言インタフェースのみ）。
- fixture 群の実測再現（OPT10-90 / MOD10-80 / DFF10-80 / CPL10-80 /
  RPA10-90 / MDT10-80）は宣言 id のみで自動実行は未実装。
- UI 入力フォーム・推定器実行パイプライン（spatial variance /
  intensity / spatial coherence / マルチスロープフィッタ）の実装は
  残件 — 表示行とテーブルラベルは配線済み。
- Pareto 参照フロントの生成基盤（exhaustive enumeration キャッシュ）
  とベンチコーパス接続は残件。
