# REV58-MEASCHAIN — 測定チェーン線形性/過負荷 + swept-sine 畳込分離 + 室音響測定ソース権威

スコープ: issue #695 / #697 / #668 (全て P0)
ブランチ: `devin/<ts>-rev58-measchain`（PR 作成後に確定、merge-test 経由マージ）
スキーマ: native schema v42 → v43（10 テーブル + インデックス追加）

## 実装範囲

### #695 測定チェーン線形性/過負荷権威

新規 `backend/src/htdt/cad_measchain_linearity.py` +
`cad_measchain_linearity_repository.py`（テーブル
`cad_measchain_linearity_profiles` / `cad_measchain_overload_observations` /
`cad_measchain_qualifications`）。

- `CadAcquisitionStage` — mic カプセル→内蔵電子部→外部プリアンプ→パッド→
  アナログ I/F 入力→ADC→ドライバ/取得ソフトの**順序付き**ステージグラフ。
  カノニカル順序（`acoustic→digital`）からの逆行はモデルバリデータで拒否 —
  順不同チェーンは「どの段が先にクリップするか」を不可視にするため。
- `CadUpperLevelSpec` — 正確な max-SPL 意味論を保持:
  `threshold_kind`（thd_3pct / thd_1pct / clip_point / manufacturer_max_spl）、
  `level_semantics`（peak/rms）、`signal_class`（sinusoidal/broadband/impulse）、
  周波数帯と証拠基底（`STRONG_LINEARITY_BASES` =
  manufacturer_specification / laboratory_measurement /
  standard_qualification / field_two_level_check のみ — `assumed`/`unknown`
  は線形性証拠にならない）。宣言帯域の外は何もカバーしない —
  1 kHz の適格は 30 Hz 高圧用途へ外挿しない。
- `CadDynamicProcessingBlock` — AGC / limiter / noise_suppression /
  auto_range_switching / SRC / HPF を段インデックス付きで宣言。
  `enabled`/`cannot_disable` は「レベル圧縮中」として高 SPL・DUT 非線形・
  ピーク過渡クラスを `nonlinear_measurement_ineligible` に倒す。
- `CadTwoLevelCheck` — ≥2 測定レベルのゲイン変更診断。
  `not_performed` 以外は少なくとも 2 レベル必須。
- `CadAcquisitionOverloadObservation`（mclobs- 封印）— 過負荷機構を型付き化:
  analog_front_end_clip / adc_numeric_full_scale /
  adc_internal_overload_spurious / software_dsp_limiting を混同しない。
  `no_overload_observed` はピーク読取・インジケータ状態・段ローカライズ
  のいずれかの証拠を必須化（クリーンなデジタルピークは上流アナログ段の
  線形性を一切証明しない）。確定機構はローカリゼーション証拠を必須化。
- `CadMeasChainQualification`（mclqual- 封印）+ `evaluate_measchain_qualification`
  — fail-closed ラダー: 確定過負荷 → 全 capability invalid +
  `chain_overload_confirmed` → アクティブ DSP + 高レベル要求 →
  `nonlinear_measurement_ineligible` → 疑い → `overload_suspected` →
  帯域/証拠なし高レベル要求 → `unqualified_insufficient_evidence` →
  要求レベルが証拠エンベロープ超過 → `overload_suspected` →
  two-level 線形 → `chain_qualified_within_declared_range`。
  8 capability 全件報告（省略は「問題なし」と読ませない）+
  `chain_overload_not_excluded` を正直な逆方向フラグとして維持。
  `distortion_attribution` — SPL プラトーをラウドスピーカー圧縮に自動帰属
  しない。
- #1007 `cad_input_chain_capability.py`（非スキーマ登録のインメモリ
  プロトタイプ）との関係をモジュール docstring に明記 — 本権威は
  封印・永続化・per-band 証拠を持つ正式版であり、プロトタイプの
  置き換えではなく共存関係を文書化。

### #697 Swept-sine 畳込分離/高調波分離権威

新規 `backend/src/htdt/cad_sweep_deconvolution.py` +
`cad_sweep_deconvolution_repository.py`（テーブル
`cad_sweep_deconvolution_specs` / `cad_harmonic_impulse_components` /
`cad_recovered_impulse_responses` / `cad_linear_ir_capabilities`）。

- `CadSweepDeconvolutionSpec`（swspec- 封印）— #608 stimulus sha ピン必須 +
  sweep law/帯域/duration/rate + アルゴリズム同一性（farina_inverse_filter /
  regularized_division / time_domain_least_squares / synchronized_swept_sine /
  imported_unknown）+ 実装バージョン + 逆フィルタ構成 + 正規化 +
  FFT ブロック/ゼロパディング/正則化 + リサンプリング宣言 +
  時間原点規約 + 出力スケール。**同じ UI ラベルの 2 つの log sweep は
  duration/span が違えば別の測定方法**として封印が別れる。
- `harmonic_offset_s(order)` / `harmonic_valid_band(order)` — 高調波次数の
  時間オフセットは厳密な sweep law から Δt_n = −T·ln(n)/ln(f₂/f₁) で導出
  （手書き窓からの推定は不可）。次数 N の有効基本波帯域は f ∈ [f₁, f₂/N] —
  興奮帯域を超える HDn は fail-closed。
