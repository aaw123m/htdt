# REV59-VIDMETA レビュー記録 — 映像計測・コーデック忠実度権威

対象 issue: #759 (P1), #756 (P1), #760 (P1), #753 (P2), #747 (P2)
着地: schema v54、新規 10 テーブル、リポジトリ `cad_media_fidelity_repository`、回帰テスト `test_rev59_vidmeta.py` 33 件

## 実装権威

### #759 `cad_projector_dynamic_light.py`
- `ProjectorDynamicLightProfile` (pdl-): 光源制御モードを pin（unknown 不可）
- `TemporalContrastMeasurement` (tcnt-): measurand 種別・刺激・光源モードを pin — sequential_dynamic は動的モード宣言必須
- `DynamicContrastQualification` (dcq-): claimed vs measured measurand が一致しなければ comparable を名乗れない
- `compare_contrast_claims`: measurand/light_mode 不一致 → incomparable — シーケンシャル動的値はネイティブや設置値の代理にならない

### #756 `cad_low_luminance.py`
- `DisplayLightMeasurementCapability` (lmc-): 測定フロア + 迷光管理状態を pin（ゼロフロア拒否）
- `LowLuminanceObservation` (llo-): 読み値 + FOV/可視輝度コンテキスト
- `evaluate_black_claim`: フロア未満 → `below_capability`（決して 0 でも無限大でもない）、迷光未管理 → contaminated
- `evaluate_contrast_claim`: フロア未満の黒 → `lower_bound_only`

### #760 `cad_display_acoustic_boundary.py`
- `DisplayAcousticBoundaryProfile` (dab-): 透過宣言 — acoustically_transparent/micro_perforated は証拠 pin 必須
- `FrontStageVariantRecord` (fsv-): 配置戦略 + スピーカー + verdict
- `evaluate_frontstage_claim`: 不透過壁への behind_screen_lcr → placement_blocked、透過不明/部分 → 証拠要求

### #753/#747 `cad_codec_fidelity.py`
- `CodecChainProfile` (cfp-): コーデックステップ列 + 隠れ処理 + レイアウト縮小を宣言（lossless/passthrough 宣言と hidden 検出の矛盾は拒否）
- `QualityMethodProfile` (qmp-): 評価方式（BS.1387-2/P.910/P.1204/J.247 等 — full_reference はリファレンス必須）
- `CodecFidelityObservation` (cfo-): chain+method+結果の pin
- `evaluate_fidelity_claim`: bit_transparent は全段 lossless/passthrough 必須、perceptually_transparent は方式証拠必須、impairment 検出は impaired に降格。再生成功は忠実度の証拠にならない

## 文献根拠
- SID/ICDM IDMS v1.3 A2/A3（迷光・ベーリンググレア、低輝度測定、能力未満をゼロと報告しない）
- AVIXA V201.01:2021（コントラストは設置系測定量）
- ITU-R BS.1387-2:2023（PEAQ）、ITU-T P.910/P.1204/J.247��映像品質モデル群）

## 検証
- `test_rev59_vidmeta.py` 33 テスト + fresh-migrate・roundtrip・tamper
- 登録面: NATIVE_SCHEMA_TABLES + DDL 10 + `_migrate_53_to_54` + `_ROW_BINDINGS` 10 + `media_fidelity` ブランチ + `_ReplayProbe`×10 + labels + JA 行 + manifest 5 issue

## 残件
- 計器からの自動輝度取込・EDID/デバイス照合は実機残件
