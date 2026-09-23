# Room Geometry contract

> 更新: 2026-09-16
> 実装: mainのG00 Room Geometry v2。以下は現行Contextの実装契約。
> 新native Sceneのwall ID、opening、bounds自動導出、負座標、制約参照は[CAD編集契約](CAD_EDITOR_SPEC.md)を正本とし、G00/G10とは明示adapterで接続する。現行コードが新契約に対応済みという意味ではない。

## 1. 座標契約

HTDTの部屋座標は従来どおり固定する。

- 原点: reference boxの前・左・床。
- X: 右方向。
- Y: 後方向。
- Z: 上方向。
- 単位: metre。

`width_m/depth_m/height_m`は常にreference boxを表す。非矩形室ではreference boxを実境界とみなさない。

## 2. geometry_kind

- `rectangular`: reference boxそのものがexact room footprint。
- `polygon_prism`: ordered 2D polygonを一定高さまで押し出したexact footprint。
- `reference_box`: exact footprintがまだ不明。reference boxだけ既知。

## 3. polygon_prism

`footprint_vertices`は閉じないordered vertex列として保存する。最初の頂点を末尾へ重複させない。

各頂点は次を持つ。

- `vertex_id`: Context revision内で一意な不変ID。
- `x_m`
- `y_m`

3頂点未満、重複座標、重複vertex ID、自己交差、非有限値、極小面積、reference box外への突出を拒否する。

壁edgeは隣接頂点から `from_vertex_id->to_vertex_id` として導出する。最終頂点から先頭頂点へのedgeも含む。G10ではこのedge IDを壁別clearance/tagの安定参照に使う。

## 4. 配置点の判定

speakerとMLPはまずreference boxのXYZ範囲を満たす必要がある。`polygon_prism`ではさらにXYがroom polygonにcoveredされる必要がある。

壁面境界上はroom内として扱う。これは後続G10で筐体footprintやwall clearanceを適用する前の幾何的包含判定であり、境界上への実設置を推奨する意味ではない。

## 5. 幾何ライブラリ

G00/G10のpolygon predicateはShapely 2.1.2を固定利用する。Python 3.12 / Windows x86-64 wheelが公開されていることを確認済み。

使用する基本意味論:

- validity: simple polygonとしてwell-formedであること。
- covers: polygon境界を含む包含判定。
- 後続G10: buffer/difference/distance等をfixtureで独立検証してから制約判定へ使用する。

## 6. 矩形専用モデルとの分離

既存A01 room mode / 6面一次反射は`geometry_kind=rectangular`だけ実行できる。`polygon_prism`や`reference_box`へ矩形式を自動適用しない。

REW Room Simulatorも矩形室専用なので、非矩形Contextに使う場合は別PredictionRunで`rectangular_approximation`と記録する。実室polygonのexact predictionとは分類しない。

## 7. 現在の制限

- 天井高は一定。
- polygon holeは未対応。
- 曲面壁、傾斜/段差天井は未対応。
- 家具、通路、扉、設置可能領域、壁離隔、筐体寸法はG10 ConstraintSetで扱う。
