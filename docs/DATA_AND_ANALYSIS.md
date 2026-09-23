# データ、比較計算、音響モデルの設計

> 初版: 2026-09-15 / 測定・比較データの設計契約。個別項目の実装・検証状態は[IMPLEMENTATION_STATUS](IMPLEMENTATION_STATUS.md)を参照。
> 2026-09-16追記: native editorのSceneRevision/AcquisitionContext分離とVTK座標変換は[CAD_EDITOR_SPEC](CAD_EDITOR_SPEC.md)を正本とする。以下のThree.js変換は旧browser実装用でありnative rendererへ流用しない。
> 取込元ごとの条件は[測定・連携仕様](MEASUREMENT_WORKFLOW.md)、検証方法は[ロードマップ](IMPLEMENTATION_ROADMAP.md)を参照。

## 1. 履歴モデル

初期から完全なイベントソーシングを導入する必要はない。不変のスナップショットと参照で過去を保存する。

| エンティティ | 主な内容 | 不変条件 |
|---|---|---|
| Project | ID、名称、schema_version、現在の配置版 | 「現在」を変えても過去の参照を変えない |
| RoomRevision | 形状、寸法、開口/家具状態、寸法精度、音速条件 | 形状や状態を変えたら新しい版 |
| Speaker | 物理個体ID、表示名、型番、メモ | role名を個体IDとして使わない |
| LayoutRevision | RoomRevision、個体ごとの位置/向き/役割、測定点一覧 | 保存した版の座標を上書きしない |
| MeasurementPoint | 配置版内ID、座席ID（任意）、カプセル座標・方向・位置精度 | 座席中心と実際のマイク位置を区別 |
| AVRConfiguration | 機種、入力、処理設定、サイズ、距離、レベル、クロスオーバー | 変更時に新しい版、未知値を保持 |
| MicrophoneProfile | 版ID、型番、個体、校正原本のハッシュ、校正方向 | 校正や方向の訂正は新しい版。旧測定の参照を変えない |
| AcquisitionContext | 配置版、AVR版、マイク版、点、経路、ゲイン、測定設定 | 一つの測定が参照する条件を固定 |
| MeasurementSession | 日時、目的、メモ、測定ID群 | 途中で変わった条件を一括上書きしない |
| Measurement | 独自ID、版ID、Session、Context、入力、音源群、evidence_type、品質評価 | 取得の同一性と訂正版を分け、外部IDやファイル名を主キーにしない |
| Dataset | FR/IR等、配列、単位、原本、処理状態、不変の版ID | 再解析・単位訂正も新しい版。元データを変更しない |
| ComparisonSpec | 入力Measurement版/Dataset版/Context版、A/B順、帯域、有効区間、補間・重み・レベル調整、算法版 | 測定の最新版へ参照を自動更新しない |
| AnalysisResult | 入力ハッシュ、算法版、パラメータ、結果、分類 | 入力や算法が変われば別の結果 |
| ImportRecord | 原本ハッシュ、取込時刻、形式、parser_version、警告 | 再取込と新しい測定を混同しない |
| RawAsset | ハッシュ、相対パス、種別、元の名前、サイズ | 数値原本と添付原本を区別し、同じ内容を共有可能 |

「古い測定へのメモ追記」と「古い測定条件の訂正」は別操作。条件の誤記を直す場合は訂正版と修正理由を残し、旧版も参照可能にする。配置を複製しただけの未測定の候補に、既存測定を移して「新しい配置の実測」と見せない。

### 訂正と保存済み比較

比較が参照する配置・AVR・マイク・Context・Datasetの版は不変とする。MeasurementPointはLayoutRevisionとの組で識別し、Contextの点が別の配置版に属していれば保存を拒否する。Speakerの表示名は編集可能でも、測定時の型番・役割・基準点は配置版に残す。未確認の座標やAVR条件には明示的なunknownを使い、取込時の現在配置を測定時の事実として補わない。

訂正は旧版を置換せず、新版・訂正理由・旧版への参照を同じトランザクションで保存する。例: マイク方向の誤記を直しても、保存済み比較C1は旧Contextを保持して「訂正版あり」と表示する。訂正版で再評価するとC2を作り、C1の警告や数値を上書きしない。

