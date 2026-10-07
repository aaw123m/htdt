# HTDT Product Domain Boundary Charter — Issue bolph71656-ai/Home-Theater-Digital-Twin#655

> 制定: 2026-09-24 / 対象: 新規feature・issue review・製品scope判断の共通基準
> 参照ルール: issue/PR参照は `<owner>/<repo>#<N>` 形式でrepo省略不可（旧repo objectは現repoの同番号と衝突する）。実装claimは `(merge <sha8>)`、回収不能な旧objectは `(unavailable)`、別PRに置き換えられたものは `(superseded)` を付記する。対応表は [MIGRATION_REFERENCE_MANIFEST](MIGRATION_REFERENCE_MANIFEST.yaml)、機械検査は `scripts/validate_canonical_references.py`。

## 基本原則

HTDTは**theater Digital Twinそのもの**、exactなcross-domain authority/lineage、
theater固有のdesign comparison、closed-loop verificationを所有する。
成熟したspecialist capabilityは再実装せず、exactなevidence/resultとして
統合・import/exportする。

このcharterは個別issueの局所non-goal（not BIM、not REW、not ERP等）を
横断的に束ねる契約であり、Digital Twinに適合する隣接domain featureを
削除するものではない。

## 3つの製品深度tier

すべての主要domainは次のいずれかに属する。

### Tier A — HTDT core所有domain

Digital Twinとcross-domain workflowの中心であり、HTDTがnativeな
first-class authoring/evaluationを提供する。「所有」は数値kernelを
自前で書くことを意味しない（OSS library/solverの内部利用は可）。

- project/Scene/SystemVariant identity
- theater room geometry
- theater entity placement
- speaker/listener/screen/projectorのphysical layout
- equipment/source/system topology identity
- measurement ↔ Scene binding
- prediction authority orchestration
- multi-objective/robust comparison
- current/proposed/as-built/measured lifecycle
- installation geometry/handoff
- cross-domain evidence/provenance
- commissioning reconciliation

### Tier B — bounded theater-adjacent capability

HTDTはDigital Twinを調整するのに必要なsubsetだけを所有する。

| domain | HTDTが所有する範囲 | 所有しない範囲 |
|---|---|---|
| Video | projector/display geometry、image aperture/sightline、presentation mode、imported/measured photometric・colorimetric commissioning、exact picture-mode/settings evidence | 完全なdisplay calibration engine、LUT生成suite、pattern-generator ecosystem |
| Lighting | theater fixture/zone identity、playback/measurement関連のoperating scene、ambient-light evidence、bias-light commissioning | architectural lighting design、photometric building simulation、electrical lighting-control engineering |
| Electrical/rack | equipment inventory、documented power/load arithmetic、PDU/circuit assignment、physical rack layout、documented ventilation/service clearance | code compliance、breaker/conductor sizing、structural rack certification、HVAC/CFD |
| Procurement | project BOM derivation、selected/owned/ordered/received/substituted state、exact design/equipment定義へのlink | general accounting、supplier ERP、payment/invoice、theater projectと無関係のinventory管理 |
| Sound isolation | evidence-bound theater/adjacent-space target、observed/measured isolation evidence、bounded comparative planning | 完全なbuilding-acoustics/flanking structural engineering |

### Tier C — specialist-tool delegated domain

原則としてHTDTはrecreateせずintegrate/import/exportする。adapterと
exact provenanceを持つことはdelegationと矛盾しない。

- REW級のmeasurement signal generation / advanced acoustic analysis
- CalMAN/ColourSpace級のdisplay calibration
- Dirac/Audyssey/YPAO級のfull correction engine
- 一般BIM/IFC authoring
- structural engineering
- NEC/JIS/building-code compliance
- HVAC load/duct design
- professional electrical design
- 一般lighting photometry
- 完全なERP/accounting

## canonical domain map

| domain | tier | HTDTのauthority | 外部境界 |
|---|---|---|---|
| Scene/project/SystemVariant identity | A | append-only revision・exact hash・lifecycle | なし |
| Theater room geometry/entity placement | A | native authoring・constraint・undo履歴 | 一般CAD/BIMはTier C |
| Measurement↔Scene binding | A | exact MeasurementPlan/Campaign・evidence binding | REW本体はTier C |
| Prediction orchestration | A | capability gate・applicability・exact input/output provenance | solver engine自体はadapter経由 |
| Multi-objective/robust comparison | A | Pareto/robust authority・lifecycle比較 | なし |
| Calibration（AVR/DSP settings） | B | exact plan/export/readback/applied-state evidence | Dirac/Audyssey等のcorrection engineはTier C、proprietary apply protocolはadapter |
| Video geometry/commissioning | B | projector/display geometry・sightline・settings evidence | display calibration suiteはTier C |
| Lighting（theater scope） | B | fixture/zone/scene identity・ambient-light evidence | 建築照明設計はTier C |
| Electrical/rack | B | inventory・documented load arithmetic・rack layout | code compliance/詳細設計はTier C |
| Procurement/BOM | B | project BOM・owned/ordered/received/substituted状態 | ERP/accountingはTier C |
| Sound isolation | B | evidence-bound target・observed evidence | building-acoustics構造設計はTier C |
| REW measurement engine | C | acquisition context/plan定義・evidence import・Scene/SystemVariant binding | signal generation・解析本体はREW |
| Display calibration engine | C | operating context定義・measurement/settings import・target検証 | calibrator本体 |
| Correction engines | C | 計画/context・settings evidence・検証 | Dirac/Audyssey/YPAO |
| 一般BIM/IFC | C | theater workflowが要求するbounded subsetのimport | full IFC authoring |
| Building code/electrical design | C | exact source/profile保持・advisory arithmetic・evidence記録 | PASS/FAIL判定は専門authority |
| HVAC | C | clearance/換気のdocumented evidence | load/duct設計 |
| ERP/accounting | C | BOM linkのみ | 本体 |

