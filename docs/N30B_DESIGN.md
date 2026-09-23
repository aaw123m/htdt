# N30b wall / opening design

> 2026-09-17 / Issue #53 / PR #54

N30aのroom footprintを正本とし、N30bでは壁・開口・壁別clearanceの**stable reference identity**を追加する。footprintは室内仕上げ面であり、wall thicknessは表示属性として外側へ表現する。厚さによって室内footprintや容積を暗黙に縮めない。

## Domain boundary

- `RoomPrism`: ordered room vertices and height。幾何妥当性の正本。
- `WallSegment`: stable `wall_id`, from/to vertex IDs, thickness, optional `source_wall_id`。
- `WallOpening`: stable `opening_id`, `wall_id`, wall-start offset, width, sill, height, kind/open state。
- `WallConstraintBinding`: N30bではwall IDとclearance値の参照連続性だけを保持。本格constraint solver semanticsはN50へ分離。
- `WallTopology`: boundary-edge-to-wall 1:1 mapping + openings + constraint bindings。

`WallTopology`は第二のpolygon modelではない。すべてのwallは`RoomPrism`のordered boundary edgeへ正確に1:1対応し、opening/constraintは存在するwall IDだけを参照できる。

## Edit rules

### Move wall

対象wallの両endpoint vertexを同じXY deltaで移動する。wall/opening/constraint IDは変更しない。結果のRoomPrismがsimple polygonであり、すべてのwall-local openingが参照wall内に収まる場合だけ確定する。

### Split wall

1つのstable room vertexを挿入し、旧wallを2つの新wall IDへ置換する。両childは`source_wall_id=old wall_id`を保持する。

openingがsplit前側へ完全包含される場合はfirst childへ、後側へ完全包含される場合はsecond childへ移し、後側ではsplit offsetを差し引く。split pointを跨ぐopeningは曖昧なので編集全体を確定拒否する。

clearance bindingが旧wallを参照していた場合、binding IDとclearance値を維持したまま両child wall IDへ展開する。

### Merge walls

ordered隣接wallで、共線・同方向・同thicknessの場合だけmergeを許可する。共有room vertexを除去し、新wall IDを生成する。second wall側openingはfirst wall lengthをoffsetへ加算して移送する。両wallを参照するclearance bindingは重複を除いてmerged wall IDへ統合する。

異なるwall属性や非共線cornerを無言で片側へ寄せない。

### Delete wall

選択wallの終点vertexを除去し、選択wallとsuccessorを明示replacement wallへ置換する。3壁未満にはしない。affected pairにopeningまたはconstraint bindingが残る場合は参照切れを推測解決せず確定拒否する。thickness conflictやinvalid room geometryも拒否する。

## Transaction boundary

Topology関数はpureで、候補`(RoomPrism, WallTopology)`を返すか`WallTopologyError`を送出する。CommandHistoryやSQLiteは直接触らない。

`RoomWorkingDocument.replace_room_topology()`がSceneDocument全体を再validateし、room geometry + wall topology + migrated opening/constraint referencesを**1 command**として確定する。Undo/Redoはそのsnapshotを一括復元する。

SceneDocumentはwall topologyを持つ場合schema v3。`wall_topology=None`の旧sceneではcanonical JSONからfield自体を省略し、N05〜N30aのhash互換を維持する。

## Cross-tool composition

N30b完了時点では`CadEditorWindow`を通常product entryとする。これはN30a room toolとN30b wall toolの境界を調停する薄いcomposition layerで、domain logicをGUIへ重複実装しない。

- wall topology作成前はN30a room sketch / vertex insert / deleteを従来どおり使える。
- topology作成後も、vertex ID/orderを保つ頂点移動・edge寸法・天井高は既存topologyとopening/constraintを再validateして確定できる。
- topology作成後のroom vertex insert/delete/room再作図は、stable wall refsを暗黙に作り直さず無効化し、wall split/deleteへ誘導する。
- room変更でopening/constraint条件を満たせなくなる場合は例外をUIへ漏らさず確定拒否する。

この境界により「N30aを壊さない」と「N30bのstable wall IDを無言で失わない」を両立する。

## Native interaction

- Top viewでwallをクリック選択し、mouse dragでmoveする。
- toolbarからsplit / merge / delete / door opening / clearance bindingを操作する。
- Inspectorでwall thicknessとclearance値を数値精密化する。
- selected wall、wall prism、openingをviewportへ可視化する。
- N20b object editとN30a room editの入力モードとは排他的にし、既存編集を壊さない。
- 製品GUIは日本語を優先し、技術用語やIDなど翻訳すると不明瞭になる箇所だけ英語を残す。

## Acceptance

A09はF3（F2凹room + opening + wall clearance + 2 measurement points）を使用する。実Windows mouse inputでwall select/moveを行い、split/merge/delete、opening/constraint参照追跡、曖昧split拒否、Undo/Redoを確認する。

2026-09-17、通常起動と同じ`CadEditorWindow`を使うcommit `5ede848e8e0b0967a50c04c83ff679a649ca439b`でA09 PASS。詳細は[N30b A09 Windows acceptance](N30B_ACCEPTANCE_2026-09-17.md)。