v0.1から比較保存時のAnalysisResultも残す。入力版とハッシュ、適用した判定・警告、グリッド条件、有効点数、オフセット、差分配列と要約値を保持する。分類・品質評価もMeasurement版に固定し、後の訂正で旧比較の根拠を変えない。アプリ更新で旧算法を実行できなくなっても保存済み結果を表示できるようにし、新算法での再計算は別結果にする。全旧算法を永続的に実行可能にする基盤は不要。

### 所有情報と値の出所

重要な条件にはvalueとsource（imported/manual/derived/unknown）を保持する。captured_atとimported_atを分け、取得日時の元文字列・タイムゾーン・不明状態を保存する。日時が不明なら取込日時を測定日時に代入しない。

部屋・AVR・マイクなどの構造は必要項目から始め、任意メタデータを追加できるようにする。単独ユーザー向けに汎用プラグイン基盤や複雑な継承階層を作らない。

## 2. 入力とスピーカー役割

speaker_idは物理個体、roleはその配置での役割、input_roleは測定信号の入力先。これらを別の値として扱う。

初期の役割はfront_left、front_center、front_right、surround_left/right、surround_back_left/right、height_front/middle/rear_left/right、subwoofer、other。サブの番号は個体ラベルで表し、入力LFEとは別物にする。高さにはmounting_type（front_height / ceiling / upfiring / other / unknown）を添え、ベンダーの端子名も保存する。

~~~text
Measurement
  id: HTDT UUID
  revision_id: 不変の版ID
  session_id
  acquisition_context_id?  # 実測ではunknownを表せるContextを参照
  input_role: "center"
  source_speaker_ids: ["speaker-C", "speaker-FL", "speaker-FR"]
  radiation_scope: single | bass_managed | mixed | unknown
  routing_evidence: verified | manual | inferred | unknown
  captured_at?
  evidence_type: measured | derived | predicted | unknown
  datasets[]
  provenance
~~~

v0.1の取込時からREW演算結果はderived、シミュレーター結果はpredictedとして分類する。分類を保存することと、HTDTで演算・予測を実装することは別である。形式だけで判別できなければunknownを既定とし、measuredへの確定には取得元情報またはユーザーの明示選択を残す。unknownは実測の根拠として自動診断へ投入しない。

derivedは分かる範囲の入力・演算・元データ参照を来歴に残す。predictedはモデル・幾何版・設定・出力条件を来歴に残し、不明ならunknownとする。実際に使っていないマイクやAVRのAcquisitionContextを必須にして捏造しない。v0.1では来歴メタデータとして保存できればよく、予測エンジン用の汎用スキーマは後続で決める。

## 3. 座標契約

旧計画の操作上の分かりやすさを保ち、変換を明示する。

| 軸/項目 | HTDTの規約 |
|---|---|
| 原点 | 正面スクリーンに向かったときの左前床隅 |
| +X | 正面を向いたときの右 |
| +Y | 部屋後方 |
| +Z | 上 |
| 単位 | m、秒、Hz。UIでcm/msにしても保存値は変えない |
| 矩形室の範囲 | 0≤x≤W、0≤y≤D、0≤z≤H |
| スピーカー座標 | 音響基準点。どの点かをメモし、筐体位置と分ける |
| マイク座標 | カプセル中心 |
| 向き | 単位方向ベクトルaim_xyz。初期はrollを使わない |

表示用のyawは正面（-Y）を0°、右（+X）を+90°。pitchは水平0°、上向きを正。方向ベクトルは(cos(pitch) sin(yaw), -cos(pitch) cos(yaw), sin(pitch))。UIの角度はdegree、三角関数入力はradian。

この物理的な軸の並べ方は左手系になる。右手系を仮定するライブラリへそのまま姿勢を渡さない。Three.js側は(x_t, y_t, z_t)=(x, z, y)とし、スクリーンが-Z、上が+Yになる。変換行列の行列式は-1なので回転行列として扱わず、点と方向を変換して描画オブジェクトを組み立てる。将来メッシュを取り込む場合は法線・面の巻き順も確認する。

任意シミュレーターには専用アダプターを置く。たとえば右手系の音響計算座標を(x, D-y, z)とする場合、方向は(dx, -dy, dz)。REWの座標規約への変換は対象版で確認し、同じ値をそのまま送らない。

座標の正しさは対称な部屋だけで確認しない。左右非対称の3点、向き、正面、座席から見た左右を使って往復と表示を検証する。角度ガイダンスでは座席の前方向を基準にし、部屋の正面と一致するとは仮定しない。

## 4. 周波数応答・IRの内部契約

