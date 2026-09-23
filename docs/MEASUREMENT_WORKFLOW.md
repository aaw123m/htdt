# 測定手順と外部連携の契約

> 2026-09-15 / 計画仕様。実機測定・インポーター実装は未実施。
> スコープは[全体計画](PROJECT_PLAN.md)、内部表現は[データ・解析](DATA_AND_ANALYSIS.md)を参照。

## 1. 最初に収集する情報

| 項目 | 記録する内容 | 不明な場合 |
|---|---|---|
| Windows/PC | OS版、x64/ARM、メモリ、GPU/HDMI音声ドライバー | Windows 11 x64以外の保証をしない |
| REW | 完全なビルド番号、安定版/ベータ、Java同梱状態 | 公開Webヘルプから対応版を推定しない |
| マイク | 型番、個体番号、校正ファイル、測定方向、USB接続条件 | 相対比較に留め、絶対SPLを保証しない |
| AVR | 型番、ファームウェア、入力端子、表示される入力信号形式 | RX-A4A等を自動設定しない |
| 音声経路 | PC→AVR直結か、TV/eARC等を経由するか | 出力チャンネルを実際に確認する |
| スピーカー | 本数・型番・端子/役割・サブ有無 | 正式なAtmos構成名より実物を優先 |
| 部屋 | 幅・奥行・高さ、開口、家具、マイク点 | 参照用形状と音響計算に使える形状を区別 |

これらは実装の依存条件を確認するための記録であり、すべてが分からないとファイル取込を拒否する設計にはしない。

### 採用マイク: miniDSP UMIK-1

HTDTの第一対象測定マイクはminiDSP UMIK-1とする。UMIK-1は48 kHzで使用し、個体serialから0°/90°の校正ファイルを取得する。ホームシアターの基準測定はマイクを天井へ向け、90°校正ファイルを使用する。単一スピーカーへ正対する0°測定は別条件として記録する。実serialと個体校正原本はローカルRawAssetとして保持し、公開リポジトリへ自動コミットしない。

Contextにはマイク機種・serial・sample rate・calibration profileと`MeasurementPoint.aim_xyz`を保存する。校正ファイルのバイト列は別のRaw attachmentとして保存し、ファイル名だけを校正適用の証明にはしない。

## 2. Windowsと測定チャンネル

