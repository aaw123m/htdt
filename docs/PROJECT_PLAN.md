# Home Theater Digital Twin — 製品計画

> 2026-09-16 / CAD-first改訂
> Windows・個人利用・ローカル完結。旧GUI/API/保存形式の互換は不要。
> 実装順・完了条件の正本は[IMPLEMENTATION_ROADMAP](IMPLEMENTATION_ROADMAP.md)。

## 1. 目的

HTDTは、部屋とホームシアター配置を3D CADのように直接構築・編集し、その配置に実測・予測・比較・最適化候補を結び付けるデジタルツインとする。

Room/Placementでは大きなviewportを中心に、mouseで壁を描き、スピーカー・座席・スクリーン・家具を置き、移動/回転、snap、寸法入力で精密化する。3D navigationはMMB pan、Shift+MMB orbit、wheel zoomを既定とする一般CAD型の操作契約を持つ。一方、Measurements/Optimizeは3D viewportへ全機能を押し込まず、taskに適したpage/table/plot workspaceを使う。設定表や内部IDを読むことを最初の作業にしない。数値入力は直接操作と同じcommand/validationへ接続する。

成功は「見た目が3D」だけではなく、**どこで何をできるかを初見で理解できること**、部屋と配置を迷わず作れ、誤操作を戻せ、保存した条件に対する測定・比較を再現できること。「概要」は次に行う作業とblockerを示し、「部屋 / 測定 / 最適化」の少数workspaceへdeep-linkする。UIはdark-firstで、contentをchromeより優先し、直接操作・即時feedback・一貫したsurface hierarchy・限定的なaccent・目的のある短いmotionを共通原則とする。Room 3Dもdark appearanceとし、neutral lighting、低contrast grid、明確なselection、整理されたoverlayで空間理解を優先する。最適配置は制約・複数目的・モデルの適用範囲を伴う候補として扱い、シミュレーションだけで音質を断定しない。O90では、nominal性能だけでなくspeaker/seat位置やaim等の現実的な設置誤差に対する感度・性能分布・feasible fractionを独立objectiveとして扱い、施工誤差に強い候補とのtrade-offをParetoで比較する。O100ではさらに、**現在存在しないSL/SR等を仮想speakerとして追加し、channel topology・機種/source model・配置可能範囲そのものを設計変数にする**。3.0.2 baselineを保持したまま5.0.2等のproposalを比較し、選択案はAs-built/Measuredへappend-onlyで移行する。

## 1.1 North Star Architecture Principle

製品・authority・solver・measurement workflow・system comparisonの最上位設計原則は [ARCHITECTURE_PRINCIPLES](ARCHITECTURE_PRINCIPLES.md) を正本とする。

HTDTは、LiDAR等の単一sourceをtruth sourceへ固定せず、real-room evidenceをsource-neutralに扱う。raw evidence → semantic authority → compiled/solver representation → resultを分離し、equipment/directivity/playback chain、treatment、measurement evidenceをexact SceneRevision / SystemVariant lineageへ統合する。current / proposed / as-built / measuredを自動昇格せず、measured / predicted / derived / hypothesisを混同しない。

同じ仕様を本計画へ重複定義せず、現在の実装事実は [IMPLEMENTATION_STATUS](IMPLEMENTATION_STATUS.md)、実装順・gateは [IMPLEMENTATION_ROADMAP](IMPLEMENTATION_ROADMAP.md) を参照する。

## 2. 利用条件

