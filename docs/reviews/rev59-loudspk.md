# REV59-LOUDSPK レビュー記録 — 大信号・絶対出力・熱圧縮・マイク入射角・アレイ・グリル透過

対象 issue: #754 (P1), #734 (P1), #731 (P1), #732 (P1), #737 (P2), #735 (P2)
着地: schema v53、新規 19 テーブル、リポジトリ `cad_loudspeaker_evidence_repository`、回帰テスト `test_rev59_loudspk.py` 31 件

## 実装権威

### #754 `cad_large_signal.py`
- `LargeSignalTransducerModel` (lstm-): 証拠クラス（IEC 62458 パラメータ測定 / IEC 60268-22 大信号測定 / 独立ラボ / HTDT 実測 / モデル導出 / メーカー公称 / ユーザー入力 / unknown）・非線形パラメータ種別（bl_x / kms_x / le_x_i / asymmetry_offset 等）・エンクロージャ整合・レベル適用域を pin。`model_derived_excursion` は非線形パラメータ宣言必須、`large_signal_validated_to_level` は測定系証拠クラス + validation ref 必須、`unknown` パラメータ種は拒否
- `ExcursionCapability` (excp-): Xmax 系データを `convention`（片側ピーク/pp）+ `definition_basis`（performance/parameter/nominal）+ `distortion_criterion` で pin — 公称・定義・歪規準が揃わない Xmax は比較不可。標準系証拠は standard_ref 必須
- `VentFlowCapability` (vflw-): ポート乱流/圧縮/雑音/寄生共鳴・パッシブラジエータ限界を機構別に pin — ベント機構はベント系エンクロージャ必須
- `MechanicalOutputLimitAssessment` (molm-) + `evaluate_output_limit`: unknown レベル → insufficient_evidence、small_signal_only → small_signal_only（**線形外挿を昇格しない**）、保護機構 → protection_limited、実測系 → measured_bounded、非線形パラメータ → model_bounded
- `compare_excursion_datums`: convention/basis/歪規準の一致のみ equivalent、差異は mismatched/incomparable

### #734 `cad_source_normalization.py`
- `LoudspeakerSourceNormalization` (snrm-): capability 階層（relative_shape_only → absolute_spl_at_reference_drive → absolute_transfer → sound_power → large_signal_curve → maximum_output → installed_measured）と規格化メソッドを pin。**絶対 capability は per-frequency / per-polar 再規格化の下では宣言不可**（角度別絶対レベル関係を破壊するため）
- `ReferenceDriveCondition` (rdrv-): 駆動量の型（電圧rms/電流rms/電力/dBFS/デジタル基準）と値 — 型付き量は値必須
- `AbsoluteAcousticOutputAnchor` (aout-): drive ref・基準距離・音響原点・linear_scaling 適格性
- `evaluate_absolute_prediction`: unknown/relative → 証拠不足 or relative_response_only、アンカー未 pin（駆動条件・距離・原点）→ insufficient、linear_scaling 適格 → absolute_spl_prediction_eligible、限界付 → absolute_with_limitations、大信号モデル要求 → large_signal_unsupported
- `convert_reference_drive` (SRCN30): 電圧↔電力変換は pin されたインピーダンス証拠必須 — なければ equivalence_blocked、跨領域（電気↔デジタル）は incomparable

### #731 `cad_thermal_compression.py`
- `SustainedOutputTest` (sout-): capability 階級（small_signal_reference / short_burst_peak / standard_max_linear_output / sustained_output / thermally_stabilized / repeated_burst / recovery / protection_limited）。**持続系クラスは duration_s 必須**、`standard_max_linear_output` は standard_ref（AES75-2023 等の方法改訂）必須
- `CompressionSample`: elapsed_s + loss_db（+帯域・歪/コヒーレンス変化）
- `ThermalCompressionObservation` (tcmp-): 帯域両端対・**測定チェーン過負荷中は DUT 原因帰属を拒否**（#695 測定境界フック）
- `RecoveryProfile` (rcvp-): fully_recovered は baseline_within_db 必須
- `claim_output_capability`: 持続 claim が burst/small_signal 試験由来 → burst_only_evidence、保護が観測限界 → protection_limited、チェーン過負荷 → measurement_chain_limited、持続 claim + 持続試験 + 圧縮観測 → sustained_verified

### #732 `cad_microphone_incidence.py`
- `MeasurementMicrophoneDirectionalProfile` (mdpf-): 校正音場種別（pressure / free_field / random_incidence / diffuse_field / manufacturer 0deg/90deg correction / angular_response_dataset / user_measured_directional）を pin。角度系種別は dataset ref + 角度/周波数グリッド宣言必須、`angle_sensitive_above_hz` で周波数ゲートを宣言
- `ReceiverOrientationState` (rors-): world_fixed フレームは測定 yaw 必須
- `MicrophoneIncidenceApplicability` (miap-): measured_grid 補正スコープは correction ref 必須
- `evaluate_incidence`: unknown kind → unknown。角度証拠未宣言の自由音場校正は周波数宣言で limited（0deg にも）。角度グリッド内+周波数窓内 → correction_available/measured_grid、窓外 → limited/derived_extrapolated、基準から ±45deg 超 → incompatible。pressure は free-field 真値に昇格しない（角度感度帯で limited）。指向性 measurand（binaural_directional）は角度データセット系必須、拡散 measurand は拡散音場量必須

