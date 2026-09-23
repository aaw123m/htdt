# miniDSP UMIK-1 実機受入手順

> 対象: Windows 11 / REW V5.40 beta 135 / Yamaha RX-A4A / HTDT
> 採用機種: miniDSP UMIK-1。実機serial・個体校正ファイルは公開リポジトリへコミットしない。

## 基準測定の契約

- サンプルレートは48 kHzを基準にする。
- ホームシアター/多方向測定ではマイクを天井へ向け、個体別`_90deg`校正ファイルを使う。
- 単一スピーカーへ正対する測定を明示的に行う場合だけ0°校正を使い、90°測定と同条件扱いしない。
- カプセル中心の座標と向きをContextへ保存する。
- REWで選択した校正ファイルの原本はHTDTへ`microphone_calibration`添付として保存する。

## 到着後の手順

1. 本体ラベルのserialをローカル記録し、miniDSP公式ページから0°/90°の2校正ファイルを取得する。
2. 校正ファイルを`%USERPROFILE%\Desktop\HTDT\calibration\UMIK-1\`へ保存する。
3. Windowsのマイクアクセス、desktop app accessを有効にし、UMIK-1を接続する。
4. REWを`-api`付きで起動し、Java input deviceでUMIK-1を明示選択する。Default Deviceのままにしない。
5. REW sample rateが48 kHzであることを確認する。
6. 基準のホームシアター測定では90°校正ファイルを選択し、マイクを天井へ向ける。
7. HTDTのREW audio preflightでinput device、input channel、cal file、audio readyを確認する。
8. `scripts/inspect-measurement-audio.ps1`の診断JSONを保存する。

## 合格条件

- Windows/REWの入力一覧でUMIK-1を特定できる。
- REW audio statusがreadyで、入力channelを取得できる。
- REWのinput calibration pathが空でない。
- ホームシアター基準測定では選択ファイルが90°用であることを目視確認する。
- 48 kHzでFL/FRのrepeat sweepを取得できる。
- 同じマイク位置・向き・校正ファイルでrepeat groupを作る。
- クリッピングやレベル警告がある測定を`usable`へ自動昇格しない。

## 根拠

- miniDSP UMIK-1/2 REW setup: 個体別0°/90°校正、UMIK-1は48 kHz。
- miniDSP UMIK-1 + HDMI Windows: multichannelでは天井向き+90°校正を推奨。
- REW Soundcard Preferences: USB micとWASAPI Exclusive出力を別deviceとして利用可能。

実測serial、校正ファイル内容、自宅測定原本はローカルRawAssetとして扱い、公開fixtureへは入れない。

公式参照:
- https://www.minidsp.com/applications/acoustic-measurements/umik-1-setup-with-rew
- https://www.minidsp.com/applications/acoustic-measurements/umik-1-hdmi-on-windows
- https://www.roomeqwizard.com/betahelp/help/html/soundcard.html

## HTDT Context記録

ContextにはUMIK-1のmodel/serial/sample rate/calibration profile/filenameとMLPの`aim_xyz`を不変保存する。serial未入力は`null`のまま許可し、実機到着前に架空serialを作らない。90°基準時のaimは`[0,0,1]`、部屋正面へ向ける0°基準は`[0,-1,0]`とする。

## HTDT measurement readiness gate

`GET /api/projects/{project_id}/contexts/{context_id}/measurement-readiness` は保存Contextと現在のREW audio状態を照合する。UMIK-1選択、48 kHz、校正ファイル名一致、REWで現在選択中の校正ファイル実バイトSHA-256とContextに紐付く校正RawAssetのSHA一致、WASAPI Exclusive多chを機械判定する。全項目通過後も、物理マイク向き・カプセル位置・RX-A4A設定・実スピーカーroutingはAPIで証明できないため `manual_confirmation_required` とする。これは測定品質の判定ではない。
