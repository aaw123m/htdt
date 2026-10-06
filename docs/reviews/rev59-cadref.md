# REV59-CADREF — 映像コーデンス / BS.1116 参照室

schema v70・`cad_cadref_repository`（2ストア）・
`test_rev59_cadref.py`（10テスト）。

## #777 映像コーデンス（P1）

- `CadenceDeliveryEvidence`（cax-）: コンテンツ cadence
  （film_24/broadcast_50/60/mixed/variable）+ 出力リフレッシュ
  関係（整数倍/VRR/QMS/fixed_conversion）+ モーション処理宣言
  + タイミング証拠 — 'unknown' 拒否。
- evaluate: **ネゴシエートされた解像度/HDRは動画忠実度でない**
  （negotiated_is_not_motion）。補間処理有効=processing_active、
  固定リフレッシュ変換=宣言的妥協、repeat/drop 検出・タイミング
  証拠なしは cadence_unqualified。

## #778 参照室プロファイル（P2/P1）

- `ReferenceRoomProfile`（rrx-）: フレームワーク（itu_bs1116/
  ebu_tech3276）+ 室・スピーカー・リスナー位置 criteria ref。
- evaluate: **BS.1116/EBU 3276 は制御試聴ベンチマークであり
  万能な設計目標ではない**（reference_is_not_design_target）—
  CEDIA/CTA-RP22 と混同しない。

## 残件

- 実コーデンス計測・参照室基準表の実装は別途 — 権威は証拠
  ゲート層。
