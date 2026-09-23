# Placement Constraint Engine

> 更新: 2026-09-16
> G10の保存・判定契約。音響評価ではなく、物理的に配置可能かを判定するhard feasibility gate。

## 1. 基本原則

- ConstraintSetは特定のContext revisionに固定して不変保存する。
- hard constraint違反候補は音響スコアを下げるのではなく、探索前に除外する。
- `feasible=true`は「設置可能条件を満たす」だけで、音響的に良いことを意味しない。
- 不明なentity位置、未知entity、未知wall edgeを推測で補わない。
- `reference_box`だけのContextではexact room境界がないためG10判定を行わない。
- ConstraintSet specはcanonical JSON SHA-256を作成時に保存し、integrity check対象とする。

## 2. Entity

Constraint対象entityはContext内のspeaker IDとmeasurement point IDで識別する。
Context作成時にspeaker ID重複、およびmeasurement point IDとspeaker IDの衝突を拒否する。

`entity_profiles`では次を保存できる。

- `footprint_radius_m`: 筐体を水平面上で近似する半径。
- `safety_margin_m`: 設置上追加する安全余白。
- effective radius = footprint radius + safety margin。

## 3. Hard constraints

| kind | 用途 | 判定 |
|---|---|---|
| `allowed_region` | entity別の設置可能領域 | effective envelope全体がregion内 |
| `exclusion_region` | 家具・ラック・扉・通路等 | effective envelopeがregionと非交差 |
| `wall_clearance` | 指定wall edgeからのmin/max離隔 | center-edge距離からeffective radiusを差し引く |
| `axis_range` | X/Y/Z固定または範囲 | fixed/min/max + tolerance |
| `movement_budget` | 現Context位置からの最大移動 | 3Dまたは水平XY距離 |
| `pair_distance` | speaker-speaker / speaker-MLP | center距離またはenvelope clearance |
| `linked_placement` | 左右鏡映・等座標・同量移動 | relation + tolerance |

`allowed_region`は単一polygonまたは複数polygonのOR集合を指定できる。
`exclusion_region`は複数constraintを保存できるため、家具・通路等を独立IDで説明可能。

左右鏡映は`mirror_axis_x_m`を省略した場合reference box中央を使う。非矩形室で実際の対称軸が異なる場合は明示値を保存する。

`pair_distance.distance_reference=envelope_clearance`では、center距離から両entityのeffective radiusを差し引いた実効隙間を判定する。

## 4. 判定結果

評価APIは次を返す。

- `feasible`
- `overridden_entity_ids`
- Context baselineを含む`resolved_positions`
- `observations`
- `rejections`
- engine / geometry version
- ConstraintSet ID / spec SHA-256

各rejectは最低限、`constraint_id`、`kind`、対象entity、message、実値/閾値を保持する。
したがって「なぜその候補を探索対象から除外したか」を後から再現できる。

## 5. API

- `GET /api/projects/{project_id}/constraint-sets`
- `POST /api/projects/{project_id}/constraint-sets`
- `GET /api/projects/{project_id}/constraint-sets/{constraint_set_id}`
- `POST /api/projects/{project_id}/constraint-sets/{constraint_set_id}/evaluate`

ConstraintSetに更新/delete APIは設けない。条件を変える場合は新しいConstraintSetを作成する。

## 6. UI

Room/Layoutで選択しているContextに紐づくConstraintSetを一覧表示し、speaker/MLP候補座標を入力してhard constraint評価できる。

G10 UIはguided builderを備え、JSON/APIを直接編集せずに次を作成できる。

- entityごとの筐体半径と安全余白。
- 単一/非連結allowed region、家具・通路等のexclusion region。
- wall edgeごとのmin/max clearance。
- X/Y/Z fixed/range、movement budget。
- center/envelope基準のpair distance。
- mirror/equal/equal-delta linked placement。

候補評価画面は上面図でroom polygon、Context基準位置、候補位置、移動線を表示し、reject対象entityと違反理由を視覚的に確認できる。

実室固有のallowed/exclusion region、家具、通路、壁離隔などの値は推測で生成しない。実測・入力された制約だけを保存する。

## 7. G10とO10の境界

G10は候補1件を決定論的に`feasible/rejected`判定するengineまでを担当する。
格子、seed、刻み、linked master→slave生成、候補列挙・重複除去はO10 Search Spaceで実装する。
O10は必ずG10 evaluatorを通してから候補をPredictionへ渡す。
