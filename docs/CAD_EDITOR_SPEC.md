# Native CAD editor — 編集・保存契約

> 2026-09-16 / N05以降の実装仕様。**本書の型・機能は計画であり、実装済みを意味しない。**
> 優先順位は[ロードマップ](IMPLEMENTATION_ROADMAP.md)、画面操作は[UI設計](UI_DESIGN.md)、判定方法は[受入仕様](CAD_EDITOR_ACCEPTANCE.md)。

## 1. 実装境界

最初から全機能を備えたフレームワークを作らない。N05の縦断試作で次の境界を小さなmoduleとして確立し、必要な機能から追加する。

| 境界 | 責務 | 入れてはいけないもの |
|---|---|---|
| domain | SceneDocument、entity、幾何、検証、保存用DTO | Qt/VTK/FastAPI import、actor参照 |
| editor | WorkingDocument、CommandHistory、selection、tool、snap | DBへの直接書込、描画meshを正本とする変更 |
| desktop | Qt shell、入力、dock、単位付きInspector | 独自の配置・幾何判定 |
| rendering | entityとactorの対応、picking、camera、overlay | 測定の履歴変更、保存処理 |
| services/adapters | repository、REW、計算job、export | GUI widgetを計算入力にすること |

既存PythonのREW・幾何・比較ロジックは、再利用価値のあるものをdomain/serviceへ抽出する。native GUIからlocalhost HTTPへ自己通信する構成は必須にしない。FastAPIは旧UIを動かす間のadapterとし、同じserviceを呼ぶ。解析jobだけは必要に応じ別processにする。

## 2. Sceneと測定Contextを分離する

現行Contextはroom・speakerと一つのmeasurement pointを含む。これを家具・複数座席・複数測定点を持つeditor全体のmodelへ無理に拡張しない。

| 概念 | 内容 |
|---|---|
| SceneDocument | document_id、schema_version、座標規約、room、entity集合、constraint参照 |
| SceneRevision | revision_id、parent_revision_id、SceneDocumentの不変snapshot、content hash |
| WorkingDocument | 現在の編集対象、保存元revision、edit generation、未確定preview |
| AcquisitionContext | SceneRevision、選択したmeasurement point、音源対応、AVR/マイク条件の不変snapshot |
| AnalysisRun | 入力SceneRevision/hash、constraint/model/algorithm版、parameter、status、result参照 |
| EditorViewState | camera、dock、selection、grid、表示単位、layerの表示/非表示、編集lock |

保存正本にactor、widget、row indexを入れない。描画非表示は物理的な撤去を意味しない。物理的な存在/設置状態はDocument属性、hide/編集lockはViewState、探索で動かさない指定はConstraintSetへ分ける。

同じentityのIDはrevision間で維持する。duplicateは新IDを作る。nameやspeaker roleはidentityにしない。測定点とseat中心、音響基準点と筐体原点は別に保持する。role未割当の複製speakerは許すが、測定Context作成時にroleの曖昧さを解消する。

### 保存・dirty・履歴

1. Saveは確定済みWorkingDocumentを検証し、新SceneRevisionをDB transactionで保存する。active drag中はSaveを実行できない。
2. 成功してから保存元revisionとclean状態を更新する。失敗時はdraftと履歴を維持する。
3. 同じ内容の再Saveはno-op。未保存判定を「履歴indexが0か」だけで行わず、最後に保存した正規化content hashと比較する。
4. Save後のUndoはdraftだけを変更する。保存済みrevisionは残る。再Save時のparentは最後に保存したrevisionとし、戻した内容も新しい履歴として残す。
5. Undo後に新操作を確定するとredo分岐を破棄する。何も変えない操作・cancel・検証失敗は履歴へ積まない。
6. 自動復旧snapshotは確定済みdraftを別に保存する。測定参照可能な正式revisionとは区別し、次回起動で復旧候補として開く。DB原子性と復旧の最小版はN10、完成度向上はN90。
7. 古い測定は元のSceneRevision/Context/RawAssetを固定参照する。過去配置をghost表示し、現在の移動に追従させない。

旧DB/API/ファイル形式の互換は不要。新schemaは別project形式/保存領域から開始可能であり、自動migrationや旧UIとの機能同等性をrelease条件にしない。現実の測定原本を無言で消すことはしない。必要になったデータだけ明示importし、取得不能な情報を推測補完しない。

## 3. 幾何・単位・姿勢

### 座標を一箇所で変換する

再利用する幾何計算の意味が明瞭なため、domainの意味軸は当面 `+X=右、+Y=後、+Z=上`、原点=部屋の前左床、保存単位=mを用いる。旧互換の義務ではなく、既存の幾何・測定知識を活かす設計判断である。

この物理的な軸の並べ方は左手系。VTK側は右手系の `(Xv,Yv,Zv)=(x,-y,z)` とし、上=+Zv、部屋正面=+Yvとする。

