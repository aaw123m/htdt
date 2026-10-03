# Native CAD UI / Interaction Design

> 改訂: 2026-09-18 / Issue #118 workflow-first UI/UX overhaul
> 実装順は[ロードマップ](IMPLEMENTATION_ROADMAP.md)、編集・保存契約は[SPEC](CAD_EDITOR_SPEC.md)、判定は[受入仕様](CAD_EDITOR_ACCEPTANCE.md)。
> 本書は従来の「viewportを常に主画面＋dock追加」方針を置き換える。

## 1. 設計問題と変更方針

現行native GUIは機能を追加するたびに `QToolBar` / `QDockWidget` / tree / formを継承階層へ足してきたため、最終workspaceで認知負荷とlayout競合が増えている。

現行composition:

~~~text
NativeEditorWindow
  -> RoomEditorWindow
  -> WallEditorWindow
  -> CadEditorWindow
  -> TheaterEditorWindow
  -> TheaterWorkflowWindow
  -> ConstraintEditorWindow
  -> MeasurementEditorWindow
  -> MeasurementWorkspaceWindow
  -> PredictionWorkspaceWindow
  -> OptimizationWorkspaceWindow
~~~

問題は配色ではなく**information architecture**である。今後は「機能追加=新dock」ではなく、task単位のworkspaceとprogressive disclosureで構成する。

## 2. UX benchmark — Home Theater Maestro

HTMの公開UI/Live Demoをworkflow benchmarkとして参照する。画面・asset・codeをコピーせず、以下の設計原則を抽出する。

Official references:
- https://hometheatermaestro.fr/en/
- https://hometheatermaestro.fr/en/features.html
- https://hometheatermaestro.fr/demo/?lang=en

Official screenshots:
- Room / 3D: https://hometheatermaestro.fr/images/screens/salle-complex-en.png
- Acoustics: https://hometheatermaestro.fr/images/screens/acoustique-en.png
- Measurements: https://hometheatermaestro.fr/images/screens/audio-en.png
- Overview: https://hometheatermaestro.fr/images/screens/synthese-en.png

参考にする点:

- global navigationを少数のdestinationに限定する。
- Room内だけでcontext navigationを出し、全機能を一つのtoolbarへ並べない。
- Overviewは状態一覧ではなく「次に何を直すか」を優先順に出す。
- 3DはRoom/Placementでは主役だが、Measurements/analysisまで無理にdockへ押し込まない。
- Advanced/provenance/internal IDは必要時だけ開示する。
- Measurementsはimport -> assignment -> quality -> comparisonと上から読めるpage構造にする。
- feature search / command paletteを持ち、場所を記憶しなくても操作へ到達できる。
- page title / section / card / field / badgeのvisual hierarchyを全画面で統一する。

## 3. Top-level information architecture

HTDTのuser-facing navigationはdomain/service境界ではなくtaskで構成する。

~~~text
プロジェクト
├─ 概要
│   ├─ 部屋・システムの要約
│   ├─ 準備状況 / 要対応
│   ├─ 最近の測定 / 予測
│   └─ 次の操作 -> 該当画面へ
│
├─ 部屋
│   ├─ 形状
│   ├─ 物体
│   ├─ スピーカー・座席
│   ├─ 音響
│   └─ 履歴
│
├─ 測定
│   ├─ 読み込み
│   ├─ 割り当て
│   ├─ キャンペーン
│   ├─ 品質
│   ├─ 比較
│   └─ 機器の準備
│
├─ 最適化
│   ├─ 探索設定
│   ├─ 候補
│   ├─ 比較
│   ├─ 介入計画
│   ├─ ばらつき耐性
│   └─ 測定・検証
│
└─ 取り込み / アクティビティ / ライブラリ / サポート / 設定
~~~

ヘルプはrailの常設destinationではなく、`Ctrl+K` のcommand paletteから `help` で検索して開く（ショートカット一覧・用語・workflow説明・障害対応の19 topic）。

Predictionを単独global destinationとして固定しない。

