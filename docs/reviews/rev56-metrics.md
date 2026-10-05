# REV56-METRICS — バックグラウンドノイズ指標権威 + STI + ラウドネス権威

スコープ: issues #580 (P1), #605 (P1), #607 (P1)
ブランチ: `devin/rev56-metrics`
スキーマ: native schema v27 → v28（12 テーブル + 14 インデックス追加;
v27 は REV56-TARGETS が確保 — マージ時に再採番済み）

## 実装範囲

### #580 バックグラウンドノイズ指標権威

新規 `backend/src/htdt/cad_room_noise_metrics.py` +
`cad_room_noise_metrics_repository.py`（テーブル
`cad_room_noise_metric_profiles` / `cad_background_noise_measurements` /
`cad_noise_criterion_evaluations`）。

- `RoomNoiseMetricProfile`（`rnprof:` sha256 封印）— メトリクス系統
  `nc` / `ncb` / `rnc` / `rc_mark_ii` / `a_weighted_level` / `custom` /
  `other` のクローズドタクソノミー。曲線表・採用帯域・評価手順
  （`sil_designation` / `tangency` / `rc_mark_ii` / `scalar`）・適用
  レベル帯・制限を標準版ごとに pin。シードは NC-2026 (S12.2-2026
  Table)、NC-2019、NCB-1995（Beranek JASA 1989 Table 1 + 振動領域
  A/B マーカ）、RC-II、A特性、dBA 相対レベルの 6 プロファイル。
- `BackgroundNoiseMeasurement`（`bnm:` 封印）— 運転状態
  （`NoiseOperatingState`: hvac_state / projector_state /
  occupancy_state / door_window_state）、時間的クラス
  （steady / slowly_varying / low_frequency_fluctuating /
  impulsive）、測定経路 `NoiseAcquisitionContext`（校正器 refs・
  窓関数・規格版・機器チェーン）、位置ごとの 1/1 オクターブ
  スペクトル（`NoiseBandLevels`、16 Hz–8 kHz、
  `lf_time_domain_correction_db` で RNC 低域補正を保持）を全保存。
  `level_semantics` が `absolute_spl` 以外の位置は評価を `limited`
  に降格 — 相対レベル証拠を定格しない。
- `NoiseCriterionEvaluation`（`ncev:` 封印）— 測定とプロファイルの
  sha256 pin を必須化。評価系統ごとの専用評価器:
  - **NC**（S12.2-2026 Annex D 2ステップ手順）: SIL 帯域
    (500/1k/2k/4k) 平均 → `NC-<round(SIL)>` の SIL 指定値。超過帯域が
    あればタンジェンシー経路で `NC-<N> (<band> Hz)` と支配帯域を保持
    （NC-30 形状スペクトル → NC-31、125 Hz 隆起 → NC-45 (125 Hz)）。
  - **NCB**（Beranek 1989）: SIL 帯域で補間曲線照合、`rumble` /
    `hiss` / `vibration` のバランス修飾子を独立列挙（63 Hz 帯 >+3 dB
    で rumble 等）。NC 値への縮退はしない。
  - **RC Mark II**（S12.2 Annex B 系）: MF 帯域 (500/1k/2k) 算術平均 →
    RC-<MF>、LF/HF 偏差で N（中立）/ R（ランブル）/ H（ヒス）/
    Q（QA: クエリーブランチ — 中域偏差 +3 dB 超）領域診断を保持。
  - **A特性** — dBA 値は独立スカラー指標として併記、曲線系統の
    代替にはならない。
  - **RNC** — Blazier 曲線表がレジストリ未登録のため fail-closed:
    `rnc_curve_table_not_registered` 制限で `limited`（補正あり）
    または `out_of_scope`（低域時系列補正なし）。
- 適用外判定: 低域変動騒音は NC/NCB/RC では `out_of_scope`
  （時間定常状態のみ有効）。運転状態が全 unknown → `limited`。
- 集計は worst-position（最悪位置が勝つ）。空間的ばらつきが閾値を
  またぐ場合は `spatial_spread` 制限注記。
- 閾値判定は #577 系ガードバンド: `target_rating` に対し測定
  不確かさ `uncertainty_db` の ±帯内は `indeterminate_guard_band`
  であって pass/fail 捏造しない。
- `assert_no_scalar_substitution` — NCB 評価を NC として引用する等の
  系統跨ぎをハードエラー化。`profile_for_rp22` は RP22 が pin する
  NCB-1995 プロファイルのみを返す。

### #605 音声明瞭度（STI）権威

新規 `backend/src/htdt/cad_sti_authority.py` + `cad_sti_repository.py`
（テーブル `cad_speech_intelligibility_profiles` /
`cad_sti_measurements` / `cad_sti_predictions` /
`cad_dialogue_intelligibility_assessments`）。

