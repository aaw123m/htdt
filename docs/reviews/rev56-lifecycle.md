# REV56-LIFECYCLE — コミッショニング後ヘルス/ドリフト監視 + 機器代替/変更影響権威

スコープ: issues #595 (P1), #596 (P1)
ブランチ: `devin/1791216918-rev56-lifecycle`
スキーマ: native schema v29 → v30（13 テーブル追加）

## 実装範囲

### #595 コミッショニング後ヘルス/ドリフト監視

新規 `backend/src/htdt/cad_health_drift.py` +
`cad_health_drift_repository.py`。全レコード封印済み
（content-derived id + sha256 再計算検証）。#568 の
`cad_system_health`（コミッショニング時チェックラン）は置き換えず、
運用後ライフサイクルの権威として併置。known-good ベースラインは
#592 `DeviceKnownGoodBaseline`/#585 検証レコードを `baseline_refs`/
`comparison_key`+`compared_to_repr` で pin する — 第二の真実ストアは
作らない。

- `DeviceMonitoringDeclaration`（`hlmon-`）: 監視能力クラス6種
  （`active_telemetry`/`pollable_readback`/`event_log_export`/
  `manual_service_observation`/`periodic_measurement_only`/
  `unobservable`）。`unobservable` は他能力との混在を拒否 —
  監視不能は故障ではない。`MonitoringAuthorization` が
  `remote_collection_allowed`・最小スコープ・コンテンツ除外を明示化
  （リモート収集は許可境界を越えない）。
- `LifecycleObservation`（`hlobs-`）: 観測分類16種（オンライン/
  ファームウェア/構成ハッシュ/DSP/エラー/温度・ファン/UPS/
  ネットワーク/PTP/HDMI/ライセンス/ストレージ/再起動/AV自己診断/
  ユーザー報告/現場測定/竣工変化）。`comparison_key` が無い観測は
  「変化なし」にも「変化あり」にも使えない。
- `ChangeEventRecord`（`hlchg-`）: ファームウェア/構成/サービス/
  機器交換/部屋/ネットワーク/電源/校正変更の監査ログ。ドリフトに
  一致する変更イベントが無くても「原因不明の変化」として記録できる
  （原因を捏造しない）。
- `classify_drift_component`: 厳密な分類ラダー
  `insufficient_observability`（能力なし or 観測なし）→
  `not_comparable`（観測あるが基準 pin なし — 「変化なし」は比較が
  成立したときだけ claim）→ `hard_failure`/`intermittent_fault`
  （故障イベント、間欠は閾値パラメータ化）→ `configuration_drift`/
  `performance_drift`（基準と不一致・一致する変更イベントなし）→
  `expected_change`（種別・対象・時間窓が一致する `ChangeEventRecord`
  あり）→ `no_material_change`（比較が実際に走り一致）。
- `DependencyImpactRule` + `derive_stale_marks`: 非クリーン成分が
  発火した規則だけが依存権威を `stale`/`recompute`/`remeasure`/
  `review_required` 化 — 全面無効化はしない。規則未登録の依存は
  `unmapped_dependents` → `review_required`（未知の依存は継続有効を
  意味しない）。カバレッジは `mapped`/`partially_mapped`/`unmapped`
  で報告。
- `TrendAssessment`（`hltnd-`）: 記述統計のみ（中央値・中心偏差・
  最小二乗傾き/日・ステップ検出・帯域外点数）。ガウス仮定なし。
  `band=None` は `no_threshold` でドリフトを断言しない、
  サンプル不足は `insufficient_samples`。閾値は版付き
  `TrendThresholdSpec`（metric_key + center/band/step/min_samples）。
- `SymptomEpisode`（`hlsym-`）: 相関記録だが因果を主張しない —
  `reason_state='confirmed'` は `confirming_refs` 必須、解決済みは
  解決ノート必須。