- spatial prediction -> Room / Acoustics
- predicted-vs-measured -> Measurements
- candidate batch prediction -> Optimize

とし、同じresult authorityをtask context別に見せる。

## 4. Shell

### Left rail

常設するもの:

- project selector + save state
- feature search / `Ctrl+K`
- 概要
- 部屋
- 測定
- 最適化
- workspace以外のapplication destination: プロジェクト / 取り込み / アクティビティ / ライブラリ / サポート
- 下端に 設定（ヘルプは常設buttonではなく `Ctrl+K` palette経由）

global destinationは原則4〜5個以内。icon-onlyを既定にせず、short labelを併記する。必要ならrail自体をcollapseできる。

### Top context bar

現在workspace内のsub-contextとprimary modeだけを表示する。

例（出荷済みのcontext名）:

- 部屋: 形状 / 物体 / スピーカー・座席 / 音響 / 履歴
- 測定: 読み込み / 割り当て / キャンペーン / 品質 / 比較 / 機器の準備
- 最適化: 探索設定 / 候補 / 比較 / 介入計画 / ばらつき耐性 / 測定・検証

Editor / Room / Wall / Object / Audio等の複数toolbarを常時併置しない。

### Main content

- Overview: summary / blockers / next actions
- Room: viewport-centric
- Measurements: page / plot / table-centric
- Optimize: candidate / comparison / chart-centric

**viewportを全画面で常に主役にする旧方針は撤回する。** 3Dが意思決定に必要なworkspaceでのみ主役にする。

### Right contextual inspector

selectionまたはactive taskに応じて必要なpropertiesだけ表示する。

- object selected -> transform / dimensions / acoustic role
- wall selected -> geometry / opening / material
- speaker selected -> role / pose / aim / source model

Constraint / Measurement / Prediction / Optimizationをpermanent dock tabとして常設しない。

### Bottom/status

短い状態だけ:

- saved / unsaved / stale
- units
- snap state
- active compute / cancel
- concise tool hint

長い説明、tree、設定formは置かない。

## 5. Overview / guided workflow

Overviewはnavigation authorityを持つ。

表示例:

- 部屋形状が未完成 -> 「部屋を完成させる」
- スピーカー役割が未設定 -> 「役割を設定」
- 測定がない -> 「REWを読み込む」
- magnitude-only measurement -> 「この測定ではタイミング/位相比較を利用できません」
- 予測条件が変更済み -> 「再計算」
- 検証が停止中 -> 短い理由 + 該当画面への操作

各actionはworkspace/entity/subsectionへdeep-linkする。

初回projectの軽いguided path:

1. 部屋
2. スピーカー / 座席
3. 測定（任意）
4. 予測 / 解析
5. 最適化

wizardで入力を強制せず、常にOverviewへ戻れる。

## 6. Command palette / feature search

`Ctrl+K` から以下を検索できる。

- navigation destination
- common command
- entity
- task: 部屋を作図 / スピーカーを追加 / REWを読み込む / 予測を実行 / 候補を比較
- 設定 / ヘルプ

検索結果はcurrent contextとavailabilityを反映し、disabledの場合は短い理由を表示する。

## 7. Room workspace

Roomだけは3D viewportを主作業領域にする。

### Contextual controls

表示するのはactive sub-contextに必要なものだけ。

Geometry:
- draw/edit room
- vertex/wall/opening tools
- dimensions

Objects:
- add palette
- move/rotate/duplicate
- hide/lock

Speakers & seats:
- role/template
- pose/aim
- placement constraints

Acoustics:
- materials/acoustic participation
- prediction overlay
- reflection/field layer controls

### Direct interaction / CAD shortcut contract

3D navigationは**一般的なCAD操作に寄せた中ボタン中心の既定値**を採用する。初期defaultはFusion系の操作感を基準にし、右buttonをcamera navigationとcontext menuで競合させない。

