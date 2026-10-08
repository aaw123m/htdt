# #950 決定ブリーフ証拠ゲート自動解決

## 概要

`recommended`/`ready` を宣言済みピンのみの設計から脱し、比較候補ごとに
5 つの証拠ゲートを**既存の封印済みプロデューサ評決**から自動解決する。

`backend/src/htdt/cad_decision_brief_evidence.py`
(`DecisionBriefEvidenceResolver`):

- **solver_gate** → `CorrectionQualificationRecord`
  (`correction_qualification` ピン種)。状態ラダー:
  `DEPLOYED_AND_REMEASURED` / `SPATIALLY_HOLDOUT_VERIFIED` /
  `MEASURED_AT_CONTROL_POINTS` → verified(実測)、
  `QUALIFIED_WITH_LIMITATIONS` → conditional(実測)、
  `SIMULATED`/`DESIGN_ONLY`/`INSUFFICIENT_EVIDENCE` → conditional(予測のみ)、
  `INCOMPATIBLE` → failed。注記に補正バンド + 領域内設計位置を含める。
- **channel_verify** → `ChannelVerificationVerdict`
  (`channel_verification_verdict` ピン種)。`verified`→verified、
  `failed`→failed、`ambiguous`/`incomplete`/`unknown`→conditional。
  スコープは plan の `expected_speaker_entity_ids` が候補リビジョンの
  実体に含まれること、かつ変種がそれらのスピーカーを変更していないこと。
- **deployment** → `CalibrationDeployment`
  (`calibration_deployment` ピン種)。スコープは
  `calibration_plan_id` → `CadCalibrationPlan` の
  (scene_revision_id, content_hash, system_variant_id, sha256) で証明。
  プラン読出は保存ペイロードの封印性に依拠し、書込時 invariant
  (`save_plan` の参照権威検証) は読込では再要求しない。
  `deployment_verified`→verified、`attested`/`unverified`→conditional、
  `mismatch`/`superseded`→failed。
  `DeploymentPipelineRecord` はデバイススコープのみのため
  freshness `unknown` フォールバック (baseline のみ、gate を満たさない)。
- **campaign** → `CampaignVerdict` (`campaign_verdict` ピン種)。
  `recommendation_eligible`→verified、`owned_room_insufficient`/
  `not_evaluated`→failed、他→conditional。スコープは
  `campaign_ref` → `CampaignPreregistration` の scene/variant 参照。
- **production_gate** → `ProductionReadinessDecision`
  (`production_readiness_decision` ピン種)。`production_ready`→verified、
  `limited`/`no_go`→failed (no_go)、limited は conditional。

## fail-closed 規約

- `(scene_revision_id, content_hash)` と
  `(system_variant_id, sha256)` の両軸でスコープ一致を再検証:
  別リビジョン/別変種 = 拒否、同 ID 別 sha = stale (満たさない)、
  変種候補に対する変種未束縛レコード = 不適用。
- 宣言済み evidence_refs は信頼せず、各プロデューサ store から
  ref_id で読み出してスコープを再検証。
- 例外的な失敗は missing gate + JA 注記として捕捉 (probes never raise)。
- 古いブリーフの再評価は常に現在の store 状態から行い、
  旧結論の流用はしない。

## 配線

- `kind_resolvers()` が `CadDecisionBriefRepository` 要求の
  `kind → KindResolver` マップを生成し、save 時の
  `ExactAuthorityResolver` が各ピンを実レコードへ解決できる。
- `decision_brief_panel` の再計算ボタンは、ベースライン以外の各候補に
  `resolve_gates` を走らせ、`gates=` + ゲート由来の推奨を渡す。
  全ゲート satisfied + comparable の候補だけが
  `apply_candidate` 推奨 → ready ティアになる。
- 未充足ゲートは JA ラベル付きで `collect_evidence` の why に列挙
  (測定/検証の次アクション・実施者を含む、独自スコアや根拠のない dB/費用
  主張なし)。

## 検証

`backend/tests/test_issue_950_decision_brief_evidence.py`:

- 全 5 ゲート verified+current+comparable → ready + `apply_candidate`
- 欠損/部分証拠/予測のみ/失敗評決 → not_ready
- 別リビジョン/別変種/stale sha/変種のスピーカー変更 → not_ready
- save→read で同一 sha、パネル `recompute_brief()` が同一結果を再現
