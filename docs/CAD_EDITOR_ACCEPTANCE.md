# Native CAD editor — 受入仕様

> 2026-09-16 / **未実行の受入計画**。過去のPoC報告は[実装状況](IMPLEMENTATION_STATUS.md)へ分離する。
> 対象: Windows 11 x64、所有PC。計画の機能一覧を「検証済み」へ読み替えない。

## 1. 記録と対象範囲

各gateでcommit SHA、Python/Qt/PyVista/VTK版、Windows build、CPU/GPU/driver、RAM、画面解像度とDPI、fixture、結果、既知問題をPRへ残す。CI成功と実機mouse操作成功を分ける。必要な短い動画/画像はGitHubのPR添付等で残し、再生成可能な巨大出力をrepoへ増やさない。

実機操作は `C:\Users\ka092\Desktop\HTDT\` の最小checkout/検証環境で行う。通常の編集と成果の正本はGitHub。Windows/GPUを利用できない実行環境では受入を未実施と明記する。

## 2. 共通fixture

| ID | 内容 | 狙い |
|---|---|---|
| F1 | 6×4×2.4 m、FL/C/FR、MLP。左だけ家具、FRだけ既知aim、FLはunknown | 左右/正面/高さ、unknown保持 |
| F2 | 8頂点L字室。XY(m)=(0,0),(6,0),(6,4),(4,4),(4,2),(2,2),(2,4),(0,4)、高さ2.4 m | 凹polygon、頂点順、無効preview |
| F3 | F2＋wall opening＋wall別clearance＋2席/2測定点 | 壁の分割/結合と参照保持 |
| F4 | F1＋50 editable objects＋1,000 analysis markers | 通常負荷の操作計測 |
| F5 | F4＋10,000 markers＋後続で64³ scalar grid | glyph/LOD。N70追加gate |

F4/F5のentityとmarkerは別に数える。1,000 actorを必ず生成する設計にはしない。scene規模・triangle数・計算精度を記録し、数だけで速度を比較しない。

## 3. Gate一覧

| Gate / milestone | 手順 | 合格条件 |
|---|---|---|
| A01 / N05 | F1をQtInteractorへ表示し、pick→axis drag→数値修正→Esc→Undo→Save→終了→reopen | 値と向きが一致。ドラッグ完了1回が1 Undo。cancelが履歴を増やさない |
| A02 / N05 | standalone packageを開発venvに依存しない状態で起動 | Qt platform plugin/VTK DLL不足なし。自動browser起動なし。consoleの有無に依存せず終了 |
| A03 / N10 | Scene tree/viewport/Inspectorから順に選択、hide/lock、object削除、project再open | selection同期、ID保持、callback二重発火なし、削除actorが残らない |
| A04 / N10 | Save成功後にUndo、新操作、Save失敗、復旧snapshotからreopen | clean/dirtyとredo分岐が正しい。既存revision/測定が変わらない。失敗でもdraftを失わない |
| A05 / N20a | Top/Front/Side/Perspectiveでmove/rotate、unknown aimのspeakerをmove | 同じworld値、意図したaxis、unknown保持。床面と高さの編集が混線しない |
| A06 / N20a | drag中Esc、window外release、Alt+Tab、tool/project切替 | before値・gizmo・camera操作状態を復元。以降も通常操作可能 |
| A07 / N20b | zoomを変えながらvertex/edge/alignmentへsnap。2物体をまとめて移動 | 同じ候補の安定選択、snap feedback、相対配置保持。numeric入力と同じ検証 |
| A08 / N30a | 空sceneからF2をclick作図。頂点挿入/移動/削除、寸法入力 | マウスだけで形を作れる。数値精密化可能。自己交差は確定不可。boundsは自動更新 |
| A09 / N30b | F3の壁移動・分割・結合・削除→Undo | opening/constraintの参照が追跡され、曖昧な変更を無言で実行しない |
| A10 / N40 | 空project→部屋→3.0.2 speaker→seat/screen/furniture→保存→再open | 主作業をmouseで完遂、数値入力は精密化に使用。role/寸法/位置の復元一致 |
| A11 / N50 | 通路/壁離隔違反を作成し、該当理由を選択 | sceneで対象と距離を強調。feasibleを音質評価として表示しない |
| A12 / N60 | SceneRevision Aに測定を紐付け、Bで移動して比較 | Aの測定点が不変。ghost配置を識別。REW停止時も保存済み結果が表示できる |
| A13 / N60–N80 | jobを開始→編集→cancel→project変更後に遅延完了 | stale/取消結果を現在sceneへ自動適用しない。UI応答と終了が保たれる |
| A14 / N70–N80 | 非矩形室で矩形専用solverを選択、候補をpreview/適用 | 非対応/近似を区別。入力版を固定し、適用は1 commandでUndoできる |
| A15 / N90 | package更新、project backup/restore、アンインストール | app binaryとuser data分離。データ復元と再起動が成立 |

A01/A02は技術選定の終了条件。失敗理由を記録し、一回の改善sliceでも解消しない構造的問題があれば同fixtureで代替構成を比較する。軽微な不具合だけで全面rewriteしない。

## 4. UI・DPI・性能の測り方

N05/N20の操作gateは100%、150%、200% DPI（可能なら異なるDPIの画面間移動）でpick位置とhandle位置を確認する。1280×800 logical sizeを最小設計目安、1440×900を通常layoutの確認条件とし、実画面が小さい場合は折り畳みとdock復元を確認する。browserの390 px mobile layoutはnative製品gateから外す。

次は**目標値であり測定結果ではない**。所有PCで条件を固定し、未達時は改善または根拠付きで目標を改訂する。

| 対象 | 初期目標 | 計測 |
|---|---|---|
| pick / selection feedback | p95 ≤100 ms | 入力時刻から次の対応render完了、30回以上 |
| drag / orbit、F4 | p95 frame time ≤33 ms、100 ms超の停止なし | 同じ操作を30秒、warm-up後計測 |
| 数値変更のfeedback | p95 ≤100 ms | 確定から次のrender完了 |
| Save、F4、raw asset追加なし | ≤1秒 | DB commitまで。大規模raw importを混ぜない |
| repeat open/close | 10回で例外・残留worker・observer重複なし | メモリ推移とprocess終了を記録 |

OSから画面に見えるまでの厳密なinput latencyはrender計時だけでは測れないため、体感の引っ掛かりは動画と併記する。F5はN70で容量・負荷の測定後に別budgetを設定し、未知の性能を約束しない。

## 5. 短い利用確認

N40で初見操作として「L字室を作り、speakerを左右に置き、座席へ向け、距離を直し、誤操作をUndoし、保存」を行う。所要時間、迷った箇所、誤選択、説明を要した箇所を記録する。主操作はtooltip/短いlabelで発見でき、manualを読まないと分からない導線を修正する。見た目だけの承認でgateを通さない。

色以外にshape/line/labelで選択、unknown、実測、予測、禁止状態を区別する。focus可視化、keyboard数値操作、単位、十分なhit targetを確認する。

## 6. 検証コスト

文書・余白・色など可逆で低影響の変更へ新規testを追加しない。実装時に回帰損害がある不変条件（座標変換、Undo/cancel、形状/参照整合、保存原子性、stale結果の抑止）だけ必要な自動testを選ぶ。

Qt/VTKの実機操作を、headless CIやpixel一致testの成功で代替しない。今回の文書レビューではrelative link・milestone・差分・根拠を検査し、Windows実機gateは実行しない。既存CIが自動実行される場合、その結果もPRへ記録する。


## 7. Issue #118 UX-series acceptance

Issue #118は既存A01〜A15のdomain/editor correctnessを置き換えない。UX-seriesは**発見性・情報設計・layout安定性**を追加で受け入れる。

| Gate | 手順 | 合格条件 |
|---|---|---|
| UX-A01 / UX110 | 新規projectを開きOverviewだけを見る | Room作成、既存project確認、REW importへの入口をmanual説明なしで発見できる。current workspaceとsave stateが明確 |
| UX-A02 / UX110 | `Ctrl+K` で「部屋を作図 / スピーカーを追加 / REWを読み込む / 予測を実行 / 候補を比較」を検索 | commandまたは該当workspaceへ到達。利用不可なら短い理由を表示。内部class/job名は出さない |
| UX-A03 / UX120 | F2をRoomで作図→speaker/seat配置→選択/寸法/aim編集 | viewportが主領域。Geometry/Objects/Speakers context以外の不要controlを常設しない。selection Inspectorが一致 |
| UX-A04 / UX130 | REW fixtureをimport→speaker/seatへassignment→quality確認→prediction比較 | import/assignment/quality/compareの順が画面上で理解でき、巨大right dockや複数nested scrollを必要としない |
| UX-A05 / UX140 | SearchSpec作成→candidate生成→objective/Pareto比較→MeasurementPlan→validation | Setup/Candidates/Compare/Measure-Validateの現在地が明確。internal IDsを知らずに完遂できる |
| UX-A06 / UX150 | dark appearanceで1280×800と1440×900、100/150/200% DPIのOverview/Room/Measurements/Optimizeを巡回 | primary controlのclipping/overlapなし。accidental horizontal scrollなし。surface hierarchy、label baseline、control height、spacing、accent usageがtokenに従う |
| UX-A07 / UX160 | Room→Measurements→Optimize→Overviewを往復し、entity/resultをdeep-link | SceneRevision、selection、stale、measured/predicted capabilityが矛盾しない。戻る/移動で古いresultをcurrentへ誤適用しない |
| UX-A08 / UX160 | 初見task: L字室→3.0.2→REW import→比較→candidate確認 | primary actionの場所についてmanualを要求しない。迷った箇所・誤操作・説明が必要だった箇所を記録し、未解消ならgate未達 |
| UX-A09 / UX150 | dark RoomでF2/F4をViewing/Edit/Acoustics focus modeに切替え、orbit/select/drag/overlay比較 | room edge・surface orientation・speaker/seat・selection/gizmoが背景から識別可能。grid/labels/chromeがgeometryより目立たず、overlayを重ねても主対象を見失わない |
| UX-A10 / UX150 | hover→press→drag→commit/cancel、Inspector selection変更、workspace/context移動を連続実行 | feedbackが一貫し、layout jumpやfocus lossがない。motionは因果関係を示すだけで、操作を待たせない。interrupt可能 |
| UX-A11 / UX150 | FR/heatmap/waterfallをdark appearanceでmeasured/predicted/stale/warning付き表示 | trace/grid/textが読め、scientific colormapとselection/warning colorが競合しない。色だけに依存せず状態を識別できる |
| UX-A12 / UX120 | 3D viewportで中button drag→Shift+中button drag→wheel→右click、M/R/F/Home、Esc/Enter、Ctrl+Z/Y/S/Dを連続実行 | MMB=pan、Shift+MMB=orbit、wheel=zoom、RMB=contextが競合なく成立。shortcutはtextbox focus中にsceneへ誤発火せず、tool hint/menuの表示と一致 |
| UX-A13 / UX150 | 概要/部屋/測定/最適化、menu、tooltip、empty/blocked/error stateを巡回 | GUIは日本語が基本。内部class/job/schema名や不要な英語labelを露出しない。REW/FR/SPL/RT60/CPU/GPU/CAD/3D/dB/Hz等の合理的な英語・略語例外は一貫して使用 |
| UX-A14 / UX160 | 初見ユーザーにshortcut一覧を事前提示せず、部屋でpan/orbit/zoom/move/rotate/fitを実施 | 主要操作はtoolbar tooltip/context hintから発見でき、MMB navigationは一般CAD操作として自然に完遂できる。shortcutを知らなくてもUI操作でも同じcommandへ到達できる |
| UX-A15 / O90D | 最適化で同じ候補のnominalと設置誤差評価を開き、±位置/aimの感度・sampled envelope・3D tolerance overlayを確認 | nominalとrobustnessが別指標として見える。bounded intervalだけでpercentileを表示しない。infeasible sampleを隠さない。3D envelopeはsampled evidenceか近似かを識別でき、overall scoreでA/Bを勝手に一本化しない |
| UX-A16 / O100G | current 3.0.2 fixtureから仮想SL/SRを追加し、左右の配置可能範囲・role/source modelを設定→candidate生成→3.0.2/5.0.2比較→1案applyまで実施 | baseline SceneRevisionはproposal作成で変化しない。SL/SRはproposed badge/ghostで識別できる。directivity等が不足するobjectiveはdisabled reasonを表示する。candidateはG10/O80 constraintを通り、apply時のみ新SceneRevisionを作る。未設置SL/SRへmeasured badge/evidenceを付けない |

### UX visual evidence

UX160では同一fixtureでOverview / Room / Measurements / Optimizeの代表screenを保存し、少なくとも以下を比較する。

- 1280×800 @100%
- 1440×900 @100%
- 1440×900 @150%
- 可能なら200%

pixel-perfect一致は要求しない。代わりにlayout integrity、text visibility、control alignment、scroll behavior、active state、warning/state badgeを確認する。

headless screenshotは補助資料にできるが、Windows実機mouse/focus/DPIの代替にはしない。RDCを使う場合はUX160の一回へ可能な限りまとめる。

### UX acceptanceの停止条件

以下が残る場合は「見た目は改善した」としてcloseしない。

- primary taskが複数toolbar/dockに重複していて入口が曖昧
- user-facing画面でUUID/hash/schema/job等を理解しないと操作できない
- 1280×800で主要controlが画面外へ押し出される
- DPI変更でbutton/textが重なる
- Measurements/Optimizeが一枚の巨大scroll formのまま
- selection/contextと右panel内容が一致しない
- Overviewが単なる数値dashboardで、actionable deep-linkを持たない
- pure black背景＋高彩度neonでしか階層を作れていない
- 3D grid/chrome/labelsがroom geometryやselectionより目立つ
- hover/pressed/focus/disabled/selectedの区別が曖昧
- decorative blur/glassがplot/table/textの可読性を下げる
- animationが操作完了を待たせる、または常時動いて注意を奪う
- 右drag orbitと右click contextが競合する等、navigation gestureが曖昧
- 中button pan / Shift+中button orbit / wheel zoomが一貫して使えない
- button/menu/navigationに不要な英語が残り、日本語と英語が無規則に混在する
- 直訳調の長い日本語でbutton/panelが肥大化する
- 英語placeholderでlayout gateを通し、日本語実文字列でclippingする
- O90のbounded toleranceを確率分布として表示する
- nominalとrobustnessを一つの不透明な総合scoreへ縮約する
- O90のinfeasible perturbationやsampling不足をUIから隠す
- O100 proposal作成だけでbaseline SceneRevisionを変更する
- 仮想speakerと実際に設置済み/実測済みspeakerを同じ表示・evidence stateにする
- source/directivity/SPL dataが無いのにcoverage/headroom objectiveを数値化する
- baselineに存在しないchannel responseを0としてtopology比較する
- routing/phaseが未定義のmulti-channelをcoherent sumして「system FR」と表示する