| 入力 | 動作 |
|---|---|
| 左click | 選択。空白clickで解除 |
| Ctrl＋左click | 選択を追加/解除 |
| 選択対象のhandle drag | active plane/axisで変形 |
| **中button drag** | **pan / 画面移動** |
| **Shift＋中button drag** | **orbit / 視点回転** |
| **wheel** | **cursor近傍を基準にzoom** |
| Shift＋中buttonの開始位置 | （未実装）orbit pivotの候補 — 現行のorbit pivotはcamera焦点（F/Homeのfit先に追従） |
| 右click | context menu。camera操作には使わない |
| Esc | active操作をcancel。Idleでは安全なSelect状態へ |
| Enter | sketch / numeric edit等、現在操作を確定できる場合にcommit |
| Ctrl+Z | 元に戻す |
| Ctrl+Y / Ctrl+Shift+Z | やり直す |
| Ctrl+S | 保存 |
| Ctrl+O / Ctrl+N | （未実装）開く / 新規project — 現行は「プロジェクト」destination画面で作成・切替 |
| Delete / Backspace | 選択対象を削除 |
| Ctrl+D | 複製 |
| Ctrl+A | すべて選択 |
| Ctrl+I | 選択を反転 |
| F | 選択対象へfit |
| Home | scene全体へfit |
| M | 移動tool |
| R | 回転tool |
| D | （未実装）Room sketchの寸法tool — 寸法は作図中のnumeric input / Inspectorで入力 |
| T | 距離・寸法の計測tool |
| H | 表示/非表示を切替 |
| L | ロックを切替 |
| X / Y / Z | transform中のaxis constraint |
| Shift | transform中のsnap一時無効 |
| Alt | ~~（`--legacy-ui` のみ snap一時反転）~~ REV36-UX140Cで旧構成とともに削除。workflow shellではShiftがsnap一時無効 |
| Ctrl+K | コマンド検索（help / 設定 / 各commandへも到達） |

既存実装の `W=Move` や `right-drag=orbit` は新UIの既定契約にはしない。必要なら移行期間のaliasにできるが、tooltip/help上のprimary shortcutは上表へ統一する。

Autodesk Fusion等と同様に、将来Settingsへnavigation presetを追加できる構造は許容する。ただし初期releaseではshortcut customization自体を目的にせず、まず一つの一貫したdefaultを完成させる。

keyboard shortcutはmouse cursor/focusのあるworkspaceで作用する。textbox・numeric field・検索fieldにfocusがある間は文字入力を優先し、`M/R/T/X/Y/Z/Delete` 等をscene commandへ流さない。shortcut実行時はstatus/tool hintで現在commandを短く表示する。

### Feedback

| 状態 | 表示 |
|---|---|
| Hover | weak outline + name |
| Selection | clear outline + handle + Inspector sync |
| Drag | ghost position + delta + active axis + snap marker |
| Invalid geometry | exact edge/vertex + short reason; commit disabled |
| Constraint violation | offending entity + clearance line + reason |
| Unknown aim | unknown badge; fake arrowを出さない |
| Unsaved/save failed | project title area |
| Historical/stale result | ghost/line/badgeでcurrentと区別 |

色だけに意味を持たせずshape/line/labelを併用する。

## 8. Measurements workspace

Measurementsは巨大右dockではなくpageとして設計する。

推奨順序:

1. Import / acquire
2. Assign to speaker / receiver / SceneRevision
3. Quality and capability
4. Predicted vs measured
5. Detailed plots / advanced metadata

主画面にはmeasurement name、channel/seat、measured badge、quality warnings、主要plotを見せる。

通常画面へUUID、raw hash、repository row IDを出さない。必要なprovenanceはAdvanced drawer。

measurementとsceneの相互selectionは維持し、必要なら小さい3D context/ghostを補助表示する。

## 9. Optimize workspace

現行一枚scroll panelを廃止し、少数のsub-contextへ分ける。O100では既存systemの配置だけでなく、現在存在しないspeaker/channelをProposed entityとして追加したSystemVariantも同じworkspaceで比較する。

### Setup
- movable entities
- bounds / constraints
- objective selection
- model / valid band

