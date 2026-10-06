# REV59-ACOUST2 レビュー記録 — 治具散乱・スペクトル推定・証拠継承権威

対象 issue: #743 (P2), #749 (P2), #765 (P1)
着地: schema v57、新規 6 テーブル、リポジトリ `cad_measurement_setup_repository`、回帰テスト `test_rev59_acoust2.py` 24 件

## 実装権威

### #743 `cad_observer_scattering.py`
- `MeasurementFixtureProfile` (fxp-): マイクマウント・観測者距離・近傍物体・散乱特性化 ref を宣言
- `FixtureScatteringObservation` (fxo-): 治具への汚染観測 pin
- `evaluate_transparency_claim`: 校正済みマイクは治具透明性を証明しない。散乱検出は contaminating に降格

### #749 `cad_spectral_estimator.py`
- `SpectralEstimatorProfile` (sep-): 窓・記録長・FFT サイズ・ゼロ詰め率を分離宣言。`true_resolution_hz` は記録長拘束、`displayed_bin_spacing_hz` は表示上のみ
- `SpectralResolutionClaim` (src2-): 分解能クレームを estimator pin に束縛
- `evaluate_resolution_claim`: 記録長限界より細かい claim は `padded_display_not_resolution`、窓不明は unresolvable

### #765 `cad_evidence_supersession.py`
- `ExternalEvidenceSource` (ees-): tier/発行者/日付/スコープ/製品ファミリ・ティアを pin
- `EvidenceSupersessionRecord` (ess-): 2件以上のソースに和解 verdict — 'superseded' は勝者 pin + 理由必須
- `evaluate_conflict_claim`: ティア/スコープ相違 → 両方有効、同製品で能力スコープ相違 → ベンダー確認要、同スコープ矛盾は自動解決しない

## 文献根拠
- Harris 1978（窓関数と分解能）; #765 本体記載の Trinnov/Dirac 事例

## 検証
- `test_rev59_acoust2.py` 24 テスト + fresh-migrate・roundtrip・tamper
- 登録面: NATIVE_SCHEMA_TABLES + DDL 6 + `_migrate_56_to_57` + `_ROW_BINDINGS` 6 + `measurement_setup` ブランチ + `_ReplayProbe`×6 + labels + JA 行 + manifest 3 issue

## 残件
- 散乱特性化の実測プロシージャ・外部ソース取込は実機/後続残件