- `DriftAssessment`（`hldrf-`）: 成分別分類が記録本体 — 不透明な
  集約ヘルススコアは存在しない。`operational_severity`（5段階）と
  `evidence_certainty`（`confirmed`/`suspected`/`unknown`）は分離 —
  `confirmed` は全成分がクリーンでないと claim 不能、severity は
  最悪成分未満を拒否、非クリーン存在時は派生既定で 'low' 下限。
- `ReverificationTrigger`（`hlrev-`）: 成分別に最小限の防衛可能な
  キャンペーンを合成 — 故障→`service_investigation`、構成ドリフト→
  `restore_config`（known-good pin がある場合のみ、無ければ
  `domain_reverification`）、性能ドリフト/証拠陳腐/比較不能→
  `reference_check`、観測性不足→`manual_observation`、stale マーク→
  `domain_reverification`。`full_recommission_required` は関数が
  自動では出さない — 明示フラグ+理由必須（軽量チェックは再
  コミッショニングではない、issue §6）。
- `RestoreConfirmation`（`hlrst-`）: 復元確認ループ。
  チェック0件→`confirmation_inconclusive`（構成ハッシュ一致だけでは
  物理性能の復元を主張しない）、passed のみ→`confidence_restored`、
  failed 含む→`differences_remain`、inconclusive 含む→
  `confirmation_inconclusive`、明示→`escalate_service`。

### #596 機器代替/変更影響権威

新規 `backend/src/htdt/cad_substitution_impact.py` +
`cad_substitution_impact_repository.py`。#894 の BOM 行
`SubstitutionRecord`（置換の存在マーカー）と #592
`ReplacementDeviceAssessment`（構成移植性）は置き換えず、本権威が
「同等性証拠行列 + 影響再修飾」を担う。

- `SubstitutionEquipmentRef`: `definition_id`+`definition_version`+
  `definition_sha256` で製品同一性を pin。sha なしは正直な
  「未カタログ化」で、`identical_to` は sha 一致必須 —
  同型番ラベルでは同一を主張しない。
- `EquipmentSubstitutionProposal`（`subprop-`）: 正確な旧/新品同一性、
  理由（7種）、証拠クラス（`same_exact_evidence`/`verified_equivalent`/
  `declared_equivalent`/`insufficient`）。`same_exact_evidence` は
  同一性必須（型番一致だけのラベル変更は証拠にならない）、同一品
  の提案は `service_replacement` 理由時のみ有効。
- `DimensionalAssessment`: 7次元群（物理設置/電気音響/信号DSP/
  映像投影/相互運用/インフラ/ライフサイクル）それぞれに
  `product_difference`（PRODUCT_DIFFERENCE）と
  `requirement_verdict`（PROJECT_REQUIREMENT_VERDICT + 要件 ref pin）
  を分離保持 — 「製品として劣るが要件は満たす」「スペック同一だが
  設置不適合」を両立可能に。
- Fail-closed: 通過判定（`equivalent_by_same_exact_evidence`/
  `meets_or_exceeds_requirement`/`different_but_acceptable`）は
  `bound_to ∈ {proposed, requirement, shared_exact, field}` の証拠を
  必須化 — **旧機器の証拠だけでは新機器の通過判定を成立させない**。
  `same_exact` は `shared_exact` 証拠必須。否定判定（`inferior`/
  `incompatible`）は示す証拠必須、`not_applicable` は理由必須。
- `AffectedAuthorityEntry` + `_ROLE_DIMENSIONS`: 依存ロール20種を
  感受次元へマッピングし `disposition_for_role` で機械導出 —
  不適合→`remeasure`、低下/差異許容→`recompute`、証拠不足→
  `review_required`、ロール未マップ（`other`）→常に
  `review_required`。`extra_dispositions` は重複 ref を拒否。
- `ChangeImpactAssessment`（`subimp-`）: 行列から
  `technical_verdict` を派生（不適合/要件失敗→incompatible、低下/
  差異許容→limited、証拠不足/review_required→indeterminate、
  他→acceptable）— 保存値と行列が不整合ならモデルが拒否。
  `commercial_state` は技術判定に一切混入しない。
