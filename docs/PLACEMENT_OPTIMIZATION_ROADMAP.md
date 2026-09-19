# 配置探索・シミュレーション最適化ロードマップ

> 改訂: 2026-09-19 / O90 robust optimization＋O100 system expansion正式化
> 状態: 配置探索アルゴリズムの長期仕様。実装順・release条件は[CAD-firstロードマップ](IMPLEMENTATION_ROADMAP.md)を正本とし、任意形状solverの技術判断は[ACOUSTIC_SOLVER_RESEARCH_2026-09-18](ACOUSTIC_SOLVER_RESEARCH_2026-09-18.md)に従う。
> O10〜O50はnative CADへ実装・接続済み。O60 full validationのsoftware authorityは実装済みだが、owned-room model gateは未通過。実室O60はmeasurement前にValidation Campaignでcalibration/holdout、target response、帯域、閾値、sensitivity/repeatability/separation/applicability条件をimmutable事前登録する。O70/O80はcampaign-backed real-data gateを満たすまで自動推薦・拡張探索として有効化しない。**O90はO90A〜O90Cまで実装済み**。bounded / distribution / empirical / discrete uncertainty、probability-gated statistics、cancel/cache/resume/stale protectionに加え、auditable multi-fidelity screening、R140 exact execution/cache、common-fidelity robust-Pareto finalizationまでmainへ反映済み。O90D UXとO90E owned-room validationがplanned。trackingは[Issue #140](https://github.com/bolph71656-ai/Home-Theater-Digital-Twin/issues/140)、詳細authorityは[O90 Robust Optimization](O90_ROBUST_OPTIMIZATION.md)。**O100はO100A〜O100Fまで実装済み**で、equipment/source capability、system objectives、multi-fidelity authority、proposal-aware robustness / nominal-vs-robust Paretoまでmainへ反映済み。O100G UX/as-built/measurement loopがplanned。trackingは[Issue #142](https://github.com/bolph71656-ai/Home-Theater-Digital-Twin/issues/142)、詳細は[O100 System Expansion](O100_SYSTEM_EXPANSION_OPTIMIZATION.md)。
> 以下の既存座標/G00/G10契約を新Sceneへ接続する際は[編集契約](CAD_EDITOR_SPEC.md)のadapterを用いる。

## 1. 目的

HTDTの配置探索は、部屋・スピーカー・MLPの可動範囲から候補を生成し、検証済みの音響モデルで予測し、複数の評価軸で有望候補を絞り、REW実測で確認するための機能とする。

完成形は「シミュレーションだけで最適位置を断定する」ものではない。次の閉ループを短い測定回数で回すことを目的とする。

1. 探索範囲と物理制約を定義する。
2. 候補配置を再現可能に生成する。
3. 適用条件を満たすモデルで予測する。
4. 多目的評価とPareto集合で候補を絞る。
5. 人が実際に配置を変更し、REWで測定する。
6. 予測と実測の差を保存し、モデルの妥当性を更新する。
7. 条件を満たした場合だけ、次に測る価値が高い候補を提案する。

## 2. 変更しない原則

- 実測、実測由来、予測、仮説を同一データとして扱わない。
- 単一の「音質点数」で物理的な最適配置を断定しない。
- 低域平坦性、深い谷、左右差、席間差、移動量などを別々の目的として保持する。
- Pareto非劣解を基本表示とし、重み付き選好を使う場合は重みと算法版を保存する。
- 予測モデルは適用帯域、幾何仮定、版、入力、処理条件を保存する。
- モデルの調整に使った配置と、妥当性確認に使う配置を分離する。
- 実測で順位や傾向が再現しないモデルから自動推薦を出さない。
- スピーカー移動、AVR設定変更、REW発音をHTDTが自律実行しない。
## 3. 対象と非対象

最初の探索対象は、サブウーファーなしの主シナリオに合わせてFL/FRとMLPを優先する。矩形室での低域予測を最初のモデル対象とし、正確な適用帯域はモデル検証時に固定して保存する。

探索変数は段階的に追加する。

| 変数 | 初期 | 後続 |
|---|---|---|
| FL/FR前後位置 | 対象 | 対象 |
| FL/FR左右位置 | 対象 | 対象 |
| MLP前後/左右位置 | 対象 | 対象 |
| 左右対称制約 | 対象 | 任意解除 |
| acoustic aim yaw (`aim_yaw_deg`) | 記録・synthetic capability | 指向性モデルが成立した場合に探索 |
| physical cabinet toe-in (body yaw) | **software実装済み** | `body_yaw_deg`でorientation+aimを同一delta回転し、回転後footprintでhard constraint再評価。production acoustic recommendationはdirectional modelのowned-room O60成立後 |
| 高さ | 固定から開始 | モデルが高さを扱える場合に探索 |
| C/Surround | 固定から開始 | 個別の要件とモデル検証後 |
| サブウーファー | v1.0必須外 | 将来の独立トラック |

家具、開口、材料、スピーカー指向性を無視するモデルで中高域まで最適化したと主張しない。Issue #101では20–300 Hz wave acoustics＋中高域geometrical acousticsのhybridを基本方針とするが、production backendはR100 benchmarkで選ぶ。FDTDはfirst PoC、FEMは独立reference/alternative、BEM/FMMはsecondary candidateとし、方式名だけで採用を固定しない。

### 3.1 実室Geometry: 非矩形を正本にする

実室形状は矩形を前提にしない。初期の一般形状は**任意頂点数の単純2Dポリゴンを一定天井高で押し出したpolygon prism**とする。8頂点の部屋を標準受入fixtureに含め、凸形状だけでなく凹形状も扱う。段差/傾斜天井、許容誤差付きで面分割した曲面壁はR120Bのgeneral-3D入力/保存/compilerと対応数値gateへ割り当てる。prism先行のR120Aだけで任意3D形状を完了としない。R170Bで実3D air volumeとentity envelopeの包含/衝突判定を接続し、XY footprintは成立していても傾斜天井を突き抜ける候補等を拒否する。このadapter未対応の形状はsearchを無効にし、矩形prismへの置換で通さない。

- 座標系は既存の`X=right / Y=rear / Z=up`を維持する。
- footprint vertexは順序付きで保存し、自己交差を拒否する。
- 外接`reference_box`は座標参照や矩形モデルへの近似入力に使ってよいが、実室境界そのものとは扱わない。
- speaker/MLPはfootprint内かつ0〜heightの範囲に存在することを検証する。
- 家具、通路、設置不能領域はroom boundaryへ無理に混ぜず、別のconstraint geometryとして保存する。壁のopeningはCAD上のwall/opening authorityを保ち、R120 acoustic compile時にexplicit PortalまたはBoundaryTerminationへ解釈する。
- Context revisionには正確なpolygonと、矩形近似を使った場合の近似規則・誤差・用途を別々に保存する。

REW Room Simulatorは公式にrectangular room用なので、8頂点実室のexact predictorとしては使わない。REWモデルは`rectangular_approximation`として明示し、実室polygonと混同しない。

## 4. モデル再利用方針

**矩形基準モデル**としてREW Room Simulatorを維持する。S01は入力設定、結果取得、単位、座標変換、再現性、バッチ利用可能性のbaseline/referenceであり、非矩形実室では`rectangular_approximation`としてしか扱わない。

S03のpyroomacoustics評価は**geometrical-acoustics reference/PoC**へ位置付けを修正する。一般polyhedral/non-convex room、image-source/ray tracing、absorption/scattering、RIR生成の再利用価値は高いが、低域の干渉・回折・room modeを解くfull-wave solverではない。したがって、pyroomacousticsだけを「非矩形exact predictor」としてO20へ昇格させない。

Issue #101のarbitrary-room modelはR-seriesで追加する。

- R100A: solver-neutral benchmark authority。region/portal/source/receiver/boundary/environmentとreference observableを先に固定する。
- R100B: FDTD/FEM/geometric referenceを同じfixtureで比較してproduction stackを決める。
- R130A/B/C: 20–300 Hz low-band wave predictorをrigid core → independently verified lossy boundary → causal frequency-dependent boundaryの順で構築する。
- R150: direct/early specular＋general-polyhedral ray tracing。pyroomacoustics/Embree等をreference/candidateとして比較する。
- R160: CoherentTransfer / DeterministicPathSet / LateEnergyDecayを区別し、double-countingを避けたoverlap/crossoverを実装する。
- R170A→R180A: 検証済み低域waveをtyped result adapterでCPU batch/O30/O40/O50/N60/O60へ接続し、低域owned-room campaignを先行する。R140/R150/R160完成を待たない。
- R170B→R180B: R140/R160後にmulti-fidelity・hybrid・O70適応/O80拡張を接続し、追加能力ごとにvalidationする。receiver batching / source grouping / reciprocity等の再利用は成立条件付き。

REW baseline、geometric reference、wave predictor、hybrid predictorは別model ID/versionとして保存し、結果を上書き・暗黙昇格しない。

REW側を安全かつ再現可能に自動駆動できない場合は、無理に画面自動操作へ依存しない。公開API、手動結果取込、検証済み外部ライブラリ、限定的な自前幾何モデルの順で具体的な不足を評価する。

すべての予測器は内部的に同じ最小契約へ合わせる。汎用プラグイン基盤は作らず、必要になったモデルだけをアダプターとして追加する。

### 4.1 Arbitrary-room prediction authority

R-series predictionでは、少なくとも次をPredictionRunへimmutable bindingする。

- exact SceneRevision/content hash
- semantic acoustic geometry hash
- compiled representation hash + acoustic compiler version/tolerance
- material/boundary configuration hash + material capability state
- source excitation/directivity dataset hash + input-channel→physical-source routing/gain/delay/filter model hash
- receiver model/calibration hash + receiver set
- environment/air-state hash
- solver/model ID + version
- CPU/GPU backend/device
- grid/mesh/BVH resolution
- valid frequency band
- approximation/simplification rule
- stochastic seed
- cache identity

material authorityは用途別に分ける。

- geometric acoustics: banded absorption / scattering / optional transmission
- wave acoustics: complex impedance/admittanceまたは明示したfrequency-dependent boundary model

scalar absorption coefficientから一意なphase-bearing impedanceを無言で生成しない。ラグ/カーテン等のthin surfaceがgrid resolutionで消える場合は、equivalent boundary modelをprovenance付きで用いるかunsupported/unresolvedとして止める。

### 4.2 Multi-fidelity execution

任意形状solverを全candidateへ最高精度で適用しない。

1. hard constraintでfeasible候補を生成。
2. 対象形状・帯域で検証したcheap/reference predictorとcacheで粗screening。矩形専用modelで非矩形候補を除外しない。
3. reduced setだけwave/geometric medium fidelity。
4. Pareto/uncertainty候補をrefinementし、最終比較の候補は共通の検証済みfidelity・帯域・objective条件で再評価。
5. MeasurementPlanへ落とし、O60/O70で実測価値を更新。

fixed geometry/materialではwave-grid、ray BVH、FEM matrix/preconditioner等のsetupを再利用する。さらにreceiver batching、source-equivalence grouping、Green's-function/transfer reuse、reciprocity等が**選択したsource/receiver modelと境界条件で成立する場合に限り**利用し、candidateごとの重複solveを避ける。
### 4.3 Screening / reuseの成立条件

粗screeningによる音響候補の除外は、hard constraintによる設置不能判定とは別である。coarse/fineのdiscrepancy・順位逆転・不確実性を検証し、除外候補のaudit sampleと順位逆転時の再探索を保存する。誤差境界が未検証ならheuristic shortlistと表示し、全探索のPareto集合を保ったとは主張しない。unsupported/missing objectiveを0・無限大・低scoreへ変換して候補を消さない。portal接続や必要な薄い物体が粗gridで消えた場合も同様に止める。

物理geometry/material/environment、離散化・周波数/formulationが同一の場合だけoperator/setupを再利用する。speaker/seat等をacoustic obstacleとして含む場合は、その移動・body yaw変更でもgeometry/grid/BVH/matrixの該当cacheを失効する。source/receiver markerの移動と物理物体の移動を区別する。新receiverの時系列を保存していなければ追加評価/solveが必要であり、batch対応から任意位置の結果保管を推定しない。

cache artifactとPredictionRunのexact SceneRevision bindingを分離する。同一物理入力の再利用でも、新要求への参照を明示し、過去runを書き換えない。reuse対fresh solve、cabinet回転、material変更、receiver移動、view-only hideをfixture化する。

最終simulated Paretoは同一の検証済みfidelity・帯域・source/reference・objective specで比較する。再評価budgetが不足した場合はpreliminaryのまま。wave/GAのvalid bandにgapがある場合はその帯域を必要とするobjectiveを停止し、O60 production gateを迂回しない。

## 5. 正式マイルストーン

| ID | 段階 | 主な成果 | 完了条件 |
|---|---|---|---|
| G00 | Room Geometry v2 | polygon-prism実室、reference box、座標契約 | 8頂点・凹polygonを保存/再読込し、自己交差や室外点を拒否できる |
| G10 | Placement Constraint Engine | entity別allowed region、禁止領域、壁離隔、相互離隔、連動拘束 | hard constraint違反候補を生成せず、拒否理由を機械的に説明できる |
| O00 | 探索前提 | 同条件再測定、配置A/B、S01モデル契約、G00 | 測定ばらつきと予測モデルの適用条件を表示できる |
| O10 | Search Space | O00、G10 | **ソフトウェア実装済み**。同一feasible候補集合を再生成可能。実測運用はO00の測定前提が満たされるまで推薦へ使わない |
| O20 | Batch Prediction | O10 + 使用モデル契約。非矩形exact predictionはIssue #101 R-seriesの該当model gate通過後 | **ソフトウェア実装済み**。中断・再開可能で、予測を実測として保存せず、同一入力で再現する。REW Room Simulatorのowned-Windows position transactionも受入済み。非矩形exact modelは別gate |
| O30 | Objective Vector | 帯域別偏差、ピーク/谷、左右差、席間差、移動量などの独立指標 | **ソフトウェア実装済み**。独立指標・算法版・評価条件・evidence provenanceをimmutable保存 |
| O40 | Pareto Search | 非劣解抽出、粗探索→局所探索、候補多様性 | **ソフトウェア実装済み**。objective vectorを保持したPareto集合、semantic snapshot de-dup、native比較UIを実装 |
| O50 | Measurement Loop | 測定候補キュー、Context複製、REW実測との対応 | **ソフトウェア実装・owned-Windows受入済み**。candidate→exact applied SceneRevision→Measurement Plan→N60 measured evidenceをappend-only追跡 |
| O60 | Model Validation | 保留配置、感度分析、予測対実測の比較 | **software authority実装済み / real-data gate未通過**。実室ではmeasurement前のValidation Campaign preregistrationを必須とし、calibration/holdout、共通target response、objective条件、sensitivity、repeatability、candidate separation、applicabilityを固定する。post-hoc splitやcampaign以前のmeasurementでは推薦gateを開かない |
| O70 | Adaptive Planner | surrogate model、uncertainty、次測定候補の選択 | **software実装済み / synthetic acceptance PASS**。O60 calibration残差のobjective別GP補正と不確実性から次測定候補を決定し、SearchSpec/candidate-set/ValidationRecordへimmutable保存する。`development_synthetic`は完全PASS synthetic fixtureを許可するがproduction gateを開かない。`production_owned_room`はcurrent campaign-backed eligible ValidationRecordを必須とする |
| O80 | Extended Search / Adaptive Extended | 多席、多チャンネル、acoustic aim、physical toe-in、高さ等 | **software実装済み**。`aim_yaw_deg`と`body_yaw_deg`を明示capability付きで扱い、physical toe-inはorientation-aware hard constraintを再評価する。Adaptive Extended Planはbase XYZ + extended parameterをaxis span正規化してobjective別residual GP/uncertainty acquisitionへ入力し、exact O80 authorityへimmutable bindingする。synthetic laneはproduction gateを開かず、owned-room有効化はdirectional modelの独立O60 gate後だけ |
| O90 | Robust / Tolerance-aware Optimization | 設置誤差・入力不確かさに対する性能分布、感度、feasible fraction、robust Pareto | **O90A–O90C実装済み / O90D–O90E未実装**。immutable `RobustnessSpec`で±位置/aim/seat等のuncertaintyを定義し、nominal・local sensitivity・sampled envelope・明示distribution時のみpercentileを別objectiveとして保持する。有限samplingを真のworst-caseと呼ばない。perturbationごとにG10/O80 hard constraintを再評価し、infeasible sampleもevidenceとして保存する。software laneはsyntheticで検証可。production robustness recommendationは対象model/observable/perturbation domainのeligible O60/R180 evidenceを必須とする |
| O100 | System Expansion / Virtual Channel Topology | 現在存在しないSL/SR等を仮想追加し、topology・equipment/source・配置範囲・aimを含めて比較 | **O100A–O100F実装済み / O100G未実装**。baseline SceneRevisionを変更せずSystemVariant/ProposedEntitySpecを作り、role別allowed regionからG10/O10/O80でcandidate生成。source/directivity/SPL等はcapability/provenance付きEquipmentDefinitionへbindingし、coverage/SPL/FR等は成立するobjectiveだけ評価する。3.0.2 vs 5.0.2等をPareto比較し、selected proposal→As-built→Measuredをappend-only lineageで保持。O90 robustnessとO60/R180 gateを再利用する |

O10以降の拡張は安定個人版の必須条件にしない。まずCAD基盤を成立させ、その後はCAD-firstロードマップのN50/N60/N70/N80の依存に従って進める。O90はO10〜O80を置換せずnominal候補へrobustness evidenceを追加し、O100はbaseline topologyを壊さずProposed system variantを探索へ追加する後続milestoneとする。

## 6. 探索空間と制約

探索仕様は単なる最小/最大座標ではなく、次を保存する。

- 対象となるContext revisionとroom polygon revision。
- entity別の`allowed_region`。FL/FR/MLPで別領域を指定できる。
- 家具、扉、通路、ラック等の`exclusion_region`。複数polygonを許可する。
- 各壁または壁タグごとの最小/最大離隔。例: 前壁から0.20〜1.20 m、側壁から0.30 m以上。
- スピーカー筐体を点ではなくfootprint/radiusで扱う安全余白。
- speaker-speaker、speaker-MLP等の最小/最大相互距離。
- 変更可能な座標軸、固定高さ、高さ範囲、刻み幅またはサンプリング規則。
- FL/FRの左右対称、鏡映、等距離、同時前後移動などの連動拘束。
- MLPやスピーカーの現在位置からのhardな移動量上限。
- 固定するAVR条件、測定点、モデル設定。
- 乱数を使う場合のseedと探索算法版。

hard constraintとsoft objectiveを混同しない。設置不能位置はスコアを悪化させるのではなく候補集合から除外する。一方、移動量、ケーブル長、見た目上の好みなど「許容はできるが避けたい」条件は独立Objectiveとして保持できる。

G00でpolygon演算にShapely 2.1.2をpinした。CPython 3.12 / Windows x86-64 wheelを所有PCで導入確認し、`is_valid` / `covers`を8頂点fixtureで検証する。G10で`buffer`による壁離隔、`difference`による禁止領域除外、距離演算を追加する際も、各意味論をfixtureで独立検証してからConstraintSetへ使う。

### 6.1 hard constraint実装順

| ID | 制約 | 初期実装 | 判定 |
|---|---|---|---|
| C01 | Allowed region | speaker/MLPごとのpolygon/multipolygon | entity footprintが領域内。G10実装済み |
| C02 | Exclusion region | 家具、ラック、扉可動域、通路 | entity footprintとの交差で除外。G10実装済み |
| C03 | Wall clearance | wall edgeごとのmin/max距離 | 指定edgeまでの最短clearance。G10実装済み |
| C04 | Cabinet clearance | speaker footprint/radius + 安全余白 | point中心だけで判定しない。G10実装済み |
| C05 | Pair distance | FL-FR、speaker-MLP等のmin/max | center/envelope clearance。G10実装済み |
| C06 | Linked placement | 左右鏡映、等Y、同時前後移動 | G10関係判定 + O10 master→slave生成を実装済み |
| C07 | Axis/height | x/y/z固定または範囲 | G10範囲判定 + O10 deterministic grid量子化を実装済み |
| C08 | Movement budget | 現在位置からの最大移動 | hard上限として候補除外。G10実装済み |

各候補は`accepted/rejected`だけでなく、拒否したconstraint IDと実測値/閾値を保存する。これにより「なぜその位置を探索しなかったか」を後から再現できる。

実行前に候補数を見積もり、過大な全探索は明示的に縮約する。小さい空間は全列挙、大きい空間は粗い格子、空間充填サンプリング、局所細分化などを比較して選ぶ。
## 7. 多目的評価

初期の評価候補は次とする。ただし有効な帯域、平滑化、参照レベル、複数席の集約方法を評価設定として保存し、暗黙の既定値にしない。

| 目的 | 例 | 注意 |
|---|---|---|
| 目標応答との差 | 指定帯域のRMS/形状差 | レベル差を含むか分離するかを保存 |
| 過大ピーク | 最大超過量、帯域内面積 | 狭い1点だけで全体を支配させない |
| 深い谷 | 最大不足量、連続帯域幅 | 平滑化と測定再現性を併記 |
| 左右差 | FL/FRの形状差、帯域別差 | 左右の測定条件一致を前提にする |
| 席間差 | 複数測定点の分散や範囲 | MLP単独探索では使用しない |
| 実装コスト | 現在位置からの移動量 | 音響指標と別目的として扱う |

標準UIは目的ベクトルとPareto集合を表示する。ユーザーが重み付けを指定した場合は並べ替えを提供してよいが、その結果を「真の最適」と表示しない。

### 7.1 O90 robust objective

O90ではnominal objectiveを残したまま、明示したuncertainty/toleranceに対する派生指標を独立objectiveとして追加できる。

例:

- nominal objective;
- nominalからのdegradation;
- local sensitivity;
- sampled range / sampled adverse value;
- explicit distributionがある場合だけadverse percentile / exceedance probability;
- feasible-sample fraction / constraint violation rate.

`±20 mm`のようなbounded intervalだけから確率分布を捏造しない。有限sampleの最大/最小は`sampled_worst`とし、数学的・探索的に保証していない`worst_case`と呼ばない。

robust Paretoでも単一scoreへ縮約しない。例えば「nominal FRはAが優位、±20 mm耐性はBが優位」を同時に保持する。詳細は[O90 Robust Optimization](O90_ROBUST_OPTIMIZATION.md)。

### 7.2 O100 system topology / expansion objective

O100では「存在しているspeakerをどこへ動かすか」だけでなく、**どのroleを追加するか・どの機種/source modelを使うか・どのinstallation zoneへ置くか**を明示的なdesign variableとして扱える。

例:

- baseline 3.0.2を保持;
- `+SL/SR` の5.0.2 SystemVariantを派生;
- SL/SRそれぞれにallowed/exclusion、高さ、pair symmetry、aim範囲を指定;
- EquipmentDefinitionをbinding;
- feasible placementをO10/O80で生成;
- layout/coverage/SPL/FR等、現在のcapabilityで成立するobjectiveだけをO30へ追加;
- O40 Paretoでbaselineとproposalを比較;
- surviving proposalをO90でtolerance評価。

baselineにSL/SRが存在しないことをresponse=0として数値比較しない。channel topologyの差はSystemVariantとして保持する。multi-channel acoustic comparisonはper-channel transferまたはrouting/gain/delay/filterを固定した明示excitation scenarioだけで行い、無関係channelを未定義coherent sumにしない。

詳細は[O100 System Expansion](O100_SYSTEM_EXPANSION_OPTIMIZATION.md)。

## 8. 実測閉ループ

O50以降では、予測候補から実測対象を選んだ時点でMeasurement Planを保存する。実際にスピーカーやMLPを動かす操作は人が行う。

実測時は候補から新しいContext revisionを作り、元候補の予測入力を変更しない。REW測定取込後に、予測曲線と実測曲線の差、目的ベクトルの差、同条件再測定のばらつきを同じ検証記録へ保存する。

予測誤差を後からモデルへ反映する場合も、過去のPredictionRunは不変にする。新しい補正モデル、surrogate model、探索結果は新しい版として追加する。

### 8.1 Owned-room Validation Campaign

実室O60では、実測値を見てからcalibration/holdoutや評価条件を選ばない。対象candidateの測定完了前にValidation Campaignを保存する。

CampaignはSearchSpec/candidate-set SHA、model版、candidate split、target response、帯域、objective、trend threshold、sensitivity pair/threshold、repeatability候補/回数、candidate separation倍率、required applicability codeを固定する。campaign作成以前にcapturedされたmeasurement、またはcampaign保存時点ですでにmeasured planとなっていたcandidateは、そのcampaignのvalidation evidenceへ昇格させない。

prediction/measured objectiveはcampaignに保存した同一target response・evaluation specからO30 vectorを導出する。readinessはmissing/ambiguous evidenceを明示し、条件不足を自動補完しない。O70が読むeligible ValidationRecordはpersisted campaignへ再照合できるものに限定する。

### 8.1a R-series result / calibration adapter（未実装）

現行 `CadModelValidationService` はRoomSim attemptからFRを取得し、campaignのobjectiveはFRの4種類に限定される。R170Aでは新wave resultと既存RoomSimを区別するtyped result providerを追加し、candidate/SceneRevision、model/config/result hash、band、unit/level/time referenceをO20/O30/O50/O60/O70へ伝える。既存recordを保持し、RoomSim用payloadの偽装やmodel IDだけの付替えで接続しない。

最初は検証済みsingle-sourceの低域FR objectiveから接続できる。複数音源は入力routing・gain/delay/EQ/source responseによる複素励振とroom transferを分離し、coherent sumとdB平均を混同しない。未知のAVR経路やsource responseをmaterial fittingへ押し付けない。receiver移動時のREW timing-reference経路と、適用済みmic correctionも比較条件へ含める。

Calibrationはfit対象parameter/bounds・固定値・目的・training evidence・正規化をcampaignで事前登録する。fit後はmodel/configuration hashをappend-onlyで確定してからholdoutを評価する。この遷移をcampaign schemaで表現し、fit前に未生成hashを要求したり、holdout評価後のhash変更を許したりしない。低いresidualだけで材料が一意に同定できたと断定せず、parameter sensitivity・correlation・非一意性/識別不能groupを報告する。ValidationRecordは「予測精度は対象observableで許容、物理parameterは一意同定不能」の状態を表現できるようにし、best-fit値を真の材料定数へ昇格しない。fitは任意で、固定した明示modelの評価経路も保持する。

holdoutを見てmodel/parameter/正規化を調整した後は、そのデータを次版の独立holdoutとして再利用しない。新production claimには新しい独立holdoutを用意する。既存の実測前campaign保存とsynthetic分離は維持する。

eligible recordの適用範囲をmodel/config、band、observable、source/receiver/room/material条件で限定する。Measurement/AcquisitionContext側のcapabilityもmagnitude-valid、relative-phase-valid、common-time-reference/arrival-time-valid等に分け、弱い測定authorityから強いphase/IR claimへ昇格しない。FR合格はphase/IR/decay/directional validationではない。R180Aは低域FR等の対象だけ、R180Bは追加observableのmetric/measurement adapterと独立検証を受け持つ。hash不一致、holdout使い回し、FR合格からphaseを誤解禁するnegative fixtureを含める。

## 8.2 Synthetic software-completion lane

実測を待たずソフトウェアを完成・検証するため、O70/O80には明示的なdevelopment laneを設ける。synthetic fixtureはO60のresidual/trend/sensitivity/repeatability/separation/applicabilityをすべて通過できるが、evidence scopeは常に `synthetic_fixture` のままとし、production recommendation gateは開かない。

O70の `development_synthetic` は、O60の唯一のstop reasonがowned-room evidence不足であるsynthetic ValidationRecordだけを入力にできる。production側の `production_owned_room` は従来どおりcampaign-backed `eligible` recordを要求する。この2経路を同じフラグや暗黙fallbackで混ぜない。

synthetic laneの目的はUI、保存、stale guard、adaptive algorithm、extended search、package/CIを最後まで完成させることであり、実室model妥当性の主張ではない。

2026-09-18、PR #92/#93/#94でこのsoftware-completion laneを完了した。final authorityはPR #94 merge `6faf554bcf3670f64ff13c530fa4fc79ab1881b8`、CI #548 / run `35313405578` PASS、Windows Release Artifact #93 / run `35313405629` PASS。以後の未完了事項はIssue #83の実室owned-room evidence gateであり、software実装不足ではない。

O70 base Adaptive Planは互換性を保ってO10 XYZ feature vectorのまま維持する。O80Aでは別のimmutable Adaptive Extended Planを追加し、base O10 axisとO80 extended axisをaxis spanで0–1相当に正規化したfeature vectorへ統合する。metreとdegreeをraw値のまま同一kernelへ入れない。exact Extended SearchSpec/capability/candidate-set SHAへbindingし、current observationに実測があるextended candidateはproposalから除外する。synthetic/production gateは従来どおり分離する。

O80Pでは`body_yaw_deg`をphysical cabinet toe-inとして追加する。body yawは`orientation`を回転し、同じyaw deltaを`aim_xyz`へ適用してbody/aim関係を維持する。新規O10 SearchSpecはsource orientationの実筐体XY footprintをconstraint snapshotへ固定し、O80P候補では回転後footprintでroom/allowed/exclusion/wall/envelope pair constraintを再評価する。

## 9. 適応探索の導入条件

Bayesian Optimization等の適応探索は最初から必須にしない。O60で予測と実測の関係が独立配置でも有用と確認できた場合だけ評価する。

導入時は次を必須とする。

- 学習に使ったMeasurement IDとContext revisionを保存する。
- 保留検証データを学習へ混ぜない。
- 不確実性または探索不足を表示する。
- 次候補を選んだ取得関数、seed、算法版を保存する。
- 単純な格子/ランダム/粗探索基準と比較し、測定回数削減の効果を検証する。
- データが少ない場合や外挿領域では強い推薦を出さない。
詳細なsynthetic completion契約とdemo手順は [O70/O80 Synthetic Software Completion](O70_O80_SYNTHETIC_COMPLETION.md) を参照する。

### 9.1 physics-aware accelerationの順序

Issue #101 R-seriesのfull-order solverがcorrectness gateを通過した後、探索高速化はgeneric surrogateを先に増やすのではなく、物理operatorの再利用を優先する。

1. fixed geometry/materialでのreceiver batching、成立条件付きreciprocity、grid/BVH/matrix/preconditioner/factorization reuse、source-equivalence grouping;
2. modal/Green-function/reduced-basis/ROMを明示parameter domain内で評価;
3. forward/boundary modelが検証済みの場合のみadjoint gradientによる高次元calibration/optimizationを研究;
4. その後にresidual GP等のstatistical surrogateを追加する。

ROM/adjointはO-series production gateの依存にしない。採用する場合はtraining/full-order authority hash、適用domain、独立error validationまたはerror estimator、geometry/material/source変更時のinvalidation ruleを保存する。研究論文のspeedup値をHTDTの性能保証へ転記しない。

## 10. 保存契約

探索機能は少なくとも次の概念を不変版として保存できるようにする。実装上のテーブル名は別途データ設計で決める。

- RoomGeometry: polygon-prism頂点列、高さ、座標系、reference box、geometry版。
- ConstraintSet: entity別allowed/exclusion polygon、壁離隔、筐体余白、相互離隔、連動拘束。
- SearchSpec: O10 schema v5でContext/ConstraintSet SHA、可動軸min/max/step、linked derivation、算法版、candidate limitを不変保存。乱数算法導入時はseedを追加する。
- PlacementCandidate: 候補座標と元Context revision。
- PredictionRun: 使用モデル、モデル版、入力、出力、警告。
- ObjectiveVector: 評価値、目的定義、算法版。
- CandidateSet: Pareto集合やユーザー選択集合、その生成条件。
- MeasurementPlan: 実測する候補と手順。
- ValidationRecord: 予測対実測、保留配置、感度分析、判定根拠。
- AdaptiveModel: 学習Measurement、特徴量、モデル版、不確実性情報。
- RobustnessSpec: base candidate-set/model/objective、uncertainty axis/model/correlation、sampling/fidelity/budget、算法版。
- PerturbationSample: candidate、exact delta、resulting Scene/config identity、feasibility、PredictionRun、ObjectiveVector、failure/reuse provenance。
- RobustnessEvaluation: nominal、sensitivity、sampled envelope、explicit distribution時のpercentile、feasible fraction、robust objective/provenance。
- SystemVariant: baseline SceneRevisionから派生したtopology/equipment/placement構成。current/proposed/as-built/measured stateとexact diffを保持。
- TopologySearchSpec: add/remove/replace可能なrole/group、installation zone、equipment alternative等を明示。
- ProposedEntitySpec: 未導入speaker等のrole、physical envelope、source/equipment、placement/aim constraint、provenance。
- ChannelRoleBinding / EquipmentDefinition reference: role/layout-profileとsource capabilityを名前だけでなくversion/provenance付きでbinding。

予測結果を再計算して過去の表示を上書きしない。再計算は新しいPredictionRunとして保存し、旧結果から新結果への参照を持てるようにする。

## 11. UIの最低要件

| 画面/領域 | 必須操作 | 必ず見える情報 |
|---|---|---|
| 実室/制約編集 | room polygon、allowed/exclusion region、壁離隔、筐体余白を編集 | 8頂点等の実境界、reference box、feasible region、各constraint ID |
| 探索条件 | 可動範囲、拘束、帯域、モデルを設定 | 候補数見積り、固定条件、モデル適用制限 |
| 候補一覧 | Pareto候補を絞り込み、比較 | 各目的値、移動量、予測/実測区分 |
| 候補詳細 | 配置、予測曲線、既存実測を重ねる | モデル版、入力Context、警告 |
| 測定キュー | 実測対象を選ぶ、完了測定を対応付ける | 候補ID、変更点、測定状態 |
| モデル検証 | 予測対実測、残差、保留配置を見る | 学習/検証区分、適用帯域、感度 |
| ばらつき耐性 / O90 | 設置誤差axis・範囲を設定し、nominal対robust候補を比較 | nominal値、感度、sampled/percentile semantics、feasible fraction、model/validation scope |

3Dは候補理解を助ける表示であり、探索計算の必須UIにしない。数値表、平面図、グラフだけでも同じ候補を再現できることを優先する。

## 12. 検証fixtureと受入観点

- 既知の禁止領域を含む小さい探索空間で、候補数と座標を手計算結果に一致させる。
- 同じSearchSpecとseedから同じ候補集合を再生成する。
- 人工的な目的ベクトルで支配関係が既知の集合を作り、Pareto抽出を検証する。
- 予測曲線と実測曲線を意図的にずらしたfixtureで、残差の符号と帯域集約を検証する。
- 学習配置と保留配置が重ならないことをテストする。
- 適応探索は単純基準より有利かを独立データで比較し、有利でない場合も結果を記録する。
- 中断、モデル失敗、一部候補失敗があっても成功候補と失敗理由を混同しない。
- 8頂点の凸polygonと凹polygonをfixture化し、頂点順、面積、inside/outside、境界上の扱いを手計算結果と一致させる。
- allowed regionから家具/通路exclusionを差し引き、指定wall clearanceと筐体footprintを満たす候補だけが残ることを検証する。
- 同一SearchSpec/seedからpolygon内の同一候補集合を再生成し、reference box内でも実室polygon外の点を生成しない。
- REW矩形近似を使ったPredictionRunには`rectangular_approximation`を残し、polygon実室のexact predictionへ自動昇格しない。
- R130 wave fixtureではrigid rectangular analytical mode、grid/mesh convergence、独立FEM/reference比較を行い、backendごとのvalid upper frequencyを測定で固定する。
- R150ではdirect delay、first-reflection point/path length、seed repeatabilityを既知解と比較する。さらにray数・receiver estimator/radius・time bin・打切りをrefineし、独立seed間のばらつきと収束を検証する。sampling uncertainty内の候補差で確定的なPareto優位を主張せず、未収束/到達数不足はゼロresponseに変換しない。
- frequency-domain resultからIRを得る場合は、周波数grid/範囲、位相、正規化、時間span、再構成法を固定して遅延既知transferと打切りfixtureを検証する。FR合格をIR合格や広帯域decayへ転用しない。
- R160ではoverlap bandのlevel/energy continuity、direct-arrival timing、unsupported high-band phaseを生成しないことを検証する。
- CPU/GPU backend差は同一authority inputで数値許容差を定義し、device/backend/versionを保存する。
- O90では同一RobustnessSpec/seedから同一sample identityを再生成し、bounded intervalだけではpercentileを生成しない。
- O90ではperturbationでhard constraint違反になったsampleを捨てず、feasible fraction/violation evidenceへ含める。
- O90の有限sample最大/最小はsampled worstとして表示し、未証明のworst-caseへ昇格しない。
- O90 production robust recommendationは対象model/observable/perturbation domainのeligible O60/R180 evidenceが無ければfail-closedにする。

## 13. 自動推薦を止める条件

次の場合、HTDTは順位付きの自動候補推薦を停止し、予測値と実測候補の比較だけを提供する。

- 保留配置で予測の傾向または順位が再現しない。
- モデル適用帯域外の成分が評価を支配する。
- 探索結果が小さな入力誤差で大きく入れ替わる。
- 同条件再測定のばらつきが候補間差と同程度以上である。
- 必要な部屋形状、境界条件、チャンネル対応が不明である。
- 非矩形実室に対し、矩形近似モデルしか検証できていない。
- feasible regionが空、または候補数が少なすぎて探索結果が制約境界だけで決まる。
- O90で小さな現実的perturbationにより候補順位が不安定だが、そのrobustness評価が未完了である。
- O90のproduction robustness claimに必要なO60/R180 sensitivity/applicability evidenceが不足している。

この停止条件は失敗ではなく、モデルの信頼範囲を越えて断定しないための通常動作とする。