### 構成比較 / O100
- baseline topology（例: current 3.0.2）
- proposed topology（例: +SL/SRで5.0.2）
- Proposed speaker role
- EquipmentDefinition / source capability
- role別配置可能範囲
- add/remove/replace差分
- objective availability / unavailable reason
- current / proposed / as-built / measured state

### Candidates
- list/table + spatial preview
- filter
- failure reason
- proposed entityはcurrent/as-builtと異なるghost/outline + 「仮想」/「提案」badge
- topology/equipment/placement identityを同じcandidate詳細で確認

### Compare
- objective vectors
- Pareto
- selected candidate A/B
- confidence/uncertainty
- nominal性能とO90ばらつき耐性を別指標で比較

### ばらつき耐性 / O90
- 設置誤差axisと範囲
- local sensitivity
- sampled envelope
- 明示distributionがある場合だけpercentile
- feasible fraction / constraint violation
- 3D tolerance / aim overlay
- sampled evidenceと近似envelopeを区別
- overall robustness scoreは作らない

### Measure / Validate
- MeasurementPlan
- campaign assignment
- residual / holdout
- adaptive next action

Advanced:
- candidate hashes
- algorithm/seed
- solver backend
- internal provenance

## 10. Progressive disclosure

Standard view:
- primary task
- primary result
- actionable warning
- next action

Advanced view:
- solver/model IDs
- hashes/provenance
- exact numerical settings
- expert diagnostics

rare/dangerous actionsはMore (…)またはcontext menuへ置く。確認dialogはirreversibleまたはevidence semanticsが変わる場合に限定する。

## 11. Visual & interaction language

HTDTは**dark-first**で仕上げる。単に背景を黒くするのではなく、視覚階層・直接操作・feedback・motion・3D renderingを一体のsystemとして設計する。初期releaseのappearanceはdarkをauthoritativeにし、light themeはdark版がUX160を通過した後の別scopeとする。

参考原則:
- HTM: dark/light対応、real-time 3D scene、stackable overlays、Viewing/Editing mode、feature search、phase-based workflow。
- Apple HIG: dark appearanceでは背景/elevated surfaceの差で階層を作る、toolbarを過密にしない、sidebarはflatなnavigationへ限定する、重要情報に十分なspaceを与える、feedbackは状態と結果を明確にする。
- Windows native applicationとして実装するため、Apple固有asset/font/controlを模倣しない。platform-native keyboard/focus/window semanticsとWindows向けfont renderingを優先する。

### 11.1 Content first / chrome second

最も重要な情報または編集対象が最も高い視覚優先度を持つ。

優先順位:
1. room / object / plot / candidate / warningなど、ユーザーが判断するcontent;
2. current selection / current task / primary action;
3. navigation / inspector;
4. secondary metadata;
5. provenance / diagnostic / internal identifiers.

navigationやpanelがcontentより明るい・彩度が高い状態を禁止する。常設chromeは静かにし、hover / selected / warningの時だけ必要なcontrastを上げる。

### 11.2 Dark surface hierarchy

pure black一色で領域分割しない。semantic surface tokenで階層を作る。

- `surface.canvas`: 3D viewport / graph background。最も深いneutral dark。
- `surface.base`: main page background。
- `surface.raised`: sidebar / inspector / cards。
- `surface.overlay`: popover / command palette / temporary tool HUD。
- `surface.modal`: modal/sheet。backdropと明確に分離。

相対的な明度差を使い、強いborderで全componentを箱囲みしない。separatorは必要なgroup境界だけに使う。

透明・blurは**contextを残すためのtemporary overlay**に限定する。Measurementsの表、plot、長文、数値入力panelをdecorative glassにしない。読解contentは原則opaque surfaceでcontrastを安定させる。

### 11.3 Accent discipline

accent colorは「押せるもの全部」に使わない。

accent用途:
- primary action;
- current selection;
- active navigation/context;
- focus ring;
- one-off interactive affordance.

semantic data/state colorはaccentと分離する。

