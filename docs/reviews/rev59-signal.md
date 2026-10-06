# REV59-SIGNAL — コーデック忠実度/スペクトル推定/クロック/外部事実/BOM

schema v69・`cad_signal_authority_repository`（6ストア）・
`test_rev59_signal.py`（27テスト）。

## #747/#753 コーデック忠実度（P1）

- `CodecFidelityEvidence`（cfx-）: media kind（audio/video）+
  codec family + 検出劣化（transcoding/ABR適応/世代劣化/レイアウト
  削減/SRC/隠れDRC/ブロッキング/テクスチャ損失/クロマ劣化/時間
  軸 artifact）+ ソース/マスター ref — 'unknown' family 拒否。
- evaluate: **再生成功は忠実度でない**（playback_is_not_
  fidelity）、非可逆 codec の透過証拠なし・lossless でマスター
  未 pin は transparency_unverified。

## #749 スペクトル推定（P1）

- `SpectralEstimatorProfile`（sep-）: 記録時間・FFT サイズ・ゼロ
  パディング・窓関数・ENBW・コヒーレントゲイン補正・リーク境界
  を宣言 — 'unknown' 窓拒否。
- evaluate: **密なビングリッドは分解能でない**（density_is_not_
  resolution）、矩形窓＋リーク未境界・ゲイン未補正は
  amplitude_uncorrected。`cad_fft_spectral_estimator_profiles`
  テーブル（measurement-setup の同名テーブルと区別）。

## #670 デジタルクロック（P1）

- `ClockDomainObservation`（cdo-）: ドメイン種（internal/
  word_clock/aes3/spdif/madi/ptp/asrc）+ ロック状態 + ASRC 有無
  — 'unknown' 拒否、relock イベントは証拠必須。#609 は測定
  タイムベース、こちらは再生チェーン。
- evaluate: 非ロックドメインは lock_unverified、ASRC 存在は
  asrc_undeclared（ビット透過でない）。

## #765 外部事実保存（P1）

- `ExternalFactClaim`（efc-）: subject + statement + source ref
  + 公開日 + 適用範囲 — 不変の主張として保存、可変 product
  行を上書きしない。
- `FactConflictResolution`（fcr-）: recency/version_scope/
  tier_scope/hierarchy で解決 — 両側保存、unresolved_conflict
  は rationale 不要、解決済みは必須。
- evaluate: 同一 subject+適用範囲で矛盾する主張は
  conflict_preserved（解決 pin があれば理由付きで両側保存）。

## #667 BOM/見積（P1）

- `BomEstimate`（bom-）: bom_version + line items +
  engineering_ref 必須 — エンジニアリング済みシステムから派生。
  価格行は supplier_ref 必須（**価格/税/労務/仕入先は技術真実と
  分離**）。Modus Docs/TCD 対抗の権威基盤。

## 残件

- 実測劣化検出（codec artifact 解析・スリップ計測・FFT 計算
  パイプライン本体・BOM 生成 UI/価格フィード連携）は別途 —
  権威は証拠ゲート層。
