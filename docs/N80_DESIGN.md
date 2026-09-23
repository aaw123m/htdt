# N80 design — native optimization workspace

Tracking: Issue #65  
Branch: `feat/n80-optimization-workspace`  
Base main: `286f3b86a742ef6e3454d91c609ce1f84d840c57`

## 1. 目的

N80は、N50のhard constraint、N60のmeasurement authority、N70のprediction authorityを、探索候補とobjective/Pareto比較へつなぐnative CAD workspaceとする。

最重要原則は**native SceneRevisionを正本に保つこと**。既存O10実装は算法serviceとして再利用するが、legacy Context / Store / browser Search UIをnative authorityへ昇格しない。

## 2. 実装slice

### N80a — native SearchSpec + candidate workspace

最初にO10の既存 deterministic search-space engineをnative Sceneへ接続する。

- immutable native SearchSpecを保存する。
- SearchSpecはexact `document_id` / `scene_revision_id` / `scene_content_hash` / native constraint workspace hashへbindingする。
- native SceneDocumentを`scene_to_g10_context()`でtransient G10/O10 payloadへprojectionする。
- native `CadConstraintSet`からG10 stored specを構成し、既存`validate_search_spec()` / `generate_search_space()`を呼ぶ。
- candidatesそのものを大量永続化せず、immutable SearchSpecから決定論的に再生成する。
- raw / feasible / rejected / duplicate countと`candidate_set_sha256`を保持する。
- candidate cloudはN70のbulk marker primitiveを再利用する。
- candidate previewはview stateであり、SceneRevisionを変更しない。
- explicit applyだけが`TransformEntitiesCommand`相当の1 commandでWorkingDocumentへ反映し、1 Undoで完全復元する。
- source SceneRevisionまたはconstraint hashが変わったSearchSpecはstale。current sceneへ再生成・applyしない。

N80aではnative constraint modelに存在しないlegacy `linked_placement`を偽装しない。linked derivationはnative constraint contractを追加する独立sliceで扱う。

### N80b — batch prediction / objective vector

O20/O30の該当gateが成立したmodelだけ接続する。

- candidate batch predictionはmodel applicability、model version、source SceneRevision、SearchSpec identityを固定する。
- objectiveは独立vectorとして保存する。
- flatness、peak/dip、左右差、席間差、移動量等を単一スコアへ暗黙縮約しない。
- predicted / measured / derived evidenceを区別する。

### N80c — Pareto / measurement loop

O40以降を接続する。

- Pareto非劣集合を基本表示とする。
- weighted preferenceを追加する場合も元objective vectorとweight/versionを残す。
- candidate → explicit apply → SceneRevision → Measurementを履歴として追跡する。
- O60の独立model validation前に自動推薦へ昇格しない。

## 3. Native SearchSpec authority

初期schemaは最低限次をimmutable保存する。

- `search_spec_id`
- `document_id`
- `scene_revision_id`
- `scene_content_hash`
- `constraint_workspace_hash`
- algorithm `deterministic_grid`
- algorithm version `search-space-grid-1`
- grid axes: entity ID / X,Y,Z / min / max / step
- candidate limit
- canonical SearchSpec JSON + SHA-256
- creation time

SearchSpec更新はしない。編集は複製→新規保存。

Constraint workspace hashはcanonical native `CadConstraintSet.model_dump(mode='json')`から計算する。保存時と生成時に再計算し、一致しなければstaleとして拒否する。

## 4. O10 adapter boundary

既存`search_space.py`はlegacy Context-shaped dictを受けるが、これは算法input contractとしてのみ使用する。

native adapterの手順:

1. exact source SceneRevisionをrepositoryから読む。
2. source content hashがSearchSpec bindingと一致することを確認する。
3. native `CadConstraintSet`のdocument ID/hashを確認する。
4. `scene_to_g10_context()`でtransient context payloadを作る。
5. `build_g10_constraint_request()` + legacy G10 validatorでconstraint stored specを作る。
6. `SearchSpecCreate`を既存O10へ渡し、canonical stored specを得る。
7. 生成時も同じsource revision / constraint hashで`generate_search_space()`を呼ぶ。

legacy project/context/search_spec DBにはnative SearchSpecを書かない。

## 5. Candidate semantics

Candidateは**feasible geometry candidate**であり、音響品質順位ではない。

各candidate:

- deterministic candidate ID
- raw/feasible index
- touched entityのexact XYZ override
- source SearchSpec SHA
- source candidate-set SHA

Candidate preview:

- current sceneをcommitしない。
- non-pickable ghost/analysis representationを使う。
- candidate cloudは候補数に比例してactorを増やさない。

Candidate apply:

- source revision/hash/constraint hashがcurrent authorityと一致する場合のみ可能。
- touched entitiesを一括して1 commandで置換する。
- 1 Undoで全entity位置をbeforeへ戻す。
- applyだけではSceneRevision保存しない。既存Save actionで明示保存する。