- measured;
- predicted;
- selected/current;
- warning;
- error;
- unsupported;
- stale;
- acoustic heatmap/scientific colormap.

同じ色を別の意味に再利用しない。色だけで状態を伝えず、icon / line style / badge textを併用する。

### 11.4 Typography and numeric presentation

Windowsではplatform UI fontを基本にし、`Segoe UI Variable` が利用可能なら第一候補とする。AppleのSF系fontをbundle/模倣しない。

階層は少数に固定する。

- workspace title;
- section title;
- body/control label;
- secondary/metadata;
- numeric/readout.

page titleを大きくしすぎず、desktop appとして情報密度を保つ。重要数値は周囲の説明文より読み取りやすくする。寸法、周波数、dB等で桁が揺れる場合はtabular numeral相当を評価する。

boldを状態表現に乱用しない。secondary textのcontrastを落としすぎてdark background上で読めなくしない。

### 11.5 Spacing, shape and control rhythm

ad-hoc marginを禁止し、token化する。

初期token候補（UX150で実機調整してfreeze）:
- spacing: 4 / 8 / 12 / 16 / 24 / 32 logical px;
- compact control: 約28–30 px;
- standard control: 約32–36 px;
- primary/prominent control: 約38–42 px;
- small / standard / large radiusの3段階以内.

全controlをpill/rounded cardにしない。shapeはhierarchyの補助であり装飾ではない。隣接panel・button・popoverのcorner geometryに一貫性を持たせる。

### 11.6 Direct manipulation and continuity

「設定してApply」より、可能な範囲で**対象を直接操作し、即座にpreviewし、確定・取消できる**ことを優先する。

- drag中はghost / delta / snap targetを即時表示;
- inspector数値変更も同じCommand lifecycleへ接続;
- hover / pressed / selected / disabled / focusを見た目で即座に区別;
- selection変更でInspectorの場所を飛ばさず、同じ領域でcontentだけ更新;
- workspace移動後に元のselection/contextへ戻れる;
- Undo可能な操作を不用意なconfirmation dialogで止めない;
- destructive/evidence-changing actionだけ明確なconfirmationを使う.

### 11.7 Motion

motionは装飾ではなく**因果関係とcontinuityの説明**に限定する。

使う:
- sidebar / inspectorのopen-close;
- selectionによるcontext panel更新;
- page/sub-context transition;
- popover / command palette;
- applied candidateのpreview -> commit;
- warning / completion feedback.

使わない:
- 常時pulsing/glowing;
- bounce/springを標準feedbackにする;
- large plot/3D objectを意味なくanimateする;
- compute待ちで画面全体をblockする.

初期duration目安:
- micro state: 100–160 ms;
- panel/context transition: 160–220 ms;
- page transition: 180–240 ms.

値はUX150で体感評価してfreezeする。animationはinterruptibleにし、Reduce Motion相当の設定または簡略modeを用意できる構造にする。

### 11.8 Responsiveness and perceived latency

操作感はframe rateだけでなく**入力に対する即時feedback**で評価する。

- click/selectionは100 ms以内のfeedback targetを維持;
- drag/orbitは既存p95 frame time <=33 ms targetを維持;
- long compute開始時は即座にqueued/running stateを出す;
- blocking spinnerでapplication全体を止めない;
- compute中もnavigation/viewport inspection/cancelを可能な範囲で維持;
- slow operationはprogressとcancelを同じ場所に置く;
- stale resultを遅れてcurrentとして表示しない.

### 11.9 Empty, loading, blocked states

空panelを置かない。

empty stateは:
- 何が無いか;
- なぜ必要か（必要な場合のみ）;
- primary next action 1個;
- optional secondary action.

blocked stateはdisabled controlだけで終わらせず、近接位置に短い理由と解消actionを出す。

### 11.10 Dark 3D viewport

Room 3DはHTM同様、UI shellと連続したdark appearanceを持つ。ただしsceneの可読性を最優先する。

