# 3D CAD editor — OSSコード調査と技術比較

> 調査: 2026-09-16 / main `1b510206`、PR #37 `9724f4b`、追加commit `0353768` をレビュー
> **13プロジェクトの選定したソースを読んだ設計レビュー。アプリ全体の監査・比較ベンチマークではない。**
> 実装順は[ロードマップ](IMPLEMENTATION_ROADMAP.md)、反映する契約は[SPEC](CAD_EDITOR_SPEC.md)。

## 1. 結論

第一実装はPython 3.12＋PySide6/Qt Widgets＋PyVista/VTK/PyVistaQtを維持する。理由は、mouse操作を作れるだけでなく、Pythonの測定・幾何・科学計算とmesh/field/slice/volume表示を同じアプリにまとめやすいため。

ただし標準gizmoはCAD editorの完成品ではない。selection、preview/commit/cancel、Undo、wall参照、数値編集、snap競合をHTDTが管理する。UIの使いやすさはN05/N20の実機gateで証明する。過去のWindows PoC報告は有益だが、再現可能なtracked code/lock/packageと性能計測は別途必要。

**不採用理由を「Webだから不適」「Python資産があるから他言語不可」としない。** TypeScript＋desktop shell、C#、Godotも同じfixtureで比較できる選択肢である。既存コード量を守ることは選定目的にしない。

## 2. 調査方法と範囲

GitHub APIでdefault branchのcommit SHAを取得し、そのSHAのファイルを読んだ。以下のpermalinkは調査内容の固定点であり、製品依存を開発headへ固定する提案ではない。採用release版をlockする際に差分を確認する。

重点は入力とcameraの競合、操作開始/終了、取消、履歴、selection通知、observer寿命、DPI変換、Qt embedding、グラフ。OS実行、GPU比較、全issue履歴、全submoduleのlicense監査は行っていない。

