# REV57-AUD — 音響/再生権威: チャネル同一性/極性 + カバレッジ/エイム + 個体ばらつき + メディア再生能力

スコープ: issues #621 (P1), #634 (P1), #628 (P1), #632 (P2)
ブランチ: `devin/1791251187-rev57-aud`（PR #709、merge-test 経由マージ済）
スキーマ: native schema v39 → v40（19 テーブル + インデックス追加 —
REV57-DISP が v39 を先取したため連番で v40 に着地）

## 実装範囲

### #621 Acoustic channel-identity / polarity verification

新規 `backend/src/htdt/cad_channel_identity_authority.py` +
`cad_channel_identity_repository.py`（テーブル
`cad_channel_identity_chains` / `cad_acoustic_endpoint_observations` /
`cad_channel_identity_tests` / `cad_polarity_verification_records` /
`cad_channel_identity_evaluations`）。

- `CadIdentityHop` — 論理チャネルから物理スピーカーまでのホップ
  （renderer_output / processor_dsp / dac_output / amplifier /
  cable_interconnect / speaker_instance / acoustic_endpoint）を 1 本
  ずつ独立に `known` / `inferred` / `unknown` で記録。known には
  参照またはラベルを必須化、unknown には参照を禁止 — 「宣言した
  だけ」が既知扱いされない構造。ホップ種別は一意、期待スピーカー
  実体も一意。
- `CadChannelIdentityChain`（chain- 封印）— 論理チャネル（FL/FR/C/
  LFE/surround/height 等）→ 期待スピーカー実体の結合。
  `render_session_ref`（kind='render_session'）で #603 レンダー
  経路と結線。LFE/redirected/個別サブは `channel_class` で区別。
- `CadIdentityStimulusSpec` — `channel_id_test_signal` クラスは
  #608 刺激レコードの sha ピンまたはスペクトラ記述を必須化 —
  「実験的な音源を流した」だけを同一性証拠にしない。制御不能な
  プログラム素材のみの場合は判定を限定クラスに降格。
- `CadAcousticEndpointObservation`（chiobs- 封印）— 端点観測:
  operator_listening / measurement_mic_level / measurement_mic_ir /
  near_speaker_mic / array_localization / device_telemetry / other。
  手動観測と計測観測を `evidence_class` で分離 — 同一性/極性の
  強い claim は計測系が要求。
- `CadPolarityVerificationRecord`（chipol- 封印）— 極性を 5 層に
  分離: physical_wiring / dsp_inversion / source_internal /
  acoustic_relative / frequency_dependent_phase。層ごとに独立状態。
  `relative_transfer_function` の確定判定は #609 timebase ピン必須。
  「物理配線正常 + DSP 反転 + 音響一致」の組合せは矛盾として
  拒否 — DSP workaround が配線欠陥を書き換えることはない。
- `evaluate_channel_identity`（chieval- 封印）— fail-closed 判定:
  意外端点 → `identity_mismatch`+`multiple_unexpected_endpoints`、
  端点欠落 → `identity_mismatch`+`acoustic_endpoint_mismatch`、
  デバイスマップ食違い → `device_map_mismatch`、配線逆 +
  DSP 反転 → `verified_compensated`（配線欠陥は残存）、配線逆
  のみ → `polarity_fault`、プログラム素材/手動のみ →
  `verified_with_limitations`、レンダラー未駆動 →
  `not_driven_by_renderer`（フォールバック≠死スピーカー）、
  陳腐化トリガー（AVR/DSP リセット、ファームウェア #592、
  ケーブル作業 #597、アンプ交換、スピーカー交換/移動 #596、
  レンダラートポロジ変更 #603、物理極性修理）→ `stale`。

### #634 Listener-area coverage / acoustic-aim qualification

新規 `backend/src/htdt/cad_coverage_aim_authority.py` +
`cad_coverage_aim_repository.py`（テーブル
`cad_acoustic_aim_states` / `cad_coverage_listener_areas` /
`cad_coverage_predictions` / `cad_coverage_measurement_sets` /
`cad_coverage_qualifications`）。

- `CadAimAxis` — 5 種のエイム軸を分離: cabinet_pose /
  acoustic_reference_axis / dataset_reference_axis /
  design_aim_target / as_built_observed_aim。単位ベクトル化。
  `cabinet_to_dataset_rotation` は明示的単位四元数 — キャビネット
  姿勢からデータセット参照軸への変換を暗黙の一致にしない。
- `CadCoverageListenerArea`（covarea- 封印）— #581/#590 連携の
  聴取エリア: 各位置に ear xyz + design/holdout/auxiliary ロール。
  design/holdout ID は frozenset プロパティで導出。