基本:
- neutral graphite/charcoal系background。pure black voidを避ける;
- floor/gridは低contrast。gridがroom geometryより目立たない;
- room surfaceは低彩度neutral material;
- wall/floor/ceilingの面向きが分かる程度のsoft lighting;
- soft key + fill + ambientを基本にし、harsh specularや過度なphotorealismを避ける;
- contact/ground cueを用いてobjectが浮いて見えないようにする;
- selected objectはsurface色変更だけでなくoutline/handleで示す;
- gizmo axisは識別可能だがsceneの主役にならない;
- labelは必要時だけ表示し、常時大量labelで埋めない;
- screen/projector cone / speaker coverage / reflection / mode / field等のoverlayはbase geometryとは別のvisual layerとして管理.

Editing / Viewing / Analysisでoverlay密度を変える。全overlayを同時表示できても、defaultは必要最小限とする。focus modeでProjection only / Acoustics only / Cabling only等へ切り替え可能なarchitectureを維持する。

### 11.11 Scientific visualization in dark mode

FR、waterfall、heatmap、mode map等は「綺麗なneon」にしない。

- backgroundとgridのcontrastを抑える;
- primary traceを明確にし、secondary traceはline weight/opacityで後退;
- predicted / measuredを色だけでなくline style/badgeでも区別;
- perceptually ordered colormapを使い、selection accentとscientific scaleを混用しない;
- warning colorをheatmapの通常値へ流用しない;
- cursor/selected frequency/seat等はcrosshair + labelで明確にする.

### 11.12 Inspector and floating controls

Inspectorはselection-dependentで、同じ位置に留まりcontentだけ更新する。大量fieldを最初から見せず、Basic / Acoustic / Advanced等のdisclosureを使う。

3D上のfloating controlは小さく保つ。viewportを覆う大型HUDを作らない。頻繁にtypingするfieldはfloating HUDではなくInspector側へ置く。

### 11.13 Interaction density

一画面にprimary actionを複数競合させない。

- 1 context = 1 dominant next action;
- toolbarは頻繁な操作のみ;
- rare actionはMore / context menu;
- sidebarはnavigation;
- inspectorはselection property;
- command paletteは場所を知らない時のescape hatch.

同じactionをtoolbar、dock、card、context menuへ無秩序に重複させない。重複させる場合はkeyboard shortcutとcontext menuのように役割が明確な場合に限定する。

### 11.14 Reference guidance

設計判断の参考:
- Apple HIG Dark Mode: https://developer.apple.com/design/human-interface-guidelines/dark-mode
- Apple HIG Layout: https://developer.apple.com/design/human-interface-guidelines/layout
- Apple HIG Sidebars: https://developer.apple.com/design/human-interface-guidelines/sidebars
- Apple HIG Toolbars: https://developer.apple.com/design/human-interface-guidelines/toolbars
- Apple HIG Feedback: https://developer.apple.com/design/human-interface-guidelines/feedback
- Apple HIG Searching: https://developer.apple.com/design/human-interface-guidelines/searching

これらはWindows上でApple UIを再現する仕様ではなく、content priority、hierarchy、feedback、navigation densityの判断材料として使う。

## 12. GUI language / terminology

user-facing GUIは**日本語を基本言語**とする。内部code/domainで英語を使うことと、画面へ英語をそのまま露出することを分離する。

### 12.1 日本語優先

上位navigation、button、menu、状態、warning、empty state、tooltip、設定項目は原則日本語にする。

推奨:
- Overview -> **概要**
- Room -> **部屋**
- Measurements -> **測定**
- Optimize -> **最適化**
- Settings -> **設定**
- Help -> **ヘルプ**
- Save -> **保存**
- Undo / Redo -> **元に戻す / やり直す**
- Move / Rotate -> **移動 / 回転**
- Import -> **読み込み**
- Compare -> **比較**
- Advanced -> **詳細設定**
- stale -> **要再計算** または文脈に応じた短い日本語

translation keyは内部IDとして英語でもよいが、表示文字列へ内部class/entity名を流用しない。

### 12.2 英語を残す例外