### #737 `cad_surround_array.py`
- `SameChannelSpeakerArray` (scar-): ≥2 メンバー必須・論理チャネル pin・トポロジ（physical_shared_output / independent_output_fixed / software_managed / distributed / external_processor）。**shared-output トポロジでは個別調整メンバーを拒否**
- `ArrayReproductionMode` (armd-): render_mode（5.1/7.1/Atmos/DTS:X/Auro/upmixed/fallback）+ member_behavior
- `ArrayAcousticQualification` (arqu-): 測定席 refs・holdout refs・メンバー証拠・和音場証拠・レベル/周波数/タイミングばらつき・相殺席数・定位影響
- `evaluate_array_qualification`: topology unknown → unqualified、測定席なし/和音場証拠なし/測定席<2 or holdout なし → unqualified（**単席から全席は claim 不可**）、相殺席 → seat_anomaly、オブジェクトモード+arrayed members → localization_tradeoff、spectral spread が level spread の 2倍超 → spectral_limited
- `assert_member_adjustment`: shared-output トポロジでの個別調整要求を拒否

### #735 `cad_grille_transfer.py`
- `LoudspeakerFrontLayer` (lfly-): 前面層種別（OEM グリル/クロット/枠+クロット/金属孔/樹脂孔/スロット/建築ファブリック/吸音面材/トリム/複層）。frame 宣言種は frame_present 必須、spacing_mm・asbuilt ref
- `GrilleTransferEvidence` (gtrf-): 証拠クラス（OEM 検証/メーカー測定/独立測定/HTDT ユーザー測定/数値モデル/経験導出/宣伝/unknown）・伝達種別（複素伝達/IR/振幅のみ/スカラー挿入損失）・周波数/スペーシング適用窓。**htdt_user_measured は layer-on/off 両キャプチャ必須**
- `FrontLayerApplicability` (flap-): base_dataset ref 必須・grille_on 基礎データへの補正 verdict は二重計上として拒否
- `evaluate_front_layer`: 層なし → base_directivity_directly_applicable、kind unknown → unknown、証拠なし/宣伝 → **unknown（裸応答を適用しない）**、実測系以外（経験導出）→ unknown、スペーシング窓外 → directivity_limited、複素伝達+角度 → installed_front_layer_directivity_available、複素伝達のみ → base_directivity_plus_measured_transfer、振幅のみ+角度 → base_directivity_plus_measured_transfer、振幅のみ → on_axis_only_correction、スカラー損失 → on_axis_only_correction

## 既存権威との非重複確認
- #568 correction qualification（適用補正の権威）— 本トラックは **claim 側の capability/normalization を権威化**、#568 側へは verdict で参照
- #614 boundary・#693 reproducibility — #731 は measurement_chain_overload を**観測フラグ**として扱い原因帰属を拒否（境界権威の領域に踏み込まない）
- #604 propagation・#608 stimulus・#611 calibration lifecycle・#613 survey — 駆動条件は stimulus (#608) ではなくスピーカー駆動物理量、校正ライフサイクル (#611) はマイク**校正値の適用範囲**を権威化する本件の別責務
- 名前衝突回避: `SourceNormalizationSpec`（cad_hybrid_handoff_authority）・`PointSourceNormalizationAuthority`（cad_geometric_acoustics_response）と別名 `LoudspeakerSourceNormalization` 等を採用

## 文献根拠
- Klippel, "Loudspeaker Nonlinearities — Causes, Parameters, Symptoms", JAES 54(10) 2006 および Klippel JAES 51(5) 2003 大信号パラメータ（Bl(x)/Kms(x)/Le(x,i)・asymmetry・suspension creep）
- IEC 62458（大信号パラメータ測定）・IEC 60268-22（音響出力能力、大信号条件）・IEC 60268-21（出力ベース測定方法）
- ANSI/CTA-2034-B/2054（スピーカー方向性測定・基準軸/距離の慣例）— 絶対規格化の前提
- AES75-2023（最大線形音響出力：長時間定常 vs 短時間）・AES2-2012（熱圧縮測定の実務的解釈 — 連続駆動でのレベル低下）
- Button, JAES 40（パワー圧縮・Re 温度依存）・Devantier & Rapoport AES 117 preprint（ボイスコイル温度→出力低下の実測）・Pene AES 148 preprint
- IEC 61094-3/61094-5（自由音場/圧力応答校正の区別）・IEC 61183（拡散音場校正）— 校正種別と入射角適用域の根拠
- ITU-R BS.2419-0（多チャネル再生の定位要件）・CEDIA/CTA-RP22 §5.6.2.1（サブウーファ複数配置・席間ばらつき）
- Kılıçkaya & Erol 2026, DOI 10.3397/1/37745・Olsen AES 143 (2017)（音響透過布/グリルの透過損失実測）・Dynaudio グリル EQ 実務資料（グリル on/off 伝達差分）

## 検証
- `test_rev59_loudspk.py` 31 テスト: seal 再計算・validator fail-closed・verdict 梯子・repository roundtrip/冪等/tamper 検出・fresh-migrate 19 テーブル存在
- 登録面: NATIVE_SCHEMA_TABLES + DDL 19 テーブル + `_migrate_52_to_53` + `_ROW_BINDINGS` 19 + `_RepositoryChain` `loudspeaker_evidence` + `_ReplayProbe`×19 + `application_pages` ラベル 19 + `measurement_evidence_display` JA 行 6 + manifest 6 issue

## 残件
- 権威作成 UI（プロファイル/証拠作成ウィザード）は後続 — 現状はドメイン API + repository + JA 表示行のみ
- 実測駆動条件→大信号評価の連結（#754 verdict を RP22/測定パイプラインの claim ゲートへ接続）は後続
- グリル on/off 差分の採取ウィザード（htdt_user_measured の capture 連携）は #735 後続実装