| データ | 必須項目 | 任意/不明を許す項目 |
|---|---|---|
| FrequencyResponse | frequency_hz[N]、level_db[N]、level_reference、source_hash、phase_status | phase_deg[N]、サンプルレート、校正状態 |
| ImpulseResponse | samples[N]、sample_rate_hz、amplitude_reference | start_time_s、timing_reference、t_zero/shift、校正状態 |
| ProcessingState | smoothing、resolution、normalization、window、calibration_applied | 各状態のunknown |
| Provenance | source_kind、importer_version、imported_at、原本参照 | REW版、外部UUID、測定日時 |

level_referenceはspl/relative/dbfs/other/unknown。SPLという列名と、絶対音圧校正が有効であることも分けて記録する。calibration_appliedはtrue/false/unknownとし、周波数校正と感度校正を区別する。

FRは元の周波数点を保持し、保存時の等間隔化・平滑化は行わない。位相は存在するときだけ同じNを要求する。周波数・レベルは有限値を要求し、位相の欠測を扱う拡張では明示的なvalidity maskを導入する。位相列を部分的に0で埋めない。

phase_statusはvalid/absent/unknownとし、列の存在と有効性を分ける。REWテキストでは位相なしでも0.0が出力され得るため、全ゼロだけでvalidにもabsentにも決めない。確認できない列は原本に残し、unknownとして位相依存の計算から除く。validでも共通タイミング基準が確認できるとは限らない。[REW File Menu](https://www.roomeqwizard.com/help/help_en-GB/html/file.html)

IRでstart_time_sが不明なら、sample indexを時間へ換算できても共通のt=0があるとはしない。配列の単位がPercent、normalized amplitude、Pa等のどれかを明示し、dB化された表示値と線形IRサンプルを混ぜない。

## 5. 比較してよい条件

比較は三段階で判定する。

| 判定 | 例 | 動作 |
|---|---|---|
| 計算可能 | 同じ基準、共通帯域、必要な条件がそろう | 指標を表示 |
| 条件付き | EQ、配置、窓、音量などが違う/不明 | 差を表示し、変化項目と制限を併記 |
| 計算不可 | 共通帯域なし、単位が非互換、欠測しかない | 該当指標を出さず理由を表示 |

「条件が違うから全比較を禁止」にはしない。比較したい差をintentional_differenceとして指定する。ただし既知の単位不整合を自由入力で上書きして数値計算する設計にはしない。

絶対レベル差には同じ校正・ゲイン基準が必要。音量が違うデータはそのまま表示可能だが、音響改善として解釈しない。レベル調整による形状比較は任意操作とし、調整量と基準帯域を常時表示する。

平滑化・窓が異なるデータはその差を表示する。既に平滑化済みのデータを「unsmoothedへ復元」しない。周波数応答の窓変更やETCの表示設定は同一視しない。

Measurement版にはquality_status（usable/warning/invalid/unknown）、理由、確認元、再測定グループを記録する。同じAcquisitionContextを共有する測定でも品質は個別に評価する。クリッピングや誤チャンネルなど既知の不良測定は保存・参考表示を許すが、品質評価の指標と自動候補比較から除外する。暗騒音や再現性が不明なら条件付きとし、有限のFR値だけから良好なS/Nを推定しない。同条件の繰返しを比較対象と並べ、A/B差が繰返しの差と同程度なら改善と断定しない。2回の測定だけから統計的な信頼区間は作らない。

## 6. v0.1の指標を明確にする

初期版は周波数応答のレベル比較のみ。複素和、位相差、群遅延、RT60をMVPの指標に含めない。

### 共通グリッド

指標ごとにA/Bの2入力を指定する。比較帯域は、ユーザー選択とその2入力の有効帯域の共通部分。重ね描きの3本目を追加しても、保存したA/B指標の帯域を変えない。複数ペアを順位比較する場合は全候補に共通の評価帯域・有効区間を明示的に固定する。

グリッドの初期契約はf_k = 1 Hz × 2^(k/96)。kは整数で、正の下限以上・上限以下の点だけを採る。帯域端の追加点は作らない。1 Hzの基準と96 PPO、採用したk範囲、端点判定の算法版を保存する。評価帯域とレベル調整の基準帯域には、同じ規約で別々の有効点集合を作る。

レベルはlog2(f)軸上の線形補間で揃える。これは表示・指標用の再標本化であり平滑化ではない。粗い出力を96 PPOに増やしても分解能は上がらないことを表示する。外挿せず、無効区間をまたぐ補間もしない。指標に使う点数は独立した実測の標本数ではない。

無効区間は元データの明示情報またはユーザー指定から作り、その出所を保存する。既知の出力グリッドに欠番があれば警告して再出力か区間除外を求める。未知の不等間隔データは間隔が広いだけで欠測と断定せず、最大間隔と解像度不明を表示する。無効区間に接する補間区間も使用せず、A/Bの両方で有効な点のみ採用する。自動の欠落判定を後で追加する場合は閾値と算法版を別途定義する。

要求帯域、実際の共通帯域、除外区間、全グリッド点数と有効点数・割合を保存する。除外を含む指標は「有効区間のみ」と表示し、欠けた帯域を含む全帯域の改善値と呼ばない。評価帯域の有効点が2未満なら平均差/RMSを出さない。基準帯域の有効点が2未満ならレベル調整と形状RMSだけを不可とし、生の差分は評価帯域の条件に従う。これは計算の最低条件であり、測定精度の保証ではない。

### 差分とレベル調整

共通グリッド上のA、Bのレベルをa_i、b_iとする。差の向きは常にA−B。

- 生の差: d_i = a_i − b_i。
- 平均差: mean(d_i)。
- RMS差: sqrt(mean(d_i²))。
- 形状比較: 指定した基準帯域でo = mean(a_i − b_i)を求め、Bにo dB加える。
- 形状RMS: sqrt(mean((a_i − b_i − o)²))。評価帯域は基準帯域と別に記録する。

グリッドが対数等間隔なので、各点を同じ重みとする指標は対数周波数上の評価になる。線形Hzの平均値と混同しない。評価帯域例は60–200 Hzなどを任意選択できるが、20 Hzまで有効な実測があると仮定しない。

同じ定数レベル差を形状比較で消しても、その差は生の平均差として残す。「補正の効果」と誤認させない。

### 平均と足し算の区別

初期は測定の算術平均や同時再生の予測を実装しない。追加する場合は定義を選択する。

| 演算 | 定義 | 意味/必要条件 |
|---|---|---|
| dB平均 | mean(L_k) | レベル曲線の平均。物理的な音圧和ではない |
| エネルギー平均 | 10 log10(mean(10^(L_k/10))) | パワー相当の平均。単位・校正を揃える |
| コヒーレント和 | 20 log10(abs(sum(H_k))) | 複素応答、同じ時間・レベル基準が必要 |

別々に測ったFL/FRのdBを足してL+Rの実測を再現したことにしない。周波数ごとの位相があっても、共通タイミング基準が不明ならコヒーレント和は無効。

## 7. 音響モデルの適用範囲

### L0: 距離・角度

実測座標からの幾何計算。座標精度、基準点、座席の向きを結果に持たせる。機器の距離設定値は遅延調整のパラメータであり、必ずしもメジャーで測った距離と等しくないため別保存する。

### L1a: 矩形室の固有周波数

内部の寸法をW、D、H、音速をcとすると、

~~~text
f(nx, ny, nz) = (c / 2) × sqrt((nx/W)^2 + (ny/D)^2 + (nz/H)^2)
nx, ny, nz は0以上の整数、(0,0,0)を除く
~~~

非ゼロ成分が1/2/3個なら軸/接線/斜めモード。指定f_maxまでの次数を各寸法から制限する。縮退周波数をまとめても元のモード次数は残す。

cはパラメータとして保存し、初期値343 m/sは仮定と表示する。これは固有周波数候補であって、ピークの大きさや最適配置を直接返さない。音源・受音位置、損失、開口等が応答を変える。[Genelec Monitor Placement](https://www.genelec.com/monitor-placement)

非矩形や大きい開口がある部屋では厳密なモードとして表示しない。明示的に選んだ矩形近似モデルだけに「概算」のラベルを付ける。寸法の不確かさを変えた候補範囲も後から表示できる設計にする。

### L1b: 境界反射による干渉候補

壁までの距離dだけから全ての谷をc/(4d)で決めない。初期は単一平面・鏡面反射・位相反転なしの仮定で、実音源s、受音点r、鏡像音源s'から求める。

~~~text
L0 = norm(r - s)
Lr = norm(r - s')
delta_L = Lr - L0
delta_t = delta_L / c
f_k = (2k + 1) × c / (2 × delta_L), k = 0,1,...
~~~

delta_Lが0に近いときは候補を出さない。鏡像経路が反射面の内側に当たること、開口や遮蔽で成立しないことを検査する。無限平面の近似しかできない場合はその旨を明記する。

c/(4d)はdelta_L≈2dとなる特定配置の簡略式。谷の深さは振幅比、反射位相、指向性等に依存するため、この計算から推定しない。複数面の候補が近いだけで原因を一つに確定しない。

### L2: IR/ETCと反射候補

最初に同一IRの直接音基準の相対時間を使う。直接音は必ずしも最大ピークではないため、検出候補をユーザーが確認・修正できるようにする。

ETCの初期定義案は、線形IRのHilbert解析信号h_aから20 log10(abs(h_a)/A_ref)を計算し、指定床値で表示を制限する。基準A_ref、帯域処理、端点処理、窓、正規化を記録する。REWとの比較時は同じ処理条件を揃え、同名だから同じ数値になると仮定しない。[REW Impulse Graph](https://www.roomeqwizard.com/help/help_en-GB/html/graph_impulse.html)

予測到達時間との一致は反射候補を提示する根拠に留める。AVR遅延、スピーカー内部の群遅延、クロック差、t=0変更があるため、IRの絶対時刻を無条件に空間距離へ換算しない。

### L3: 応答予測

まずREW Room Simulatorの対象条件を評価する。後から必要ならpyroomacousticsを任意依存として比較する。image-source法を「低域で原理的に全く使えない」と切り捨てない。理想的な境界条件でのモデルの性質と、実部屋の境界・指向性を再現できるかは分けて判断する。

REWの矩形室モデルは損失境界を考慮した波動方程式の解に基づき、特定の条件で時間領域のimage-source法と対応する。[REW Room Simulator](https://www.roomeqwizard.com/help/help_en-GB/html/modalsim.html)

材料の吸音率、周波数依存性、音源の指向性・低域特性、開口等が不明なら絶対SPLや細かい谷の深さを確定的に予測しない。実測に合わせたパラメータと、それとは別の検証用測定を区別する。

## 8. 配置探索の制限

矩形室の固有周波数は部屋寸法を固定したまま音源を動かしても変わらない。固有周波数一覧だけを目的関数にしてスピーカー配置を最適化しない。無指向性の点音源モデルではtoe-inの効果も評価できない。

先に実装するのは、実測済み配置A/B/Cを同じ比較契約で一覧化し、位置移動量や設置制約と並べる機能。未測定位置の応答を実測から推定したことにはしない。

予測による探索を追加する条件は次のとおり。

1. 候補位置で計算できる応答モデルがある。
2. 応答のレベル基準・帯域・ターゲット・複数席の重みを定義できる。
3. モデル調整に使っていない配置の実測で、予測の傾向を検証できる。
4. 入力寸法・吸音等を妥当な範囲で変えても順位が極端に入れ替わらないか確認できる。
5. 家具との干渉、左右間隔、可動域、移動量の制約を満たす。

最初は少数の格子候補で確認し、必要な場合にSciPyを使う。候補は「試す順序」であり改善保証ではない。実測誤差より小さい差で優劣を断定しない。

## 9. 保存・バックアップ・移行

### v0.1の構成案

~~~text
project/
  project.sqlite3
  raw/<sha256>.<extension>
  backups/<timestamp>.zip
~~~

SQLiteはメタデータ、配列、比較設定を持つ。配列はdtype（float64 little-endianを初期案）、shape、単位、codec、content_hashを添えたBLOBとして保存する。Pythonのpickleを永続形式にしない。大きいIRを扱い始めた時に、外部.npz等へ移す必要があるか計測する。

### 数値原本と添付原本

RawAssetの種別は、数値取込元、REW .mdat、マイク校正、設定メモ/画像などを区別する。数値DatasetはImportRecord経由で数値取込元に結び、SessionやMeasurement版・MicrophoneProfile版は必要な添付原本へ参照を持つ。一つの.mdatに複数測定を含められるよう、多対多の対応とする。対応付けは手動確認し、ファイル名だけで推定確定しない。

.mdatは解析せずコピー・ハッシュ保存する任意添付。存在しなくてもテキスト取込は使えるが、「再解析用.mdatなし」と表示する。校正ファイルも外部パスやハッシュだけでなく、提供されたバイト列を保持する。取得できなければ未添付と表示する。各RawAssetは参照中に上書き・削除せず、再出力や新しい校正は別の原本として登録する。

### 取込の原子性

1. 原本を一時ファイルへコピーしハッシュを算出する。
2. 解析・検証に成功したら同一ボリューム内で原本の最終パスへrenameする。
3. DBトランザクションでImportRecord、配列、条件参照をまとめてcommitする。
4. 失敗時に完了したMeasurementを残さない。未参照原本は次回起動時に検出し、保留領域へ移せるようにする。

DBだけで外部ファイルまで原子的になったとは扱わない。まず原本を確保してから参照をcommitすることで、DBが存在しない原本を指すケースを避ける。既存の原本は上書きしない。

### 復元

SQLite稼働中のDBファイルだけをコピーしてバックアップ完了にしない。書込みを停止した短い整合点でSQLite Backup API等によりDBのコピーを作り、そのDBが参照する原本とmanifestをZIPにまとめる。[SQLite Backup API](https://www.sqlite.org/backup.html)

manifestにはスキーマ版、DBと原本のハッシュ、作成日時を記録する。DBコピーが参照する全RawAsset（.mdat・校正・設定添付を含む）を収集し、参照先が欠けていれば完全なバックアップとして成功扱いしない。出力中の一時ZIPは完成品と区別する。既定のbackups配下を再帰的にZIPへ取り込まない。

復元は新しいフォルダに展開し、パスとハッシュ・参照整合性を確認してから開く。元のプロジェクトへ無言で上書きしない。元の取込ファイルを移動した後でも、同梱したrawだけで再解析できることを受入条件とする。

### スキーマ移行と削除

v0.1からschema_versionを設け、将来の移行前にバックアップを作る。未知の新しい版は書込みせず説明を出す。移行失敗時に途中状態で開かない。

測定の通常削除はアーカイブ扱いにし、比較・来歴が参照する原本を自動消去しない。完全削除とディスク清掃は必要になってから、参照確認付きの明示操作として追加する。


## 10. MeasurementQualityReport の証拠契約（Issue #172）

Measurementの旧 `quality_status` 等は既存import互換として保持するが、downstream claimを開く正本はimmutable `MeasurementQualityReport` のclaim別capabilityとする。Reportは既存native Measurement/Dataset/RawAsset/SceneRevisionを参照し、それらのauthorityを再実装しない。

品質判定は一つのscoreへ縮約しない。clipping、SNR、usable band、timing、polarity、IR window/truncation、calibration provenance、repeatabilityを独立判定し、証拠不足を `UNKNOWN` または `NOT_EVALUATED` とする。SNR・polarity confidence・repeatabilityの閾値を暗黙defaultで捏造せず、profile未設定ならその項目は `NOT_EVALUATED` とする。`FAIL` は明示的に不適合な証拠がある場合だけ使用する。

AcquisitionContextは既存設計上の別authorityである。Report側はID/hash/source-kind参照のみを保持し、存在しないContextを推測生成しない。source-kindがunknownの参照ではcontext依存claimを開放しない。phase配列の存在だけではcommon timingを成立させず、reference identity、clock/sample rate、delay correction等の明示証拠を要求する。calibrated responseはさらにno-clipping、SNR、usable band、calibration provenanceを要求する。

retakeは別Measurementとして保持し、旧reportを上書きしない。supersedes/selected lineageはappend-onlyとし、O60のpreregistered calibration/holdout assignmentや既存validation evidenceを自動変更しない。詳細は[Measurement quality authority](MEASUREMENT_QUALITY.md)を参照。


## 11. CalibrationPlan / exported settings authority（Issue #173）

CalibrationPlanはMeasurementQualityReportのclaim gateを下流で再利用する。FR配列やphase配列の存在だけで不足capabilityを補完しない。Planはexact SceneRevision/SystemVariant/Measurement/Dataset/MeasurementQualityReport hash、channel/role/source entity/physical output mapping、sample rate、gain/delay/polarity/crossover/ordered PEQ、target curve/normalization、device capability constraintへ固定する。

requested planとactual exported settingsは別authorityとする。device resolutionによる量子化はexport snapshot側へ記録し、planを書き換えない。filter count、boost/cut、unsupported filter/output/order等を超える場合はclip/omissionせずunsupportedとする。exportはuser-applied/as-built/validatedを意味しない。

VerificationMeasurementPlanはexact exported-settings hash、scene/system、measurement point、routing、reference level、required capability、before/after Measurement lineageを保持する。詳細は[CalibrationPlan authority](CALIBRATION_PLAN.md)。
