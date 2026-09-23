# ADR-0001: Native CAD editorの第一構成と採用gate

- Status: **Accepted — first implementation direction; acceptance pending**
- Date / reviewed: 2026-09-16
- Supersedes: browser-first GUI、旧形式の互換必須、browserとのfeature parity
- Related: [Roadmap](../IMPLEMENTATION_ROADMAP.md)、[OSSコード調査](../CAD_EDITOR_OSS_RESEARCH.md)、[編集契約](../CAD_EDITOR_SPEC.md)、[受入](../CAD_EDITOR_ACCEPTANCE.md)

## Context

HTDTはmouseで部屋・配置を作り、測定と予測を同じ空間で確認するソフトウェアへ移行する。従来のフォーム中心UIは操作とstateを根本から見直す。旧版の互換、既存言語、既存画面構造は採用を拘束しない。

既にnative Qt/VTK PoCの報告がある。2026-09-16の追加レビューでは13のOSSの選定したソースを確認した。gizmoの存在とCAD editorとしての操作品質を区別し、再現可能なcode/lock/packageと実機gateを先行させる必要がある。

## Decision

1. **Python 3.12 x64、PySide6/Qt Widgets、PyVista/VTK/PyVistaQtを第一実装とする。** N05でWindows用依存をlockする。理由はPython科学計算とnative Qt/科学可視化の統合の簡潔さであり、他言語やWeb技術の排除ではない。
2. **Documentとrendererを分離する。** SceneRevisionは部屋と物体集合の不変snapshot。測定Contextはそのrevisionと測定条件を参照する。現在の一測定点Contextをeditor全体のmodelにしない。
3. **操作の正本はToolController/CommandHistory。** dragはpreview、確定で1 command、取消で全復元。actor行列、Qt widget、QUndoStackを別正本にしない。
4. **selection、snap、view stateを独立させる。** entity IDを用い、同じcommand/validationをviewportとInspectorから呼ぶ。表示hide/lockと物理条件を分ける。
5. **UIをblockする計算を避ける。** Qt/VTKはGUI thread、I/O/計算は必要に応じworker。不変入力とgenerationで古い結果の適用を防ぐ。
6. **新schemaは旧形式と分離可能。** 自動migrationや機能同等性は不要。再利用するdomain/REW/比較資産だけadapterへ抽出し、測定原本は保持する。
7. **汎用CAD kernelを必須にしない。** 一室polygon prismと剛体配置から開始し、wall/opening参照を明示管理する。高度importは要求が生じた時にadapter化する。
8. **配布・保存・操作を先に検証する。** N05で縦断操作とstandalone package、N10で保存/復旧、N20でCAD品質。N90まで基礎的な配布問題を先送りしない。

## Source-driven consequences

PyVistaの確認commitではinteract callbackが新matrixのactor代入より前で、press/releaseがinteraction styleを変更する。AffineWidget3Dをそのまま永続保存へ接続しない。最終値・cancel・DPI・observer解放はadapterの責務とし、wrapperが複雑になりすぎる場合は最小VTK handleへ置換する。

新Sceneでは安定wall ID、opening参照、複数seat/measurement pointを設計する。既存G00/G10は有用な実装資産だが、新editor schemaの形を拘束しない。幾何計算の再利用は境界adapterで行う。

Qt標準widgetだけで洗練されたUXが完成するわけではない。viewport比率、contextual Inspector、直接handle、DPI、初見操作を受入にする。Pythonの統一による実装効率と、Qt/VTK入力を作り込む費用を両方認める。

## Alternatives and reconsideration

- **Godot runtime**: scene/editor実装は有益な参考。ただしeditor専用gizmoやUndoManagerはruntime製品へ自動搭載されない。Python境界と科学場表示を含め比較する。
- **C#＋Helix Toolkit**: Windows UI/manipulatorの候補。WPF版を読んだ結果を別backendへ一般化しない。
- **TypeScript＋Three.js/Babylon＋desktop shell**: CAD操作部品がある有力代替。Web技術だからという理由で却下しない。
- **C++＋Qt/VTK/OCCT**: 性能や高度CADで必要な箇所へ導入できる。全面rewriteは計測した問題がある場合に評価。
- **FreeCAD/Blender/CQ-editor拡張**: 完成app/frameworkへの依存とHTDT向けUXの縮約コストがあるため、当面は局所設計参照。

N05/N20で構造的な問題が一回の改善slice後も残る場合、失敗原因に合う候補を同じfixtureで比較する。Godotを無条件の第一fallbackに固定しない。domainを保ちながら該当するrender/interaction/shell層を再選定し、結果を新ADRまたは本書の改訂へ残す。

## Validation

A01/A02で再現可能な縦断操作とpackage、A05〜A07でmove/rotate/snap/cancel/Undo、DPIとF4性能を検証する。明示した数値は目標であり実測値ではない。

このADRのAcceptedは実装方針の採択を意味する。N05/N10/N20やGUIの完成・性能合格を意味しない。