- `CadCoveragePrediction`（covpred- 封印）— エリア sha ピン +
  ソース束縛。`CadCoverageSourceBinding`: 実測 3D 指向は
  `directivity_ref` 必須、`nominal_beamwidth_only` は構造的に
  弱いクラスで昇格しない。スクリーン透過/グリル/境界トランスファー
  は一回だけ適用（unique 制約）。early/late エネルギーの区別、
  coherent 合成は #609 timebase capability ピン必須 — 位相情報
  なしのコヒーレント加算は禁止。`single_physical_source` クラスは
  ちょうど 1 ソースのみ。
- `CadCoveragePathPrediction` — 経路ごとに off-axis/距離/到着/
  バンドレベル、オクルージョン状態（clear/partially_occluded/
  occluded/unknown）と validity をバンドHz×一意で保持。
- `CadCoverageMeasurementSet`（covmeas- 封印）— 実測観測、到着時刻
  宣言は timebase ピン必須。
- `evaluate_coverage_qualification`（coveval- 封印）—
  `CadCoverageProfileRef` で包絡公差のみ外部注入（A102 改訂版
  2022/2023 競合は #599 管轄 — 推測改訂で閾値を決めない）。
  未解決プロファイル → `profile_source_ambiguous`、nominal
  データのみ → `qualified_with_limitations`（指向限定明示）、
  制限 validity → `source_directivity_limited`、オクルージョン →
  `occlusion_limited`、design 位置欠測 + holdout 未測 →
  `spatial_sampling_insufficient`（単一 MLP トレースは全域
  カバレッジに昇格しない）、残差が包絡内 →
  `predicted_and_measured_agree_within_envelope`、超過 →
  `qualified_with_limitations`+`measurement_uncertainty` 帰属。
  位置ごとの状態・制限位置・制限バンド・残差帰属をすべて記録。

### #628 Installed loudspeaker instance-variation authority

新規 `backend/src/htdt/cad_instance_variation_authority.py` +
`cad_instance_variation_repository.py`（テーブル
`cad_instance_acoustic_evidence` / `cad_model_instance_deltas` /
`cad_matched_set_declarations` / `cad_matched_set_qualifications`）。

- `CadInstanceAcousticEvidence`（instev- 封印）— 証拠レベル:
  model_reference_only / manufacturer_population_tolerance /
  golden_sample_reference / instance_factory_qc /
  instance_lab_measurement / instance_in_room_measurement /
  instance_diagnostic_check / derived_instance_adjustment /
  unknown。インスタンス級証拠は instance 由来 observable 必須、
  リファレンス級は instance ソース不可、in_room ≠ free_field
  域分離。観測可能量: polarity / dc_impedance / impedance_zf /
  resonance_features / sensitivity / frequency_response_deviation /
  phase_group_delay / thd_nonlinear / compression_output /
  mechanical_anomaly / self_noise / dsp_firmware_state — 各値に
  band_hz・単位必須、環境交絡フラグは観測ごとに保持。
- `CadModelToInstanceDelta`（instdelta- 封印）— 型番リファレンス
  と実機の両方の証拠 sha をピンした不変デルタ。
  `validate_delta_compatibility`: on-axis→3D 指向には両側
  free-field 必須、低レベル→大信号不可、インピーダンス曲線
  claim には実機側 `impedance_zf` observable 必須。
- `classify_instance_delta` — 故障梯子:
  confirmed_fault / environmental_confound→environment_dependent /
  inconclusive→measurement_inconclusive / suspected_defect /
  公差未バインド→no_population_tolerance_available /
  within_declared_tolerance（manufacturer_population）/
  matched_within_project_tolerance（project）/ outlier。
  公差は明示注入のみ — AES-X241 系の推測限界は捏造しない。
- `CadMatchedSetDeclaration`（matchset- 封印）— l_r / l_c_r /
  surround_array / height_array / multi_sub ロールの ≥2
  `installed_instance` 一意ピン。
- `evaluate_matched_set`（setqual- 封印）— 6 メトリクス独立評価:
  level_spread→sensitivity / fr_deviation→frequency_response_
  deviation / polarity_consistency→polarity / impedance_spread→
  impedance_zf / delay_spread→phase_group_delay / nonlinear_spread→
  thd_nonlinear。2 個体未満の値または公差未バインド →
  unmeasured。outlier 検出は群内中央値からの最大乖離個体を
  `limiting_instance_ids` として同定（最大絶対値＝最も大きい
  ユニットではない）。全公差内+manufacturer_population →
  `matched_within_declared_tolerance`、project →
  `matched_within_project_tolerance`、超過 → `outlier_detected`、
  環境交絡 → `environment_dependent`、証拠なし →
  `insufficient_evidence`、未測定あり →
  `no_population_tolerance_available`。同一型番の交換は #596
  経由でシリアル証拠を陳腐化。