## 6. UI

N70 `PredictionWorkspaceWindow`を継承して日本語`最適化` dockを追加する。

N80a UI:

- source revision / constraint workspace状態
- movable entity + axis
- min / max / step
- candidate limit
- raw count preview
- immutable SearchSpec保存/履歴
- generate
- raw / feasible / rejected / duplicate
- candidate cloud
- candidate list/detail
- preview / clear preview / apply

表示語は「候補」「実行可能」であり、「推奨」「最適」「1位」を使わない。

## 7. Async / stale boundary

候補生成がGUI threadをblockingしない規模へ拡張する場合はN70のjob guard型を継承する。少なくともapply前にはcurrent document/revision/hash/constraint hashを再検証する。

document switch、revision change、constraint change後の古いcandidate setをcurrent sceneへapplyしない。

## 8. Test / acceptance

意味のあるfocused testだけ追加する。

N80a automated invariants:

- native SearchSpec save/load round-tripとsource revision/hash binding
- constraint hash mismatchを拒否
- 同一native sourceでO10 candidate IDs/order/set hashが再現
- candidatesがhard-constraint rejectを含めない
- previewがWorkingDocument history/dirty状態を変更しない
- applyが1 commandだけhistoryを増やし、1 Undoで全位置を復元
- stale source revision/constraint workspaceからapply不可
- candidate cloudがbulk actorを再利用

Owned-Windows A14:

- candidate選択→preview→explicit apply→Undoをreal interactionで確認する。
- stale/cancel/document boundaryはA13を継承する。

## 9. 非目標

N80aでは次を実装しない。

- fake objective/Pareto UI
- unvalidated acoustic prediction
- single quality score
- automatic recommendation
- browser UI parity
- legacy Context authority

O30/O40の実装が存在しない現状では、ParetoはN80b/cの明示的な後続sliceとする。

## 10. O30 objective-vector core

O20のbatch prediction/model gateとは分離して、O30の純粋な評価算法を先に実装する。入力FRのevidence authorityは呼出し側が保持し、この層は数値を実測/予測へ勝手に昇格・変換しない。

algorithm versionは `objective-vector-1`。

初期objectiveはすべて `minimize` で、独立metricとして保持する。

- `response.rms_difference_db`: targetとの差をlevel offset込みでRMS評価する。
- `response.peak_excess_db`: targetより上側の最大超過量。負値は0へclampする。
- `response.dip_deficit_db`: targetより下側の最大不足量。正方向の不足量として保存する。
- `response.shape_rms_db`: 明示したreference bandでlevel offsetを求め、そのoffsetを除いた形状差RMS。reference bandがない場合は生成しない。
- pair/left-rightも同じlevel-sensitive RMSと、明示reference bandがある場合だけshape RMSを別metricにする。
- multi-seatは全seat pairについてlevel-sensitive RMS differenceのmax/RMSを保存し、reference bandがある場合はpairwise shape max/RMSも別metricとして保存する。
- movementはcandidateが変更するentityごとの3D Euclidean distanceからtotalとmaxを別metricとして保存する。

周波数比較は既存 `comparison.py` の96 points/octave log-grid、補間、excluded band semanticsを再利用する。band、reference band、excluded bandは評価条件として明示され、暗黙の平滑化や自動level alignmentを追加しない。

objective vectorを統合する場合もmetric IDを維持し、weighted total scoreへ自動変換しない。

## 11. O40 Pareto core

algorithm versionは `pareto-front-1`。

初期Pareto coreは決定論的な非劣集合抽出のみを担当する。

候補Aが候補Bを支配する条件は、選択された全objectiveで `A <= B` かつ少なくとも1 objectiveで `A < B`。

- 同値vector同士は互いを支配しないため、両方とも非劣集合に残る。
- input candidate順を維持して結果を返す。
- candidate ID重複、objective欠落、objective ID重複は拒否する。
- objective subsetを明示指定できるが、暗黙weightやtie-break順位は導入しない。
- dominated candidateについては、どのcandidateに支配されたかを保存可能な形で返す。

このcoreだけではO40全体完了とはしない。粗探索→局所探索、候補多様性、native persistence/UI、measurement loopは後続sliceで実装する。


## 12. O20 REW Room Simulator transactional batch contract

N80bの最初の実prediction backendは、既にS01相当のread-only/実機probeを持つREW Room Simulatorを、**位置変更だけのtransaction**として接続する。

model IDは `rew-room-simulator`、初期batch adapter versionは `rew-roomsim-position-batch-1`。

### Model applicability

