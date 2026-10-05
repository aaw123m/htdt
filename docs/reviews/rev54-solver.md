# REV54-SOLVER — solver/数値/最適化系の深掘りレビュー

Scope: solver adapter/executor 数値面、SolverCapabilityManifest emit 正当性、
最適化バッチの計算負荷、geometry/solver ブリッジ、measurement→solver
データフロー、`run_solver`/`predict` の決定性とキャッシュ正当性。
検証はすべて実コード + 小ベンチ実測で行い、確認した 2 系統の実性能欠陥を
回帰テスト付きで修正した。

共通規約ファイル `C:\Users\Administrator\prompts\rev54\common.md` はこの
VM には存在しなかったため、親セッション履歴と memory 規約から同一内容を
再構成して `C:\Users\Administrator\prompts\rev54\common.md` に配置し、
それに従った (merge-test:main・scoped pytest・fail-closed 原則)。

## 修正した実性能欠陥

### 1. `pareto_front`: O(n²) 支配スイープ内で canonical SHA-256 を再計算

`pareto.py` の支配判定 `_dominates_metrics` はメトリクスごとに
`metric.definition_id` を比較していたが、これは内部で
`effective_definition()` → `ObjectiveDefinition.legacy()` (pydantic モデル
生成) → `semantic_hash` (canonical JSON dump + SHA-256) を呼ぶ。
つまり O(n²·k) 回のフルハッシュ再計算が支配スイープ内で走っていた。
`_validate_comparison_authority` が既に全候補間の definition_id 一致を
n·k で検証済みのため、ペア内チェックは完全に冗長だった。

修正: 支配スイープ前に候補ごと `(objective_id, definition_id, value,
direction)` を 1 回だけ解決 (`_metric_comparison_key`) し、ループは
タプル比較のみ (`_dominates_keys`)。`_dominates_metrics` は同じキーを
介する互換ラッパーとして残し、外部から見た挙動・エラーは不変。
`joint_pareto_front` / `robust_pareto_front` / `repository.pareto_front`
は全て `pareto_front` 経由のため自動的に効く。

計測 (N=120 候補 × k=2 目的):
- definition 付きメトリクス: 0.46s → 0.02s (約 23×)
- legacy 最小化メトリクス: 0.57s → 0.02s (約 28×)

### 2. `evaluate_constraint_set`: 候補列挙ループ内で spec/部屋ジオメトリを毎回再構築

`generate_search_space` は raw Cartesian 積 (最大 50k) の各候補で
`evaluate_constraint_set(context, spec, request)` を呼び、内部で毎回
`StoredConstraintSetSpec.model_validate`・`_room_polygon_and_edges`
(shapely polygon + 全壁エッジ LineString 再構築)・`_entity_baselines`・
profiles/region geometry を再計算していた — これらはコンテキスト +
固定 spec だけに依存し候補位置に依存しない。

修正: `placement_constraints.py` に 2 段の prepare API を追加:

```
prepare_constraint_context(context_payload)   # room polygon/edges/baselines
prepare_constraint_spec(prepared_context, raw_spec)  # spec validate + profiles
                                                 # + region geometry
prepare_constraint_evaluation(ctx, spec)       # 上 2 つの合成
evaluate_prepared_constraint_set(prepared, request)  # 候補ごとの評価のみ
evaluate_constraint_set(ctx, spec, request)    # = prepare + evaluate (互換)
```

接続箇所:
- `search_space.py` `generate_search_space`: ループ前に 1 回 prepare。
- `cad_topology_search.py` `_final_pose_constraint_rejections`: 呼出し側で
  `prepare_constraint_context` + `CadConstraintSet.model_validate` を
  base×orientation スイープ外に hoist。per-candidate の spec (final pose の
  entity_profiles は候補ごとに変わる) は `prepare_constraint_spec` で
  部屋ジオメトリを再利用しつつ個別に検証する構成に変更。

計測 (矩形部屋 5×4×2.8, seat + 2 speakers, wall_clearance/pair_distance/
entity_collision, 3 軸グリッド → raw 3087 候補, feasible 2107):
- `generate_search_space`: 2.26s → 0.86s (−62%)
- 内訳 before: `_room_polygon_and_edges` 1.13s (50%) + spec validate ~0.1s
  が全候補で再実行されていた。
- topology 経路も同じ hoist 構造 (context/constraint_set のスイープ外化)。

回帰テスト:
- `test_prepared_evaluation_is_identical_to_single_shot_per_candidate`
  (16 候補で prepared ≡ single-shot の全 dict 等価)
