# Issue #812 — R160 ハイブリッド合成の検証権威

追加日: 2026-10-07 (REV63)。スキーマ v90。

## 概要

R160 のハイブリッドモデル(クロスオーバー以下は波動ソルバー、以上は
幾何音響)は新しいモデル形式であり、構成要素の寄せ集めではない。
**R130 PASS + R150 PASS は R160 PASS を意味しない** — 合成そのものに
固有の検証/妥当性確認エンベロープが必要であり、それがなければ
ハイブリッド結果は不当な信頼度を継承する。

本モジュール(`cad_hybrid_composition_validation.py`)はそのモデル形式の
検証権威である。評価器 `evaluate_hybrid_validation` は fail-closed で
動作し、証拠不足や未知の状態は決して成功として読めない。

## レコード

| モデル | テーブル | 前置 |
|--------|---------|------|
| `HybridCompositionValidationSpec` | `cad_hybrid_composition_validation_specs` | hvspec- |
| `HybridValidationEvidence` | `cad_hybrid_validation_evidence` | hve- |
| `HybridValidationVerdict` | `cad_hybrid_validation_verdicts` | hvv- |

### HybridCompositionValidationSpec

- `subject_ref` / `wave_qualification_ref` / `ga_qualification_ref` は
  sha256 ピン必須。
- `expected_regions` は R160 ステッチング語彙(`wave_only` /
  `crossover_blend` / `ga_only`)をそのまま使う。
- `crossover_blend` を期待領域に含めるには **版付き
  `crossover_rule`**(rule_id + rule_version + 必要な同意証拠種)と
  **事前登録済み `sensitivity_requirement`**(許容帯域 + 評価対象
  observable)が必須。
- `coherent_complex` 意味論 + ブレンド領域では、ルールが
  `phase_time_compatibility` 証拠を要求していなければならない。
- `ownership_policy` は期待領域 × 5 種の寄与(direct /
  specular_reflection / scattered_late / diffracted / modal_coherent)
  をすべて宣言する完全な方針でなければならない。`unassigned` は
  明示的な宣言であり、評価器はそれを失敗として扱う。
  `complementary_blend` オーナーは `crossover_blend` 内でのみ有効。
- `applicability`(周波数帯域・現象クラス・適用コンテキスト)が
  パス判定の有効範囲を定める。

### HybridValidationEvidence

`evidence_kind` でフィールド契約が決まり、モデル検証が種別ごとの
必須条件を強制する(整形不良の行は決して証拠にならない):

- `component_qualification` — solver_path + achieved_level +
  qualification_ref(要素ソルバー自身の R130/R150 記録)。
- `common_input_binding` — 両ソルバーが消費した入力権威(幾何/
  材料/音源/受音点)をピンする provenance_refs。
- `region_evaluation` — 領域別の評価行(domain_kind)。`gap` 行は
  gap_reason 必須で、`HybridStitchGap` として verdict に写る。
- `crossover_selection_evidence` — rule_id/rule_version の discharge;
  `satisfied` には agreement_kinds と選択帯域 band_hz が必須。
- `phase_time_alignment` — 8 種の位相/時間検査をすべて記録。
  `satisfied` は全検査の satisfied が必須。
- `grid_reconciliation_error` — reconciliation_method 必須;
  `satisfied` は外挿禁止で、補間方式には interpolation_error_bound
  が必須(`exact_bin_identity_v1` は不要)。
- `crossover_sensitivity` — swept_range_hz + sensitivity_points。
- `external_benchmark` — reference_class + case_ref 必須;
  `fitted_calibrated` は別途 holdout_ref が必須(ケースごとに
  クロスオーバーを調整してから一般的な予測有効性を主張するのは
  拒否)。
- `late_field_decomposition` — late_contributions + early_field_state。
- `numerical_verification` — metrics か provenance_refs が必須。

## 評価器

`evaluate_hybrid_validation(spec, evidence, document_id)` は最初に
到達した終端状態を返す(順序が意味を持つ):

1. `components_unqualified` — 要素ソルバーの資格が spec 最低
   レベル未満。
2. `common_inputs_unverified` — 共通入力の束縛証拠なし。
3. `component_inheritance_denied` — 要素合格のみでハイブリッド固有の
   証拠がゼロ(シグネチャ状態)。
4. `region_evaluation_failed` — いずれかの領域評価が violated。
5. `crossover_selection_unsupported` — ブレンド必須なのに版付き
   ルールが許容帯域を見つけられなかった。
6. `double_count_unchecked` — エネルギーを生じた寄与に排他的な
   帰属宣言がない(または矛盾)。
7. `coherent_claim_unvalidated` — coherent 合成なのに位相/時間
   検査が未検証/違反。
8. `early_field_unverified` — 後部場検証が初期反射の不備を覆い
   隠している。
9. `crossover_sensitive` — 許容範囲内でクロスオーバーを動かすと
   予測が実質的に変わる。
10. `insufficient_evidence` — 残る要件未充足(領域未評価、選択
    未証明、グリッド未調整、感度未評価、ギャップ会計なし等)。
11. `validated_bounded` — 外部実測(`measured_external`)または独立
    数値参照(`independent_numerical`)のベンチマークが satisfied。
12. `numerically_verified` — 自己整合/数値検証のみ合格。

verdict には `region_results`(領域別に検査可能)と `gap_domains`
(`HybridStitchGap`、未カバー帯域を明示)を埋め込み、さらに
crossover/ownership/phase/grid/sensitivity/late の各サブ状態を記録
する。

## 証拠クラス

`measured_external` と `independent_numerical` のみが外部ベンチ
マークゲートを満たす。`same_code_fine_reference` は数値検証のみを
支え、`fitted_calibrated` は holdout なしでは受理されず、かつ外部
資格には一切寄与しない。

## 結合点

- スキーマ v90: 3 テーブル + インデックス(`_migrate_89_to_90`)。
- リポジトリ `CadHybridCompositionValidationRepository`(append-only、
  保存時に seal 検証)。
- `native_authority_audit`: `hybrid_composition_validation`
  リポジトリ分岐 + 3 本の _ReplayProbe。
- `native_row_integrity`: 3 テーブルの _ROW_BINDINGS。
- `measurement_evidence_display`: `hybrid_validation_verdict_line` /
  `hybrid_validation_label`(JA)。

## 非目標(issue より)

- 文献から普遍的なクロスオーバー周波数を選ぶこと。
- 回折/散乱の新物理を追加すること。
- 滑らかな見た目の曲線を検証証拠として使うこと。
- 保有ルームのホールドアウト検証を置き換えること。
