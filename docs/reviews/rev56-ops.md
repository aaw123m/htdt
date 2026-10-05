# REV56-OPS — ネットワークAV セキュリティ権威 + 制御シナリオ修飾 + セーフリスニング

スコープ: issues #598 (P1), #601 (P1), #602 (P1)
ブランチ: `devin/<ts>-rev56-ops`
スキーマ: native schema v32 → v33（18 テーブル + 18 インデックス追加;
v32 は REV56-BUILDING が確保 — マージ時に再採番済み）

## 実装範囲

### #598 Networked AV security authority

新規 `backend/src/htdt/cad_security_authority.py` +
`cad_security_authority_repository.py`（テーブル
`cad_security_assets` / `cad_security_credentials` /
`cad_security_management_surfaces` / `cad_security_observations` /
`cad_security_risks` / `cad_security_remote_authorizations` /
`cad_security_test_evidence` / `cad_security_access_reviews` /
`cad_security_reviews`）。

- `SecurityAssetDeclaration`（secasset- 封印）— ネットワークAV 機器
  1台の宣言: `management_reachability`（device_local /
  management_vlan / lan / wan_internet / vendor_cloud / mixed /
  unknown）、`lifecycle_state`（active / quarantined /
  removed_pending_credential_revocation / decommissioned /
  unknown）、ベンダーサポート状態、バックアップ redaction 状態。
  `decommissioned` は credential 無効化の証跡経由でのみ到達可能とし
  直接宣言を拒否 — 廃棄済みclaimは構造的に作れない。
- `CredentialRecord`（seccred- 封印）— アカウントのメタデータのみ。
  種別（named_account / shared_account / service_account /
  vendor_support_account / api_token / certificate / unknown）、
  能力の最小権限分類（view_telemetry 〜 security_admin）、
  スコープ、`storage_reference`（秘密の保管先クラス — 秘密そのものを
  保持するフィールドは構造的に存在しない）、MFA 状態、
  `default_credential_state`（default_changed /
  default_still_active / never_default / unknown）、
  `former_holder`（退任スタッフ/インテグレータの旧保有者 — ハンド
  オフ・プロバイダ変更時レビュー対象）。
  `account_ref` は URI 埋込認証・private key/password=/secret=/
  bearer 文字列を拒否。revoked/expired + former_holder +
  default_still_active の矛盾claimを拒否。
- `ManagementSurfaceDeclaration`（secsurf- 封印）— web_ui /
  ssh_shell / snmp_telemetry / vendor_api / mobile_cloud_app /
  remote_desktop / support_tunnel / serial_control /
  mdns_discovery / other_network_service 各面について
  requirement（プロジェクトが必要とするか）× state（有効か）×
  exposure_scope × authentication_state。未使用の有効面は risk
  candidate — HTDT 自身は絶対に機器面を無効化しない。
- `SecurityObservation`（secobs- 封印）— credential_state /
  surface_state / firmware_state / service_inventory /
  certificate_state / backup_sensitivity / access_state /
  manual_review / other の観測を evidence_class（device_readback /
  configuration_audit / vendor_documentation / authorized_scan /
  user_recorded / inferred / unknown）付きで記録。inferred/unknown
  証拠での「安全」claimは評価で verified に寄与しない。
- `SecurityRiskRecord`（secrisk- 封印）— リスク台帳。impact_domains
  （availability / confidentiality / integrity / safety / privacy /
  operational）、likelihood_class、status（open / mitigated /
  accepted / transferred / unknown）。`accepted` には risk_owner +
  review_at_utc が必須（受理は決定であって既定値ではない）、
  `mitigated` には適用した mitigation_plan_note が必須。
  `functional_av_effect` でセキュリティ対策のAV機能副作用を区別
  （security-vs-availability トレードオフを隠さない）。
  `source_profile_ref` で #599 標準プロファイル（例:
  avixa-rp-c303-01@2018）を pin 可能。
- `RemoteServiceAuthorization`（secrem- 封印）— リモートサポート/
  監視の明示的認可境界。method（vendor_cloud / vpn /
  support_tunnel / remote_desktop / on_premises_only / none /
  unknown）、`consent_ref` 必須（文書化された同意なしに遠隔アクセス
  は authorizable でない）、allowed_capabilities 集合、
  session_logging、有効期間。リモート監視は無制限の機器制御を意味
  しない — 許容能力集合そのものがレコード。
- `SecurityTestEvidence`（sectest- 封印）— 外部レビュー/
  configuration_audit / known_service_inventory /
  vulnerability_scan_report / penetration_test_summary 等の証拠。
  `authorization_ref` 必須 — オーナー認可なしのテスト記録は証拠と
  して受理しない。