- `CadHarmonicImpulseComponent`（harmn- 封印）— 次数ごとの抽出窓・有効基本波
  帯域・`overlap_state`（separated / partial_overlap / contaminates_causal /
  inseparable / unknown）。
- `CadRecoveredImpulseResponse`（recir- 封印）— `derived_full_provenance`
  は spec+raw capture+full-response ダイジェストを必須化（線形抽出は
  full response の上書きではなくビュー）。`imported_final_only` は spec/
  成分を禁じ `source_tool` を必須化 — REW 等の外部 IR は不明な
  deconvolution provenance を正直に保持する。
- `CadLinearIRCapability`（lircap- 封印）+ `evaluate_linear_ir_capability`
  — 汚染状態（linear_ir_clean_within_declared_window /
  nonlinear_components_separated / partial_overlap /
  causal_nonlinear_contamination_risk / inseparable /
  insufficient_evidence）+ clock gate（#609 由来:
  unsynchronized_declared → absolute_phase invalid + early reflections
  限定）+ chain gate（#695 由来: overload_observed は高調波成分が測定
  チェーン産物の可能性として全 capability 降格）。**非線形成分が常に
  t<0 に安全にあるとは仮定しない**（Ćirić et al. の因果部混入を評価）。
  6 メトリック（fr / direct_arrival / early_reflection / decay / clarity /
  absolute_phase）全件報告。

### #668 室音響測定ソース権威

新規 `backend/src/htdt/cad_excitation_source.py` +
`cad_excitation_source_repository.py`（テーブル
`cad_excitation_source_profiles` / `cad_source_orientation_captures` /
`cad_measurement_source_qualifications`）。

- `CadExcitationSourceProfile`（srcpro- 封印）— デバイス/モデル/インスタンス/
  形状/指向性データセット sha ピン/姿勢（位置+方位+チルト — 実オムニは
  高域で指向化するため姿勢は測定同一性の一部）/駆動パス/DSP-EQ/出力レベル
  能力/#608 stimulus ピン/回転手順。`measurand_class`（標準オムニ室応答 /
  近似オムニ室応答 / 設置スピーカー伝達 / 設置チャンネル系応答 /
  リファレンスソース伝達 / 外部不明ソース）は測定同一性に参加 —
  installed_loudspeaker_channel + standardized_omni claim はモデル段で拒否。
- `CadOmniBandCapability` — 帯域ごとのオムニ能力状態（omni_profile_verified /
  omni_within_profile_band / limited_directivity / directional_source /
  unknown_directivity）。オムニ状態は `laboratory_measurement` /
  `manufacturer_balloon_data` / `standard_conformance_test` /
  `field_verification` の強証拠を要求 — **ドデカ面体の外形だけでは
  オムニ性を推定しない**。
- `CadSourceLevelCapability` — 最大テストレベル/リミッタ状態/達成減衰
  レンジ/SNR マージン。リミッタ作動中は `insufficient`、証拠なしは
  `unknown` — 目標ダイナミックレンジのためにソースを安全限界超で
  駆動する選択肢は存在しない。
- `CadSourceOrientationCapture`（srcori- 封印）— 回転/平均測定の各取得を
  不変レコードとして保持。`aggregate_result` は #575 transform sha ピンを
  必須化 — 平均を直接の物理 IR と読み替えない。
- `CadMeasurementSourceQualification`（srcqual- 封印）+
  `evaluate_source_qualification` — 目的別 eligibility:
  standardized_room_characterization / installed_system_diagnostics /
  spatial_impression_measurement / strength_g_measurement /
  simulation_validation_comparison を個別に判定（wrong_source_class /
  directivity_out_of_profile / insufficient_source_level /
  source_state_unknown を含む）。強度 G は標準/リファレンスソース +
  omni_profile_verified + 十分レベルのみ eligible。シミュレーション比較:
  実測ドデカ↔理想オムニは検証帯域内のみ comparable（帯域外は
  insufficient_evidence）、設置チャンネル↔理想オムニは
  wrong_source_model — 不整合を室材料に吸収させない。
- ISO/DIS 3382-1 Ed.2（DIS 段階）は RESEARCH_ONLY として扱い、ドラフト
  要件への暗黙読替をしない。

## 文献根拠

### #695 測定チェーン
- IEC 61094-4:1995 — ワーキング標準マイク: 感度校正と動作レンジ上限
  （160 Hz–1 kHz で 3% THD の SPL）及び感度レベルの線形範囲を分離。
  感度校正は線形性証拠ではない。
- IEC 61672-1/-2/-3 — サウンドレベルメータ: レベル線形性・過負荷挙動・
  パターン評価・定期検証は独立した性能特性。
- IEC 60268-4:2018 — 音響システム用マイク: ダイナミックレンジは感度校正の
  帰結ではなく第一級の測定量。
- AES E-Library id=6851 *Practical Considerations on Overload-Distortion of
  ADCs* — ADC 過負荷は非高調波/スプリアス成分を生成し、DUT 歪への誤帰属を
  招く。