- `test_prepare_constraint_spec_shares_context_across_specs`
  (context 共有 + geometry version 不一致の fail-closed)
- `test_pareto_front_matches_reference_dominance_over_larger_population`
  (60 候補で naive 参照支配スキャンと front/dominator が完全一致)

## 検証済みクリーン面 (欠陥なし)

- **solver capability/provenance**: `_persist_capability_manifest`
  (r130a/polyhedral 両 executor) は result commit 時に persisted descriptor
  を semantic_sha256 で突き合わせ、不一致なら raise (fail-closed)。
  `produced_observables` には envelope の実産出 artifact が渡り、宣言済み
  非産出 observable は derive 内部で証拠剥奪 → UNSUPPORTED 行へ正直降格。
  manifest validator は全 phenomenon の 1 回宣言・band ⊆ solver domain・
  ハッシュ束縛を強制。overclaim/underclaim 経路は見当たらず。
- **PFFDTD adapter 数値**: `recombine_pffdtd_receiver_traces` は shape/
  finite を検証、`pffdtd_velocity_potential_to_pressure_trace` は有限記録
  端点を含む 2 次微分で physical pressure を構成 (truncated spectrum の
  -iωρ 乗算を回避する設計意図を docstring で明示)、
  `finite_record_pressure_transfer` は dt 重み付き直接変換 + source
  spectrum の eps floor でゼロ割 fail-closed。NaN/Inf は各所で raise。
- **GA adapter**: `_directivity_contribution` は非有限 energy で None
  返却、`spreading * directivity.energy_factor` は isfinite 検査済み、
  portal-crossing path の帯域 energy 合成も有限検査で駆動。
- **measurement→solver (IR import)**: `parse_rew_impulse_response` は
  非一様サンプリングを resample せず拒否 (時間刻みの tolerance は
  timestamp 桁精度から導出)、振幅のみ列は sample_rate 宣言必須、
  declared length/peak の不一致は warning 化。サンプリング定理に絡む
  spatial Nyquist 制約は `cad_measurement_ir.py:299` に注記済み。
- **`run_solver`/`predict`**: `build_prediction_execution_preflight` は
  cache entry の task_id/semantic_sha256/execution_input_sha256 を全て
  突き合わせて EXACT_CACHE_HIT を判定し、scene/config 差異は
  STALE_* → REJECT + full rerun (サイレント再利用なし)。UNKNOWN リソース
  推定は 0 に化けず、ETA を invent しない契約を維持。
- **geometry/solver ブリッジ**: `room_geometry_payload` は declared
  vertices から決定論的に構築、`AcousticGeometryDerivation` は hop 間を
  id+hash で束縛し mechanism のない category (merged_faces/edge
  diffraction/scattering substitution) は NOT_APPLIED 強制で
  overclaim 不可。
- **`generate_cad_candidates` ページング**: `scene_repository.get` +
  `require_search_spec_authority` はページごとに再実行されるが実測
  <1ms/回 (small fixture) であり、cache-miss 時の authority replay と併せ
  重複はあるが性能問題とは言えず — 変更せず記録のみ。
- **決定性**: 候補列挙・評価は全て決定論的 (乱数使用なし)。stochastic
  経路 (R100A) は明示 seed 束縛で replay 証明付き。

## 残存事項

- `generate_search_space` の候補ごと残コスト (~0.86s @ 3087) は
  shapely Point/envelope の実幾何評価であり、これ以上の hoist には
  評価セマンティクス変更が必要 (observations/rejections の構築を
  search 用途で skip する等)。API 面を増やすほどの価値はないと判断し保留。
- `pareto_front` 自体は依然 O(n²·k) の全ペア比較。n が数千規模になる
  ワークロードでは non-dominated sort / index 化が有効だが、現行
  ドメインの候補数 (search candidate_limit ≤ 50k でも pareto 入力は
  feasible 評価済みの実サブセット) では今回の hoist で十分。
- `_final_pose_constraint_rejections` の per-candidate `preview`
  SceneDocument 再構築 (`_candidate_document_from_parts`) は orientation
  ごとに必要な作業で hoist 不可; 大きな orientation grid では次の
  プロファイル対象。
- `OptimizationMetrics.definition_id`/`semantic_hash` はプロパティ
  アクセスごとに canonical SHA-256 を再計算する構造 (frozen model で
  cached_property 化は hash 系の同値性確認が要)。pareto では解決済みだが
  他の N² 呼出し点が現れた場合は先にこのプロパティを疑うこと。