- 点/方向に `C=diag(1,-1,1)` を適用する。逆変換もC。
- domain内のactive transformは `Tv=C4 * Td * inverse(C4)`。反射行列C自体をquaternionとして保存しない。
- normalとtriangle windingも変換する。頂点座標だけ反転して面の表裏を放置しない。
- screen座標、VTK display座標、world座標、entity local座標の変換をadapterへ集約する。Qtのlogical pixelからdevice pixelへの変換は二重適用しない。
- 正面/左右を非対称fixtureで確認する。room寸法変更でworld原点を勝手に移さない。

speakerの表示yaw/pitchは[測定データ契約](DATA_AND_ANALYSIS.md)の式を使う。aim unknownと表示用placeholderの向きは別物。位置だけの変更、duplicate、group移動ではunknownを既知に変えない。明示的なrotate/aim操作でのみ方向を設定する。

筐体には剛体pose、寸法、local acoustic_reference_offsetを持つ。poseの向きと音響aimを分離し、offsetから音響基準点を導出する。未知offsetを0と断定せず、簡略markerでは指定した音響基準点を直接使用する。scale gizmoによる一括変形は初期対象外。寸法変更はResize commandとする。

### tolerance

表示の丸め、snap刻み、幾何比較toleranceを別にする。mm表示からmへ変換し、未丸め値を保存する。既存G00/G10のtolerance・境界包含規則はadapterで明示する。新しい閾値は単位・用途と一緒に固定し、画面のzoom率で幾何判定を変えない。

### 妥当性を分ける

- 未閉鎖polygonや自己交差はtool内previewのみ。構造的に壊れたSceneRevisionは保存しない。
- 構造的に正常でも、壁変更でspeakerが室外になる等の設置問題はSceneの診断として保持できる。問題をsceneに示し、対応が必要なAcquisitionContext生成・feasible候補化を停止する。
- solver非対応形状でも編集・保存は可能。モデルの適用判定で予測を止める。非矩形を黙って矩形化しない。
- G00/G10へ渡す際はその既存契約を満たすDTOを生成する。新Sceneを現行ContextCreateで直接validateしない。

## 4. 部屋・壁・開口

初期は一室の単純polygon prism、一定天井高。footprintは室内の仕上げ面境界とする。壁厚は境界の外側に描画する補助属性であり、音響容積を勝手に縮めない。穴、曲面、傾斜天井、複数部屋は後続。

- Room vertexに安定ID、Wallに独立したwall_idと端点vertex参照を持つ。
- reference boundsはfootprintから導出する。旧reference boxを手入力で拡大しないと壁を伸ばせない操作にしない。負座標も新Sceneでは表現できる。
- Openingはwall_id、壁始点からの距離、幅、床からの高さ、高さ、種別、開閉状態を持つ。wall-local寸法で配置し、wall移動に追従する。
- splitでは旧壁をretireし、子壁へsource_wall_idを残す。openingが一方に全包含なら移す。分割点を跨ぐopening、競合する制約は影響を示して解決するまでcommand確定を止める。
- mergeでは共線など支持する条件を明示し、新wall_idへ統合する。異なる材質・離隔値を無言で片方へ統一しない。
- wall削除時も参照opening/constraintの再割当または削除を一つのtransactionで扱う。参照切れを放置しない。
- Undoは壁ID、opening、constraint参照を一括復元する。
- G10の `from_vertex_id->to_vertex_id` は現行契約として保持し、新Documentとの対応表をrevision単位で作る。永続wall_idの代用にはしない。
- 保存済みConstraintSetは変更しない。壁編集に伴う変更は新しい制約revisionとしてSceneRevisionと整合させる。
- 負座標を持つSceneから現行G00/G10へ接続する場合、必要に応じbbox最小XYを引いた座標をadapterで作り、逆変換とwall対応を入力revisionへ記録する。表示位置・測定位置を無言で移さない。表現できない制約は未対応として止める。

扉開口を描けたことと、透過/回折を音響計算できることは別。N30の開口は編集・寸法・可視化の範囲とし、予測で無視する場合は適用条件に明記する。

### Post-0.1 acoustic configuration（Issue #101、未実装）

R110/R120は上記v0.1の単室prism仕様を拡張する後続工程である。exact SceneRevisionに結び付く不変acoustic configurationへ、object/surface material、source/receiver/environment、隣接AcousticRegionとPortal/BoundaryTerminationを保存し、Inspector等から入力できるようにする。既存Sceneの読込時に未知材質や隣接空間を推測確定しない。stable surface/Scene IDを保持し、設定変更はUndo/Redo・保存/再open・prediction stale判定へ接続する。

R120Aの初期対応範囲は凹polygon prism、対応object surface/volume、接続したprism regionまたは明示termination。R120Bでは向き付きpolyhedral surface/air volumeへ拡張し、段差/傾斜天井と許容誤差を保存した曲面の面分割を対象にする。native編集または明示importからSceneRevisionへ保存し、material/surface ID、再open、volume/topology、表示診断を検証する。compilerと対応R130/R150数値gateを通過するまでunsupportedとし、汎用B-rep CADや全import形式は要求しない。prism対応だけでIssue #101の一般3D要件を完了にしない。

