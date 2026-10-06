# Issue #806 — 校正デプロイ/検証ループ権威

designed ≠ exported ≠ deployed ≠ verified を証拠権威として強制する。

## モデル (`cad_calibration_deployment.py`)

- `DeploymentCapabilityDeclaration` (`dcd-`): アダプタ/デバイスファミリが宣言する apply / read-back / materialization 能力と feature/limit セット。#804 の実機受入マトリクスの欄定義にも対応する宣言面。
- `CalibrationDeployment` (`cald-`): plan/export/materialization/binding/declaration の各 SHA をピンしたデプロイ記録。`evidence_mode` は `machine_readback` / `operator_attestation` / `unverified_export` の3区別を強制:
  - `deployment_verified` は `machine_readback` + 観測スナップショット必須 — apply ACK やエクスポートの存在だけでは絶対に到達しない
  - `deployment_attested` は `operator_attestation` + attestor 必須 — 手入力確認は検証より弱い証拠として残る
  - `deployment_unverified` はスナップショット非持ち — エクスポートのみの状態
  - `deployment_superseded` は保存不可 — ロールバック記録からのみ導出される
- `DeploymentEffectivenessReport` (`defx-`): デプロイ済み状態に紐づく前後再測定比較。`improvement_verified` は post 測定 ref + improved 差分必須、`regressed` 差分混入は失格。
- `DeploymentRollbackRecord` (`drbk-`): ロールバックは対象デプロイと `affected_evidence_refs` だけを無効化し、部屋/機材/測定資産のエビデンスは触れない。

## 評価器 (fail-closed)

- `evaluate_deployment_gate(materialization, declaration=None)` — 宣言欠落 / apply・materialization 未対応 / `unsupported_items` 非空 → `deployment_blocked`。
- `derive_deployment_state(observed_source, observed_divergence)` — スナップショット無し → `unverified`; 乖離検出 → `mismatch`; `read_back` → `verified`; `operator_entered` → `attested`。
- `evaluate_deployment_effectiveness(deployment, report)` — verified/attested 以外のデプロイ状態はレポート存在でも `inconclusive`。
- `deployment_state_with_rollbacks` / `rollback_invalidates_evidence` — ロールバック後状態の導出と、エビデンス無効化の範囲判定。

## リポジトリ (`cad_calibration_deployment_repository.py`)

append-only `_SealedStore` × 4 (`cad_deployment_capability_declarations` / `cad_calibration_deployments` / `cad_deployment_effectiveness_reports` / `cad_deployment_rollbacks`)。save 時シール再検証、get 時は全バインド列検証 (ネスト None を含む)、list 時は document_id と payload の一致を検証。スキーマ v79、`native_row_integrity` / `native_authority_audit` / ライフサイクルラベル配線済み。

## 既存レイヤとの関係

既存の `AdapterCapabilityReport` / `MaterializedCalibrationSettings` / `DeviceApplyAck` / `EffectiveAppliedSettingsSnapshot` (`cad_device_adapter`) は機械的操作面を担う。本モジュールはそれらを「権威」に昇格させる層: 能力宣言をシール済み記録として保持し、デプロイ状態を証拠区分で判定し、効果を再測定に紐づける。アダプタが「適用しました」と ACK しても `unverified_export` のままであることが権威として強制される。

Refs #806
