# Windows受入手順

> CAD-first改訂: 本書は既存測定/browser経路の受入手順。native CADの操作・DPI・保存・配布は[CAD_EDITOR_ACCEPTANCE](CAD_EDITOR_ACCEPTANCE.md)を使用する。旧browser smokeの合格はnative操作の合格を意味しない。

> 対象: Windows 11 x64 / 同一PC / Yamaha RX-A4A / miniDSP UMIK-1 / 現在3.0.2

M10は「自動化できる保存・比較・復元契約」と「実REW/実測が必要な受入」を分離する。測定マイクとREW実環境が未準備の間、実機互換性を完了扱いにはしない。

## 1. CIで自動確認する合成E2E

`backend/tests/test_e2e_workflow.py` はWindows CI上で次を1本のシナリオとして実行する。

1. 新規Projectを作成する。
2. 矩形室、MLP、RX-A4A、3.0.2相当のspeaker snapshotをR1として保存する。
3. R1へFLの合成REW textを取り込む。
4. 同じRawAssetを同条件再測定として追加し、Datasetは別、RawAssetは重複候補として扱うことを確認する。
5. `.mdat`相当のRaw attachmentを測定へ紐付ける。
6. R1を親にR2を作成し、FL位置だけを変更する。
7. R2へ別FRを取り込み、R1/R2を比較する。
8. `speakers.FL.position`が意図した変更で、他条件がconfounderになっていないことを確認する。
9. R2作成後もR1の座標が変わっていないことを確認する。
10. 保存済みComparisonから自己完結HTML reportを生成する。
11. ZIP backupを作成する。
12. 同じdata directoryでアプリを再生成し、Project/Comparison/指標/整合性が一致することを確認する。
13. backupを別data directoryへrestoreする。
14. R1/R2、3 Dataset、Raw attachment、Comparison resultが同一ID/同一内容で復元されることを確認する。
15. restore後のintegrity checkが`ok`になることを確認する。

このシナリオはファイルI/O、SQLite、FastAPI、比較保存、report、backup/restoreをWindows runner上で横断する。ただしREWそのものやオーディオデバイスは使用しない。

## 2026-09-16 実機進捗

- REW V5.40 beta 135 API版を所有PCへ導入。`-api`起動で127.0.0.1:4735を確認。
- HTDTから実REWへread-only接続し、measurement一覧と96 PPO FR取得を確認。
- 合成FR 958点で周波数軸最大誤差約2e-11 Hz、magnitude最大差約0.00723 dB。
- RX-A4AはまだPC未接続。接続後のendpoint/channel mappingは未実施。
- 同一measurementのREW text exportとAPI取得の照合は未実施。

## 2. 実機を用意した後の最終受入

以下は合成fixtureでは代替しない。

1. 使用するREW安定版を記録し、同じ測定をtext exportと読取専用APIの双方で取得する。
2. UMIK-1を48 kHzで接続し、ホームシアター基準では天井向き+個体別90°校正ファイルを選択して保存する。絶対SPLを使う場合は校正状態を確認する。
   - HTDT measurement readinessでUMIK-1、48 kHz、REW選択中校正ファイルとRawAssetのSHA-256一致、REW input、EXCL多chを照合する。machine passでも物理向きとroutingは手動確認する。
3. RX-A4Aへの実際のHDMI/チャンネル割当を確認し、FL/FR/C/高さのrouting evidenceを記録する。
4. R1でFL/FRを各2回以上測定し、repeat groupとして取り込む。
5. スピーカーまたはMLPを1項目だけ変更してR2を作り、同じ測定条件で再測定する。
6. A/B比較で意図した変更とconfounderを確認し、HTML/JSON reportを保存する。
7. HTDTとREWを終了し、Windows再起動後に同じProject/Comparisonが再現することを確認する。
8. backup ZIPを別フォルダへrestoreし、RawAsset、添付、Dataset、Comparisonが一致することを確認する。
9. REW textとAPI取得の周波数軸・レベル・平滑化来歴を照合する。
10. A04のpeak/dip prominenceと候補対応が実部屋で過検出/未検出にならないか確認し、必要なら既定値を調整する。

## 3. 完了判定

- セクション1がWindows CIで成功: **合成E2E受入済み**。
- セクション2が所有PC・実測データで成功: **M10実データ受入済み**。

実機項目が未実施の間は、アプリ本体の実装が進んでいても「REW互換性」「RX-A4A routing」「測定品質」「実部屋での閾値」を完了扱いにしない。
