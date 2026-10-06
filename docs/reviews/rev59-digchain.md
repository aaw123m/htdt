# REV59-DIGCHAIN レビュー記録 — デジタル音声チェーン権威

対象 issue: #745 (P2), #744 (P2), #739 (P2), #650 (P2)
着地: schema v53、新規 11 テーブル、リポジトリ `cad_signal_integrity_repository`、回帰テスト `test_rev59_digchain.py` 41 件

## 実装権威

### #745 `cad_clock_jitter.py`
- `SampleClockJitterProfile` (jmp-): 計器が実際にカバーする測定量を pin — ロック表示はジッタ計器ではない
- `SampleClockJitterObservation` (job-)、`JitterTransferMeasurement` (jtf-)、`ConverterJitterSusceptibility` (cjs-)
- `evaluate_jitter_claim`: ロック状態だけでは `supported_with_limitations`（`lock_only_not_jitter`）。`converter_immune` は専用感受性試験が必須 — 通常の音声測定では感受性を特徴づけられない（Dunn 1994）

### #744 `cad_wordlength_path.py`
- `DitherNoiseShapeProfile` (dns-): undithered/subtractive/non-subtractive TPDF/shaped の分類を pin（noise_shaped は次数必須）
- `DigitalPathTransformRecord` (dpt-): 各再量子化/ゲイン段を封印 — requantization は rounding_mode 宣言かディザ pin 必須
- `evaluate_low_level_claim`: パス内に undithered truncation があれば `uncontrolled` — 末端フォーマットラベルは再量子化を隠せない

### #739 `cad_playback_src.py`
- `PlaybackSrcProfile` (srcp-): レート+アルゴリズム宣言（`unknown` 拒否、等レート+非同期アルゴリズムはパススルー宣言として拒否）
- `SrcQualificationRecord` (srcq-): alias/image 除去の実測値、`ClockDomainCrossingRecord` (cdc-): 宣言 vs 観測の横断状態
- `evaluate_src_claim`: declared same_domain + observed crossing → `hidden_conversion`；未適格ステージ → `converted_unqualified`；変換存在下の exact-sample claim は不可能

### #650 `cad_interchannel_crosstalk.py`
- `InterchannelLeakageMeasurement` (xtk-): driven≠observed ペア・ステージ・方式・刺激 pin
- `ChannelSeparationQualification` (csep-): threshold/worst-case/ペア網羅の封印 verdict（qualified は全必須ペア + worst_case ≤ threshold）
- `evaluate_separation_claim`: 正しいルーティングだけでは `insufficient_evidence` — 意図的マトリクス混合は漏洩と別宣言として扱う

## 文献根拠
- AES-12id-2020（ジッタ性能仕様: wideband/baseband/period/long-term、スペクトル、PLL 伝達、コンバータ感受性）；AES-12id-R は公開まで RESEARCH_ONLY
- Dunn, AES UK 9th Conf. 1994（サンプリングジッタ↔変調積、通常測定では感受性不十分）
- AES17-2020（デジタル音声機器測定法、FS/dBFS セマンティクス；AES17-R は公開まで非現行）
- Vanderkooy & Lipshitz 1989（ゲイン/EQ/オーバーサンプリング後の再丸めは量子化歪みを再導入、適切な再ディザで線形化）
- Lipshitz, Wannamaker & Vanderkooy 1992（ディザ分類）
- Midya, Roeckner & Schooler AES 121 #6863（ASRC は低ジッタ系クロックへのドメイン横断）
- IEC 60268-3:2018（マルチチャネルアンプのクロストーク/分離・チャネル間ゲイン/位相差）

## 検証
- `test_rev59_digchain.py` 41 テスト: seal/validator/fail-closed verdict/ゲート/repo roundtrip・冪等・tamper 検出・fresh-migrate テーブル存在
- 登録面: NATIVE_SCHEMA_TABLES + DDL 11 テーブル + `_migrate_52_to_53` + `_ROW_BINDINGS` 11 + `signal_integrity` ブランチ + `_ReplayProbe`×11 + `application_pages` ラベル + `measurement_evidence_display` JA 行 + manifest 4 issue

## 残件
- 計器/チェーンからの自動観測取込（実機接続）は残件 — 権威と fail-closed 判定面のみ着地
- UI 登録経路は後続
