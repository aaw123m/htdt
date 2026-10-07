# Issue #813 — 所有ルーム ホールドアウトキャンペーン権威

追加日: 2026-10-06 (REV63)。スキーマ v90。

## 概要

「HTDT は既に見た測定にフィットできるか」ではなく、「外部ソルバー適格と
事前登録条件での校正の後、未見の物理条件と候補の方向/順位を、測定/
再現性フロアを超えて正しく予測できるか」を問う最小フィールド
キャンペーンの権威レイヤ。ASME V&V-20 の外挿境界ロジック。

## レコード

| モデル | テーブル | 前置 |
|--------|---------|------|
| `CampaignPreregistration` | `cad_campaign_preregistrations` | crc- |
| `CampaignMeasurement` | `cad_campaign_measurements` | crm- |
| `CampaignVerdict` | `cad_campaign_verdicts` | crv- |

### CampaignPreregistration

ホールドアウト取得**前**にハッシュ束縛するプロトコル凍結:
protocol/version、scene/SystemVariant/solver/adapter/入力権威
(全て sha ピン)、校正条件集合と holdout 条件集合 (**構造的に
disjoint**)、holdout 次元(受音位置/座席/構成/候補配置/音源/
独立セッション)、測定位置、候補 ID、メトリクス、不確かさ手法、
判定閾値、許容除外、停止条件、環境要求、spatial claim。
- `supersedes_campaign_ref` + `leakage_reason` は対必須 — holdout
  が校正に漏れたら同一 identity を名乗らず新キャンペーンへ。

### CampaignMeasurement

役割宣言 (`calibration`/`holdout`/`repeatability`/`perturbation`/
`screening`) + 条件 + 位置 + 生アセット sha256 + 測定権威チェーン
(#813 §7): マイク校正、校正ファイル、SPL 校正 (`claims_absolute_
level` なら必須)、サンプルレート/IF、ルーティング、AVR/DSP 状態、
位置/向き権威、不確かさ予算、品質レポート。

### CampaignVerdict

- 主張ファミリ別の個別 verdict (`absolute_response`/
  `shape_after_normalization`/`modal_peak_location`/
  `arrival_timing` と `candidate_direction`/`candidate_ranking`/
  `pareto_trend`/`robustness_tolerance`) — 絶対応答と決定予測は
  別判定。`pass` は証拠 ref 必須。
- 昇格ゲート 9 本: external_benchmark, numerical_convergence,
  input_qualification, measurement_uncertainty, holdout_residual,
  repeatability, candidate_separation, applicability, robustness。
- `recommendation_eligible` は全ゲートピン + stale ref ゼロを
  構造的に要求。trend/domain/eligible は `holdout_residual_ref` 必須。

## 評価器 `evaluate_campaign_promotion`

構造違反を検出して判定を floor する fail-closed ラダー:

```
not_evaluated → external_only (holdout ゼロ)
→ owned_room_insufficient (prereg 欠落 / preregが最古holdout取得以後
   / holdout↔calibration 漏洩 / repeatability・uncertainty・
   separation 未ピン / stale ref 存在 / 決定系主張 pass なし)
→ owned_room_absolute_prediction_limited (決定 pass・絶対 fail)
→ owned_room_trend_validated (両方 pass・コアゲート未完)
→ owned_room_domain_validated (コアゲート完・非 eligible)
→ recommendation_eligible (verdict 自身が eligible 宣言 = 全ゲート)
```

誠実な失敗: `StopReason` (model_invalid / authority_insufficient /
repeatability_floor_too_high / unresolvable / not_generalizable /
unstable / additional_evidence_required)。推奨は強制しない。

テスト: `backend/tests/test_issue_813_owned_room_campaign.py` (19 本)。
