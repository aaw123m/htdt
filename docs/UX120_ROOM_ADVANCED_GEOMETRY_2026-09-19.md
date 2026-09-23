# UX120 Room advanced geometry integration — 2026-09-19

Issue #118 の新Room workspaceへ、legacy N30a/N30bで成立していた高度な部屋・壁・開口編集を
workflow-first componentとして移植した記録。

## Authority

新UIは独自geometry authorityを作らない。

commit経路:

- room snapshot: `RoomWorkingDocument.replace_room()`
- room + wall topology: `RoomWorkingDocument.replace_room_topology()`
- wall topology validation: `cad_walls.validate_wall_topology()`
- wall move: `cad_walls.move_wall()`
- wall split: `cad_walls.split_wall()`
- wall merge: `cad_walls.merge_walls()`
- wall delete: `cad_walls.delete_wall()`
- opening add: `cad_walls.add_opening()`
- wall thickness update: `cad_walls.update_wall_thickness()`
- opening update/delete: `cad_walls.update_opening() / delete_opening()`

`update_wall_thickness / update_opening / delete_opening` はlegacy UIに閉じていた操作を
再利用可能なdomain helperへ移したもの。全操作は既存`validate_wall_topology()`を通る。

## Direct manipulation

`RoomGeometryInputController` はtransient pointer/selection stateのみを保持する。

- vertex click / drag
- edge midpoint selection
- edge midpoint dragによるwall move
- selected vertex / selected edge state
- invalid polygon / opening overflow / dangling wall referencesのfail-closed reject
- current wall topologyがある場合はID/opening/bindingを維持したcommit

wall topologyが未作成のroomでedgeをdragした場合は、`make_wall_topology()`をtransientに生成し、
実際にmoveが成立したcommit時だけSceneへ入れる。単にedgeを選択しただけではSceneを変更しない。

## Geometry Inspector

Room > 形状 の右側context surfaceを専用Inspectorへ切り替える。

部屋:
- bounds / vertex count
- 天井高
- 形状編集開始/終了

vertex:
- X / Y numeric edit
- vertex delete

edge:
- exact edge length
- midpoint vertex insert
- wall topology enable

wall:
- wall ID
- wall length
- thickness
- merge with next wall
- wall delete

opening:
- selected-wall opening list
- door / window / passage / other
- wall-local offset
- width
- sill
- height
- open/closed flag
- add / apply / delete

通常のobject InspectorはObjects/Placement contextへ残し、geometry controlを常設しない。

## Reference preservation

topologyが存在する場合:

- vertex coordinate / edge dimension / ceiling heightは既存wall IDsを保持して再validation
- midpoint insertは`split_wall()`でopening/bindingをchild wallへmigration
- vertex deleteは`delete_wall()`の保守的orphan checkを使用
- wall moveはopening wall-local offsetを維持
- openingが新しいwall length/room heightからはみ出す編集はcommitしない
- wall deleteがopening/bindingをorphanする場合はcommitしない

room全体の再作図は、既存topologyにopeningまたはbindingがある場合はfail closedする。
参照を無言で破棄しない。

## Viewport feedback

geometry edit中は:

- vertex handles
- edge midpoint handles
- selected handle
- selected wall highlight
- selected wallに属するopening outline

を表示する。openingはwall-local寸法を3D位置へ投影するが、表示はpresentationでありauthorityではない。

## Validation

focused tests:

- shared wall thickness / opening edit helpers
- midpoint split後もopening ID / offsetが維持される
- topology付きedge length変更でopeningがはみ出す場合はfail closed
- geometry context専用Inspector mount
- Inspectorからopening追加がexisting wall authorityへpersistされる

既存`test_cad_walls.py`がsplit/merge/delete/move/orphan semanticsのauthority regressionを継続する。

## Remaining UX120 / UX-series work

Roomの主要editing parityはここで成立した。残りは主にUX150/UX160品質gate:

- visual hierarchy / spacing / Japanese copy polish
- viewport lighting / material / overlay tuning
- hit target / hover / focus feedback
- 1280x800 / 1440x900
- 100 / 150 / 200% DPI
- owned-Windows first-use / visual acceptance

UX140のlegacy QMainWindow adapter除去は別cleanup。

RDCは使用しない。
