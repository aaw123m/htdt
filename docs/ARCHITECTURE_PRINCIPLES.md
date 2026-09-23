# HTDT Architecture Principles — North Star

Date: 2026-09-20  
Status: canonical architecture principle

## North Star Architecture Principle

HTDTは、実空間から得たgeometry evidence、実機equipment/directivity/playback-chain authority、acoustic treatment、measurement evidenceを、immutableな`SceneRevision`とexplicit provenanceによって**一つのrevisioned Digital Twin**へ統合する。

予測、最適化、Standards評価、headroom評価、SystemVariant比較、施工、As-built化、再測定は、すべてその同じDigital Twin上のexact authorityから派生する。

HTDTは次を混同しない。

- raw evidence;
- semantic authority;
- compiled / solver representation;
- predicted result;
- measured evidence;
- derived result;
- hypothesis.

未知・未対応の情報を都合のよい既定値へ暗黙補完してauthorityへ昇格しない。

## 1. Source-neutral real-room evidence

HTDTのreal-room truthはLiDAR単独では定義しない。

利用可能なsourceには、たとえば以下がある。

- LiDAR / phone scan;
- photogrammetry;
- imported OBJ / GLB mesh;
- CAD / manual geometry;
- tape / laser dimension;
- photo evidence;
- manufacturer drawing;
- user-authored geometry.

どのsourceも、provenance、source identity/version、raw asset hash、known/unknown semanticsを明示して扱う。

異なるsourceが同じ部屋を記述しても、それらを理由なく同一truthへ潰さない。

## 2. Geometry authority layers

Canonical flow:

```text
Real-world geometry evidence
→ Raw visual / imported / manual geometry evidence
→ explicit diagnostics / bounded repair when authorized
→ SemanticAcousticGeometry
→ compiled geometry / region / portal / boundary authority
→ AcousticSceneSnapshot
→ solver-specific execution input
→ prediction result
```

重要な境界:

- raw visual mesh != SemanticAcousticGeometry;
- SemanticAcousticGeometry != solver-ready geometry;
- compiled geometry != solver result;
- visual plausibility != acoustic authority.

RawVisualMeshを、見た目が妥当という理由だけでacoustic room truthやsolver-ready geometryへ昇格しない。

Repairはexplicit・bounded・versionedに行い、original asset bytes/hashを保存する。

## 3. Equipment / source / playback-chain authority

Equipment evidence is bound explicitly:

```text
Equipment evidence
→ EquipmentDefinition
→ DirectivityDataset / source capability
→ R110 compiled source authority
→ optional explicit acoustic excitation authority
```

Electrical playback chain is separate:

```text
amplifier capability
+ speaker electrical load authority
+ routing / excitation scenario
→ PlaybackChainScenario
→ PlaybackChainEvaluation
```

Speaker acoustic capabilityとamplifier electrical capabilityを混同しない。

Sensitivity、nominal impedance、manufacturer text等から、根拠のないphase、source strength、complex impedance、volume velocityを生成しない。

## 4. Treatment authority

Treatment is attached authority, not a rewrite of base construction.

```text
AcousticTreatmentDefinition
+ AcousticTreatmentPlacement
+ exact host SemanticSurface
→ TreatmentBoundaryOverlay
→ treatment-aware AcousticSceneSnapshot
```

Base construction material、attached treatment、furniture-equivalent absorption、transmission authorityを区別する。

Scalar absorption dataから根拠なくcomplex impedanceを作らない。

No-treatmentはsynthetic zero-materialではなく、exact empty treatment placement stateとして表現する。

## 5. Measurement evidence

Measurement evidence remains distinct from prediction.

```text
AcquisitionContext
+ raw measurement asset
+ exact SceneRevision / SystemVariant context
→ Measurement
→ MeasurementQualityReport
→ validation / calibration evidence
```

Raw measurement assetのSHA-256と取得条件を保持する。

Measured、measured-derived、predicted、derived、hypothesisを同じstatusへ潰さない。

過去測定を現在配置へ自動で付け替えない。

## 6. Immutable revision and lifecycle

Physical/editable scene truth is revisioned.

Core lifecycle:

```text
current
→ proposed
→ explicitly applied / installed
→ as-built
→ measured
```

この順序は自動昇格ではない。

- proposalはphysical/as-built/measuredではない;
- comparisonはbaselineをmutationしない;
- installation後もproposal historyを上書きしない;
- re-measureは過去measurementを更新せず新しいevidenceを追加する.

`SceneRevision`、`SystemVariant`、definition/model/evaluationのexact id/version/hashをlineageの正本とする。

## 7. One Digital Twin, exact variant comparison

System comparisonは別々のloosely-related projectを比較するのではなく、同一Digital Twinのexact baselineに対してnamed candidate authorityを束ねる。

