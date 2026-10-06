# REV59-AUDIO2 — 知覚連続性/可聴性/結合/固体伝搬/リマップ

schema v68・`cad_audio_perception_repository`（6ストア）・
`test_rev59_audio2.py`（24テスト）。

## #652 パンニング連続性（P1）

- `PanningContinuityEvidence`（pce-）: 特定 ch ペアの音色/レベル/
  時間差を測定に束縛（'unknown' stimulus 拒否）。
- evaluate: **ch 別校正は ch 間連続性を証明しない**
  （per_channel_is_not_interchannel）。

## #669 サブウーファー定位（P1）

- `SubwooferLocalizationProfile`（slp-）: crossover + 分離角 +
  台数 + stimulus + 歪漏洩境界 + delay/alignment ref を宣言。
- evaluate: **「80Hz 以下は定位不能」の固定ルールは誤用**
  （fixed_rule_misapplied）— detectability は複合要因依存
  （AES リスニング研究根拠）。漏洩未境界・delay 未 pin は
  localization_context_unqualified。

## #657 群遅延可聴性（P1）

- `GroupDelayAudibility`（gda-）: ピーク値+周波数+stimulus+
  閾値 ref + 測定 ref を必須 — ピーク値だけでは可聴性判定
  でない（peak_is_not_audibility）。
- 補正で群遅延数値を下げても閾値文脈が無ければ改善未検証
  （lower_is_not_better）。

## #702 ヘッドホン結合（P1）

- `HeadphoneCouplingEvidence`（hpc-）: 機種 + 補償種別 + 装着
  状態 + フィルタ ref を必須 — 'unknown' 拒否、uncompensated
  や nominal_placement での個別補償 claim は失格。
- evaluate: **正確な BRIR/HRTF レンダは鼓膜に届かない**
  （brir_is_not_eardrum）— HpTF はリスナー・再装着で変動。

## #653 固体伝搬（P1）

- `StructurebornePath`（sbp-）: 振動源（sub/cabinet/fan/rack/
  tactile）+ マウント結合 + 受信室を宣言 — 'unknown' 拒否。
- evaluate: **空気伝搬遮断は固体伝搬を含まない**
  （airborne_is_not_structureborne）、rigid 結合+証拠なしは
  隣室励起 unbounded。ISO 10848-1 根拠。

## #664 空間リマップ（P1）

- `SpatialRemappingEvidence`（srm-）: リマップモード（2d/3d/
  automatic）+ 実測位置 + 参照レイアウト + ファントム着地点
  検証 — 'unknown' 拒否、実測位置なしは fail。
- evaluate: 理想配置のみのリマップは拒否
  （measured_positions_required）、宣言リマップの着地点未検証
  は remap_unverified。Trinnov 2D/3D Remapping 根拠。

## 残件

- 実測検出ルーチン（連続性スイープ解析、群遅延閾値表、
  リマップ着地点測定）は別途 — 権威は証拠ゲート層。