| 条件 | 方針 |
|---|---|
| Windows 11 x64、個人利用 | native desktop、オフラインで編集・保存済み結果の閲覧が可能 |
| 所有PCで開発・利用 | クラウド、ログイン、サーバー運用を要求しない |
| サブウーファーなしが主要シナリオ | 現在3.0.2。役割・本数は可変、将来の追加は可能 |
| 測定機器 | RX-A4A、REW、UMIK-1の現状は[実装状況](IMPLEMENTATION_STATUS.md)を正本とする |
| 実際の室形状・機器寸法 | 不明を0や一般値で確定しない。sampleと実測寸法を区別する |
| 旧互換 | 不要。必要なdomain知識・計算・原本だけ再利用 |
| 実装言語 | 制約なし。第一候補はPython/Qt/VTK、未達なら根拠付きで再選定 |
| GUI言語 | 日本語を基本とする。REW/FR/SPL/RT60/CPU/GPU/CAD/3D/dB/Hz等、翻訳が不自然・冗長または標準記号である語だけ英語/略語を維持 |
| ローカル作業 | `C:\Users\ka092\Desktop\HTDT\` は必要な実機確認に使用。成果・計画・進捗の正本はGitHub |

## 3. 中心workflow

global navigationの表示は「概要 / 部屋 / 測定 / 最適化」を基本とし、内部N/O/R milestoneやrepository/job構造をnavigationへ露出しない。機能検索から主要taskへ到達できるようにする。

1. 「概要」から次の作業を選び、「部屋」でroom footprintと高さをmouseで構築する。
2. speaker、seat、screen、furniture、measurement pointを配置する。
3. snap/寸法で調整し、SceneRevisionを保存する。
4. 測定点と音源・AVR・マイク条件を選び、AcquisitionContextを固定する。
5. REWで手動測定し、text/API出力と原本をHTDTへ取り込む。
6. scene上の対象からFR・比較を確認する。配置変更は新revisionにする。
7. 制約内の候補をpreviewし、対応するモデルで予測、必要なら多目的比較する。
8. system expansionを検討する場合はO100で現在存在しないspeaker/channelをProposed entityとして追加し、role・機種/source model・配置可能範囲を指定してbaseline topologyと比較する。
9. 必要に応じてversioned StandardsProfileでexact SceneRevision/SystemVariantをcriterion別に評価する。規格適合性は音響最適性や総合scoreにせず、source/evidence不足はUNKNOWNのまま保持する。
10. 必要に応じてO90で既存/仮想speakerの設置誤差・aim誤差等へのばらつき耐性を評価し、nominal性能とのtrade-offを比較する。
11. 選択案を実際に導入した場合はproposalを上書きせずAs-built SceneRevisionを作り、REW再測定と残差で仮説を評価する。

未測定でもCAD編集はできる。高度なCAD操作を覚えないと測定を登録できない設計にもせず、templateとInspectorによる入力経路を用意する。

## 4. Releaseの範囲

| 段階 | 含むもの | 含まないもの |
|---|---|---|
| CAD foundation preview | N05〜N40、一室polygon prism、物体配置、view、snap、Undo、保存、最小package | 測定統合完成、予測場、最適化、汎用CAD |
| 実測workspace | N50/N60、制約表示、REW読取/取込、履歴とFR比較 | 自動発音、自動AVR変更、未検証の推薦 |
| 予測・候補workspace | 対応modelを検証したN70/N80 | 非矩形精密予測や広帯域精度の無条件保証 |
| 安定個人版 | 公開機能のN90受入、保存/復元/更新、操作の仕上げ | すべての将来解析機能が完成すること |

N70/N80の完成をCAD previewや安定個人版の条件にしない。カレンダー上の納期は約束せず、fixtureと受入結果で段階を進める。

## 5. 再利用と自作の境界

| 領域 | 再利用 | HTDTで作る部分 |
|---|---|---|
| 測定・校正・詳細解析 | REW | 条件・配置・原本の対応、取込、必要な比較 |
| Windows shell | PySide6/Qt Widgets | workflow shell、dark-first design tokens、contextual Inspector、command palette、motion/feedback、単位・状態表示 |
| 描画・科学可視化 | PyVista/VTK/PyVistaQt | entity projection、入力/選択、overlay、job結果対応 |
| editor設計 | FreeCAD、Godot、Three.js等の局所設計 | domain独立のCommand/Tool/Selection/Snap |
| 幾何 | Shapely、既存G00/G10 | room/wall/openingと制約の参照、adapter |
| 数値計算・比較 | 既存HTDT、必要なNumPy/SciPy | 来歴、指標、適用範囲 |
| FR plot | PyQtGraphを評価 | Dataset選択、線種、比較条件、export |
| 予測 | 適用可能なREW Room Simulator等 | revision/model/parameterの記録、適用判定 |
| 保存 | SQLiteと原本保管 | SceneRevisionと測定Context、不変参照、復旧 |

ソースを読んだ箇所と判断は[OSS調査](CAD_EDITOR_OSS_RESEARCH.md)。新規solver、gizmo全体のfork、game engine全体の導入を無条件に選ばない。汎用libraryで足りる部分は依存利用する。

## 6. 保存・意味の保持

SceneRevisionは編集された物理空間、AcquisitionContextは測定時の条件を固定する。StandardsProfile/StandardsEvaluationはその物理truthを置換せず、exact SceneRevision / optional SystemVariantへ外部基準のcriterion evidenceを付与する。profile更新は過去evaluationを書き換えず、明示re-evaluationで新しいrecordを追加する。過去測定を現在配置へ自動で紐付け直さない。camera、hide、dockは測定条件を変えない。

実測、実測由来、予測、仮説を識別する。予測は幾何・吸音・指向性・solverの適用範囲を持ち、未知条件を確定値として補わない。低音振分けがある時は入力チャンネルと実放射音源を分ける。位相/タイミングが不明ならその前提を要する計算を止める。

.mdatはREWで再解析するための原本として任意添付し、独自の内部解析形式にはしない。未提供の原本をあるものとして扱わない。[測定仕様](MEASUREMENT_WORKFLOW.md)、[データ契約](DATA_AND_ANALYSIS.md)

## 7. 実装構成

Qt shell → editor service → domain/repository/adapterの境界を置く。native GUIはPython serviceを直接呼び、HTTPを必須にしない。Qt/VTK変更はGUI thread、長いI/O/計算は取消可能なjobとし、古い結果の自動適用を防ぐ。[編集契約](CAD_EDITOR_SPEC.md)

旧browserは新CAD機能を追加せず、測定確認に必要な間だけ残す。新project形式は別保存領域から開始可能。互換層・migration・機能同等性を義務化しない。native測定経路の受入後、不要なfrontend/API配信/Node buildを削除する。

保存先はapp binaryと分離したローカル領域を基本とし、backup/restoreを提供する。過剰な認証・TLS・RBACを作らない。個人利用に必要な入力/参照/容量検証と保存原子性を保つ。

## 8. 初期非目標

汎用B-rep CAD、FEM/BEM/FDTD、一般拘束solver、独自スイープ/ASIO/WASAPI engine、マイク校正engine、EQ/YPAO/Dirac/Audyssey相当の補正、未公開.mdat解析、Atmos encoder、常駐AVR制御、マルチユーザー、クラウド、LLM説明機能は**v0.1初期releaseでは対象外**。任意形状wave/FEM/FDTD等はpost-0.1のIssue #101 / R-seriesで後続実装する。

一室・一定天井高・primitive家具から開始し、BIM/STEP/複雑mesh importや複数室は実要件が生じた時に追加する。

## 9. 検証と記録

native GUIをWindowsのDPI/mouse/keyboardで確認する。headless CIを操作品質の証拠にしない。仕様上の目標、過去のPoC報告、今回の再検証を分ける。

変更に適した検証だけを行い、可逆・低影響変更へ不要なtestを追加しない。Issue #170ではcriterion evaluator/profile data/persistenceをGUIやO100D objectiveから分離し、公開sourceに明示された境界だけをbuilt-in criteriaへ採用する。compliance FAILは明示hard-constraint opt-inなしにcandidateを削除しない。N05〜N90/O10〜O80 software pathは完了済み。O90はO90Aとcanonical O90Bまで実装済みで、O90C以降はplanned / 未実装。O100はO100B virtual topology/placement searchまで実装済みで、O100C以降はplanned / 未実装。現在はIssue #101のR100B solver bakeoff、Issue #118のUX100〜UX160 UI/UX overhaul、O90、O100を独立trackとして管理する。R100BはUI非依存で並行可能だが、R110+の新しいuser-facing acoustic inputを現行dock shellへ増築しない。採用gateはR-series fixture/ADRとUX acceptance、進捗は[実装状況](IMPLEMENTATION_STATUS.md)へ残す。


## Competitive gap closure lifecycle — Issue #166

HTDTの実用上のgap closureは、既存N/O/R authorityを置換せず、次のend-to-end lifecycleで設計判断とevidenceを切らさないことを製品scopeとする。

```text
Capture / Import
→ Semantic 3D Room
→ Equipment / Directivity / Materials
→ Standards / Physical Constraints
→ SystemVariant
→ Acoustic Prediction
→ Multi-objective / Robust Optimization
→ Installation Plan
→ As-built
→ Guided Measurement / Quality Gate
→ Predicted-vs-Measured Validation
→ Calibration Plan / Export
→ Re-measure
→ Validated Final System
```

Issue #167–#176の正本状態・依存・残gateは[IMPLEMENTATION_ROADMAP](IMPLEMENTATION_ROADMAP.md)の「Competitive gap closure — Issue #166 canonical tracking」を参照する。

製品上の境界は以下を維持する。

- raw visual meshをsilent repairやsolver-ready truthへ昇格しない。
- EquipmentDefinition、materials、treatment、standards、measurement、DSP evidenceのprovenanceを分離する。
- no-treatment/current/proposed/as-built/measuredをsynthetic valueで代替しない。
- unsupported prediction capabilityからSPL、phase、impedance、source strength等を捏造しない。
- named comparison / Paretoは独立objectiveとexact evidenceを保持し、hidden総合scoreを作らない。
- REW等の専門engineは再利用し、HTDTはSceneRevision/SystemVariant/measurement/evidence lineageを保持する。
- domain/software completionと、#118 Windows visual acceptance、#101 numerical solver validation、#83 owned-room evidenceを別gateとして扱う。

2026-09-20時点で#167/#168/#169/#170/#171/#172/#173/#174/#175/#176はdomain acceptance完了。#171のtreatment named A/B/no-treatment comparisonはPR #223で成立済み。#101 arbitrary-room acousticsはsolver-neutral snapshot/dispatch authorityまで進んでいるが、production solver selectionとowned-room numerical validationは未完了である。