ViewStateのhide/lockは音響的な撤去としない。acoustic participation/omissionを別属性として保存する。speaker/receiver markerを自動的に固体とせず、物理cabinetを含める場合はsourceとの結合とdirectivityの二重計上を検証する。詳細と工程は[研究文書 §5](ACOUSTIC_SOLVER_RESEARCH_2026-09-18.md#5-acoustic-data-model)と[正本ロードマップ](IMPLEMENTATION_ROADMAP.md)に従う。

## 5. 操作transaction

`Idle → Previewing → Validate → Commit / Cancel → Idle` を全toolの共通ライフサイクルとする。

- pointer downで対象ID、before state、pivot、座標系、snap設定、edit generationを取得。
- drag中はpreviewへ更新。Document、DB、履歴へ書き込まない。
- pointer upで最終値を再取得・検証し、意味のある変更だけ1 commandとして確定する。
- Esc、capture loss、window deactivate、対象削除、tool/project切替ではcancelして全対象を復元する。window外でreleaseしても操作を残留させない。
- InspectorはEnter/編集確定で同じcommandを発行。文字入力途中の不完全な数値をDocumentへ渡さない。
- 複数選択の移動/回転は共通pivot・同じdeltaで原子的に適用し、相対配置を保つ。個別snapでgroupを歪めない。
- defaultはworld軸・selection中心pivot。local軸は単一選択で提供し、複数選択ではworld固定を明示。
- persistent grouping/入れ子transformは初期必須外。複数選択の一括操作をN20、恒久groupはN40後半で必要性を確認する。

CommandHistoryを唯一の編集履歴とし、QUndoStackを別の履歴として併用しない。Qt Actionはdomain commandに接続する。履歴はメモリ内で上限を持ち、永続event sourcingや全command replayを初期要件にしない。

## 6. Picking・selection・snap

entity_idとrender proxyの1対多対応を保持する。mesh再生成でselectionを失わない。解析marker/寸法/gizmoにはpick用途を設定し、通常選択を奪わせない。layerをhideしたentityはviewport pick対象外。lock対象は選択・確認可能だが変更不可とする。

snapはworld幾何候補の生成とscreen上の選択を分離する。

1. active axis/planeを適用。
2. 対象平面に整合するvertex/edge/alignment候補を生成。
3. camera projectionで距離をlogical pixelへ変換。
4. 初期案は取得半径8 DIP、保持半径12 DIP。実機N20で調整し、設定値を固定する。
5. priority、screen距離、stable IDで決定論的に選ぶ。対象の切替にhysteresisを入れる。
6. 幾何候補がなければgridへsnapし、最終値をdomain validateする。移動中entity自身は候補から除外する。

N20aはgrid/axis/angle、N20bはvertex/edge/midpoint/alignment。snap候補・拘束軸・単位をviewportに表示する。viewにほぼ平行な軸のdragは不安定な割算をせず、別plane/数値入力へ誘導する。snap解除・精密移動はmodifierとtoolbar両方から操作可能にする。

## 7. VTKとの接続

`AffineWidget3D` はPoC/adapter候補であり、editorの正本にしない。[ソース調査](CAD_EDITOR_OSS_RESEARCH.md)の通り、Actor更新とcallbackの順序、interaction style変更を確認して接続する。

- 入力はToolControllerが所有し、camera navigationと編集の同時発火を防ぐ。
- actor行列を直接保存せず、adapterが提案transformをdomain値へ変換する。
- cancel時はmodelだけでなくactor、gizmo origin、widget cache、selection、interaction styleを復元する。
- widget破棄時はobserverを解除する。project再openでcallbackを重複登録しない。
- private fieldの継続的な書換えに依存する場合、限定wrapper/必要最小限のVTK handleへ置換する。最初からwidget全体をforkしない。
- 変更entityのproxyのみ更新し、drag中にroom全meshや解析volumeを再生成しない。

## 8. 計算・計測・グラフ

Qt/VTKの生成・変更・renderはGUI threadで行う。REW等のI/Oは非同期worker、CPU負荷の高いPython計算は必要時にprocess workerを使う。Windowsのspawnとfrozen app終了も受入対象にする。分散queueは作らない。

job inputは不変snapshotとし、job_id、input revision/hash、edit generation、algorithm/model/constraint版を持たせる。取消済みjob、閉じたproject、世代が変わったdraftへの遅延結果は自動適用しない。過去結果として保存できてもstale表示にする。candidate applyはpreview→明示確定→1 command。

FR dockはPyQtGraphを第一評価候補とする。既存の比較計算結果を表示し、描画downsamplingした曲線から指標を再計算しない。対数周波数軸、Hz/kHz、dB単位、NaN gap、8曲線、cursor、線種、exportをN60で確認する。

## 9. 非目標

汎用B-rep CAD、一般拘束solver、任意plugin API、複数user同期、browser機能同等性、CAD import全形式、写実render、音響solverの全面自作を初期要件にしない。既存OSSの直接利用を優先し、ソース移植時だけcommit・path・変更内容・noticeを実装PRへ記録する。
