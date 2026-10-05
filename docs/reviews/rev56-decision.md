# REV56-DECISION — 証拠認識型決定ルール + 不確かさ伝播/堅牢設計

スコープ: issues #577 (P0), #604 (P0)
ブランチ: `devin/1791198586-rev56-decision`
スキーマ: native schema v22 → v23（4 テーブル追加）
マージ: merge commit `ee278881`（merge-test 経由で main へセルフマージ済み）

## 実装範囲

### #577 証拠認識型決定ルール

新規 `backend/src/htdt/cad_decision_rule.py`。

- `DecisionRuleSpec`（凍結・`decision-rule:<sha>` 封印）: 決定種別
  （pairwise_candidate_preference / pareto_dominance /
  hard_constraint_conformity / target_tolerance_conformity /
  before_after_improvement / production_recommendation /
  evidence_gate_unlock）、判定基準 `criterion_id`、宣言済み不確かさ
  ソース、実用上同等しきい値、ガードバンド、リスクポリシーを固定。
- `RecommendationEvidenceVerdict`（`decision-verdict:<sha>`）:
  DR10–DR70 判定分類 — `clearly_superior_with_declared_evidence` /
  `superior_with_limitations` / `evidentially_indeterminate` /
  `practically_equivalent` / `insufficient_evidence` / `incomparable` /
  `conflicting_objectives` / `constraint_pass_with_guard_band` /
  `constraint_not_yet_conforming` / `constraint_fail`。
  数値的に小さい目的関数値が自動で推薦になることはない。
- `UncertaintyCompositionManifest`: 二重計上防止マニフェスト。
  各 `UncertaintySourceRef` は出所クラス・包含/除外理由・
  独立/共通モード/宣言相関のスコープ・相関グループを持つ。
  `pairwise_resolution()` は共通モードを相殺、相関グループ内は
  線形加算→グループ間 RSS、独立源は RSS。全源が除外/空なら
  `None` → `insufficient_evidence`（fail-closed）。
- ガードバンド（ILAC-G8 流儀）: 規格限界と受入限界を分離
  （`w = TL − AL`）。限界近傍の適合は `pass_with_guard_band` /
  `not_yet_conforming` で報告。逆転ガードバンドはバリデータ拒否。
- 実用上同等: `PracticalEquivalence` はしきい値+根拠を必須化。
  普遍的 JND は捏造しない（根拠なし宣言は拒否）。
- 評価器: `evaluate_pairwise_preference` /
  `evaluate_limit_conformity` / `evaluate_pareto_dominance`。
  名目的支配のみの場合 `nominal_dominance_only_not_robust` として
  リーダーは指名するが `superior_with_limitations` に留め、
  推薦へは昇格しない。`EvidenceRequest` で abstention を解く
  ための証拠要求（value-of-information）を記録。

### #604 不確かさ伝播 / 堅牢設計

新規 `backend/src/htdt/cad_uncertainty_propagation.py`。

- `UncertainInputSet`（`uncertain-input-set:<sha>` 封印）:
  ~16 種の入力分類（材料係数・位置/幾何・測定値・モデル
  パラメータ・登記・…）と不確かさクラス（aleatory / epistemic /
  mixed / unknown）を分離。
- 表現は黙って変換しない: `bounded_interval` /
  `empirical_samples` / `discrete_scenarios` / `distribution` /
  `credible_interval` / `pbox` / `correlated_samples` をそれぞれ
  検証 — 公差を捏造ガウスへ変換しない。相関は `correlation_group_id`
  と合同行で明示。
- `PropagationSpec`（`propagation-spec:<sha>`）: 方式
  （`deterministic_corner_pairs` OAT 1+2n /
  `deterministic_low_discrepancy` 内容ハッシュ由来の決定論的
  座標 / `explicit_states`）、標本数、`MAX_PROPAGATION_SAMPLES=256`
  を固定。小さい掃引を「収束」とは呼ばない。
- `PropagatedState` は候補間で共有の状態インデックスを持ち
  paired 比較が可能 — `PairwiseRobustOutcome.paired_reversal_fraction`
  で順位逆転を実測（paired 評価で実際に逆転が検出されることを
  テストで実証）。
- `SensitivityStudy`（`sensitivity-study:<sha>`）: 方式を正直に
  命名（`deterministic_oat_range` / `deterministic_sample_correlation`）
  — Sobol を名乗らない。
