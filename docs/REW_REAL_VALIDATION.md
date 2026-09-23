# REW実機API検証記録

> 実施日: 2026-09-16 / 所有PC上の実REWで確認

## 対象環境

- Windows 11 x64
- Lenovo IdeaPad Pro 5 14AHP9 / machine type 83D3
- AMD Radeon 780M Graphics
- REW V5.40 beta 135 API build
- REW API: `http://127.0.0.1:4735`
- HTDT: schema v3を含む当時のmain

REWは `%USERPROFILE%\Desktop\HTDT\REW-5.40-beta135\` に導入し、
`roomeqwizard.exe -api` で起動した。APIのOpenAPI documentは
`GET /doc.json` から取得できた。

## 実API接続

HTDTを所有PC上で起動し、実REWに対して次を確認した。

- `GET /api/rew/status` -> `connected=true`
- `read_only=true`
- REW未ロード時のmeasurement countは0
- REW側の測定一覧はobject-keyed形式で返る
- HTDTはその形式を正常に正規化する

この確認ではHTDTからREWへPOST/PUT/DELETEを送っていない。
## 周波数応答の実デコード確認

HTDTのread-onlyアダプターを実REW応答で検証するため、REW APIへ検証専用の
合成FRを1本だけ直接importした。これはREW API互換性確認用であり、実測ではない。

- start: 20 Hz
- spacing: 96 PPO
- points: 958
- end: 約20.041 kHz
- phase: なし

REWが生成したmeasurement UUIDをHTDTのGET-onlyアダプターへ渡し、
`GET /api/rew/measurements/{uuid}/frequency-response?ppo=96&unit=SPL`
で読み戻した。

結果:

- 958点を復号成功
- returned unit: `SPL`
- returned smoothing: `1/48`
- returned PPO: 96
- 周波数軸の投入値との差の最大値: 約 `2e-11 Hz`
- magnitudeの投入値との差の最大値: 約 `0.00723 dB`

したがってbig-endian float32 Base64復号と96 PPO周波数軸復元は、
REW V5.40 beta 135の実API応答でも成立した。
## RX-A4A接続前のaudio baseline

RX-A4Aはこの時点ではPCへ未接続。したがってYamaha endpointが存在しないことは正常。

REW実APIで確認したbaseline:

- audio driver: Java
- sample rate: 48 kHz
- current output device: Default Device
- current hardware output channels: 2
- output choices: L / R / L+R
- WASAPI Exclusive候補は現状Realtekのみ

REW公式仕様ではWindowsのJava driverでmultichannelを使えるのは、
名前が`EXCL:`で始まるWASAPI Exclusive deviceのみ。RX-A4A受入では上限値を仮定せず、実際に8 hardware channelsが見えることを確認する。

所有PCのIdeaPad Pro 5 14AHP9は公式PSREF上、HDMI 2.1出力を持つ。
RX-A4Aは公式仕様上、HDMIでPCM 2〜8ch、最大192 kHz/24-bitを受けられる。
したがって接続後はRX-A4Aの`EXCL:` endpointがREWへ現れ、hardware channel countが
2より増えるかを最初に確認する。

確認用: `scripts/inspect-measurement-audio.ps1`
## まだ完了扱いにしない項目

- 同一measurementのREW text exportとAPI取得の数値照合
- RX-A4A接続後のWindows/REW endpoint列挙
- FL/FR/C/Heightのhardware channelと実発音源の照合
- 測定マイクを使った実測FR
- 実測repeatabilityとA04閾値の評価

REW V5.40 beta 135の公開APIにはmeasurement text export専用endpointが見当たらないため、
text export互換性確認はREW GUIの`Export measurement as text`で実ファイルを作成して行う。

## 参照

- REW API: https://www.roomeqwizard.com/help/help/html/api.html
- REW beta releases: https://www.roomeqwizard.com/beta.html
- Lenovo IdeaPad Pro 5 14AHP9 PSREF:
  https://psref.lenovo.com/Product/IdeaPad/IdeaPad_Pro_5_14AHP9
- Yamaha RX-A4A specifications:
  https://manual.yamaha.com/av/20/rxa4a/ja-JP/2246476299.html

## A03 UI実機確認

HTDTを`127.0.0.1:8765`で起動した状態でChrome headlessから実ページを実行し、
React描画後のDOMに実REW measurement `HTDT synthetic real-API fixture` が現れることを確認した。
同じDOMでRequested PPO=96、Unit=SPLの入力も確認できた。

したがって所有PCでは、REW -> HTDT backend -> A03 React UIのread-only経路まで実接続済み。
FRプレビューボタン押下後の視覚的グラフ評価は、実測データ取得後の操作性確認へ残す。

## 接続後のpreflight

HTDTは`GET /api/rew/audio-preflight`でREWのaudio状態を変更せず取得する。Windows/Javaの多ch確認では、`EXCL:`出力が選択され、hardware output channelsが2を超え、stereo-onlyでないことを確認する。入力側はdevice/input/channelとREW input calibration fileの有無を表示するが、入力endpointがreadyでも測定マイクの同定・校正済みとは自動判定しない。RX-A4A接続後はこのpreflightと`scripts/inspect-measurement-audio.ps1`を同時に保存してrouting evidenceとする。

## 日本語device名の文字コード確認

REW V5.40 beta 135のHTTP生バイトを確認した結果、Java audio device名はUTF-8として正しく返っている。Python/HTDT側では追加補正不要。PowerShell 5.1の`Invoke-RestMethod`経路でのみ表示上のmojibakeが発生したため、診断スクリプトはHTTP応答ストリームをUTF-8として明示デコードする。routing判定はdevice名だけには依存しない。

## REW API snapshot保存の実機受入

同じ`HTDT synthetic real-API fixture`を使い、HTDTのsnapshot保存経路を所有PCで実受入した。REW側への通信はmeasurement list/detail/frequency-responseのGETのみ。保存時にREW UUIDの現在一意性とdetail summaryのbefore/after一致を確認した。

保存結果:

- HTDT Dataset: 958 points / 20 Hz–20041.155831556098 Hz
- requested: SPL / 96 PPO / smoothing指定なし
- returned: SPL / 96 PPO / `1/48` smoothing
- phase: absentのまま保存（zero phaseを生成しない）
- evidence / quality / routing: すべて`unknown`のまま。API取得のみで昇格しない
- 保存Datasetと保存直前の実REW GET値: 周波数軸最大差`0.0 Hz`、magnitude最大差`0.0 dB`
- canonical RawAsset SHA-256: `38f00586c639ffff4fbe4916a4af4c31bd9cd470856c7d8fdbaaa77fe0bc698e`

その後REWを終了し、REW APIがofflineである状態からHTDTだけで保存Measurementを列挙し、保存Dataset同士のcomparison APIを実行した。self-check comparisonは957 grid points、RMS difference `0.0 dB`で完了した。

さらにsnapshot保存直後に作成したbackup ZIPから別Storeへrestoreし、958点のfrequency/magnitudeが完全一致し、RawAssetのSHA-256も上記値と一致、integrity problemsは0件だった。これによりREW停止後の保存データ利用とRawAssetを含むbackup/restoreを実機経路で確認した。