```text
exact baseline SceneRevision
+ exact SystemVariant candidates
+ exact evaluation authorities
→ named comparison
→ independent ObjectiveVector
→ direction-aware Pareto
```

HTDTはhidden overall scoreへ自動縮約しない。

Coverage、SPL/headroom、electrical headroom、Standards、robustness、treatment、video geometry等は独立authority/evidenceとして保持する。

Missing / unsupported / UNKNOWNを有利なnumeric sentinelへ変換しない。

## 8. Solver boundary

A solver is an adapter behind exact scene authorities, not the owner of room truth.

Canonical flow:

```text
AcousticSceneSnapshot
→ AcousticPredictionRequest
→ exact solver adapter/configuration authority
→ AcousticSolverDispatchBinding
→ external solver execution
→ exact result artifact authority
→ typed result interpretation / validation
```

Dispatch READYは「exact inputがexact adapter contractに適合する」ことだけを意味する。

それは以下を意味しない。

- solver execution success;
- numerical convergence;
- physical validation;
- production solver adoption;
- owned-room validation.

## 9. Evidence state semantics

Every authority or result should be classifiable, where applicable, as one of:

- measured;
- manufacturer;
- user-defined;
- imported;
- inferred;
- derived;
- predicted;
- hypothesis;
- unknown / unsupported.

Category名だけで十分でない場合は、source kind、method、uncertainty、valid domain、version/hashを追加する。

Inferenceをmeasurementとして表示しない。

## 10. Feature / authority design checklist

新しいfeature、authority、adapter、solver integrationを追加するときは最低限以下を確認する。

1. これはraw evidence / semantic authority / compiled representation / request / resultのどこに属するか。
2. exact `SceneRevision` / `SystemVariant`へどうbindするか。
3. provenance、source identity/version、SHA-256は何か。
4. measured / predicted / derived / hypothesis / unknownのどれか。
5. unknownやunsupportedを暗黙補完していないか。
6. current / proposed / as-built / measured lifecycleを飛び越えていないか。
7. save/reopenでexact authorityを再解決できるか。
8. stale resultをcurrentへ自動昇格しないか。
9. comparisonでdefinition/unit/direction/model/fidelity/evidence identityを保持できるか。
10. missing evidenceをhidden scoreやfavorable sentinelで隠していないか。
11. external solver/libraryの結果をHTDT固有truthとして過剰解釈していないか。
12. domain/software completionとWindows visual acceptance、owned-room evidence、numerical solver validationを混同していないか。

## 11. Canonical authority flow

```text
Real-world evidence
  ├─ geometry evidence
  │   → raw/imported/manual geometry
  │   → SemanticAcousticGeometry
  │   → compiled geometry / boundary authority
  │
  ├─ equipment evidence
  │   → EquipmentDefinition
  │   → DirectivityDataset / source capability
  │   → amplifier / load / playback-chain authority
  │
  ├─ treatment evidence
  │   → AcousticTreatmentDefinition / Placement
  │   → TreatmentBoundaryOverlay
  │
  └─ measurement evidence
      → AcquisitionContext / Measurement
      → MeasurementQualityReport

All exact authorities
  → immutable SceneRevision / SystemVariant
  → AcousticSceneSnapshot / exact evaluation inputs
  → prediction / optimization / standards / headroom / robustness
  → named topology / treatment / system comparison
  → installation
  → As-built
  → re-measurement / validation / calibration
```

## 12. Detailed specifications

This document defines the top-level principle only. Detailed semantics remain canonical in their owning documents.

- [DATA_AND_ANALYSIS.md](DATA_AND_ANALYSIS.md) — immutable data/history/evidence semantics
- [IMPLEMENTATION_ROADMAP.md](IMPLEMENTATION_ROADMAP.md) — milestone, dependency and acceptance status
- [PROJECT_PLAN.md](PROJECT_PLAN.md) — product scope and workflow
- [O90_ROBUST_OPTIMIZATION.md](O90_ROBUST_OPTIMIZATION.md) — robustness/tolerance authority
- [O100_SYSTEM_EXPANSION_OPTIMIZATION.md](O100_SYSTEM_EXPANSION_OPTIMIZATION.md) — SystemVariant/topology comparison
- [ACOUSTIC_SOLVER_RESEARCH_2026-09-18.md](ACOUSTIC_SOLVER_RESEARCH_2026-09-18.md) — R-series solver research/decision boundary
- [COMPETITIVE_PRODUCT_RESEARCH_2026-09-19.md](COMPETITIVE_PRODUCT_RESEARCH_2026-09-19.md) — external product gap analysis
- [MEASUREMENT_WORKFLOW.md](MEASUREMENT_WORKFLOW.md) — measurement acquisition boundary

Implementation facts and future plans must not be inferred from this principle document; use the implementation status and roadmap documents for current completion state.