- exact modelとして使えるのはaxis-aligned rectangular roomだけ。
- native polygon rectangleがshifted originを持つ場合は、exact rectangle frameの `origin_x_m / origin_y_m` を引いてREW room-local座標へ変換する。
- L字等の非矩形roomを外接矩形へ暗黙変換しない。
- REWのroom width/length/heightがnative exact rectangle dimensionsと一致しなければwrite前に拒否する。
- speaker source positionはphysical body centreではなく、明示`acoustic_reference_offset_m`から解決したacoustic referenceだけを使う。reference不明speakerは拒否する。
- receiverはmeasurement pointまたは明示acoustic referenceを使う。
- candidateが移動するspeakerは必ずexplicit native entity -> REW source bindingを持つ。
- combined FRではREWのactive source全てにexact mappingが必要。余分なactive sourceがある状態を黙ってcombined predictionへ混ぜない。
- source-specific FRでは指定REW sourceがnative mappingに含まれることを要求する。

### Transaction

production GET-only `RewApiClient`の通常契約は維持し、writeはO20用`RewRoomSimControlClient`だけで明示的に行う。

1. full Room Simulator snapshotを取得しcanonical SHA-256を固定する。
2. write直前にもう一度full snapshot hashを確認する。
3. source/head positionだけをPOSTで変更する。room dimensions、absorption、options、source configurationは変更しない。
4. 各write直後の実snapshotをtransaction-owned stateとして保持する。左右連動等で指定外source positionへ副作用が出ても追跡する。
5. 全write後のfull stateがnative candidateから構築したexact intended stateと一致しなければFRを採用しない。
6. FR取得後もfull state hashが変化していないことを確認する。
7. 成否にかかわらずrestoreを実行する。owned stateから変わっていないpositionだけを元へ戻し、ユーザーが途中変更したfieldを上書きしない。
8. full stateがpre-stateへexact restoreされた場合だけbatch resultを返す。
9. external change、partial apply、restore mismatchはhard error。FRはobjective evidenceへ昇格しない。

REWにはHTDT用CAS/transaction APIがないため、競合を「防止した」とは扱わない。複数snapshotとfail-closed restoreで検出可能な範囲を明示し、real-machine acceptanceではREW UI途中変更も検証対象にする。

### Native adapter

`SceneRevision + CadSearchSpec + CadCandidate + CadRoomSimBinding`からbatch requestを作る。

- SearchSpecのdocument/revision/content hashを再検証する。
- candidate preview documentでacoustic reference world positionを解決する。
- exact rectangular frameへlocalizeする。
- legacy Contextを保存authorityとして使わない。
- O20結果を後段で保存する際はSceneRevision/SearchSpec/candidate/model version/pre-state hashをimmutable provenanceとして持たせる。


## 13. O20 immutable batch/result persistence and resume

PR #71で確立したposition-only Room Simulator transactionを、native SearchSpec/candidate authorityへ不変に結びつけて保存する。

### BatchSpec

1つの `CadRoomSimBatchSpec` は以下を固定する。

- exact document / SceneRevision / scene content hash
- SearchSpec ID / SHA
- O10 candidate-set SHA
- explicit `CadRoomSimBinding` canonical JSON / SHA
- model ID `rew-room-simulator`
- adapter version `rew-roomsim-position-batch-1`
- 実行対象candidateごとのraw/feasible index
- candidateから構築済みのexact Room Simulator request canonical JSON / SHA

candidate実行時にcurrent WorkingDocumentからrequestを再構築しない。batch作成時にnative authorityを解決してfreezeし、後続I/Oはその不変requestを使用する。

### Candidate attempt

各candidate実行はappend-only `CadRoomSimCandidateAttempt` として保存する。

completed attempt:

- exact candidate ID / attempt index
- exact REW version
- pre/applied/restored full-state SHA
- canonical Room Simulator FR payload / SHA
- started/completed timestamp
- attempt identity SHA

failed attempt:

- candidate ID / attempt index
- error type/message
- started/completed timestamp
- attempt identity SHA
- response payloadは持たない

completed attemptが1つ存在するcandidateは同じbatchで再実行しない。failed attemptは履歴を上書きせず次のattempt indexとしてretry可能にする。

### Resume / cancel / failure semantics

- cancellationはcandidate間だけで受け付ける。active REW transactionは必ずrestore pathまで完了させる。
- resumeは同一immutable BatchSpecを再利用し、completed candidateをskipする。
- failureはattemptとして保存してそのbatch invocationを停止する。安全確認なしに次candidateへ連続writeしない。
- retryは明示resumeで新attemptを追加する。
- repository saveに失敗した場合は成功扱いにせずcallerへ伝播する。
- completed attemptのFRはO30へ渡す `predicted` evidence sourceとして参照できるが、実測へ昇格しない。

このsliceはUI・Pareto表示・自動推薦を追加しない。owned-Windows writable acceptanceはbatch persistence/orchestrationとnative UI接続をまとめた後に1回へ集約する。
