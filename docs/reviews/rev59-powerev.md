# REV59-POWEREV レビュー記録 — 電源シーケンス・電源品質・EMC 権威

対象 issue: #736 (P1), #738 (P2), #752 (P2)
着地: schema v55、新規 7 テーブル、リポジトリ `cad_power_evidence_repository`、回帰テスト `test_rev59_powerev.py` 20 件

## 実装権威

### #736 `cad_power_sequencing.py`
- `PowerSequencingProfile` (psq-): 段順・アンプ機器・常時電源機器・インラッシュ段階化を pin（power_off はアンプ宣言必須）
- `PowerSequenceEvent` (psev-): 観測遷移の pin
- `UpsTransitionRecord` (upst-): UPS 移行観測 + 復帰シーケンス pin
- `evaluate_sequence_claim`: 回路容量や動くシーンは決定的順序を証明しない。アンプ last-on/first-off・常時電源存続を別判定（アンプ順序違反・常時電源違反を個別理由コード化）

### #738 `cad_power_quality.py`
- `PowerQualityMeasurement` (pqm-): 現地 RMS 電圧/周波数/THD + 宣言済み PQ イベント（dip/swell/短断/長断/RVC/過渡過電圧）
- `PowerQualityQualification` (pqq-): verdict + 限度プロファイルを証拠 pin
- `evaluate_supply_claim`: 容量≠安定供給。中断/過渡は供給劣化、測定なしは unmeasured_supply

### #752 `cad_emc_evidence.py`
- `EmcProductProfile` (emc-): 放射/イミュニティ試験を規格別に pin（試験構成も宣言）
- `EmcSymptomRecord` (emcs-): 現場症状は診断経路への pin であり適合 verdict ではない
- `evaluate_emc_claim`: 安全承認は EMC 証拠にならず、症状だけでは不適合を立証できない。両ドメイン完備でのみ emc_evidence_complete

## 文献根拠
- IEC 61000-4-30（PQ 測定量クラス）、EN 50160（供給電圧特性）
- CISPR 32/35、EN 55032/55035（マルチメディア放射/イミュニティ）

## 検証
- `test_rev59_powerev.py` 20 テスト + fresh-migrate・roundtrip・tamper
- 登録面: NATIVE_SCHEMA_TABLES + DDL 7 + `_migrate_54_to_55` + `_ROW_BINDINGS` 7 + `power_evidence` ブランチ + `_ReplayProbe`×7 + labels + JA 行 + manifest 3 issue

## 残件
- 実シーケンサー/UPS からの遷移イベント自動取込、PQ 計器連携は実機残件