REWのWindows向けJava出力で多チャンネルを扱う場合はWASAPI Exclusive（EXCL）デバイスを確認する。入力と出力を別デバイスにできるため、USBマイクとHDMIを組み合わせる候補になる。ASIOは利用可能なドライバーが必要な場合に評価し、ASIO4ALLの導入を初期必須条件にしない。[REW Soundcard Preferences](https://www.roomeqwizard.com/help/help_en-GB/html/soundcard.html)

### チャンネル対応の確認

1. PC、AVR、REWの設定画面に出るデバイス・出力名を記録する。
2. REWで確認用信号を出し、選択した出力から実際に鳴るスピーカーを確認する。
3. Windows/REW上の番号、入力ラベル、AVR端子、実スピーカーIDの対応表を保存する。
4. AVRのスピーカー割当・信号経路を変更したら、以前の対応表をそのまま再利用しない。

~~~text
ChannelMapEntry
  output_device_label
  rew_channel_label
  hardware_channel_index?
  input_role
  expected_speaker_ids[]
  observed_speaker_ids[]
  verification = verified | unverified | mixed | unavailable
  verified_at?
  avr_configuration_id
~~~

低域は他のスピーカーへ振り分けられ得るため、確認した周波数帯も記録する。

### Atmos・高さスピーカー

「5.0.2の配置を描ける」「AVRがAtmos対応」「REWから高さスピーカー単体へ出力できる」は別の能力として扱う。通常のPCM経路に高さチャンネルが自動で追加されるとは仮定しない。WindowsのSpatial Audioは専用APIを用いる仕組みであり、通常の音声出力選択の対応数とは別に確認が必要である。[Microsoft Spatial Sound](https://learn.microsoft.com/en-us/windows/win32/coreaudio/spatial-sound)

v0.1の測定対象は個別出力を確認できたチャンネル。高さは配置・役割の記録のみでもよい。将来は既存の対応ソフト、検証済み外部スイープ再生、端子再割当等を個別評価するが、再生経路が変わる測定は別条件として記録する。アップミックスで鳴った高さスピーカーを単独測定と呼ばない。

## 3. サブウーファーなしの測定

サブなしでも、Small設定のチャンネルに含まれる低音がメインへ振り分けられる場合がある。RX-A4Aの資料もクロスオーバー以下の出力先としてサブまたはフロントを示している。具体的な挙動は所有機種と設定で確認する。[Yamaha RX-A4A User Guide — pp.208–209](https://data.yamaha.com/files/download/other_assets/5/1335595/AV19-0070_RX-A4A_user_guide_En_UCRABGLFP_H0.pdf)

| 測定例 | 保存する名称の例 | 誤って推定してはいけないこと |
|---|---|---|
| FL入力、FLのみを確認 | FL入力・通常再生設定 | 全帯域で他音源がゼロとは未確認のまま断定しない |
| C入力、低域はフロントへ転送 | C入力・低音転送あり | C単体の低域特性 |
| LFE入力、サブなし | LFE入力・実出力経路確認済み | LFEチャンネル＝物理サブウーファー |
| L+R同時 | L+R同時再生 | FL/FR単独応答のdB平均と等価 |

HTDTからスピーカーをLargeへ変更したり、低域スイープ範囲を自動拡張したりしない。周波数範囲は実際のスピーカーと測定目的に合わせてREWで設定する。

## 4. 最小の測定プロトコル

### A. 条件を固定する

- UMIK-1を支持し、カプセル中心の座標・高さ・向き、個体serial、校正ファイルを記録する。ホームシアター基準は天井向き+90°校正。
- AVRの入力・音量・音場モード・EQ・距離・レベル・クロスオーバー・サブ設定を保存する。
- YPAO Volume、Adaptive DRC、tone、Enhancer、Extra Bass等は対応機種で状態を記録する。不明はunknown。
- Windows側の音量、音響効果、空間オーディオ、サンプルレート、REWの出力・スイープ条件を記録する。
- 扉、カーテン、主要家具、在室者、目立つ暗騒音をメモする。

### B. 基準測定を作る

最初はMLPでFL、FR、それぞれの再測定を取得する。再現性確認のため、マイクを動かさず同じ設定で繰り返す。必要に応じて少し位置を変えた測定を別の測定点として残す。低S/Nを隠すための強い平滑化や自動レベル合わせは行わない。

各測定でREWのクリッピング・レベル・タイミング警告を確認し、警告内容と採用/再測定の判断を残す。入力クリッピングは周波数応答の誤差につながるため、その測定を良好な基準にしない。[REW Making Measurements](https://www.roomeqwizard.com/help/help_en-GB/html/makingmeasurements.html) 出力音量を変えて測り直した場合は別Contextにする。HTDTへ品質情報が渡らない場合はunknownとして保持する。

USBマイクとHDMI出力は独立したクロックを持ち得る。位相・IRの比較を行う際はREWの音響タイミング基準とクロック補正の設定を記録する。HTDTが後から独自に補正して元の時間情報を復元したことにはしない。[REW Making Measurements](https://www.roomeqwizard.com/help/help_en-GB/html/makingmeasurements.html)

### C. 一つの変更を比較する

EQの差を見る場合は同じ配置・マイク点・音量・経路で、変えた項目だけを明示する。配置の差を見る場合は他の条件を維持する。複数条件が変わったデータも保存できるが、因果を一つに決めない。

可能ならA→B→Aの順で基準へ戻すか、各条件を繰り返す。長い測定や配置移動では位置ずれ・暗騒音・機器状態の変化が混ざるため、同条件の差とA/B差を同じ帯域で併記する。基準へ正確に戻せなければ「復帰測定」と断定せず、新しい配置版と位置精度を記録する。

YamahaのThroughはParametric EQを使わない設定である。距離・レベル・低音振り分けを含む全処理が無効になったという意味の「YPAO OFF」には置き換えない。Straight、Pure Direct、EQ Throughも別フィールドに記録する。[Yamaha RX-A4A User Guide — p.213](https://data.yamaha.com/files/download/other_assets/5/1335595/AV19-0070_RX-A4A_user_guide_En_UCRABGLFP_H0.pdf)

### D. 原本と出力を残す

REWで.mdatを保管し、HTDT用のテキストを書き出す。原本は取込後も変更しない。REWの現在の表示設定と、実際に書き出されたデータの処理状態を区別する。

v0.1で指定形式を再現するため、1測定につき1ファイル、小数点ピリオド・桁区切りなし、対応済みの区切り文字を選び、出力帯域・分解能・平滑化をメモする。複数測定を1ファイルにまとめた出力は初期対象外とし、個別出力へ案内する。出力ダイアログの選択を固定することと、REW内部の応答が未処理であることを混同しない。[REW File Menu](https://www.roomeqwizard.com/help/help_en-GB/html/file.html)

テキストのほか、指定された.mdat・校正ファイル・設定メモもHTDTへ添付してプロジェクト内へコピーする。添付は解析対象と分けて管理し、バックアップへ含める。一つの.mdatと複数の測定の対応を確認する。未提供の添付は不足として表示するが、テキスト取込を妨げない。

## 5. v0.1のファイル取込契約

最初の対応形式は、対象REW版の「Export measurement as text」で出した周波数・レベル・任意の位相列。一つの形式を実ファイルで確認してから対応範囲を広げる。任意のCSV、FRD、インピーダンス、全グラフ出力への汎用対応は約束しない。

REW出力には分解能や平滑化などの設定差がある。間引かれたデータから元の狭いピークを復元することはできない。[REW File Menu](https://www.roomeqwizard.com/help/help_en-GB/html/file.html)

### 初期対応プロファイル

| 項目 | 初期契約 |
|---|---|
| テキスト | UTF-8（BOM可）/ASCII、CRLF/LF。その他は文字コード選択とプレビューを経て対応 |
| 数値 | 小数点はピリオド、指数表記可。小数カンマは自動推測しない |
| 区切り | 対応フィクスチャで確認したタブまたは空白をまず採用 |
| ヘッダー | コメントと列名を解析し、未解釈の行も原文を保持 |
| 周波数 | Hz、正、有限、厳密に増加。重複・逆順は行番号付きエラー |
| レベル | dB値と基準を保持。SPL/relative/dBFS/unknownを区別 |
| 位相 | degreeの任意列とphase_status（valid/absent/unknown）。列があるだけでvalidにしない |
| 校正・平滑化 | 元データに適用済みかを記録。不明を「未適用」にしない |
| 不正値 | NaN/Infinity、空の必須列、不一致の列数を報告。無断削除しない |
| 範囲 | 元の周波数点をそのまま保存。未測定帯域を補完しない |
| 非対応 | 理由と対応形式を提示。取込途中の測定を完成扱いしない |

分解能・校正・レベル基準・窓情報がヘッダーから分からなければ取込画面で補足できる。ユーザーが入力した情報にはmanualの来歴を付ける。ファイルの更新日時を測定日時と同一視しない。

REWは位相情報のない測定でも位相列へ0.0を出力する。ゼロ列を有効な実測位相と解釈せず、元測定の情報で判定できなければunknownにする。真にゼロ位相のデータもあり得るので全ゼロ判定だけでabsentにもしない。v0.1のレベル比較は位相不明でも使える。[REW File Menu](https://www.roomeqwizard.com/help/help_en-GB/html/file.html)

### 取込画面の流れ

1. ファイルの形式、列、単位、最初と最後の数値をプレビューする。
2. 実測/実測由来/予測/不明を確認し、実測には入力チャンネル、スピーカー群、測定点、配置版、AVR版を対応付ける。分からない条件はunknownとする。
3. 位相の有効性・品質・不明項目を確認し、可能な比較と制限を表示する。予測に実在のマイクやAVRを強制的に割り当てない。
4. 原本と正規化データを保存し、測定IDを表示する。

ファイル名によるチャンネル推定は候補提示まで。ファイル名だけで確定・上書きしない。複数ファイルは各ファイルの成否を表示し、成功したものだけの保存を明示的に選べる。失敗ファイルの再実行で成功済みの測定を重複登録しない。

重複は「同じバイト列の再取込」と「同一測定の別出力」で分ける。同じハッシュなら既存取込を提示し、同じ条件なら再利用、誤記なら条件の訂正版を作る。ヘッダー、平滑化、出力範囲を変えた再出力は別ハッシュでも同じ取得に属し得るため、ユーザー確認で既存Measurementへ別Datasetを追加する。元のDatasetと保存済み比較は保持する。

独立した測定でも量子化・丸め等で同一内容になり得るので、ハッシュを測定IDにしない。別の取得であるという確認と来歴があるときだけ、新しいMeasurementが同じRawAssetを共有できる。ファイル名・REW UUID・ハッシュのいずれか一つだけで自動統合や新規取得の確定をしない。

## 6. IR取込はv0.2の独立した契約

最初は対象版のREW IRテキストを優先し、WAVは書出し条件の添付情報があるものを扱う。

- IRの時間軸はstart_time_s + sample_index / sample_rate_hz。
- 振幅の基準、正規化、校正の適用、窓、t=0設定、サンプルレートを保存する。
- monoを最初の対応にする。複数チャンネルのWAVを黙って合算しない。
- WAVが録音音声か、デコンボリューション済みIRかを明示選択する。録音からのIR生成はREWに任せる。
- 正規化済みIRは時間的特徴の表示には使えても、絶対SPLを自動復元できない。
- 時間基準が不明なファイルは、同一IR内の相対反射時間と、測定間の絶対遅延の比較を区別する。
- マイクの校正を二重適用しない。FRとIRで補正状態が異なる可能性も保存する。

REWのIR書出しには正規化・窓・t=0位置等の選択があるため、WAV単独で同じ時間・レベル基準が保証されるとは扱わない。[REW File Menu](https://www.roomeqwizard.com/help/help_en-GB/html/file.html)

## 7. REW APIは任意の取込経路

ファイル取込と同じ内部モデルへ変換する。v0.1の必須要件にしない。

### 資料で確認したAPI上の注意

REW APIは既定127.0.0.1:4735、仕様は/doc.jsonで参照できる。GETはAPI稼働中に利用でき、自動スイープ制御はPro要件を伴う。数値配列はBase64化されたbig-endian float32。測定UUIDも同じ測定を複数読込みすると重複し得る。FRには線形または対数の周波数軸情報が付く。IRは取得時の正規化や単位に注意が必要。/roomsimも公開されている。[REW API](https://www.roomeqwizard.com/help/help_en-GB/html/api.html)

### HTDT側の設計

1. ユーザーが起動したREWへ接続する。自動起動は後で必要なら追加する。
2. 接続先、REW版、取得したOpenAPIのハッシュ、対応機能を記録する。
3. 測定一覧を取得し、ユーザーが選択したものだけをスナップショットとして取り込む。
4. ローカル測定IDを発行し、REW UUID・取得時刻・要求パラメータ・応答原文を来歴に残す。
5. 取得中に一覧・対象データが変わった兆候があれば中断または再取得する。複数GETに原子性があるとは仮定しない。
6. 取込後のデータはREWが閉じても利用できる。外部UUIDだけを保存するリンク方式にしない。

read-only段階はGETのみ。削除、名前変更、平滑化設定変更、測定実行、AVRへの操作は行わない。未知の版は機能を限定するかファイル取込へ案内し、404を空データとして成功扱いしない。

初期タイムアウト案は接続2秒、読取15秒。大きなIRは個別に設定可能とし、キャンセルできること。自動再試行は読取だけに限定し、取得失敗後にデータを上書きしない。

### 周波数軸とデコード

- 線形: f_i = start_frequency_hz + i × step_hz。
- 対数: f_i = start_frequency_hz × 2^(i / points_per_octave)。
- Base64をbig-endian float32として読んだ後、内部float64へ変換する。精度が回復したとは扱わない。
- 単位・平滑化・取得時の補間設定を明示して保存する。APIの既定表示条件に任せて「生データ」と呼ばない。
- REW APIでppoを指定すると、必要に応じppo/2の平滑化も適用される。例えばppo=96は条件により1/48 octave平滑化を伴う。HTDT内部の96 PPO比較グリッドと同じ処理ではないため、取込で機械的にppo=96を強制しない。要求値と応答の実際の平滑化を両方保存する。[REW API](https://www.roomeqwizard.com/help/help_en-GB/html/api.html)
- UUIDが曖昧な場合は読取りを確定せず、REW側の重複ロード整理またはファイル取込を案内する。

具体的なフィールド名・クエリー値は対象版のOpenAPIを正本とする。Webヘルプの例を全REW版に通用する固定スキーマとして実装しない。

## 8. REW Room SimulatorとAVRの境界

REWの部屋シミュレーション結果を利用する際は、まず手動で設定した結果を予測として保存できるかを評価する。測定と同じ取込経路でもevidence_type=predictedを指定できるようにし、出力形式だけで実測と判定しない。

/roomsimの状態読取と、HTDTからの座標・設定変更は別機能とする。後者はREWのシミュレーター状態を変えるため、明示的な実行、設定の退避、終了時の復元試行と失敗通知を設計してから追加する。実機音響測定を起動する機能とは混同しない。

AVRはv0.1で手入力のみ。後続でネットワーク読取を加える場合も、機種・ファームウェアで存在を確認できた公開機能に限る。EQ内部係数の自動取得やYPAO全状態の取得を前提にしない。


## 9. MeasurementQualityReport と downstream capability gate（Issue #172）

Native N60で保存済みのMeasurement/Dataset/RawAssetを正本とし、品質判定は別の不変 `MeasurementQualityReport` として追加する。REWの測定・取込engineは変更しない。

Reportはexact Measurement/Dataset hash、raw asset SHA-256、SceneRevision/content hash、measurement entity/point、利用可能な場合はAcquisitionContext ID/hash、quality algorithm/profile version/hashへ固定する。profileや閾値を変更した再評価は新しいReportを作り、旧Reportを書き換えない。

品質項目は clipping、noise/SNR、usable frequency band、timing reference、polarity、IR window/truncation、calibration-file provenance、repeatability を独立に `PASS | FAIL | UNKNOWN | NOT_EVALUATED` で保持する。SNR・polarity confidence・repeatabilityの閾値はprofileで明示し、未設定ならPASSを生成せず `NOT_EVALUATED` とする。FRのfrequency+dBだけからclipping/SNR/common timing等を推定せず、phase配列だけからcommon timing成立としない。詳細matrixとclaim gateは[Measurement quality authority](MEASUREMENT_QUALITY.md)を正本とする。

downstreamは単一quality scoreではなくclaim別 `ALLOWED | BLOCKED | UNKNOWN` を消費する。FR-onlyはmagnitude表示を維持する一方、phase/arrival/decay/common timing/calibrated response/repeatability/polarityを証拠なしに開放しない。`calibrated_response` はno-clipping、SNR、usable band、calibration provenance、AcquisitionContextを要求する。要求帯域があるconsumerは明示usable-band evidenceも満たす必要がある。

retakeは別Measurementとして保存し、append-only lineageでsupersedesとselected measurementを記録する。旧Measurement/Dataset/Reportは保持し、O50/O60のcalibration/holdout assignmentをretakeへ暗黙転送しない。


## 10. CalibrationPlan と re-measure loop（Issue #173）

validated measurement capabilityの範囲だけをCalibrationPlanへ渡す。magnitude系のtarget/gain/crossover/PEQはrequired-band付き`magnitude_response` gate、absolute delayは`common_timing`、polarity inversionは`polarity`を要求する。phase配列だけではcommon timingを開かず、magnitude-only measurementからphase correctionを生成しない。初期authorityではcoherent inter-channel phase correctionが未成立のためall-pass correctionは明示unsupported。

Planはdevice-neutralなchannel/role/source entity/physical output mapping、sample rate、gain、delay、polarity、crossover、ordered PEQ、target curve normalization、device constraintを保持する。generic biquad exportではrequested planと量子化後actual settingsを別hashで保存し、filter countやboost/cut超過をsilent clip/omissionしない。

export、user-applied、remeasured、validatedは別のappend-only lifecycle stateであり、export操作だけでは実機適用・as-built・validatedにならない。再測定はVerificationMeasurementPlanでexact exported settings、scene/system、measurement point、routing、reference level、required capability、before/after Measurement IDを固定する。詳細は[CalibrationPlan authority](CALIBRATION_PLAN.md)。
