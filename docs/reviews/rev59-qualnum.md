# REV59-QUALNUM レビュー記録 — 数値再現性・撮像チェーン・無線AV権威

対象 issue: #703 (P1), #716 (P1), #717 (P1)
着地: schema v60、新規 9 テーブル、リポジトリ `cad_numerical_transport_repository`、回帰テスト `test_rev59_qualnum.py` 32 件

## 実装権威

### #703 `cad_solver_reproducibility.py`
- `NumericalReproducibilityProfile` (nrep-): 精度/並列性/リダクション順序/シード方針を pin。順序 pin は再現可能リダクション ref（ReproBLAS 級）必須
- `StochasticRealizationRecord` (srez-): 確率実現は seed 記録必須、結果 pin 必須
- `CrossPlatformNumericalComparison` (nxcmp-): ≥2 実現 + 実測偏差 + 任意アンサンブル CI
- `evaluate_numerical_difference_claim`: 変動帯域内の差は決定順序を主張不可。帯域超過でもプロファイルが順序/seed を pin しない限り決定的と名乗れない。文献: Demmel&Nguyen 2015、Iakymchuk 2020、GPU run-to-run 変動研究

### #716 `cad_imaging_chain.py`
- `ImagingMeasurementChain` (imc-): カメラ/レンズ/センサ/シャッタ/露出/WB/ISP/幾何アライメントを宣言。'qualified' は 5-marker アライメント pin + 対象 measurand 列挙必須
- `CameraCalibrationProfile` (camcal-): 歪み（ISO 17850）・MTF（ISO 9335:2025）・モアレ評価を独立 pin
- `CameraDerivedObservation` (cdo-): measurand + 処理状態を pin、証拠なし拒否
- `evaluate_imaging_evidence_claim`: 未適格チェーンはディスプレイ真値でない、自動露出/WB は比較不能、tone-mapped/compressed は生光学計測でない、rolling shutter は時系列を汚染、解像度 claim はカメラ MTF 適格が要。文献: IDMS v1.3 §3.8/§3.2.11-12/§3.10

### #717 `cad_wireless_av.py`
- `WirelessAVLink` (wav-): WiSA HT/E・LE Audio・BT classic・Wi-Fi・独自 RF を宣言。プロバイダ主張値は claim ref pin 必須
- `WirelessTransportObservation` (wto-): 遅延/損失/ドロップアウト/再接続/チャネル変更を現地観測 pin
- `WirelessSynchronizationEvidence` (wsync-): 実測スピーカー間オフセット
- `evaluate_wireless_claim`: 論理ルーティング≠伝送検証、プロバイダ主張のみは field 未検証、ドロップアウト観測は qualified 阻止、多端末は実測同期必須。文献: BAP 1.0.2/CAP 1.0.1、WiSA HT/E

## 検証
- `test_rev59_qualnum.py` 32 テスト + fresh-migrate・roundtrip・tamper
- 登録面: NATIVE_SCHEMA_TABLES + DDL 9 + `_migrate_59_to_60` + `_ROW_BINDINGS` 9 + `numerical_transport` ブランチ + `_ReplayProbe`×9 + labels + JA 行 + manifest 3 issue

## 残件
- 実ソルバー実現レコードの emit、実カメラチェーンの登録 UI、無線現地測定ウィザードは実機残件