- `SpeechIntelligibilityProfile`（`stiprof:` 封印）— IEC 60268-16
  edition 2020+COR1:2025（α 重み男女 7帯域）と legacy 2011
  （female_legacy 重みを別名保持 — 代入互換なし）。メソッド:
  `direct_sti_measurement` / `impulse_response_derived` /
  `stipa_indirect`（派生系）/ `simulated_sti`。7 オクターブ帯域
  （125–8000 Hz）× 14 変調周波数（0.63–12.5 Hz）を固定宣言。
- MTF 経路: `mtr_to_snr_db`（m→SNR 変換 = 10·log10(m/(1−m))、±15 dB
  クリップ）→ TI=(SNR+15)/30 → 帯域平均 TĪ →
  `STI = Σ α_k·TĪ_k − Σ_{k=1..6} β_k·√(TĪ_k·TĪ_{k+1})`
  （Steeneken Table II の隣接帯域冗長性項のみ）。完全伝送で
  1.381−0.381 = 1.0 を厳密に回復する回帰テストあり。
- IR 派生 `mtr_from_band_ir`: 帯域エネルギー時系列 h² の離散変調
  転送 `m(f_m)=|Σ h²·e^{-j2πf_m·n/fs}|/Σh²`。テストは正弦変調
  （整数サイクル窓で A/2 を厳密回復）と指数減衰（幾何級数閉形式
  +2% 以内）で数値検証。
- `STIMeasurement`（`stim:` 封印）— **#580 騒音測定 pin が必須**
  （noise_measurement_id は `^bnm:` パターン + sha256）。宣言必須の
  `STIPathContext`（signal_path / voice_position / mic_position /
  seat_ref / speech_level_db / signal_chain_class）。`simulated_*`
  メソッドは測定レコードとして封印不可 — シミュレーションは
  `STIPrediction` のみ。
- `STIPrediction`（`stip:` 封印）— `model_version` + **#566
  バリデーション pin が必須**（validation_ref なしでは封印失敗）。
  `predicted_not_measured` 制限を自動付記。
- `DialogueIntelligibilityAssessment`（`dia:` 封印）— シートごと
  `STISeatResult`（sti_value + uncertainty + evidence_ref + occlusion
  フラグは独立保持）を保存し、worst / P10 パーセンタイルを集計。
  閾値の普遍基準は捏造しない — `target_class` は
  `measurement_standard` / `project_target` /
  `external_application_criterion` の宣言制。
- `compare_sti_evidence` — before/after 比較は
  path/method/profile/noise バインド/レベル/校正状態の不一致ひとつで
  `incomparable`。同一経路のみ `improved` / `degraded` /
  `unchanged_within_uncertainty`。
- 制限: `signal_chain_class` が codec/vocoder 系 →
  `sti_not_validated_for_compressed_chain`；変動騒音 →
  `sti_valid_for_captured_steady_state_only`。STI は周波数応答目標と
  別権威で、置換・冗長化しない旨をドキュメント化。

### #607 コンテンツラウドネス / 正規化権威

新規 `backend/src/htdt/cad_loudness_authority.py` +
`cad_loudness_repository.py`（テーブル
`cad_content_loudness_profiles` / `cad_programme_loudness_measurements`
/ `cad_normalization_observations` / `cad_playback_gain_states` /
`cad_loudness_matching_records`）。

- `ContentLoudnessProfile`（`ldnprof:` 封印）— ITU-R BS.1770-5
  （production_current）、BS.1770-2026-draft（research_only —
  ドラフトの適格性を強制）、AES77-2023、EBU R128 v5.0 の独立
  プロファイル。`channel_config` は stereo / multichannel_5_1 /
  extended_layout / object_based。
- `compute_integrated_loudness_lufs` — BS.1770 K-weighted ブロック
  パワーの絶対ゲート（−70 LUFS）と相対ゲート（−10 LU）の 2 段
  ゲーティングを実装。`loudness_channel_weights` はサラウンド 1.41、
  LFE 除外（5.1 = 5 チャンネル重みのみ）。`compute_true_peak_dbtp`
  は Annex 2 oversampling 後のピークを dBTP で返す。
- `ProgrammeLoudnessMeasurement`（`plm:` 封印）— プロファイル sha256
  pin + `profile_eligibility`（evaluate_loudness_profile_eligibility:
  draft → research_only、immersive コンテンツは extended/object
  プロファイル必須 → stereo プロファイルでは
  `immersive_content_requires_extended_or_object_profile`）。
  LUFS/LRA/true-peak/momentary/short-term max を別フィールドで保持。
- `NormalizationObservation`（`norm:` 封印）— `mode` が `off` の
  観測は `applied_gain_db` を持てない（オフモード観測が適用ゲインを
  主張するのは封印時ハードエラー）。
- `PlaybackGainState`（`pgs:` 封印）— 正規化観測への参照、マスター
  ボリューム、室内実測 SPL を分離保持。`normalization_observation_id`
  指定時は観測行の存在をコミット順で強制。
