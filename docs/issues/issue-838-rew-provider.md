# Issue #838 — 委託測定プロバイダ + ファイルエクスポートデプロイ権威 (slice A)

REW はコアにコピーしない委託プロバイダとして扱い、ファイル書き込みは検証済みランタイム状態に一切到達しない。

## モデル (`cad_delegated_provider.py`)

- `DelegatedProviderManifest` (`dpm-`): プロバイダID/バージョン、アダプタID/バージョン、エンドポイント (localhost / remote_operator_configured / file / none)、capability 条件テーブルをシール。capability は version-bound — エンジン更新は別マニフェスト。
- `ProviderCapabilityEntry`: `supported` / `licensed_required` / `unsupported` / `unknown` の条件付き。`licensed_required`・`unsupported` は理由必須。`observed` はセッション時点で実応答があった面を記録 (宣言≠観測)。
- `ProviderAcquisitionRecord` (`dpa-`): `manifest_ref` (sha256 ピン) + capability + request identity/hash + outcome + evidence refs + raw artifact sha。`observed` は evidence refs か raw hash の一方必須で error_detail 不可。`cancelled`/`error`/`capability_rejected` は証拠 ref を持てない — 部分結果は破棄。
- `evaluate_provider_gate` — fail-closed: マニフェスト欠落・capability 未宣言・unknown/unsupported → `provider_blocked`。`licensed_required` は `provider_license_required` — 絶対に `provider_capable` に昇格しない。
- `build_provider_acquisition` — `observed` はゲート強制: 能力不足なら例外。
- `REW_DECLARED_CAPABILITIES` + `build_rew_provider_manifest` — 文書化済み REW API 面 (measurement list / frequency response / impulse response / group delay / RTA / SPL/Leq / SPL logger / generator / sweep / EQ alignment / trace processing / subscriptions / file import) を条件付きで宣言し、#599 `RewEngineSession` のシール済みアイデンティティを合流。`automated_sweep` は `licensed_required` (REW Pro 必須)。RTA/SPL/Leq のマシンリーダブル観測は #793 ワークフローへマップする語彙。
- `build_htdt_native_import_manifest` — ファイルがプロバイダ出力で HTDT がインポータとなる経路 (endpoint_kind=`file`、source identity ピン)。

## ファイルデプロイ証跡 (`cad_file_export_deployment.py`)

Equalizer APO 等のファイル系ターゲットでは、公式ドキュメント上 unsupported 行は黙って無視される — つまり「書いた」≠「適用された」。

- `FileExportDeployment` (`fed-`): エクスポートした正確なバイト列の SHA256 + レンダラ ID/バージョン + フォーマット ID + ターゲットスコープ repr をシール。
- `file_state`: `exported_config` → `file_installed` → `file_readback_matched`/`file_readback_mismatch`、`export_blocked`、`unknown`。ファイルレベルの真実のみ。
- `runtime_state` は2値のみ: `runtime_not_attested` (既定・天井) / `post_measurement_verified` (post-deployment measurement ref 必須 + readback一致必須)。ファイルから直接検証済み状態には構造的に到達不可。
- `evidence_mode`: `unverified_export` < `operator_attestation` (install_attestor 必須) < `file_level_readback`。手動証言は常に弱い証拠として表示。
- `derive_file_export_state` / `evaluate_file_export_verification` — 証拠が支持する最強の正直な状態のみ導出。

## Equalizer APO レンダラ (`cad_equalizer_apo_export.py`)

- 境界付きサブセットのみレンダリング: `Device`/`Channel`/`Preamp`/`Delay`(ms)/`Filter n:` (PK・LSC・HSC・LPQ・HPQ・NO・BP・AP)。
- `render_equalizer_apo_config` — 未対応パラメータは `ApoExportUnsupportedError` で全件報告 (黙殺しない)。
- `verify_exported_apo_config` — 生成テキストを #808 インポータ (`build_equalizer_apo_artifact`) で再パースし、正規化セマンティクスをリクエストと比較 → `matched`/`mismatch`。`Channel: ALL` 配下の `Preamp` は APO 仕様通りグローバルに累積する点も検証。一致でもファイルレベルの証拠でしかない。

## 汎用エクスポートフォールバック (`cad_generic_dsp_export.py`)

対象ターゲットの適格アダプタが無くても常に提供 (issue §action 6):

- `render_generic_peq_export` — parametric spec (type/Fc/gain/Q|BW) のポータブルテキスト。
- `render_generic_biquad_export` — #679 系 `build_biquad_filter` の検証済み RBJ 係数 (a0正規化 b0,b1,b2,a1,a2)。対象外タイプ・q 未指定・Nyquist 超過は `biquad_export_support_problems` / `GenericExportUnsupportedError` で fail-closed。
- `render_generic_fir_export` — チャネル別タップ列 + サンプルレート明示。
- `render_manual_settings_handoff` — AVR 手入力指示書。デバイス状態については何も主張しない。
- `ExportFilterBand` — APO/汎用双方の共有正規化バンド語彙。

## リポジトリ (`cad_delegated_provider_repository.py`)

append-only `_SealedStore` × 3 (`cad_delegated_provider_manifests` / `cad_provider_acquisitions` / `cad_file_deployments`)。save 時シール再検証、get 時全バインド列検証、list 時 document_id 一致検証。スキーマ v95、`native_row_integrity`・`native_authority_audit` (リプレイプローブ)・ライフサイクルラベル配線済み。

## 責務分担との関係

HTDT = オーケストレーション・正確な同一性・出自・デプロイ/読み戻し・前後検証。外部ツール = 測定・ライブ解析・リアルタイムDSP。CamillaDSP ライブデプロイ/readback/rollback と miniDSP レジストリは slice B (姉妹 PR) が担当 — 本スライスは共有語彙 (`DelegatedProviderManifest`, target literals) のみ提供。

Refs #838
