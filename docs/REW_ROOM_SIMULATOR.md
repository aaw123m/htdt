# REW Room Simulator integration contract

> 検証日: 2026-09-16
> 対象: REW V5.40 beta 135 API 0.9.8 / Windows 11 x64

## 目的

REW Room SimulatorをHTDTの配置探索で再利用できるかを、実OpenAPI・公式資料・所有PC実機で確認する。
初期統合はGET-onlyとし、予測結果を実測と混同しない。

## 公式仕様上の制限

REW Room Simulatorは**rectangular room**の複数source/mic位置の周波数応答を計算するモデルである。
したがって8頂点などの非矩形実室をexactに表現するモデルとしては使用しない。

非矩形Contextへ適用する場合は、矩形reference boxまたは明示的な近似矩形を入力とする
`rectangular_approximation`として記録し、実室polygonの予測結果へ自動昇格させない。
## 実beta135 API契約

確認済みGET endpoint:

- `/version`
- `/roomsim/room-size?unit=metres`
- `/roomsim/room-is-sealed`
- `/roomsim/absorptions`
- `/roomsim/options`
- `/roomsim/head-position?unit=metres`
- `/roomsim/mic-posn-offsets`
- `/roomsim/sources`
- `/roomsim/source-names`
- `/roomsim/mic-positions`
- `/roomsim/{src}/position?unit=metres`
- `/roomsim/{src}/configuration`
- `/roomsim/frequency-response?micposition=...`
- `/roomsim/{src}/frequency-response?micposition=...`

HTDTの初期adapterは上記GETのみを使用する。
## 座標契約

HTDTは原点を前左床とし、`X=right / Y=rear / Z=up`を使う。
REW Room Simulatorは`fromLeft / fromRear / fromFloor`を返すため、room lengthを`D`とすると:

- `x_m = fromLeft`
- `y_m = D - fromRear`
- `z_m = fromFloor`

逆変換は`fromRear = D - y_m`とする。
実機ではREW Left `(fromLeft=1.0, fromRear=4.0, fromFloor=1.0)`、room length 5.0 mが
HTDT `(1.0, 1.0, 1.0)`へ変換され、既存HTDT fixtureの向きと一致した。

## 実機GET結果

- room: length 5.0 m / width 4.0 m / height 2.4 m
- active sources: Sub1 / Left / Right
- mic positions: Main + 前後左右上下offset
- combined Main FR: 20 Hz開始 / 192 PPO / 751 points / SPL / smoothing `None`
- phaseも751 points返却
- 同一stateで5回連続GETしたraw JSONとBase64は完全一致
## stateful batch利用の実機プローブ

将来のBatch Prediction可否だけを確認するため、検証スクリプトでRoom Simulatorのhead positionを
`fromRear 1.90 -> 1.91 m`へ1 cmだけ変更し、FRを取得後、`finally`で元値へ復元した。
これはHTDT本体adapterの機能ではない。

結果:

- 1 cm移動後のFR raw SHA-256は変更前と異なった。
- 復元後のRoom Simulator全stateは変更前と完全一致した。
- 復元後のFR raw JSON/Base64も変更前と完全一致した。
- REW measurement listは前後で完全一致した。

よってbeta135では`state snapshot -> candidate apply -> FR GET -> restore`方式は技術的に成立する見込みがある。
本実装では復元失敗、REW途中変更、ユーザー操作競合を検出できるまでwriteを有効化しない。

## 参照

- REW API: https://www.roomeqwizard.com/help/help/html/api.html
- REW Room Simulator: https://www.roomeqwizard.com/help/help_en-GB/html/modalsim.html
- pyroomacoustics Room: https://pyroomacoustics.readthedocs.io/en/stable/pyroomacoustics.room.html


## N80 O20 production transaction boundary

N80でwriteを無条件解禁するのではなく、`RewRoomSimControlClient`をO20 transaction専用として追加する。通常の`RewApiClient`はGET-only運用を維持する。

初期write範囲:

- POST `/roomsim/head-position`
- POST `/roomsim/{src}/position`

REW公式APIはRoom SimulatorをAPIからread/setできる対象として記載し、データmodel設定にはPOST/PUTが利用できる。HTDT初期O20は既存実機probeで確認したposition変更だけへ範囲を限定する。

production batchは以下を必須とする。

- exact rectangular native roomとREW room dimensionsの一致
- explicit native speaker acoustic reference
- native entity -> REW source mapping
- combined response時のactive-source完全coverage
- full-state pre hash
- write後full-state一致
- FR取得後full-state不変
- `finally`相当のrestore
- exact pre-state復元確認
- external change / restore failure時は結果破棄

左右連動等でAPI writeが指定外source positionを動かした場合も、write直後のsnapshotをtransaction-owned stateとして記録し、安全に復元する。transaction中にユーザー/外部processがさらに変更したfieldは上書きせずhard errorにする。

この契約がCIと実機gateを通るまで、O20 batch resultを自動推薦へ使用しない。
