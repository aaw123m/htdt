# REV59-BUILDENV レビュー記録 — 製品安全・占有IAQ・VOC 権威

対象 issue: #751 (P2), #740 (P2), #750 (P2)
着地: schema v56、新規 4 テーブル、リポジトリ `cad_building_environment_repository`、回帰テスト `test_rev59_buildenv.py` 21 件

## 実装権威

### #751 `cad_product_safety.py`
- `ProductSafetyProfile` (psf-): リスティング種別（NRTL/認証/自己宣言/コンポーネント承認/未登録）、規格・証明書 pin・市場・認証モデルを宣言
- `evaluate_safety_claim`: 工学的適合は市場向け認証の代用にならない。市場不一致・認証モデル不一致は個別 verdict。自己宣言/コンポーネント承認は製品リスティングを名乗らない

### #740 `cad_iaq_occupancy.py`
- `OccupiedIaqObservation` (iaq-): 占有状態の CO2/温度/湿度/換気効率の実測 pin（非占有観測は占有 claim に使えない）
- `OccupiedIaqQualification` (iaqq-): verdict + 限度プロファイルを観測 pin に束縛
- `evaluate_iaq_claim`: 宣言風量達成は占有時条件を証明しない（`airflow_is_not_occupied_iaq`）

### #750 `cad_voc_evidence.py`
- `VocEmissionProfile` (voc-): 材料ごとの排出スキーム（CDPH SM v1.2/AgBB/GREENGUARD 等）+ 証明書/チャンバー試験 pin
- `evaluate_emission_claim`: 製品証明は製品を文書化するのみ — 室内 IAQ claim は占有時観測（#740）必須で外挿しない

## 文献根拠
- IEC/UL 62368-1（AV/ICT 安全）、ASHRAE 62.1（換気）/55（温熱快適）、ISO 7730（PMV/PPD）、CDPH SM v1.2/AgBB/GREENGUARD（排出スキーム）

## 検証
- `test_rev59_buildenv.py` 21 テスト + fresh-migrate・roundtrip・tamper
- 登録面: NATIVE_SCHEMA_TABLES + DDL 4 + `_migrate_55_to_56` + `_ROW_BINDINGS` 4 + `building_environment` ブランチ + `_ReplayProbe`×4 + labels + JA 行 + manifest 3 issue

## 残件
- CO2/温湿度センサー・証明書レジストリからの自動取込は実機残件
