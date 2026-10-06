# REV59-AUDIOMET-B — 適応同定/ライブTF/マイクアレイ/インピーダンス

schema v66・`cad_field_metrology_repository`（15ストア）・
`test_rev59_audiometb.py`（23テスト）。

## #661 任意刺激適応同定権威（P2）

- `AdaptiveIdentificationProfile`（aam-）: 推定器手法を宣言
  （subband_adaptive_identifier 等）。'unknown' は fail-closed、
  ネイティブ手法は `license_review_status='reviewed_permitted'`
  必須 — FSAF 系コード自体は実装せず import-only（#661 §18 の
  ライセンスゲート準拠）。
- `ArbitraryStimulusMeasurement`（asm-）: 刺激の同一性を厳格化 —
  file_segment は asset_sha256 + 区間両端、generated_noise は
  realization_seed を要求。crest factor / defect_state を記録。
- `AdaptiveTransferEstimate`（ate-）: profile+刺激+帯域別
  excitation_support を束縛。system_changed_during_adaptation は
  stationarity 証拠 ref を要求。
- `ResidualEvidence`（are-）: 残差を成分分解で保持。
  adaptive_residual_tdn_like は sweep THD と比較不可を強制
  （comparable_to_sweep_thd=False）。
- `evaluate_adaptive_claim` → 推定なし → no_estimate、手法不適格
  → unqualified_method、状態変化 → stationarity_violated、
  非同期クロック → clock_limited、刺激欠陥 →
  stimulus_defect_limited、部分帯域 → band_limited_estimate。

根拠: Fattah & Zhu 1997（FSAF）、REW 5.40 の adaptive/FDAF 実装
観察 — 推定値は推定器・励起スペクトル・収束状態に強く依存するため
claim は必ず手法レコードに束縛する。

## #663 ライブデュアルチャンネル TF/コヒーレンス（P2）

- `LiveTransferFunctionSession`（lts-）: reference_kind
  （electrical_loopback / acoustic_loopback / digital_bus …）、
  参照+測定チャンネル、サンプルレート、クロックトポロジを宣言。
  'unknown' 参照種別は拒否。
- `DualChannelTFObservation`（dto-）: estimator（h1/h2、
  provider_defined は推定器不適格扱い）、MTW 法、平均化方式 —
  いずれも 'unknown' 拒否。qualified_capture は
  tf_payload/delay/coherence/timebase/stimulus_state の5 ref を
  必須にして「表示だけでなく状態証拠」を pin。
- `CoherenceObservation`（coh-）: 生トレース ref 保持 + 原因
  分類（low_measurement_snr 等）。
- `ReferenceDelayTrack`（rdt-）: auto_found / auto_tracked /
  manual の遅延追跡。auto_tracked で masked_change 疑い時は
  発生元観測 ref を要求。
- `evaluate_capture_claim` → セッション/観測なし →
  ephemeral_only、provider_defined → estimator_unqualified、
  async_uncorrected → clock_limited、マスク変化疑い →
  tracker_masked_change、delay 未評価 → delay_unqualified。

根拠: Smaart 系のライブ TF ワークフロー（delay tracking +
coherence + captured snapshot）— ライブ表示は証拠でなく、
qualified_capture は全依存状態を束縛しないと再現不能。

## #658 マイクアレイ空間サンプリング（P2）

- `MicrophoneArrayGeometry`（mag-）: 位相形状（linear/curved/
  planar/binaural/…）、≥2 素子 ref、測量位置、最小/最大間隔、
  開口を宣言 — #266 の同期チャンネル権威に「幾何」を追加。
- `SpatialSamplingCapability`（ssc-）: sync_capability
  （magnitude_only_array vs coherent_beamforming_eligible —
  'unknown' 拒否）、伝搬モデル、帯域別状態
  （well_sampled / spatial_aliasing_risk /
  low_frequency_resolution_limited / transition）。
- `BeamformingTransform`（bft-）: アルゴリズムと steering
  モデルを宣言（両方 'unknown' 拒否）。direction_supported
  出力は psf_evidence_ref（点広がり関数証拠）が必須。
- `evaluate_spatial_claim` → 変換なし → no_transform、
  sync 不足 → sync_insufficient、モデル不整合 → model_mismatch、
  エイリアス帯域 → spatial_alias_ambiguous、開口不足 →
  aperture_limited、複経路未解決 → multiple_paths_unresolved、
  重複イベント → temporal_overlap_limited。

根拠: Van Trees, Optimum Array Processing — 空間サンプリングは
間隔・開口・帯域の関数。同期済みであることは無曖昧再構成を
意味しない（#266 の補完）。

## #662 スピーカー電気インピーダンス/T-S 測定権威（P2）

- `ImpedanceMeasurementProfile`（zmp-）: target_kind —
  active_device_input は別ドメインとして明示拒否 —
  手法（calibrated_dual_channel_divider / constant_current 等）と
  端子 ID、サンプルレート。
- `ImpedanceCalibrationState`（zcs-）: open/short/
  reference_load_cal の手順記録。基準負荷校正は実測値+
  不確かさ必須。
- `MeasuredLoadEvidence`（zle-）: evidence_class
  （measured_complex_load / measured_magnitude_only_load /
  manufacturer_catalog…）・signal_domain（small/large signal）。
  measured_complex_load は phase_trace_ref + calibration_ref 必須。
- `ThieleSmallDerivation`（tsd-）: added_mass/dual_added_mass は
  質量+不確かさ、sealed_box は容積+不確かさ必須。
  model_fit_state を宣言。
- `evaluate_load_claim` → 証拠なし → no_evidence、校正なし →
  calibration_missing、完全体端子（クロスオーバ越し）への T/S
  → derivation_ineligible_target、低域不足 →
  insufficient_low_frequency_data、大型信号証拠 → state_limited。

根拠: IEC 60268-5 / Thiele 1971 — T/S 導出は小信号・可逆・
ドライバ単体を前提。回路モデルフィットの有効性は残差と
入力レンジに限定される。

## 残件

- 推定器・ビームフォーマ・T/S フィットの実体アルゴリズムは
  未実装 — 本トラックは証拠権威レコードのみ。
- coherence→品質判定・PSF 生成は証拠 ref のゲート止まり。
- live TF セッションの UI 配線（capture bridge）は既存 UI 層の
  別 track。
