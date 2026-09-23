# RX-A4A HDMI / REW routing 実機受入手順

> 対象: IdeaPad Pro 5 14AHP9 / Windows 11 / Yamaha RX-A4A / miniDSP UMIK-1 / REW V5.40 beta 135
> 状態: 接続前手順を確定。RX-A4AはまだPC未接続。

## 目的

PCからRX-A4AへHDMI PCMを送り、REWから選んだ論理チャンネルと、
実際に鳴った物理スピーカーを1本ずつ照合する。

ここでは「REWで選べるチャンネル名」と「実際の発音源」を同一視しない。
HTDTへ`routing_evidence=verified`を保存するのは実発音を確認した経路だけとする。

## 確認済みの能力

- IdeaPad Pro 5 14AHP9の内蔵HDMIはHDMI 2.1、最大4K/60 Hz。
- RX-A4AはHDMI入力を7系統持ち、PCM 2～8ch、最大192 kHz/24-bitに対応。
- REWのWindows Java driverでmultichannelを使う場合、`EXCL:`で始まる
  WASAPI Exclusive outputを選ぶ必要がある。
- 初回の7.1 PCM bed確認では`L/R/C/LFE/SL/SR/SBL/SBR`を論理役割として照合する。REWのmapping label自体は設定可能なので、名称だけをrouting証拠にしない。

参照:
- https://psref.lenovo.com/Product/IdeaPad/IdeaPad_Pro_5_14AHP9
- https://manual.yamaha.com/av/20/rxa4a/ja-JP/2246476299.html
- https://www.roomeqwizard.com/betahelp/help/html/calsoundcard.html
## 1. 物理接続

最初の受入は変換アダプター、USB-C映像出力、eARCを使わず、
**PC内蔵HDMI -> RX-A4A HDMI入力**を直結する。

RX-A4AのHDMI OUT1は通常使用している表示機器へ接続したままにする。
PC側は最大4K/60なので、初回routing確認に4K/120や8Kは不要。
映像交渉で問題が出る場合は1080p/60へ一時的に下げ、音声endpoint確認を先に行う。

RX-A4AのHDMI Video Formatは既定の4K Mode 1から開始する。
4K/60までの初回測定用途では、先に8K Modeへ変更する理由はない。

接続前baselineを保存:

```powershell
.\scripts\inspect-measurement-audio.ps1 `
  -OutputPath "$env:USERPROFILE\Desktop\HTDT\samples\audio-before-rxa4a.json"
```

接続・電源投入後に同じコマンドを`audio-after-rxa4a.json`へ出力する。
## 2. Windows / REW endpoint確認

WindowsのAudioEndpoint一覧に接続前にはなかったHDMI音声endpointが増えることを確認する。
名称はEDIDやドライバーで変わり得るため、`RX-A4A`という文字列だけを合否条件にしない。
before/afterの差分とAMD High Definition Audio Device系endpointを併用して判断する。

REWは`roomeqwizard.exe -api`で起動し、まず48 kHzを使う。
REW Output Deviceでは通常の共有deviceではなく、対象HDMIの`EXCL:` entryを選ぶ。
Windows側で「アプリケーションにこのデバイスを排他的に制御させる」が無効なら、
REWのWASAPI Exclusive利用条件を満たさないため有効化を確認する。

対象を選んだ後、診断JSONで次を確認する。

- before/afterで特定したHDMIの`EXCL:` entryが存在
- `currentOutputIsExclusive=true`
- `outputDeviceChannels >= 8`
- `javaMultichannelReady=true`
- HTDTの`/api/rew/audio-preflight`でも同じ状態を確認
- `outputChannelMapping`を実際の発音結果と照合する

endpoint名に`Yamaha`や`RX-A4A`が含まれなくても、before/after差分と実発音で対象を特定できれば検証を継続する。
## 3. bed channel routing検証

RX-A4Aは測定時に`STRAIGHT`を使い、サラウンドデコーダーやCINEMA DSPによる
upmixを避ける。STRAIGHTはマルチチャンネル入力を音場効果なしで再生する。

低い再生レベルから、REWの各出力を1本ずつ確認する。

| REW label | 期待する論理役割 | HTDTでの扱い |
|---|---|---|
| L | front_left | FLだけが主発音源か実耳/測定で確認 |
| R | front_right | FRだけが主発音源か確認 |
| C | front_center | Cだけが主発音源か確認 |
| LFE | LFE | サブなし環境では無音/低音リダイレクトを別途確認 |
| SL/SR | surround | 現3.0.2では存在しないので自動的に他SPへ対応付けない |
| SBL/SBR | surround_back | 現3.0.2では存在しないので自動対応付けしない |

特にサブウーファーなしではbass managementにより、入力チャンネルと
実際に低音を出したスピーカーが一致しない可能性がある。
`input_role`と`source_speaker_ids`を分けて記録する。

RX-A4A前面表示の`Output Channel`情報も補助証拠として確認できる。
## 4. Front Presence / Heightの扱い

現在の3.0.2にあるFront Presence L/Rは、RX-A4AではCINEMA DSP効果音または
height channelを出すスピーカーである。一方、通常の8ch PCMでREWが扱う標準ラベルは
`L/R/C/LFE/SL/SR/SBL/SBR`であり、Front Presence専用ラベルは含まれない。

したがって接続前の段階では、REW HDMI sweepからHeight L/Rを独立励振できるとは仮定しない。
Dolby SurroundやNeural:Xでbed入力をHeightへupmixして鳴らしても、
それをHeightの独立routing証明には使わない。

RX-A4A自身のSpeaker設定にはFront PresenceとTest Toneがあるため、
内部テストトーンは配線・チャンネル存在確認の補助には使える。
ただしREW sweepと同一の測定経路ではないため、周波数応答の直接代替にはしない。

Heightについては、個別sweep経路を実証できるまでHTDTのrouting evidenceを
`unknown`または検証内容に応じた`manual`に留める。

## 5. 初回受入の合格条件

- Windowsで接続前になかったHDMI audio endpointを特定できた。
- REWに対象HDMIの`EXCL:` outputが現れた。
- 48 kHzで8 hardware channelsが列挙された。
- L/R/CについてREW labelと実発音源を個別確認できた。
- AVRはSTRAIGHTで、upmixによる別スピーカー発音を避けた。
- LFEはサブなし時の実発音源を別途記録した。
- Heightは未証明なら未証明のまま保存した。

結果は`docs/REW_REAL_VALIDATION.md`へ追記し、診断JSONは
`%USERPROFILE%\Desktop\HTDT\samples\`へ保存する。
