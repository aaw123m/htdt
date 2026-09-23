# O10 Search Space Contract

> 更新: 2026-09-16

O10は、G10で定義したhard constraintsの内側にある配置候補を、同じ入力から同じ順序で再生成する層である。
音響予測、順位付け、Pareto評価は行わない。

## 保存単位

schema v5で`search_specs`を追加する。

- Project ID
- source Context revision ID
- source ConstraintSet ID
- ConstraintSet canonical spec SHA-256
- algorithm / algorithm version
- entity + X/Y/Zごとのmin/max/step
- linked master→slave derivation
- candidate limit
- SearchSpec canonical SHA-256

SearchSpecは作成後に更新しない。編集は既存版をGUIへ複製し、新しいSearchSpecとして保存する。

## Grid semantics

初期算法は`search-space-grid-1` / `deterministic_grid`のみ。

- 軸値はdecimal演算で`min + n * step`から作る。
- `max`が格子点でなければ、maxを勝手に追加しない。
- axis順はentity ID、X/Y/Z順に正規化して保存する。
- raw candidate countは生成前に計算する。
- `candidate_limit`を超える場合は部分生成せず422で停止する。
- system上限は50,000 raw candidates。
- 乱数は使用しないためO10 v1にseedはない。将来random/sampling算法を追加する場合はseedを必須保存する。

Context baseline位置がないentityを可動軸またはlinked derivationへ使わない。
reference box内の格子点であっても、G10でexact room/allowed/exclusion等を満たさなければ候補にならない。

## Linked derivation

G10の`linked_placement`をO10でmaster→slave生成へ利用する。

- `mirror_x`: 明示mirror axis、なければroom reference width中央を使う。
- `equal_x/y/z`: slave軸をmaster値へ合わせる。
- `equal_delta_x/y/z`: Context baselineからのmaster移動量をslaveへ同量適用する。
- slave対象軸を独立grid axisとして同時指定できない。
- 同一slave axisを複数derivationで生成できない。
- v1はchained/cyclic derivationを許可しない。

導出後の配置も必ずG10 evaluatorへ渡す。linked derivationがあること自体をconstraint合格の代替にはしない。

## Feasibility / identity

各raw grid組合せについて、可動軸とlinked derivationでcandidate overrideを作り、G10 hard gateを実行する。

- hard rejectは`rejected_candidate_count`へ計上する。
- 同じ最終配置へ畳み込まれた候補は`duplicate_candidate_count`へ分離する。
- `raw = feasible + rejected + duplicate`を維持する。
- reject理由はconstraint ID別candidate countとして集約する。

Candidate IDはSearchSpec SHA-256とcandidate override座標から決定論的に生成する。
feasible candidate ID列から`candidate_set_sha256`を生成し、再生成一致を検査できる。
候補そのものはO10では大量永続化せず、immutable SearchSpecから導出可能な結果として扱う。

生成APIは全candidate count/hashを計算する一方、座標本体はページングする。
`offset >= 0`、`1 <= limit <= 500`を要求する。

## API

- `GET /api/projects/{project_id}/search-specs`
- `POST /api/projects/{project_id}/search-specs/preview`
- `POST /api/projects/{project_id}/search-specs`
- `GET /api/projects/{project_id}/search-specs/{search_spec_id}`
- `POST /api/projects/{project_id}/search-specs/{search_spec_id}/generate?offset=...&limit=...`

SearchSpecまたはConstraintSetのSHA整合性が壊れている場合は生成しない。
SearchSpecが保存時に固定したConstraintSet SHAと現在値が一致しない場合も生成を拒否する。

## UI

`Constraints → Search → Measure`を主フローとする。
Search画面では、ConstraintSet、可動entity/axis、min/max/step、linked relation、candidate limitをvisual formで設定する。

保存前にraw candidate countをpreviewできる。保存済みSearchSpecはimmutable表示し、`複製して編集`でdraftへ戻す。
生成後はraw / feasible / rejected / duplicateを分離表示し、上面図へ候補雲を描く。
候補行を選ぶと選択点を強調し、可動entityのXYZを表示する。

## O10 acceptance

手計算可能なfixtureで次を固定する。

- 3 × 2 = 6 raw grid candidates。
- rack exclusionで1候補だけhard rejectし、5 feasible candidates。
- 同一SearchSpecを2回生成してJSON、candidate順、candidate IDs、candidate set SHAが一致。
- candidate limit超過は生成開始前に拒否。
- room bounds外axis、unknown entity、baselineなしentityを拒否。
- linked master/slaveの不整合、独立gridとの衝突を拒否。
- backup/restore後も同じ候補集合を再生成。
- page offset/limitを変えても全体candidate set SHAは不変。

8頂点凹polygonのvisual QAでは81 raw candidatesから62 feasible、19 rejected、0 duplicateを生成し、1440 pxと390 pxで横はみ出しがないことを確認した。

## Scope boundary

O10は探索空間の列挙だけを行う。REW Room Simulatorやpolygon predictorのbatch predictionはO20以降。
音響objective、重み付きscore、Pareto順位、推薦はO30/O40以降であり、O10 candidateを「良い配置」と表示しない。