## theater固有のcoordination価値テスト

featureのHTDT所有を判断する問い:

> このcapabilityは、exactなtheater Digital Twinにbindされ、design/as-built/
> measured comparisonに参加することで、固有の価値を得るか?

所有を正当化する例:

- display color measurementをexact projector mode + screen + room-lighting状態へbindする
- speaker wiring verificationをexact installed speaker instanceへbindする
- BOM数量をexact selected designへbindする
- rack loadをexact installed equipmentへbindする
- treatment designをmeasurementとinstallation状態と比較する

深い再実装を自動的に正当化しない例:

- 汎用3D mesh編集
- 任意回路設計
- 汎用color calibration
- 汎用purchase-order workflow

## 隣接domain featureのdepth-tier宣言（必須checklist）

隣接domainに触れるfeature/issueは次を明記する:

```text
Domain depth
- Tier A core / Tier B bounded adjunct / Tier C delegated integration

なぜこの深度か?
HTDTが所有するexactなDigital Twin authorityは何か?
外部に残るspecialist capabilityは何か?
import/export/adapter境界は何か?
HTDTが明示的に主張しないことは何か?
```

判断を複数issueに散在する散文へ委ねない。

## reuse優先順位

成熟したspecialist capabilityでは次の順で評価する:

1. import
2. external adapter/API
3. reproducible export
4. bounded derived evaluation
5. 最後にnative再実装

例:

- **REW**: HTDTがacquisition context/planを定義 → REWが測定 → HTDTがexact evidenceをimport → Scene/SystemVariantへbind。外部依存を一つ減らすためだけに完全な測定engineを書かない。
- **display calibration**: HTDTがtheater/display operating contextを定義 → specialist calibratorが測定/適用 → HTDTがmeasurement/settings evidenceをimport → selected targetに対して検証。
- **CAD/BIM**: HTDTのtheater固有CAD + 有用なgeometry/reference formatのimport。theater workflowがbounded subsetを要求しない限り一般architectural BIM semanticsは実装しない。

## numerical solver境界

HTDTはacoustic solver adapterと、正当化される場合はsolver実装（Issue bolph71656-ai/Home-Theater-Digital-Twin#101系）を
所有しうる。ただし次を分離する:

- Digital Twin authority
- solver input compilation
- numerical engine
- validation evidence

solver実装が高度化してもHTDTはgeneral multiphysics platformにならない。
新規physics domainは明示的なproduct-boundary reviewが必須。

## authoring深度境界

native authoringはfield/theater interactionが重要な箇所で深くする:

- speaker placement/aim、room geometry、treatment placement
- screen/projector geometry
- operational/service clearance
- installation cutout/mounting constraint

次の汎用機能は上記workflowに奉仕しない限り実装しない:

- 無制約mesh sculpting、mechanical assembly、任意parametric solid
- 完全なarchitectural documentation

## evaluation境界

HTDTがevaluateする条件: 入力がexact/typed、algorithm scopeがbounded、
解釈がtheater関連、結果がvalidation可能。

外部standard/code/professional judgementが必要な場合:

- exact source/profileを保持する
- advisory arithmetic/contextを表示する
- 根拠なきPASS/FAILを主張しない

例: circuit load合計の表示は可、電気工事code complianceは専用の
authoritative profileとvalidated実装なしに不可。

## evidence vs automation

HTDTは外部の専門作業をautomateするより**記録・reconcile**することが多い:

- installerによるstructural backing確認
- electricianによるcircuit確認
- calibratorによるreport
- acoustic consultantによるmaterial data
- manufacturerによるdirectivity/impedance

Digital Twinの価値はexact identity・provenance・どのdecisionがその
evidenceに依存するかを保持することにある。

## 運用

- 本charterへの変更はproduct-boundary判断の変更であり、該当issue/PRで
  変更理由を記録する。
- tier分類が争点になるissueは本書の該当節を引用して判断する。
- 本書は製品scopeのcharterであり、実装状態は
  [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)を正本とする。