| 対象 | 確認したファイル / symbol | コードから確認した点 | HTDTへの反映 |
|---|---|---|---|
| PyVista | [pyvista/plotting/affine_widget.py](https://github.com/pyvista/pyvista/blob/5cb68204922cac06b18dd3587b92db05163a72be/pyvista/plotting/affine_widget.py) / AffineWidget3D、_move_callback、_release_callback、disable | actor.user_matrixを更新。interact callbackがその代入より先。releaseでcache更新、interaction style変更 | wrapper/ToolControllerにtransaction責務。最終値再取得とcancel復元 |
| PyVistaQt | [pyvistaqt/plotting.py](https://github.com/pyvista/pyvistaqt/blob/aa091ec97e225da8ec8cf43074bfe565a943f839/pyvistaqt/plotting.py) / QtInteractor.render、close | render_signal、timer、close後renderの抑止と解放処理 | 自前QMainWindowにQtInteractorを埋込み、終了と再openを検証 |
| VTK | [Wrapping/Python/vtkmodules/qt/QVTKRenderWindowInteractor.py](https://github.com/Kitware/VTK/blob/62f6ed6404031187b13654a78c7a401f5b08f368/Wrapping/Python/vtkmodules/qt/QVTKRenderWindowInteractor.py) / _setEventInformation、Finalize | Qt位置をDPI倍率でdevice座標へ変換しY反転。終了でRenderWindowをFinalize | 二重DPI変換を防ぐ。100/150/200%と画面外releaseをgate化 |
| VTK | [Interaction/Widgets/vtkBoxWidget2.cxx](https://github.com/Kitware/VTK/blob/62f6ed6404031187b13654a78c7a401f5b08f368/Interaction/Widgets/vtkBoxWidget2.cxx) | widgetのinteraction eventとrepresentationを分離 | 必要なら最小VTK handleのadapter。汎用widget forkは先行しない |
| FreeCAD | [src/Gui/Selection/Selection.cpp](https://github.com/FreeCAD/FreeCAD/blob/a4ce44d33b7e42e16cb8156058a1894237042ab4/src/Gui/Selection/Selection.cpp) / SelectionSingleton::notify、addSelection | selection変更の集約・通知・gate | entity IDのSelectionServiceを正本にする。実装全体は取り込まない |
| CQ-editor | [cq_editor/widgets/object_tree.py](https://github.com/CadQuery/CQ-editor/blob/3e8ef6b9b9af8cdf58fc1375a1b9f7b86409ecf8/cq_editor/widgets/object_tree.py) / handleSelection、handleGraphicalSelection と [cq_editor/main_window.py](https://github.com/CadQuery/CQ-editor/blob/3e8ef6b9b9af8cdf58fc1375a1b9f7b86409ecf8/cq_editor/main_window.py) | tree選択からviewer/Inspectorへsignal、逆選択、Qt shell構成 | dock/双方向選択の参考。AIS shapeやQt itemをHTDT identityにしない |
| Godot | [editor/editor_undo_redo_manager.cpp](https://github.com/godotengine/godot/blob/dfa06cafb4546eac47d863dfa12aec54e4efa586/editor/editor_undo_redo_manager.cpp) / create_action、commit_action | do/undo操作、redo破棄、saved_versionとの関係 | 保存後Undo/新規分岐のdirty判定を明示 |
| Godot | [editor/scene/3d/node_3d_editor_plugin.cpp](https://github.com/godotengine/godot/blob/dfa06cafb4546eac47d863dfa12aec54e4efa586/editor/scene/3d/node_3d_editor_plugin.cpp) / update_transform_gizmo、snap設定 | editor内でgizmo、選択、snap、view設定を統合 | 操作仕様の参考。runtime exportでeditor機能が自動提供されるとは扱わない |
| Three.js | [examples/jsm/controls/TransformControls.js](https://github.com/mrdoob/three.js/blob/eabc262db760c5e85bd890a6dc627ba14e70e8c3/examples/jsm/controls/TransformControls.js) / pointerDown、pointerUp、reset | 開始poseの退避、translation/rotation snap、開始/終了通知、reset | renderer-independent transaction設計の参考。TS案の具体的比較部品 |
| Babylon.js | [packages/dev/core/src/Gizmos/gizmoManager.ts](https://github.com/BabylonJS/Babylon.js/blob/1c0985aa179a186d955fae1ed218d7d43aca49dc/packages/dev/core/src/Gizmos/gizmoManager.ts) / attachToMesh、dispose | attach対象限定、pointer observer、utility layer、解除 | gizmoと通常pickを分離。observerを生存期間で管理 |
| Helix Toolkit | [Source/HelixToolkit.Wpf/Visual3Ds/Manipulators/CombinedManipulator.cs](https://github.com/helix-toolkit/helix-toolkit/blob/2b94a3cd030e311a92998306297d2a8a87fd2f66/Source/HelixToolkit.Wpf/Visual3Ds/Manipulators/CombinedManipulator.cs) / Bind、UnBind、Pivot | 軸別move/rotate許可、pivot、WPF bindingと解除 | C#案の具体候補。読んだWPF版の性質をSharpDX版へそのまま一般化しない |
| napari | [src/napari/components/layerlist.py](https://github.com/napari/napari/blob/18ffd37dcff853f6ac9bc5211fdead979dc5dabd/src/napari/components/layerlist.py) / _process_delete_item、remove_selected | selection event、locked layerの削除制御、削除時disconnect/unlink | layerとselection寿命の参考。医用/画像viewerを主shellにはしない |
| 3D Slicer | [Libs/MRML/DisplayableManager/vtkMRMLAbstractWidget.cxx](https://github.com/Slicer/Slicer/blob/4eafce4f2e3f2d5ca19d52f5fa924c9fa9b4ffe0/Libs/MRML/DisplayableManager/vtkMRMLAbstractWidget.cxx) / SetEventTranslationClickAndDrag | 状態別のpress/move/release、keyboard event translation | VTKでもtool状態機械を独立させる設計参照 |
| PyQtGraph | [pyqtgraph/graphicsItems/PlotDataItem.py](https://github.com/pyqtgraph/pyqtgraph/blob/ca90db2c6299a11922012d8ffe69d2070c919c82/pyqtgraph/graphicsItems/PlotDataItem.py) / setLogMode、setDownsampling、connect | log軸、表示downsampling、非有限値で接続を切る設定 | N60のFR dock第一候補。数値解析は元Datasetを使う |
| Blender | [source/blender/editors/transform/transform_ops.cc](https://github.com/blender/blender/blob/325bb9d27dbfa9f7ee8db95db02e02f097f0ed74/source/blender/editors/transform/transform_ops.cc) / transform_modal、transform_cancel | modal操作、navigationとの処理分担、cancel時の状態遷移 | 操作仕様のみ参考。GPLコードは今回コピーしない |

napariは調査時点で `src/napari/`、FreeCADのSelectionは `src/Gui/Selection/` にある。以前のpathだけを列挙する調査から、実際に取得できたpathへ修正した。

## 3. 具体的に発見した統合リスク

### AffineWidget3Dのcallbackをそのまま保存へ接続しない

読んだPyVista commitでは、_move_callbackでuser callbackを呼んだ後に今回のmatrixをactorへ代入する。そのためcallback引数を「今回の確定値」と仮定するとpreviewが遅れたりsnap結果を上書きしたりし得る。_release_callbackはcache更新後にrelease callbackを呼ぶ。

さらにpress/releaseでinteraction styleを切り替える。HTDTのcamera/tool設定、Esc/capture loss、multi-select、unknown aim、DB保存はwidget任せにできない。

対策はSPEC §5/7。gestureをHTDTが所有し、adapterの出力を検証、final matrixを確定時に読み直す。callback順序に依存しないpreview経路を作り、標準widgetを使いにくい場合だけ最小VTK handleへ替える。**この所見は固定commitのコード読解であり、報告済みPoCの全版で障害が再現したという意味ではない。**

### Qt/VTKはDPIと寿命まで含めて検証する

QVTKRenderWindowInteractorは既にpixel ratioを使っている。Qt overlayの座標をさらに同じ倍率で変換するとずれる。mapper/actorの変更もGUI threadへ戻す。PyVistaQtにrender_signalがあることは、任意のVTK操作がthread safeである証拠にならない。[Qtのthread規則](https://doc.qt.io/qt-6/threads-qobject.html)

### Godotのeditorコードと配布runtimeを区別する

Node3DEditorとEditorUndoRedoManagerはeditor側の実装。Godotを採用しても、HTDT用runtime UI、gizmo、保存、Pythonとの境界を設計する作業が残る。EditorNode3DGizmoはeditor用の拡張口であり、製品runtimeにCAD editorが付属すると数えない。[公式EditorNode3DGizmo](https://docs.godotengine.org/en/stable/classes/class_editornode3dgizmo.html)

### 旧ContextDraftの成果を製品完成と数えない

PR #37のContextDraftは全payloadのcopy履歴、is_dirty=index判定、既存ContextCreateでvalidationする試作。保存後のclean基準、家具/複数測定点、wall ID、cancel transaction、非同期結果の世代管理は別途必要。試作の短さを理由に現行Contextへ新editor全体を詰め込まない。

レビュー中にPRへ追加された `0353768`ではnative shellと起動スクリプト、直接依存の版固定を確認した。tree/viewport選択と読取Inspectorはあるが、編集/Save/UndoのGUI接続とpackage受入は未完。単なるviewerと完成したeditorを区別する。

## 4. スタック比較

以下は上記コードと公式資料を元にした**設計評価**。数値スコア、FPS、開発日数は測定していない。

| 構成 | 有利な点 | HTDTで残る主要作業 | 判断 |
|---|---|---|---|
| Python＋Qt Widgets＋VTK | Qt embedding、Python計算、科学可視化、同一言語のservice | CAD入力/handle、外観、package/DPI | 第一実装。N05/N20で操作を検証 |
| C++＋Qt＋VTK/OCCT | native統合、低level制御、CAD kernelへの接続 | binding/build、Python計算境界、所有者が保守する量 | 実測したhot pathに限定導入。最初の全面rewriteは根拠不足 |
| Godot 4 runtime（C#/GDScript）＋Python worker | scene/gizmo設計の参考、3D描画とUI | editor機能のruntime化、IPC、科学場表示、配布 | 操作/描画が構造的に未達なら比較 |
| C#＋WPF/Helix Toolkit＋Python worker | Windows desktop、manipulator、.NET UI | scientific field、Python配布/IPC、選択render backendの確認 | Windows操作/配布問題が主因なら比較 |
| TypeScript＋Three.js/Babylon＋desktop shell | transform部品、UI構成、可視化の選択肢 | Python境界、desktop shell配布、科学場pipeline | 有力な代替。Web技術という理由だけで除外しない |
| Qt Quick/QML＋Qt Quick 3D | declarative UI、2D/3D構成 | VTKとの統合または独自field描画、moduleごとの配布条件 | 現段階でWidgetsと二重UI基盤を持たない |
| FreeCAD workbench/fork / CQ-editor拡張 | CAD framework、OCCT、tree/selection | HTDT向けUXの縮約、測定/field統合、依存 | 操作・設計参考。主shellにはしない |
| Blender add-on / Sweet Home 3D拡張 | 成熟した空間編集・住宅配置操作 | 主model/UXの適合、測定・科学表示、配布 | UX参考。汎用appを改造する規模を避ける |
| napari / Slicer拡張 | layer、科学可視化、Qt/VTK設計例 | ホームシアターCADのtoolと製品導線 | 局所設計参照。画像/医用domainを引き継がない |
| Rust＋wgpu/egui/Bevy | renderer/型/性能を細かく制御可能 | editor部品、科学可視化、Python境界の新規実装 | 今回コード精査/benchは未実施。低level再開発の必要性が出た時に比較 |

Qt Quick 3Dは独立した選択肢であり、PySide6を選んだだけで同じmodule条件とみなさない。[公式Qt Quick 3D](https://doc.qt.io/qt-6/qtquick3d-index.html)

## 5. 今回深入りしないOSS

前回挙がったOCCT、SolveSpace、IfcOpenShell、Clipper2、OpenSCAD、BRL-CAD、SALOMEは、今回その実装を新規精査していない。用途を限定し、調査済み/採用済みと過大表示しない。

- OCCT/CadQuery: 正確なSTEP/B-rep importが必要になった時のadapter候補。room polygon/配置のためだけにkernelを持ち込まない。
- SolveSpace/FreeCAD solver: 一般拘束解法が実要件になった時の候補。寸法編集・axis/snapのために先行導入しない。
- IfcOpenShell: IFC import要求が出た時。内部DocumentをBIM schemaにしない。
- Clipper2: 既存Shapelyのoffset/robustnessで具体的な問題が出た時に比較する。
- OpenSCAD/BRL-CAD/SALOME: solid/mesh/solver workflowの候補であり、今回の主GUI基盤候補ではない。
- Sweet Home 3D: 旧文書の `github.com/SweetHome3D/SweetHome3D` は今回404。公式サイトはSourceForgeのOSSと案内するが、今回PlanControllerソースは取得できなかった。room/furniture UXの候補として残し、コード確認済み一覧には含めない。[公式サイト](https://www.sweethome3d.com/)

## 6. 依存利用・参考・移植を区別する

| 対象 | 今回確認した根拠 | 扱い |
|---|---|---|
| PyVista / PyVistaQt | [MIT](https://github.com/pyvista/pyvista/blob/5cb68204922cac06b18dd3587b92db05163a72be/LICENSE) / [MIT](https://github.com/pyvista/pyvistaqt/blob/aa091ec97e225da8ec8cf43074bfe565a943f839/LICENSE) | 依存利用を第一候補。修正が必要な時だけ限定adapter/patch |
| VTK | [BSD形式のCopyright.txt](https://github.com/Kitware/VTK/blob/62f6ed6404031187b13654a78c7a401f5b08f368/Copyright.txt) | 依存利用。必要なnoticeと同梱依存を採用版で確認 |
| PySide6/Qt | [Qt for Python licenses](https://doc.qt.io/qtforpython-6/licenses.html) | 選んだmoduleと配布形態で確認。Qt全moduleを一つのlicense条件と扱わない |
| CQ-editor / Babylon.js | [Apache-2.0](https://github.com/CadQuery/CQ-editor/blob/3e8ef6b9b9af8cdf58fc1375a1b9f7b86409ecf8/LICENSE) / [Apache-2.0](https://github.com/BabylonJS/Babylon.js/blob/1c0985aa179a186d955fae1ed218d7d43aca49dc/license.md) | 設計参考。CQ-editorのQt bindingまで無条件に引き継がない |
| Godot / Three.js / Helix / PyQtGraph | [MIT](https://github.com/godotengine/godot/blob/dfa06cafb4546eac47d863dfa12aec54e4efa586/LICENSE.txt) / [MIT](https://github.com/mrdoob/three.js/blob/eabc262db760c5e85bd890a6dc627ba14e70e8c3/LICENSE) / [MIT](https://github.com/helix-toolkit/helix-toolkit/blob/2b94a3cd030e311a92998306297d2a8a87fd2f66/LICENSE) / [MIT](https://github.com/pyqtgraph/pyqtgraph/blob/ca90db2c6299a11922012d8ffe69d2070c919c82/LICENSE.txt) | 局所参考または評価候補。移植時は出典/変更/noticeを残す |
| FreeCAD / Blender | 上記参照ファイルのSPDX: LGPL-2.1-or-later / GPL-2.0-or-later | 今回は設計参考のみ |
| Slicer | [独自のBSD型license](https://github.com/Slicer/Slicer/blob/4eafce4f2e3f2d5ca19d52f5fa924c9fa9b4ffe0/License.txt) | event設計参考。単純なBSD-3と同一視しない |
| napari | layerlistのみ精査 | 今回は設計参考。移植/依存を選ぶ時点で採用版licenseを確認 |

今回、他projectの実装コードをHTDTへコピーしていない。実装PRでは依存追加かコード移植か設計参考かを明記する。移植する場合はupstream commit、path、変更内容、copyright/license/NOTICEを必要に応じ添付する。ライセンス名だけを根拠に無条件のcopy可としない。

## 7. 次の比較を実行可能にする

[受入仕様](CAD_EDITOR_ACCEPTANCE.md)のF1/F4を共通入力とし、A01/A02/A05〜A07を同条件で比較する。N05でQt/VTKの最小packageを試し、N20で操作・DPI・cancel・snapを評価する。

第一候補の問題は一回の改善sliceで切り分け、callback接続/入力設計の問題ならその層を直す。構造的な問題が残る時だけ、原因に合う代替を1〜2案選び、同じDocument/Command契約で試す。候補の個数やstar数では採用を決めない。

配布はまず `pyside6-deploy` のstandalone modeをN05で評価する。公式にはNuitkaを使用するが、VTKを含むHTDTの配布成功は未確認である。失敗箇所を記録し、必要時にPyInstallerのdirectory形式と比較する。installerや自動更新の作り込みはN90。[公式deployment資料](https://doc.qt.io/qtforpython-6/deployment/deployment-pyside6-deploy.html)