日本語化すると不自然・冗長・意味が曖昧になる、または業界で記号/英語表記が標準である場合は無理に訳さない。

例:
- REW
- FR
- SPL
- RT60 / EDT / C50 / C80
- EQ
- CPU / GPU
- CAD / 3D
- dB / Hz / ms
- Atmos / DTS:X / Auro-3D
- USB / ASIO / WASAPI
- algorithm/model固有名、製品名、format名

一般的な英単語でも、日本語より短く意味が明確でユーザー層に自然な場合は例外を許す。ただし同じ概念を画面ごとに日本語/英語で揺らさない。

### 12.3 直訳禁止

内部technical termを機械的に一語ずつ訳さない。

悪い例:
- `prediction stale` -> 「予測が古い」
- `measurement capability` -> 「測定能力」
- `constraint violation` -> 「制約違反」

画面ではuser actionに結び付く自然な表現を優先する。

例:
- 「条件が変更されています。再計算してください」
- 「この測定では位相比較を利用できません」
- 「壁からの必要距離を満たしていません」

messageは「何が起きたか -> 必要なら理由 -> 次にできること」の順で短くする。

### 12.4 Terminology authority

UX100でuser-facing terminology inventoryを作り、一つの概念につき代表表記を1つ決める。最低限、navigation、Room/CAD操作、測定、予測、最適化、validation、material/source/receiver、保存/復元について揺れを除く。

shortcut名はmenu/tooltipへ日本語名とkeyを併記する。

例:
- 「移動　M」
- 「回転　R」
- 「画面移動　中ボタン」
- 「視点回転　Shift + 中ボタン」
- 「選択範囲に合わせる　F」

### 12.5 Copy density

短くできるところを説明文で埋めない。通常画面では名詞label＋短い状態＋actionを中心とし、詳細説明はtooltip / help / Advancedへ退避する。

日本語化によってbutton幅やform高さが不安定にならないよう、UX150で実際の日本語文字列を使ってDPI/layout acceptanceを行う。英語placeholderでvisual gateを通さない。

## 13. Layout stability

- fixed/minimum heightを常用しない。
- nested scrollを最小化する。
- page-level scrollを優先する。
- form label width / baseline alignmentを統一する。
- 1280×800 logical pxを最低target。
- 1440×900を標準確認条件。
- 100 / 150 / 200% DPI。
- clipping / overlap / inaccessible control / accidental horizontal scrollを禁止。
- saved dock/splitter stateが壊れた場合はsafe defaultへ戻せる。
- current screen geometryを超えるwindow stateを復元しない。

## 14. Precision input / geometry

寸法はmm/cm/mを入力可能とし内部mへ変換する。未丸め値を保存する。

Inspectorは対象ごとに必要最小限のfieldを表示する。multi-selectで異なる値はmixed stateとし、一つの値で無言に上書きしない。

Room editではvertex / edge midpoint handle、live dimension、opening/wall relationをscene内で扱う。CSV頂点列やID入力を主要導線にしない。

## 15. Constraints / evidence semantics

UI簡略化のためにdomain authorityを弱めない。

- feasible = 音が良い、ではない。
- measured / predicted / derived / hypothesisをbadgeとtextで区別する。
- model valid band / approximation / staleを判断に必要な場所で隠さない。
- phase/timing capabilityが無いmeasurementから強いclaimへ昇格しない。
- old measurementは元SceneRevisionへbindingしたまま表示する。

## 16. Issue #118 implementation slices

### UX100 — information architecture freeze
- current task/control inventory
- top-level/sub-context/deep-link schema
- primary/contextual/advanced分類
- user-facing Japanese terminology inventory / glossary
- CAD navigation/shortcut map
- current UI screenshot inventoryとlayout failure catalog

### UX110 — shell
- left rail
- Overview
- workspace router
- context bar
- command palette
- dark-first theme/design token foundation
- semantic surface / accent / typography / focus state