- KLIPPEL TBM ドキュメント — 高レベル音響試験はアンプだけでなく**マイク**
  飽和でも制限され得る。

### #697 畳込分離
- Farina, AES 108th Paper 5093 (2000) — exponential sweep deconvolution は
  線形 IR と高調波歪インパルス応答を時間分離する。
- Novak/Lotton/Simon, JAES 63(10) (2015) — 同期 swept-sine: 高次高調波の
  正確な解析には再生/取得の同期が必須。
- Ćirić et al., Applied Acoustics 74(3) (2013) — 非線形アーティファクトの
  一部は復元 IR の**因果部**に侵入し室音響パラメータを偏らせる。

### #668 励起源
- ISO 3382-1:2009 — 測定ソースは実用的に可能な限り無指向性であること、
  有効な減衰測定に十分なレベル/ダイナミックレンジが必要。
- ISO/DIS 3382-1 Ed.2 — ドラフト段階: RESEARCH_ONLY として追跡。
- ISO 3382-2:2008 — 設置 HT スピーカーは標準励起源に自動適格しない。
- Applied Acoustics ソース指向性研究群 (S0003682X07001508 /
  S0003682X06002209 / S0003682X17300580) — ソース指向性は T30/C80 を変え、
  実ドデカは使用可能オムニ帯域以上で指向化し回転の影響を受ける。
- MDPI Appl. Sci. 9(18):3705 (2019) + DOI 10.3397/IN_2025_1076868 —
  実測ドデカ vs 理想オムニシミュレーション: 平均 RT は近いが C80 は
  2 kHz 以上で差が拡大 — 近似妥当性は帯域制限される。

## スキーマ登録

- `cad_schema_ddl.py`: 10 CREATE TABLE + 11 CREATE INDEX を
  `NATIVE_BASELINE_DDL` 末尾に、`NATIVE_SCHEMA_TABLES` に 10 テーブル登録。
- `cad_schema.py`: `NATIVE_SCHEMA_VERSION = 43`、`_migrate_42_to_43`、
  `_MIGRATIONS[43]`。
- `native_row_integrity.py`: 10 テーブルの `_ROW_BINDINGS`（全 mirrored
  カラム — ネスト `AuthorityRef` は `ref_id` パス、nullable は
  `optional=True`）。
- `native_authority_audit.py`: `_get` ローダー 3 件
  （measchain_linearity / sweep_deconvolution / excitation_source）+
  `_ReplayProbe` 10 件。
- `application_pages.py`: `_LIFECYCLE_TABLE_LABELS` に JA ラベル 10 件。
- `measurement_evidence_display.py`: JA 表示行 3 関数
  （measchain_qualification_line / linear_ir_capability_line /
  source_qualification_line）+ ラベル辞書。
- `backend/tests/test_cad_schema.py`: 移行台帳 `(43, 'migrate native
  schema to v43')`。

## テスト

- `backend/tests/test_rev58_measchain.py` — 24 テスト全グリーン:
  ステージ順序/インデックス境界、証拠なし過負荷拒否、確定過負荷の
  全 capability 無効化、隠れ AGC の高レベル不適格、帯域外外挿の
  fail-closed、two-level 判定、Farina オフセット導出、provenance
  クラス検証、汚染/ゲート行列、オムニ証拠強制、measurand 一致、
  目的別 eligibility、G/レベルゲート、シム比較 verdict、集約 transform
  ピン、封印改竄検知、リポジトリ往復、append-only 共存。
- scoped pytest: `test_rev58_measchain.py` 24 passed、`test_cad_schema.py`
  + `test_cad_schema_ddl_contract.py` + `test_authority_audit_coverage.py`
  + `test_authority_integrity_s8.py` + `test_authority_lifecycle_integrity.py`
  + `test_cad_display_labels.py` グリーン、`test_rev57_metro.py` +
  `test_rev57_mount.py` 67 passed（回帰なし）。
- `assert_row_integrity_registry_complete()` パス（全 payload テーブル
  バインド済）。

## 残存事項

- #695: 実機 two-level/alternate-gain 測定キャンペーン、#651 ゲイン構造
  権威との双方向解決、#611 校正レコードから upper-level spec への自動
  参照、UI 入力フォーム。
- #697: 実 deconvolution エンジン本体（逆フィルタ生成/適用）、#609
  timebase 評価からの自動 gate 供給（現行は評価入力として
  `clock_gate`/`chain_gate` を受ける構成）、REW インポート時の
  provenance 推定、マルチチャネル同時掃引の独立オフセット検証。
- #668: 実ドデカ directivity balloon 測定、#213 レシーバー権威との
  source+receiver ペア解決、#135 G リファレンス転送の実測パイプライン、
  #581 キャンペーン設計からのソース要件自動生成。
- 共通: 本 REV58 は権威モデル+永続化+評価器の着地点。既存 ingest /
  solver 検証 / キャンペーン UI への live 配線（#564/#566 比較の実
  適用）は次の REV で行う — 本権威がゲート用の型を提供する。