- `AccessReviewRecord` + `AccessReviewOutcomeEntry`（secacc- 封印）—
  trigger（handoff / provider_change / decommission / periodic /
  incident / other）駆動の権限棚卸。outcomes は credential_id 一意。
  `open_item_refs` 非空 = 棚卸未完了。
- `SecurityReview`（secrev- 封印）— `evaluate_security_review` のみ
  が生成する封印評価結果。8チェック（inventory / credentials /
  surfaces / firmware / remote_access / backup_sensitivity /
  access_review / decommission）、各 verified / limited / failed /
  not_applicable。状態ラダー: not_applicable → high_risk_exposure →
  mitigation_required → risk_accepted →
  reviewed_with_limitations → reviewed。証拠レコードが全種空なら
  `unknown`（未評価≠良好）。「安全」は証拠なしに claim しない。

### #601 Control / automation scenario qualification

新規 `backend/src/htdt/cad_control_scenario.py` +
`cad_control_scenario_repository.py`（テーブル
`cad_control_surfaces` / `cad_control_scenarios` /
`cad_control_scenario_runs` / `cad_control_scenario_qualifications`）。

- `ControlSurfaceDeclaration`（ctrlsurf- 封印）— コントローラ面の
  宣言（controller_family crestron/amx/control4/rti/generic 等、
  program_identity + program_version + program_sha256 の三点 pin）。
- `ControlScenarioStep` — position 一意・昇順、action_kind、
  target_label | target_ref のいずれか必須、expected_feedback、
  timeout_ms>0、continue_on_failure。`ControlScenario`（ctrlscn-
  封印）は kind（startup / shutdown / route_change /
  source_switch / volume_ramp / interlock / custom）、
  failure_notification_required なら channel 必須。
- `ControlRunStepRecord` — verified は elapsed_ms +
  observed_feedback 必須、wrong_feedback も observed_feedback
  必須。`ControlScenarioRun`（ctrlrun- 封印）の scenario_ref は
  scenario の id+sha256 を pin — 検証はリビジョンスコープ。
  completed には finished_at_utc 必須。
- `ControlScenarioQualification`（ctrlqual- 封印）—
  `evaluate_scenario_qualification` のみが生成。6チェック
  （scenario_shape / revision_freshness / execution_coverage /
  step_verification / timeout_behavior / failure_notification）。
  状態ラダー: draft / not_executed / stale / unverified /
  qualified_with_deviations / failed / qualified。run なし →
  not_executed; 同一 scenario_id だが異 revision sha の run のみ →
  stale; 必須の失敗通知が未行使 → unverified（検証記録なしに
  「動作保証」は claim できない構造）。

### #602 Safe-listening / test-exposure authority

新規 `backend/src/htdt/cad_safe_listening.py` +
`cad_safe_listening_repository.py`（テーブル
`cad_exposure_limits` / `cad_spl_capabilities` /
`cad_test_exposure_plans` / `cad_exposure_gates` /
`cad_exposure_assessments`）。

- `ExposureLimitProfile`（explim- 封印）— 人間側の曝露限界の宣言。
  basis（niosh_rel / who_safe_listening_venue /
  declared_project_policy / unknown）、criterion（laeq_twa /
  laeq_windowed / dose_pct / peak_c）、limit_level_db、
  reference_window_s、exchange_rate_db、peak_ceiling_db。
  laeq_twa/dose_pct は exchange_rate_db+window 必須、
  laeq_windowed（WHO 100dB LAeq,15min 型）は exchange 禁止、
  declared_project_policy は notes 必須（出典隠さない）。
- `SplCapabilityDeclaration`（splcap- 封印）— システムが鳴らせるSPL
  能力の宣言。rp22_parameter / in_room_measured / calculated は
  source_ref 必須（RP22 プロファイル pin・実測・計算の別を保持）、
  declared_estimate / unknown は basis_note 必須。
  「システムが鳴らせる」と「人間が聴いてよい」は別の軸で保持する。
- `TestExposurePlan`（expla- 封印）— テスト実行で発生し得る曝露:
  planned_level_db_spl、planned_peak_db_spl、planned_duration_s。
- `ExposureGateDecision`（expgate- 封印）— 超過時の人間意思決定。
  allow_with_controls は controls 必須、block は reason 必須、
  裸 allow は controls 禁止（制御なし許可の混同を防ぐ）。