### #632 Media-playback capability qualification

新規 `backend/src/htdt/cad_media_playback_authority.py` +
`cad_media_playback_repository.py`（テーブル
`cad_playback_stack_identities` / `cad_media_profile_requirements` /
`cad_playback_capability_records` / `cad_playback_operation_runs` /
`cad_playback_qualifications`）。

- `CadPlaybackStackIdentity`（pbstack- 封印）— デバイス/OS/アプリ/
  エンジン/ファームウェア/ライセンス/ソースモード/設定の同一性。
  スタック更新は証拠を陳腐化（下記 stale 判定）。
- `CadMediaProfileRequirement`（pbmedia- 封印）— コンテナ/配送
  クラス/コーデック+プロファイル+レベル/解像度/フレームレート/
  ビット深度/クロマ/比色/HDR/音声/字幕/暗号/表現を封印。
  `CadWaveProfileBinding` は `segmented_adaptive` 配送のみ許可
  （WAVE テストコンテンツはバージョン管理+任意+ストリーミング
  限定）、test_maturity（validated/beta/less_validated/unknown）を
  保持 — WAVE テストの弱さを隠さない。
- `CadPlaybackCapabilityRecord`（pbcap- 封印）— 能力クラス:
  declared_by_device_api / declared_by_application /
  tested_playable / tested_with_limitations / tested_fallback /
  unsupported / unknown。tested_* は empirically_tested /
  observed_output 証拠必須（API 宣言はテストにならない）、
  unsupported は失敗属性 ≠ unknown 必須、tested_playable は
  観測出力 `requested_profile_rendered` 必須。オペレーションは
  一回だけ記録（initial_start/continuous/random_access_seek/
  pause_resume/representation_switch/period_transition/
  track_change/audio_language_change/subtitle_change/
  app_background_resume/standby_wake — 初フレーム描画は能力の
  全体ではない）。
- `CadPlaybackObservation` — 出力状態: requested_profile_rendered /
  lower_video_representation / sdr_fallback / lower_bit_depth_chroma /
  audio_codec_fallback / multichannel_to_stereo_downmix /
  object_to_bed_fallback / transcoded / unexpected_output /
  unknown_output。bitstream モードと HDR 出力状態を分離記録。
- `evaluate_playback_capability`（pbqual- 封印）— 判定梯子:
  スタック更新以後 → `insufficient_evidence`+stale=True、
  unsupported+輸送失敗 → `transport_dependency_failed`、
  unsupported のみ → `player_unsupported`、宣言のみ →
  `insufficient_evidence`、unexpected/unknown 出力 →
  `output_profile_mismatch`、フォールバック状態 →
  `qualified_with_fallback`（SDR 化/ダウンミックス/オブジェクト→
  ベッド/トランスコードは隠さない）、失敗オペレーション →
  `qualified_with_limitations`、矛盾する tested 結果 →
  `intermittent`、その他 → `qualified_exact_profile`。DRM は
  観測のみ（解読/迂回なし）、グローバル能力バッジは発行しない。

## 統合・UI 配線

- `cad_schema_ddl.py`: 19 テーブル + インデックス（document_id /
  親参照複合 + 限定ドメインの単一索引）。`cad_schema.py`:
  v39→v40 マイグレーション `_migrate_39_to_40` +
  `NATIVE_SCHEMA_TABLES` 台帳登録。
- `native_authority_audit.py`: 4 factory 分岐（channel_identity /
  coverage_aim / instance_variation / media_playback）+ 19
  `_ReplayProbe`（sha 再計算による改竄検出を replay 監査が検証）。
- `native_row_integrity.py`: 19 テーブル分の `_ROW_BINDINGS`
  （id/sha + 全バインド列の fail-closed 比較 — 1 bit 書換で
  IntegrityError）。nullable 参照列は optional バインド。件数列
  （position_count / member_count / operation_count 等）は
  `_payload_at` の辞書限定走査に非対応のため行側のみ保持 —
  リポジトリ get_* の len 照合で担保（seat_count/step_count
  先行例と同じ設計）。