### UX120 — Room
- viewport-centric dark 3D workspace
- contextual tools
- selection Inspector
- object palette
- O100用にspeaker追加時のcurrent/proposed stateとrole/source modelを扱えるinterfaceを維持
- 「仮想スピーカーとして追加」「配置可能範囲を設定」をRoom/Optimizeの共通authorityへ接続
- standard CAD mouse/keyboard shortcut implementation
- MMB pan / Shift+MMB orbit / wheel zoom / RMB context menu
- neutral scene lighting / low-contrast grid / selection outline
- overlay layer/focus mode
- permanent toolbar/dock削減

### UX130 — Measurements
- import -> assign -> quality -> compare
- plots/cards/tablesをpage layout化

### UX140 — Optimize
- Setup / 構成比較 / Candidates / Compare / ばらつき耐性 / Measure-Validate
- current monolithic right scroll panelを廃止
- O90Dではnominal/robust比較、感度、sampled/percentile semantics、feasible fraction、3D tolerance overlayを同workspaceへ統合
- O100Gではcurrent topologyから仮想SL/SR等を追加し、配置範囲・機種/source modelを設定→候補計算→構成比較→提案適用へ進める
- proposed speakerはinstalled/measured entityと視覚的に区別し、applyしても「設置済み」とは扱わない

### UX150 — visual / motion / language / perceived-quality polish
- dark-first appearanceをauthoritativeにfreeze
- Japanese-first copy/tooltip/menu terminologyをfreeze
- spacing/alignment/typography/surface/accent token統一
- 3D lighting/grid/material/overlay visual tuning
- hover/pressed/focus/disabled/selected feedback
- motion duration/easing/interruptibility
- 1280×800 / 1440×900
- 100 / 150 / 200% DPI
- keyboard/focus/hit targets
- scientific plot dark-mode readability

### UX160 — first-use / visual acceptance
- first-use walkthrough
- screenshot evidence
- discoverability audit
- Windows owned-PC acceptanceを最後にまとめる

R100B solver bakeoffはUI非依存なので並行可能。**R110以降で新しいacoustic inputを旧dock architectureへ追加しない。**

## 17. Visual QA

visual approvalは「綺麗に見える」だけで合格にしない。

確認するもの:

- primary actionが見つかるか
- どのworkspaceにいるか分かるか
- next actionが分かるか
- clipping/overlapが無いか
- same control typeがsame size/alignmentか
- warning/state badgeの意味が一貫するか
- mouse/keyboard/focusが破綻しないか
- UI整理後もSceneRevision/evidence authorityが変わらないか

Windows実機visual acceptanceはUX160でRDCをまとめて使う。計画・構造レビューではRDCを使わない。

旧browser UIの記録は履歴として残すが、native GUIの合格証拠へ流用しない。

## 18. Issue #170 / S130 StandardsProfile integration

StandardsProfileは新しいglobal destinationにせず、既存workflowの文脈へ統合する。

- Room > 「スピーカー・座席」のright contextに、O100構成パネルと同居する「配置基準」を置く。
- Optimize > 「比較」に、current SceneRevision / SystemVariantごとの基準項目matrixを置く。
- 通常表示は日本語firstとし、PASS/FAIL/UNKNOWN/NOT_APPLICABLE のraw enumや
  UUID/hashは主画面へ出さない。statusはtext/symbolでも識別する。
- source/reference、exact SceneRevision/SystemVariant、profile/evaluation/evidence identityは
  Advancedへprogressive disclosureする。
- profile/versionはユーザーが明示選択する。旧versionのevaluationは上書きせず、
  newer profileの再評価は別recordとして残す。
- 「配置制約」としてチェックしたcriterionだけ既存hard-constraint gateへ渡す。
  未選択の不適合はevidence表示のみ。判定材料不足を選択した場合はfail-closed。
- SystemVariant比較で適合数score、winner、自動ranking、暗黙Pareto objectiveは作らない。
- 新しい独自stylesheetは追加せず、既存UX150 token / surface / control semanticsを再利用する。
- Windows DPI/font/mouse/3D screenshot/first-use acceptanceはUX160に残す。S130ではRDCを使わない。
