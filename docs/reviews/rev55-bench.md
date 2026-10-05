# REV55-BENCH — 定量検証ベンチマーク / 材料証拠互換ゲート / 指標適用性ゲート

Issue: #566（定量検証ベンチマーク）、#570（材料証拠互換ゲート）、#571（小室指標適用性ゲート）
スコープ: ソフトウェア基盤のみ。実機測定 corpus の実データ収集は #1/#564 実機待ち（スロットのみ実装）。

## 実装モジュール

| モジュール | Issue | 役割 |
|---|---|---|
| `backend/src/htdt/acoustic_validation_envelope.py` | #566 | fixture taxonomy A–D、閾値ポリシー、精度封域レコード、ベンチマークレポート |
| `backend/src/htdt/cad_material_evidence_compatibility.py` | #570 | 測定手法/物理量/入射/位相のタクソノミー、互換性マトリクス、fail-closed 判定 |
| `backend/src/htdt/acoustic_metric_applicability.py` | #571 | メトリクス適用性ゲート、Schroeder/モード重なり、空間減衰場、最適化ガード |

既存権威との統合（並列真理 store 禁止）:

- `acoustics.py` のモード/反射式を fixture 期待値の基礎として共有
- `cad_material_library` (#771) `MaterialAcousticEvidence` → `boundary_evidence_from_material_acoustic` アダプタ
- `cad_surface_scattering` (#1032) `SurfaceScatteringEvidence` + `ga_scatter_fraction` → `evaluate_surface_scattering_for_ga`（ISO 17497-2 拒否を明示判定化）
- `cad_late_decay_estimate` の Sabine 推定レーンと同じ `T60 = 55.3V/(cA)` を参照式として使用

## Fixture 一覧（#566 §10）

| ID | クラス | 内容 | 期待値の導出 |
|---|---|---|---|
| VAL10 | A 解析 | 自由音場直達音（到達時刻 d/c、−6.02 dB/距離倍加）| `d/c`、`20·log10(r2/r1)` をコード内導出 |
| VAL20 | A 解析 | 6×4×3 m 剛性矩形室モード周波数（軸/接線/斜交）| `f = c/2·√((nx/Lx)²+(ny/Ly)²+(nz/Lz)²)` |
| VAL30 | A 解析 | x=0 壁一次反射（鏡像源法: 経路長・到達時刻・レベル落）| 鏡像源経路 + 飛行時間 |
| VAL50 | C ハイブリッド | wave↔geometric 重複帯域 150–300 Hz（一貫性・遷移不連続・正規化・時刻連続）| `evaluate_hybrid_boundary` で判定 |
| VAL60 | D 実測 | 自室ホールドアウトスロット（EMPTY/UNKNOWN、実測未バインド）| 実機待ち — 推測値なし |

VAL40（ソルバー収束）は fixture ではなく `ConvergenceStatus`（収束検証済み軸を明示）として精度封域に保持。

## 判定基準

### #566 — 閾値ポリシー

- `ObservableThresholdRule` は rationale 必須（8 文字以上）。閾値 `None` = 根拠なき合格線なし → 分布のみ報告し `insufficient`（強制 PASS/FAIL 禁止）。
- `ANALYTIC_FIXTURE_POLICY_V1`: 解析 fixture には 1e-9（float64 丸めのみ監視。物理的主張ではない）を scope=`synthetic_fixture` に限定。
- 全体判定は部分の正直な集約: `fail` 一件で全体 `fail`、`insufficient` 一件で `insufficient`。
- `AccuracyEnvelopeRecord`: solver+version × observable × 帯域 × 幾何 domain × 境界仮定 × 送受信能力 × fixture 集合 × 誤差分布 × 除外 × 状態 × hash を封印。`covers()` は version/observable/band が違えば False（§8: 他の帯域・版・observable をカバーしない）。**グローバル精度スコアは存在しない**。
- 状態: `VALIDATED_FOR_DECLARED_DOMAIN` / `VALIDATED_WITH_LIMITATIONS` / `EXPERIMENTAL` / `INSUFFICIENT_EVIDENCE` / `NOT_APPLICABLE`。
- B クラス fixture: `reference_kind='self_comparison_declared_limitation'` は `reference_independence='same_algorithm_declared_limitation'` + 独立性限界注記を必須化。
- D クラス: `MeasuredCorpusSlot` は `empty_unknown` で測定値・証拠 hash を禁止、`populated` では 6 種 provenance（geometry/equipment/calibration/registration/processing/environment）+ 証拠 sha256 を全て要求。

### #570 — 互換性マトリクス（`COMPATIBILITY_RULES_V1`, version 1）

- 手法が産出しうる物理量を `METHOD_PRODUCIBLE_QUANTITIES` で固定（ISO 354 → diffuse α / 等価吸収面積のみ、ISO 10534-2 → 垂直入射 α / インピーダンス / アドミタンス / 多孔質パラメータ、ISO 17497-1 → 散乱係数、ISO 17497-2 → 指向拡散係数）。不整合は構築時に拒否。
- 消費者: `wave_complex_boundary` / `wave_local_reaction_boundary` / `geometric_arbitrary_incidence` / `geometric_scatter_fraction` / `statistical_energy_model` / `hybrid_lf_wave_boundary`。
- 判定: `DIRECTLY_COMPATIBLE` / `COMPATIBLE_WITH_DECLARED_ASSUMPTIONS` / `DERIVED_WITH_UNCERTAINTY` / `INCOMPATIBLE` / `INSUFFICIENT_EVIDENCE` + 理由コード。
- 主要ガード:
  - ISO 17497-2 directional diffusion → `geometric_scatter_fraction` は常に `INCOMPATIBLE`（`DIFFUSION_COEFFICIENT_NOT_SCATTERING_PARAMETER`）。
  - エネルギー係数（magnitude_energy_only）→ 複素位相が必要な波動境界は `INCOMPATIBLE`（`ENERGY_COEFFICIENT_HAS_NO_PHASE_AUTHORITY`）。
  - 垂直入射データ → 任意入射幾何境界は変換 artifact なしで `INCOMPATIBLE`（`NORMAL_INCIDENCE_DATA_USED_FOR_ARBITRARY_ANGLE_BOUNDARY`）。
  - 拡散場データ → 特定入射角の波動境界は変換 artifact なしで `INCOMPATIBLE`。
  - 共振型材料（`is_resonant`）+ ISO 354 → 波動/幾何消費者は `INCOMPATIBLE`（ISO 354 の弱減衰共振器除外条項）。
  - 単一数値格付（αw/NRC 等）はいかなる帯域入力にも `INCOMPATIBLE`。
  - build-up identity（製品/厚さ/密度/空隙/背裏/貫通率等）は片側不明でも不一致扱い（`BUILD_UP_MISMATCH`）。
- `BoundaryConversionArtifact`: 変換は不変 artifact（元証拠 id+hash、モデル+version、仮定、不確かさ寄与、適用帯域）として明示。導出データは `DERIVED_WITH_UNCERTAINTY` が上限で、測定権威へは再格上げしない。
- `manufacturer_declared`/`database_reference`/`inverse_estimated` は直接互換へ格上げ不可（`DECLARED_METHOD_NOT_MEASURED`）。

### #571 — 指標適用性

- `RoomMetricContext`（容積/表面積/寸法/T60 推定/形状）未宣言 → `not_applicable`（fail-closed）。
- `schroeder_frequency_hz = 2000·√(T60/V)`（代表式）。`modal_overlap_index = N(f)·Δf`（`N=4πVf²/c³+πSf/2c²+L/8c`、`Δf=2.2/T60`）。
- 帯域 < f_s（or overlap < 3）→ `transition_region`（限定適用）、明確に下（< f_s/2 かつ overlap < 1）→ `modal_non_diffuse` で不適用 + `MODAL_AUTHORITY_REQUIRED`（`required_evidence='modal_decay_record'`）。
- `DecayMetricProfile`: メトリクス（edt/t20/t30）+ ISO 3382-2 revision + 帯域 + 測定点群 + 手法 + fit 区間（EDT: 0→−10 dB、T20: −5→−25 dB、T30: −5→−35 dB を強制）+ 動的レンジ + SNR + 空間集約を封印。動的レンジ不足（EDT<15 / T20<30 / T30<40 dB）→ `insufficient_decay_range`、SNR<10 dB → `insufficient_snr`。
- `SpatialDecayRecord`: 位置ごとの減衰を集約前に保持（Prinn 2025 — 低域減衰はスカラではなく場）。`aggregation='none'` は集約スカラを禁止、`mean`/`median` は集約値が宣言手法と一致することを検証（なりすまし集約を拒否）。空間変動係数 > 15% で `SPATIAL_VARIANCE_HIGH` → 単一スカラ強制を禁止。
- `ModalDecayRecord`: 個別モード減衰（周波数・減衰時間・減衰率・モード指数・抽出法・信頼度）。`rt60`/`t60` フィールドを持たず、RT60 への再ラベルは型として不可能。
- `MetricComparisonEligibility`: メトリクス不一致・ドメイン不一致（片側 modal 等）→ `INCOMPARABLE`（数値比較を禁じる）。手法/プロファイル/位置集合の差 → `COMPARABLE_WITH_LIMITATIONS`。
- `OptimizationMetricEligibility`: 不適用指標を最適化目的にしない。モーダル領域では `modal_decay_per_mode`、位置依存では `spatial_rt_field` を代替目的として提示。

## 文献根拠

| 項目 | 出典 |
|---|---|
| Schroeder f_s = 2000·√(T60/V)、モード重なり ~3 | Schroeder 1962；Fazenda et al. "The Schroeder Frequency Revisited" |
| モード密度 N(f) = 4πVf²/c³ + πSf/(2c²) + L/(8c) | Morse & Bolt 室音響理論 |
| モード帯域幅 Δf ≈ 2.2/T60 | 同上（3 dB 帯域幅の慣用値）|
| T20/T30/EDT fit 区間・精度クラス・減衰レンジ要件 | ISO 3382-2:2008（survey/engineering/precision、T20 優先、Annex A 不確かさ）|
| RT の JND ≈ 5 %（人の可聴差 — ゲートではなく sanity floor）| ISO 3382-1 §A.3（Seraphim 1958 系）|
| ISO 10534-2 垂直入射 α と ISO 354 拡散 α は「not comparable」、Annex E は locally reacting のみ | ISO 10534-2:2023 本文/Annex E |
| ISO 354 は弱減衰共振器（ヘルムホルツ/膜/板）を適用範囲外 | ISO 354:2003 |
| ISO 17497-2 拡散係数は「幾何音響モデルの拡散アルゴリズム入力として直接使用不可」| ISO 17497-2:2012 本文 |
| 低域減衰は位置依存の場（単一スカラ強制不可）| Prinn 2025（issue #571 引用）|
| 境界条件表現は主要モデル誤差源（変換は証拠を伴う操作）| Thydal 2021 / Li 2022 / Fratoni & D'Orazio 2025 / Brinkmann 2019（round robin）|

## 残存事項

- **VAL60 実測データ**: #1/#564 の実機測定・登録証拠が入り次第、corpus slot を `populated` に移行（calibration/holdout 分離を ingest 側で維持）。
- **VAL40 相当の収束 fixture 群**: 現状は `ConvergenceStatus` + `convergence_axes_tested` として封域内に保持。グリッド/次数 sweep の fixture 化は後続。
- **閾値ポリシーの拡充**: `real_room`/`declared_domain` scope の閾値は実測誤差分布が蓄積されてから版付きで追加（現状は合成 fixture のみ）。
- **CI ゲート配線**: `evaluate_fixture_observations`/`evaluate_hybrid_boundary` は即時利用可能だが、実 solver adapter からの呼び出し配線は別 issue の実装側で行う（本変更はライブラリ基盤のみ）。
- **変換モデル実装**: `BoundaryConversionArtifact` は容器のみ。Annex E 型の垂直→拡散推定や境界モデル fitting の実装は後続（artifact にモデル/版/残差を記録する前提）。
- **Prinn 2025 活用の深堀り**: 空間場記録の枠組みは実装済み。最適化への場ベース目的関数の実配線は別トラック。