- `application_pages.py` `_LIFECYCLE_TABLE_LABELS`: 19 テーブルの
  JA ラベル（チャネル同一性評価 / カバレッジ修飾 / マッチドセット
  修飾 / 再生能力修飾 等）。`measurement_evidence_display.py`:
  4 行表示（`channel_identity_evaluation_line` /
  `coverage_qualification_line` / `matched_set_qualification_line` /
  `playback_qualification_line`）— メタデータ宣言だけでは
  「再生済み/一致」と読まない、フォールバック/陳腐化を明示。
- `test_rev57_aud.py`: 90 テスト — バリデータ拒否（ホップ状態
  規則・刺激ピン・一意制約・音響層手法要件・timebase 要件・
  DSP 矛盾・単位ベクトル/四元数・3D 指向データセット参照・
  転送一回適用・早期窓プロファイル・コヒーレント timebase・
  単一ソース・到着時刻 timebase・インスタンスソース一貫・
  free-field 非互換・installed_instance kind・デルタ順序・
  一意名・互換 3 ガード・分類全梯子・マッチドセット ≥2/
  一意/kind・WAVE segmented 限定・tested 証拠クラス・
  unsupported 属性・tested_playable 出力・重複オペレーション・
  ref kind 強制）、評価器全判定梯子、リポジトリ ラウンドトリップ +
  冪等再保存 + 行改竄 IntegrityError。

## 文献根拠

- **#621**: CEDIA コミッショニング系のチャネル ID チェックトーン
  手順（1 チャネルずつ刺激 + 端点確認）と AES 系の位相/極性規約
  — 物理配線反転・DSP/ソース反転・端点での音響極性を別層で
  検証する設計は、AVR 再配線/DSP workaround が配線欠陥を覆い
  隠す実務問題への対応。マッピング宣言と実配置の和解は
  device-map vs physical-path vs acoustic-endpoint の 3 分岐。
  「1 チャネル流れれば同一性成立」ではなく、全ホップ・全端点・
  全層を独立に記録。
- **#634**: スピーカー指向角（水平/垂直カバレッジ vs ear-height
  軸）、off-axis レベル損失、early vs late エネルギー窓、
  座席間変動の包絡 — 没入型音響のリスニングエリア評価実務
  （ITU-R BS.1116 系の聴取領域規範、Dolby/THX 系の座席公差
  プラクティス）に基づく design/holdout 分離と worst-of 判定。
  コヒーレント合成には共通測定 timebase が要るという制約は
  #609 権威と接続（位相なしではエネルギー和のみ）。
- **#628**: 製造公差実務（golden sample vs 量産ばらつき）、
  インピーダンス/共振/感度/THD の観測可能量、群中央値からの
  乖離による matched-set 選択 — リファレンス値を実機に無検証で
  適用しない原則。AES-X241 系の population tolerance 概念は
  research-only として明示バインド時のみ使用 — HTDT はメーカー
  公差を捏造しない。
- **#632**: Dolby Atmos / DTS:X の実デコード vs 宣言能力の区別、
  bitstream vs PCM 経路、DASH/HLS の representation 切替と period
  遷移、HDCP/DRM は観測限定（迂回/解読は行わない）、WAVE テスト
  コンテンツのバージョン管理（WAVE-PCM プロファイル束縛は
  segmented adaptive 配送に限定）。「Atmos コンテンツがある」≠
  「Atmos がデコード済み」の基本原則を verdict 分岐に構造化。

## 残存事項

- **実測/登録経路**: 4 権威は repository API 経由の封印ストア。
  GUI 上の入力フォーム（チェーン編集・端点観測登録・個体証拠
  取込・再生テスト記録）は既存 authority 入力パターンに従う
  別タスク。
- **評価プロファイル実体**: `CadCoverageProfileRef` /
  matched-set tolerances の供給元プロファイル権威との結合は
  別 REV スコープ — 現状は基準注入インターフェースまで。
- **#621 自動チェーン生成**: レンダラー・機器グラフ（#603/#597）
  からのホップ自動生成は接続点として設計済みだが、発見アルゴリズム
  自体は未実装。
- **#634 パス予測生成器**: `CadCoveragePathPrediction` の
  off-axis/到着計算は外部エンジン成果物の登録経路のみ —
  HTDT 内の幾何トレーサーは未実装。
- **#632 実デバイス検証**: DRM/HDCP 観測経路や bitstream 実測は
  ハードウェア依存 — 宣言+観測記録の構造のみで、実機検証器は
  スコープ外。
- **A102 改訂版選択**: 2022/2023 の差分しきい値は #599 が解決
  するまで profile_ref 経由の明示注入のみ（本権威は改訂を推測
  しない）。