- `SubstitutionApprovalDecision`（`subapr-`）: 8状態。
  `engineering_approved` は acceptable 必須、`with_limitations` は
  limited+制限引継必須、`commercial_override_accepted` は商業承認+
  理由+非 acceptable 必須、`as_built_verified` は照合 ref 必須。
  判定は proposal sha でリビジョンに bind。
- `AsBuiltReconciliation`（`subab-`）: 設置実物と承認品の同一性照合 —
  判定は導出のみ（承認≠設置の証明）。sha なし設置品は
  `identity_unverified`。
- `EquipmentScheduleRecord`（`subsch-`）: design→approved→procured→
  installed→service の append-only 履歴（`supersedes_schedule_id`
  チェーン）— 設計品は最終品で上書きされない。
- `compose_reverification_scope`: 影響 ref だけに最小タスクを合成
  （幾何→`geometry_recheck`、予測→`prediction_recompute`、規格→
  `standards_profile_reeval`、測定→`measurement_targeted`、制御→
  `control_integration_check` 等）— 全面再コミッション化しない。

### 永続化・統合

- 13 テーブル append-only（id 再保存は同 sha で冪等、異 sha は
  ConflictError）。`CadSubstitutionImpactRepository.save_assessment`
  は提案 sha→id の既永続化を要求（孤立 assessment 不可）。
- `_ROW_BINDINGS` 13 エントリ、`native_authority_audit` にリポジトリ
  チェーン 2 系統 + `_ReplayProbe` 13 件追加。
- UI 最小配線: `_LIFECYCLE_TABLE_LABELS` 13 件の JA ラベル +
  `measurement_evidence_display` に判定/状態/次元ラベル辞書と
  `drift_assessment_line`/`change_impact_line`（severity と certainty
  を別表示 — スコア化しない）。

## 文献根拠

- AVIXA コミッショニング後ハンドオーバー実務記事: 監視対象は
  オンライン/温度/ネットワーク/ファームウェア/周辺機器/コール性能/
  エラーログ/アップタイム/ライセンス/ストレージ/再発障害 —
  観測分類16種の根拠。「post-handover support は設計段階で計画」→
  `MonitoringAuthorization`/能力宣言を設計レコード化。
- NIST SP 800-53 CM-2(2): 承認済みベースラインは版付き成果物、
  ドリフト=承認ベースライン vs 実状態、変更記録への突合で
  reconcile — `baseline_refs` pin + `ChangeEventRecord` 照合 +
  `expected_change` 分類の根拠。
- ILAC-G24/OIML D 10 + ISO/IEC 17025: 中間チェックは
  「疑念を招く変更が無いことの確認」であり全再検証ではない —
  `run_reference_check` と `full_recommission_required` の峻別、
  傾向ベースの再校正間隔 → `TrendThresholdSpec` の版管理。
- 「Or Equal」代替品手続き（CSI Section 01 25 13 型）: 代替要求は
  重要品質の詳細比較・設置変化の明示・承認前発注禁止 —
  次元別証拠行列・`commercial_state` 分離・`as_built_verified` の
  設置照合の根拠。

## 残存事項

- 依存規則（`DependencyImpactRule`）の自動収穫は未実装 —
  現状は宣言入力のみ。権威間の関係グラフからの自動導出は別 issue。
- `full_recommission_required` の起動条件を権威側で提案する規則は
  未実装（呼び出し側が明示フラグで宣言）。
- 観測取り込みパイプライン（テレメトリ/ログの ingest アダプタ）は
  権威の外 — `LifecycleObservation` は結果を封印する器。
- UI はラベル/1行表示のみ — ドリフト一覧・代替審査画面の画面実装は
  別スライス。
- `test_application_pages.py` の workspace 登録テスト2件は
  main 時点の既知失敗（presentation/video workspace 未登録）、
  本変更と無関係。