- `RobustDesignAssessment`（`robust-design:<sha>`）: 候補ごとに
  nominal / worst / expected / failure probability / variance /
  regret の多次元指標 + `robust_alternative`。順位が指標間で割れる
  とき `no_robust_winner` / `indeterminate` を発し、#577 の
  verdict に合成する（隠れた単一スコアで潰さない）。

### 永続化・監査・UI

- `CadDecisionRuleRepository`（`cad_decision_rule_specs` +
  `cad_decision_verdicts`）と `CadRobustDesignRepository`
  （`cad_uncertain_input_sets` + `cad_robust_design_assessments`）。
  同一 id + 同一 payload は no-op、衝突は ConflictError。
  読み出し時に行↔payload を再検証。
- schema v23（v22 は REV56-MEASEV が使用中だったため renumber）。
  `_ROW_BINDINGS`・`_ReplayProbe`・`_LIFECYCLE_TABLE_LABELS` 登録済み。
- UI: `refresh_pareto_comparison` が O90 堅牢性エンベロープから
  pairwise verdict を導出（`bounded_linear_sum`、相関未宣言のため
  保守的。材料・測定・モデル不確かさを折り込まないことは
  `declared_limitations` に明示）。`CadDecisionRuleRepository` 経由で
  永続化（内容ハッシュで冪等 — 再描画で行は増殖しない）。
  `decision_verdict_label` に判定+理由を表示し、全早期 return 経路で
  「証拠判定は利用できません」を設定。

## 文献根拠

- ILAC-G8:2019 — ガードバンド・判定ルール（限界分離、
  shared-risk 判定）。
- 確率的/最悪ケース Pareto 支配 — 不確かさ下の支配は真偽値でなく
  4 状態 verdict。
- Thydal et al. 2021, Applied Acoustics 178:107939 — 室内音響材料
  物性の p-box/区間 UQ。`pbox`/`bounded_interval` 表現と
  aleatory/epistemic 分離の根拠。
- Pilch 2020, Applied Acoustics 170:107495 — 幾何/配置入力の
  不確かさ。`placement` 種別と配置公差合成テストの根拠。
- Abstention 決定理論 — `insufficient_evidence` /
  `evidentially_indeterminate` / `incomparable` を第一級 verdict とし、
  `EvidenceRequest` が解消に必要な証拠を記録（VoI）。
- ISO 3382-1 JND 文脈 — 実用上同等しきい値は基準ごとに根拠付きで
  宣言が必須。普遍 JND の捏造はしない。

## テスト

- `backend/tests/test_rev56_decision.py` 49 件: マニフェスト合成
  （二重計上拒否・共通モード相殺・相関グループ）、全 DR verdict 経路
  （paired 採点での順位逆転検出を含む）、ガードバンド、支配分類、
  封印+改竄拒否、リポジトリ往復+衝突、伝播プラン（corner /
  seeded low-discrepancy / explicit）、相関合同行、感度支配、
  加重期待値、キャンセル、アセスメント永続化。
- schema マイグレーション記録テストに v23 を追加。
- マージ後ツリーでスコープ回帰 215 件グリーン
  （REV56-MEASEV 新規テスト含む）。

## 残存事項

- UI 由来の pairwise verdict は O90 エンベロープ半幅のみを不確かさ
  源とする（材料・測定・モデル形状不確かさは `declared_limitations`
  記録済み）。REV56-MEASEV の測定不確かさ予算（`mub:`）を
  `UncertaintySourceRef` へ接続する統合は後続作業。
- `evaluate_pareto_dominance` は宣言済み区間証拠に基づく決定論的
  判定。確率的支配（優越確率の数値化）は RobustDesignAssessment の
  `paired_reversal_fraction` 側で提供するが、多目的への一般化は未実装。
- `SensitivityStudy` は OAT レンジ/標本相関の 2 方式。Morris 法・
  分散分解（Sobol）の実装は残件（命名規約は予約済み）。
- `PropagationSpec` は決定論的サンプラーのみ。真の MC 実行は
  既存 GUM-S1 的割当（#572 monte_carlo 合成）との接続余地あり。
- surrogate/model-form 不確かさは入力分類に存在するが、
  サロゲート誤差の自動折込はまだ — 宣言されれば伝播に含まれる。
- issue #577/#604 への実装範囲+残件コメントは本 PR と同時に投稿済み。