- `ExposureAssessment`（expassess- 封印）—
  `evaluate_exposure_assessment` のみが生成。6チェック
  （limit_profile / capability_evidence / level_vs_limit /
  duration_dose / peak_ceiling / gate_record）。
  laeq_windowed → 許容時間=ウィンドウ or 0; laeq_twa/dose_pct →
  `window * 2^(-(level-limit)/exchange)` のNIOSH型交換。
  状態: unknown（limit未宣言 or 入力不完全 — 安全の黙示はしない）/
  within_limit / gate_required（超過+gate決定なし=実行しては
  いけない）/ approved / approved_with_controls / blocked /
  not_applicable。peak_ceiling は plan.peak → capability.max_peak
  の順で照合 — 静かな plan でも能力ピークが天井超えなら
  gate_required。

## 文献根拠（一次情報 web_search 確認済）

- AVIXA RP-C303.01:2018 *Security for Networked AV Systems* —
  資産管理・認証・アクセス制御・リモートアクセス・パッチのAV実務。
  https://store.avixa.org/CPBase__item?id=a13f200000C2iRoAAJ
- ANSI/AVIXA D402.02:2013 (R2024) *AV Systems Verification* —
  旧 INFOCOMM 10:2013、~160項の検証項目。シナリオのステップ列・
  フィードバック・タイムアウト検証の系譜。
  https://store.avixa.org/CPBase__item?id=a13f200000C2iQZAAZ
- AVIXA TR-111:2019 *Unified Automation for Buildings* —
  制御・自動化の設計/検証ガイダンス（改訂中）。
  https://store.avixa.org/CPBase__item?id=a13f200000C355SAAR
- AVIXA UX 701.01（開発中 — research-only として外部標準台帳に記録）
- WHO *Global standard for safe listening venues and events* (2022,
  ISBN 978-92-4-004311-4) — LAeq 100 dB/15min 上限+5要件。
  https://www.who.int/publications/i/item/9789240043114
- NIOSH DHHS 98-126 *Criteria for Occupational Noise Exposure* —
  85 dBA 8h TWA、3dB 交換率（職業曝露限定、汎用化しない）。
  https://www.cdc.gov/niosh/docs/98-126/
- CEDIA white paper *Reference Audio Level and SPL Capabilities*
  (2025-10) — RP22 ~105dB 能力 = 85dB 基準+20dB ヘッドルーム、
  必須リスニングレベルではない（能力と曝露の分離の直系根拠）。
  https://cedia.org/en-us/smart-home-professionals/education/white-papers/

## UI 最小配線

- `application_pages._LIFECYCLE_TABLE_LABELS` に 18 テーブルの JA
  ラベル追加。
- `measurement_evidence_display` に state/check ラベル dict +
  `security_review_line` / `scenario_qualification_line` /
  `exposure_assessment_line` ヘルパ（兄弟 REV56 wave と同じ配線深
  さ — inspector graph への結線は後続 wave）。
- `cad_external_standards.seed_standard_documents` に上記7件を
  外部標準台帳として追加。

## 回帰テスト

`backend/tests/test_rev56_ops.py` — 35 件:
- #598: 未評価→unknown、default credential→high_risk_exposure、
  reviewed_with_limitations、秘匿物拒否、revoked+former+default
  矛盾拒否、direct decommissioned 拒否、consent 必須、accepted risk
  要件、repo roundtrip / append-only / row-vs-payload IntegrityError
- #601: not_executed / draft / stale（同id異sha pin）/ 他scenario
  run不勘定 / failed（timeout+通知miss）/ qualified、step バリデー
  ション、repo roundtrip / append-only
- #602: limit未宣言→unknown、within_limit（NIOSH 80dB/600s）、
  超過→gate_required（100dB→900s許容）、allow_with_controls、
  block、WHO windowed、peak ceiling 超過で静かな plan でも gate、
  capability source/basis 必須、gate バリデーション、repo roundtrip
- 共通: semantic id sha 派生、外部標準 seed 7件存在

scoped 実行: `test_rev56_ops.py` + schema/integrity/audit/既存REV56
系 9ファイル 計 181 tests 全グリーン。

## 残存事項

- inspector graph への結線（authority 画面での可視化）は兄弟 wave
  と同様に後続 issue。
- `decommissioned` 到達経路の credential 無効化ワークフロー連鎖
  （access_review → asset lifecycle）を自動駆動するオーケストレー
  ションは未実装 — 現状は宣言的拒否のみ。
- 曝露 gate の UI フロー（実行前ブロック操作）はデータモデルのみ。
- NIOSH 型交換は職業曝露基準 — 一般公開イベントへの適用は
  declared_project_policy 経由で各案件の責任宣言に委ねる。
- common.md が本 VM に存在しなかったため、規約はメモリ + 兄弟
  docs（rev56-transport 等）から再構成 — 不整合あれば指摘ほしい。
