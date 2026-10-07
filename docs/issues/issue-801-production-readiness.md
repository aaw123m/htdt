# Issue #801 — 本番推奨適格ゲート権威

追加日: 2026-10-06 (REV63)。スキーマ v91。

## 概要

製品レベルの信頼性ゲート。REV63 で着地した全チェーンを束縛する
単一の採用判定権威:

```
機器権威 → 事前登録キャンペーン(#813) → 校正/ホールドアウト/再現性
→ ソルバー数値適格(#809) → 予測vs実測残差(#810) → 適用範囲(#811)
→ [ハイブリッド検証(#812)、hybrid パスのみ]
→ 採用判定 → 推奨サーフェス有効化
```

ソフトウェア PASS は物理モデル妥当性を名乗らない — この権威は
ソルバーの数値を知らず、着地済み権威の「回答」を束縛するだけ。

## レコード

| モデル | テーブル | 前置 |
|--------|---------|------|
| `ProductionReadinessDecision` | `cad_production_readiness_decisions` | prd- |
| `RecommendationSurfaceDecision` | `cad_recommendation_surface_decisions` | rsd- |

### ProductionReadinessDecision

- 必須: scene/system_variant refs、equipment_authority_refs、
  solver_path_kind + solver_version_ref、outcome_rationale
  (no_go でも必須 — 失敗候補は暗黙フォールバックでなく監査可能な
  NO_GO)。
- `production_ready` は campaign + solver_qualification +
  residual_evaluation + applicability の全ピンを構造要求。
- `limited` は少なくとも campaign + solver_qualification。
- `hybrid` パスは `hybrid_validation_ref` 必須、非 hybrid は
  持ってはいけない。
- stale_authority_refs があれば production_ready を拒否。

### RecommendationSurfaceDecision / evaluate_surface_enablement

- `inspect_prediction` / `compare_candidates` /
  `automatic_recommendation` の3面のみ (#814 と同じ語彙)。
- production_ready → 全 enabled; limited → inspect enabled、
  compare limited、auto disabled; no_go → 全 disabled。
- `automatic_recommendation` の enabled は
  `outcome_at_issue == 'production_ready'` のみ構造許可。
- `verify_surface_decision` が発行済みマップを導出関数に対して
  リプレイ検証 — 偽造/陳腐化した有効化は存続しない。

## 数値・物理ゲートの独立性

数値適格 (#809) と物理検証 (#813 campaign) は別ピンであり、採用
判定は両方を要求する — 片側が欠けると eligible になれない。

テスト: `backend/tests/test_issue_801_production_readiness.py` (17 本)。
