# REV59-DISPLAY3 — 表示結果忠実度/計測能力/表示壁境界

schema v67・`cad_display_authority_repository`（7ストア）・
`test_rev59_display3.py`（25テスト）。

## #660 表示グラデーション（P1）

- `DisplayedGradationObservation`（dgo-）: pin した刺激 + レンジ
  意味論（full/limited — 'unknown' 拒否、レンズ不整合自体が
  バンディング源）+ 観測結果。
- evaluate: **10-bit/HDR レポートは表示グラデーションを証明
  しない**（nominal_spec_is_not_result）。

## #688 色立体積（P1）

- `ColourVolumeMeasurement`（cvol-）: IDMS v1.3 gamut-volume
  メソッド + 色空間（L*a*b*/ICtCp）+ 測定 ref 必須。coverage 比は
  参照体積 ref 必須。
- **2D 三角形カバレッジは輝度依存の色体積を記述しない**
  （triangle_is_not_volume）。

## #672 設置空間解像（P1）

- `SpatialResolutionEvidence`（sres-）: contrast_modulation /
  mtf_slanted_edge / line_pattern メソッドを設置チェーンに束縛。
- **公称4Kラスタは実際の解像ではない**
  （raster_is_not_resolution）。IDMS v1.3 根拠。

## #756 低輝度/迷光計測（P1）

- `LowLuminanceCapability`（llc-）: 最小輝度・ダークオフセット・
  迷光対策（elimination_tube/mask/frustum/instrument_corrected）
  を宣言 — 'unknown' 拒否、'none' は最小輝度宣言必須。
- **校正済み計器は DUT 黒レベルで自己ベーリンググレアを読む**
  （calibrated_meter_is_not_black_evidence）。

## #759 ダイナミックコントラスト（P1）

- `DynamicContrastMeasurement`（dcm-）: 刺激 + 制御モード +
  適応履歴を pin。'dynamic_advertised' は制御モード+履歴必須で
  **動的値は安定した物理量でない**（広告文脈のみ）。

## #760 表示壁音響境界（P1）

- `DisplayWallBoundary`（dwb-）: 壁種別/面積/背後スピーカー配置。
  透過 claim の背後配置は測定透過証拠必須 — **マーケティング
  透明性は音響でない**（transparency_unmeasured）。
- `WallAcousticImpact`（wai-）: TL/反射/SBIR を測定に束縛。
- evaluate: **映像壁は数m²の音響境界**
  （video_wall_is_also_acoustic）。

## 残件

- 実測ルーチン（IDMS ボリューム計算、コントラスト変調解析、
  透過測定）は別途 — 権威は証拠ゲート層。
