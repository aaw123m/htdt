# Issue #838 slice B — CamillaDSP デプロイ/読戻し/ロールバック + miniDSP ターゲット台帳

DSP ターゲット (#838) のうち、CamillaDSP のエンドツーエンド参照アダプタと
miniDSP 正確ターゲットプロファイル台帳 + biquad テキストエクスポートを実装する。
`SetConfigJson` の Ok を「音響検証」とは扱わない — 証拠段階は #806 の権威モデルに
厳格に載せる。

## ターゲット台帳 (`cad_deployment_target.py`)

- `DSPDeploymentTargetProfile` (`dtp-`): フィルタクラス/個数、サンプルレート、
  gain/Q/Fc 範囲、FIR 制限、チャネルトポロジー、プリアンプ有無、
  apply/read-back/runtime-attestation/rollback 能力、biquad 係数の符号規約、
  根拠ドキュメント URL をシール済みで保持。「version-bound であり永久の真実ではない」
  — `doc_sources` に出典をピンする。
- 登録プロファイル: CamillaDSP WebSocket ターゲット + miniDSP 公式 REW 表の
  正確な機種群 (2x4 @48k/6PEQ、4x10HD/nanoDIGI @96k/5、
  2x4HD/Flex/FlexEight/SHD/HA-DSP/PWR-ICE @96k/10、C-DSP 8x12 @192k/10、
  DDRC-24/Flex-Dirac/FlexEight-Dirac/FlexHT/FlexHTx/8x12DL/Harmony/DDRC-88 @48k/10、
  nanoAVR @96k/10 — Dirac 有無は別プロファイル) + 既存 generic biquad フォールバック。
- `evaluate_export_target_fit(export, profile)` — `fits` / `exceeds_profile` /
  `rate_unsupported` / `unsupported_parameters` / `unknown_profile`。
  正確なプロファイルに合わないものは fail-closed で `unsupported_items` に上がり、
  #806 の `evaluate_deployment_gate` がブロックする。

## miniDSP アダプタ (`cad_minidsp_export.py`)

- `MiniDSPBiquadExportAdapter` (`CalibrationDeviceAdapter`): `biquadN,b0=…,a2=…`
  形式のチャネル別テキストを materialize。miniDSP の分母規約
  `1 - a1 z^-1 - a2 z^-2` に合わせて a1/a2 を符号反転して書き出す。
- `apply` / 機械 read-back は未対応 (`supports_apply=False`,
  `supports_read_back=False`) — オペレータが miniDSP plugin に手動インポートし、
  attestation でのみ主張できる。ファイル受け渡しと機械読戻しは証拠強度が違う。
- `derive_minidsp_handoff_stage` — `exported_for_minidsp` /
  `operator_import_attested` / `device_state_unknown` / `post_measurement_verified`
  の段階を証拠から導出。エクスポートがあっても機器状態は `unknown` のまま。
- 正確なプロファイル必須 — ターゲット不明な miniDSP への出力は出さない。

## CamillaDSP アダプタ (`cad_camilladsp_deploy.py`)

- `CamillaDSPCalibrationAdapter` (`CalibrationDeviceAdapter`、#1072 の
  `CamillaDSPTransport` 注入シームの上): WebSocket コマンド面は公式
  websocket.md のコマンド集合に限定 (GetConfigJson / SetConfigJson /
  ValidateConfigJson / GetPreviousConfig / GetState / GetStopReason /
  GetClippedSamples / ResetClippedSamples / GetCaptureRate / GetRateAdjust /
  GetBufferLevel / GetProcessingLoad / GetSignalLevels)。
- `materialize`: 現行 config を GetConfigJson で読み、`compile_camilladsp_config`
  で htdt 領域 (`htdt_<sha12>_<ch>_…` 命名の Gain/Delay/xo/peq フィルタと
  Filter パイプラインステップ) を組み込む。チャネル同一性 (channel_id、
  フィルタ id 対応、出力インデックス) は `description` の JSON ブロブにピンし
  往復復元できる。既存の stale な htdt_ 名前は剥がすが他者のフィルタは触れない。
- `apply`: `operator_confirmed` 必須 → `ValidateConfigJson` ゲート →
  `SetConfigJson`。検証失敗時は一切書き込まない。成功 ACK は
  `DeviceApplyAck` ('ack:' + hash[:32]) — これだけでは verified に到達しない。
- `read_back`: `GetConfigJson` を `normalize_camilladsp_config` で
  `CadExportedChannelSettings` に復元し、サービスの deviation 比較で証拠化。
  復元不能なデバイス側値は黙って捨てず乖離として出る。
- `capture_baseline` / `read_back_config` / `rollback_previous`:
  deploy 前 config を SHA ピンで保持し、`GetPreviousConfig` + read-back で
  ロールバック証拠 (`CamillaDSPRollbackEvidence`) を残す。戻った先が baseline と
  一致したか (`restored_verified`) / 一致しない (`restored_previous_diverged`) /
  復元不能 (`restore_failed`) を区別する。
- `observe_runtime` → `CamillaDSPRuntimeObservation` (`crun-`): 稼働状態・
  クリッピング・信号レベル等のランタイムテレメトリ。音響検証とは別の証拠種別で、
  各 Get が対応しないデバイスでは `limitations` に列挙してフェイルしない。
- エンドポイントポリシー: `binding.device_serial` が `camilladsp://host:port`
  を担う。ループバック (localhost/127.0.0.1/::1) は既定許可、それ以外は
  `approved_remote_endpoints` に明示列挙が必要。LAN 自動発見は行わない。

## 永続化 (スキーマ v95)

- `cad_camilladsp_deployment_sessions` (`cdsp-`): deploy セッションの証拠束
  (binding/materialization/candidate/previous/readback の各 SHA と ack)。
  `readback_matched=False` の乖離読戻しも正当な証拠として保存できるが、
  runtime/effectiveness ref は matched の場合のみ封印可能。
- `cad_camilladsp_runtime_observations` (`crun-`)、
  `cad_camilladsp_rollback_evidence` (`crbk-`)。
- `_migrate_93_to_94` + `_MIGRATIONS[94]`、`native_row_integrity` バインド列、
  `native_authority_audit` リプレイプローブ、ライフサイクル JA ラベル配線済み。

## 段階梯子

`designed → compiled_for_target → deploy_requested → deploy_acknowledged →
config_readback_matched → runtime_observed → post_measurement_verified`
(=`derive_camilladsp_stage`)。各段階は対応する証拠 ref/SHA がないと進まず、
ランタイムテレメトリは `post_measurement_verified` に到達しない
(再測定権威は #806 `DeploymentEffectivenessReport` が担う)。

## 対象外 (本スライスではやらない)

REW 委譲測定プロバイダ (slice A)、Equalizer APO、汎用エクスポートの追加、
プロプライエタリな自動ルーム補正エンジンの内蔵、ベンダー非公表プロトコルの
逆解析。API 成功 = 音響成功とみなす扱いはしない。

## テスト

`backend/tests/test_issue_838_deployment_targets.py` — 台帳封印/フィット判定、
miniDSP 符号反転と fail-closed、CamillaDSP の compile/normalize 往復、
Validate ゲート、リモートエンドポイント承認、ランタイム観測の制限列挙、
ロールバック証拠、リポジトリ往復と改竄検出。