- `normalization_audit_diff` — 適用ゲイン差 vs 実測 SPL 差の
  デルタを比較し、`metadata_claim_only` /
  `partially_realized_in_room` / `fully_realized_in_room` /
  `gain_unclaimed` の監査結果を返す。メタデータ正規化と室内実効
  レベルの乖離を顕在化。
- `assert_quantity_separation` — 13 数量のクローズドタクソノミー
  （programme_integrated_loudness / loudness_range / true_peak /
  momentary_max / short_term_max / normalization_gain /
  playback_gain_state / in_room_spl / capability_spl /
  calibration_level / listening_level / dialogue_level /
  other_declared）。LUFS→SPL・能力変換は `calibrated_chain_ref`
  なしではハードエラー。`LoudnessMatchingRecord`（`lmr:` 封印）は
  2 コンテンツ間の整合比較を同一数量・同一測定窓で記録。

### 共通配線

- `native_row_integrity.py` — 12 テーブルの行投影バインドを宣言
  （`seat_count` は行専用導出列として意図的に未バインド）。
- `native_authority_audit.py` — `_RepositoryChain` に
  `room_noise_metric` / `sti` / `loudness` を追加、12 リプレイ
  プローブを登録。
- `cad_external_standards.py` — ansi-asa-s12-2@1995（superseded、
  RP22 v1.2 の NCB 系統 dependency ref）、itu-r-bs1770@5、
  itu-r-bs1770@2026-draft（draft / discovered）、aes77@2023、
  ebu-r128@v5.0 の 5 シード。
- `application_pages.py` — 12 テーブルの JA ラベル。
- 全リポジトリは `_assert_sealed` で封印整合性を保存前に再検証 —
  `model_copy` 由来の陳腐 sha 偽造ペイロードは IntegrityError。
- コミット順序強制: 評価 → 測定+プロファイル、測定 → プロファイル、
  ゲイン状態 → 観測（名前付きのとき）。

### main 破損の同梱修正

`measurement_evidence_display.py` — REV56-TARGETS マージ（origin/main
3f6551c6）で `spatial_binding_line` の `return (` 閉括弧が落ちて
構文エラーになっていたのを修正（3 行追加のみ）。

## 文献根拠

- **ANSI/ASA S12.2-2019/-2026** — NC 曲線表（Table 1）、SIL 帯域
  定義、NC 評価の 2 ステップ（SIL 指定値 → タンジェンシー）手順、
  RC Mark II の MF 帯域平均と N/R/H/Q 領域診断（Annex B/D）。
- **Beranek, JASA 1989 (NCB)** — NCB 曲線表 Table 1、SIL 帯域
  (500–4k) の補間照合、rumble/hiss バランス修飾子、低域振動領域 A/B。
- **Blazier (RNC)** — RNC は曲線表未登録のため fail-closed 枠組み
  のみ。100 ms 低域時系列補正を `lf_time_domain_correction_db` で
  表現可能にした。
- **IEC 60268-16 ed5 (2020) + COR1 (2025)** — STI 定義、14 変調
  周波数、7 オクターブ帯域、α/β 重み（Steeneken & Houtgast Table
  II: male β は 6 項のみで 7 項目は 0）、SNR→TI 変換と ±15 dB
  クリップ、MTI 平均、IR からの MTF 導出。2011 版は
  female_legacy 重みのレガシープロファイルとして保持。
- **ITU-R BS.1770-5 (11/2023)** — K-weighting 定数（shelf f0
  1681.97 Hz / Q 0.707、HP f0 38.14 Hz / Q 0.500、+4 dB shelf、
  −0.691 オフセット）、絶対 −70 LUFS + 相対 −10 LU ゲーティング、
  チャンネル重み（サラウンド 1.41、LFE 除外）、Annex 2 true-peak、
  Annex 3/4 の拡張・オブジェクト系。
- **EBU R128 v5.0 / AES77-2023** — 正規化・配信側ガイドラインの
  独立プロファイルとして登録（番組測定・室内レベルと混同しない）。
- **CEDIA/CTA-RP22 v1.2** — ノイズフロア要求が NCB 系統を pin
  する旨の dependency_refs を ansi-asa-s12-2@1995 側に明記。

## 残存事項

- RNC 曲線表（Blazier）のレジストリ登録と低域 100ms 時系列補正の
  計測導線 — 現在は制限付き fail-closed。
- STI の聴覚マスキング項（absolute reception threshold）と
  RA-STI 縮退版、実測 IR パイプラインからの帯域 ETF 抽出の配線。
- 85 dBC リファレンス等の再生系較正チェーン実測供給
  （calibrated_chain_ref の実体）とストリーミング正規化の実測
  ingest 配線。
- UI 配線は最小限（監査ページの JA ラベルのみ）— 測定ページへの
  組み込みは残件。
- `test_application_pages.py::test_shell_registers_application_*
  destinations` 2 件は本スコープ以前からの main 破損
  （presentation/video ワークスペース未登録）— 別セッションの
  対象。
