# REV59-INFRA2 — 電源シーケンス/電源品質/室内環境/製品適合

schema v66・`cad_facility_repository`（7ストア）・
`test_rev59_infra2.py`（25テスト）。

## #736 電源シーケンス（P1）

- `PowerSequencePlan`（pseq-）: 順序付きステップ + アンプ
  last-on/first-off + 突入対策 + 常時給電 + EDID/制御依存を宣言。
- `PowerSequenceEvidence`（psev-）: clean_sequence / partial /
  timeout / order_violation / transient / power_loss /
  orderly_ups_shutdown / recovery の結果。clean には観測証拠必須。
- `evaluate_power_claim`: 回路/UPS 容量は順序を証明しない
  （capacity_is_not_sequence）。

## #738 AC 電源品質（P1）

- `PowerQualityObservation`（pqo-）: IEC 61000-4-30 クラス A/S 等の
  計器クラスを必須宣言 + 観測イベント（dip/swell/interruption/
  harmonics）+ 症状相関は capture 証拠必須。

## #740 占有室 IAQ（P2）

- `IndoorAirObservation`（iao-）: センサクラス必須 + CO2 ピーク/
  温湿度/セッション時間 + capture ref 必須。
- `evaluate_indoor_claim`: **換気設計は占有結果を証明しない**
  （design_is_not_occupied_outcome）。

## #750 材料化学放出席（P1）

- `MaterialEmissionEvidence`（mem-）: VOC/formaldehyde/SVOC/VVOC/
  manufacturer_program。チャンバー試験は報告 ref + 試験対象
  ビルドアップ記述必須 — メーカー認証単体では室 IAQ を制限
  しない（certificate_is_not_room_iaq）。

## #751 製品安全（P1）

- `ProductSafetyEvidence`（pse-）: IEC 62368-1/IEC 60065/UL/CE-LVD
  + 版 + 試験バリアント + 証明書 ref + 市場スコープ。HTDT は
  認証 identity を記録し、安全性試験は複製しない。
- `evaluate_compliance_claim`: 電気性能適合は安全性を証明しない
  （performance_is_not_safety）+ 市場スコープ不一致検出。

## #752 EMC（P1）

- `EMCComplianceEvidence`（emc-）: CISPR 32（Class A/B 必須）/
  CISPR 35/FCC の試験構成証拠。
- 安全承認は EMC を確立しない（emc_unverified）、現地のハム症状
  だけでは製品 EMC 非適合を証明しない
  （field_symptom_is_not_emc）。

## 残件

- シーケンス実行エンジン・計器ファイル取込（PQDIFF等）は別途 —
  権威は証拠ゲート層。
