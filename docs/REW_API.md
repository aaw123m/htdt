# REW API read-only integration

> 状態: 実装済み / REW V5.40 beta 135実機接続・FRデコード確認済み / text export同一測定照合は未完

## 方針

HTDTのREW API連携はファイル取込を置き換える必須経路ではなく、任意の読取省力化機能とする。初期アダプターにはHTTP **GETだけ**を実装し、REWの測定・設定・EQ・ファイルを変更するPOST/PUT/DELETEは実装しない。

REW公式APIは既定で `http://127.0.0.1:4735`。HTDTも既定値をこれに固定し、接続先ホストは `127.0.0.1` または `localhost` のみ許可する。LAN上の別ホストへ向ける機能は作らない。

必要なら環境変数 `HTDT_REW_API_URL` でlocalhost上の別ポートを指定できる。

```powershell
$env:HTDT_REW_API_URL = "http://127.0.0.1:4735"
python -m htdt
```

## HTDT側のエンドポイント

| HTDT endpoint | 動作 |
|---|---|
| `GET /api/rew/status` | REW APIへの到達可否と測定数を確認 |
| `GET /api/rew/measurements` | REWに現在ロードされている測定summaryを読取 |
| `GET /api/rew/audio-preflight` | driver/sample rate/Java入力・出力device/input cal/EXCL候補/ch数/mappingをGETだけで確認 |
| `GET /api/rew/roomsim/state` | Room Simulatorの矩形room/source/mic/optionsをGET-only snapshot |
| `GET /api/rew/roomsim/frequency-response` | Room Simulatorの予測FR。`predicted_rew_room_simulator`として返す |
| `GET /api/rew/measurements/{uuid}/frequency-response` | 指定測定の周波数応答を読取・デコード |
| `POST /api/projects/{project_id}/rew-snapshots` | HTDT側へsnapshot保存。REW通信は内部でGETのみ |

REWのindex番号は測定の追加・削除・group変更で動き得るため、HTDTは永続参照にindexを使わずUUIDを使う。

Room Simulator統合の座標契約、実beta135結果、矩形限定モデルとしての扱いは[REW Room Simulator integration contract](REW_ROOM_SIMULATOR.md)を参照する。非矩形実室へ適用する場合はexact modelではなく矩形近似として分類する。


## Snapshot保存契約

REW measurementをHTDTへ保存するときもREW側はGET-onlyを維持する。保存処理は次の順で行う。

1. HTDTのProject/Context/Session参照を先に検証する。
2. `GET /measurements` で対象UUIDが現在ちょうど1件であることを確認する。
3. `GET /measurements/{uuid}` を取得する。
4. `GET /measurements/{uuid}/frequency-response` を指定unit/PPO/smoothingで取得する。
5. 同じmeasurement summaryを再GETし、before/afterが一致しなければ保存を中断する。
6. raw JSONをcanonical UTF-8 JSON RawAssetとして保存し、decoded FRをdouble BLOB Datasetへ保存する。

RawAsset wrapperは`htdt-rew-api-frequency-response-snapshot-1`で、measurement summary、実際のquery、REWのfrequency-response JSONを保持する。Base64 magnitude/phase文字列は変更しない。取得時刻はRawAssetへ混ぜずDB側の`imported_at`で保持するため、同一response/queryは同一SHA-256になる。

同じRawAsset SHAが既に存在してもMeasurement/Datasetを自動mergeしない。REW UUIDも永続的な「同一測定」判定には単独使用しない。API取得だけではquality/evidence/routingを昇格せず、既定はすべて`unknown`とする。

## 配列デコード契約

REW APIの数値配列は32-bit floatのraw bytesをBase64化した文字列で、公式仕様ではbyte orderは**big-endian**。HTDTは `struct.unpack(">...f")` 相当で復号する。

公式の検証例 `PgAAAD6AAAA+wAAAPwAAAA==` → `0.125, 0.25, 0.375, 0.5` を自動テストへ含める。

FrequencyResponseについては次を扱う。

- `startFreq`
- `ppo` または `freqStep`
- `magnitude`
- optional `phase`
- `unit`
- `smoothing`

log spacingでは `f[i] = startFreq * exp(i * ln(2) / ppo)`、linear spacingでは `f[i] = startFreq + i * freqStep` で周波数軸を復元する。

## PPO / smoothingの来歴

HTDTから`ppo=96`を要求しても、REWはsampling artefact回避のため追加smoothingを適用する場合がある。したがって以下を別々に返す。

- requested_ppo / requested_unit / requested_smoothing: HTDTが要求した値
- points_per_octave / unit / smoothing: REWが実際に返した値

96 PPO取得を「未平滑化raw測定」とは呼ばない。

## 防御

REW 5.40 beta系ではAPIモデルが改訂され得るため、測定一覧は配列形式・object keyed形式を正規化する。一方、未知の形状は推測せずエラーにする。

周波数応答は次を拒否する。

- 壊れたBase64
- 4 byte境界でない配列
- NaN / Inf
- 空のmagnitude
- startFreqまたはspacing欠損
- magnitude / phase長不一致

## オフライン動作

REW未起動・API未起動でもHTDTのProject、保存済み測定、比較、バックアップは通常利用できる。`GET /api/rew/status`はconnected=falseを返し、REWデータ取得エンドポイントだけ503となる。

## 初期アダプターで実装しない操作

- 自動スイープ開始
- REW測定の削除・名称変更
- EQ/Filter変更
- Room Simulator設定変更
- REWへのファイルimport
- Generator操作・発音
- AVR設定変更

## 実機受入条件

REWを導入したら次を確認する。

1. REW APIをlocalhostで起動する。
2. `/api/rew/status` がconnectedになる。
3. FL/FRのsummary UUID・title・帯域がREW画面と一致する。
4. 同一測定についてREW text exportとAPI取得を比較する。
5. requested PPOとreturned smoothing/unitを保存して数値を照合する。
6. REWを停止するとHTDTは保存済みデータを失わずAPIだけunavailableになる。
7. テスト中にREW側の測定数・名称・設定・音声出力が変化しないことを確認する。

2026-09-16時点で1、2、合成FRを使った5の数値デコード確認に加え、実REWからのsnapshot保存、REW停止後の保存Dataset比較、RawAssetを含むbackup/restoreまで所有PCで完了した。
同一measurementのtext exportとの照合、UMIK-1実測FL/FR、RX-A4A routing確認が残るため、実測ワークフロー全体はまだ完了扱いにしない。

詳細は[実REW検証記録](REW_REAL_VALIDATION.md)を参照する。

## 公式資料

- REW API: https://www.roomeqwizard.com/help/help/html/api.html
- REW beta releases: https://www.roomeqwizard.com/beta.html
