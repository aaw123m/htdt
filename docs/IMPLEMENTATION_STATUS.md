# 実装ステータス

> 更新: 2026-09-21 / N05〜N90 + O10〜O80 software path実装済み / O90A〜O90E software authority実装済み・actual owned-room evidence残件 / O100A〜O100G workflow-first software UX実装済み / UX160はPR #291の部分owned-Windows acceptanceと具体的不具合修正をmain反映したがfull gateはBLOCKED / Issue #170 StandardsProfile + workspace integration実装 / Issue #101: PR #289 R100B exact 4 GL2 substeps experimentはunchanged numerical gate PASSだがcandidate-wide production adoption NO_GO、PR #295 R130D target-window diagnostic後もNOT_VALIDATED、PR #292 R150 bounded one-Portal first-order reflectionをmain反映、PR #287 R160 unequal-grid reconciliation authority維持 / production solver・full numerical・R180・実室model gate未通過
> 実装順は[ロードマップ](IMPLEMENTATION_ROADMAP.md)。旧browser/backendの詳細履歴は[2026-09-16 archive](IMPLEMENTATION_STATUS_ARCHIVE_2026-09-16.md)へ保存する。

## Native CAD — 現在地

**N05〜N90のnative CAD release pathとO10〜O80のsoftware pathは実装済み。O90 robust/tolerance-aware optimizationはIssue #140で正式計画化し、O90A robustness authorityをPR #145、bounded multidimensional O90B foundationをPR #149で実装。Issue #146 completionでcanonical O90B authorityを完了し、O90CはPR #228/#236/#238でauditable multi-fidelity screening、R140 exact execution/cache、common-fidelity robust-Pareto finalizationまで実装済み。O90D workflow-first robustness software UIはPR #249で実装済み。O90E software authorityはPR #266で実装済みだが、actual owned-room evidenceは未登録のためproduction robustness gateはclosedのまま。UX160 owned-Windows visual acceptanceは未完了。O100 system expansion / virtual channel topology optimizationはIssue #142で正式計画化し、O100AをPR #144、O100BをPR #150、O100CをIssue #168、O100DをIssue #169、O100EをPR #228、O100FをIssue #229 / PR #231/#232/#233で実装。O100G backendはPR #235/#239でproposal lineage / explicit As-built / exact measured evidence、PR #244でSystemVariant-specific MeasurementPlan/Campaignまで実装済み。PR #255でRoom/Optimize workflow-first UX、Japanese-first lifecycle badge、proposal ghost、SystemVariant comparison、既存O90D「ばらつき耐性」内のread-only O100F proposal robustness表示、apply confirmation、SystemVariant measurement-state presentationを実装。Windows DPI/font/mouse/3D readability/clipping/first-use clarityはUX160 owned-Windows gateとして未完了。O70はPR #92/#93、O80はPR #94でmain反映済み。PR #94 merge `6faf554bcf3670f64ff13c530fa4fc79ab1881b8` はCI #548 / run `35313405578` とWindows Release Artifact #93 / run `35313405629`をPASSし、real-repository synthetic O10→O80 laneとpackaged seed/installerまで検証した。Issue #101のpost-0.1 arbitrary-room R-seriesはR100AをPR #110 / merge `1714c078d4063f59da93f0d733171547f7eb486d` でmain反映済み。R100B authority基盤はPR #111 / merge `7be0127fb352c7073d4a686f2e77cc22bc06eac3` でmain反映済み。raw observation evaluator / pyroomacoustics reference probeはPR #112 / merge `5725f8f2eecb150773202bf324132a34a40ba492`、PFFDTD Windows Python/Numba platform smokeはPR #113 / merge `ea5f5b8631e5097d37788210b2652b3089a28807` でmain反映済み。PR #115 / run `35349358027` でPFFDTD R100A rigid rectangular eigenfrequency fixtureは3段階grid convergence + p=2 Richardson extrapolationにより4 observableすべてPASS。accepted evidence summaryを `benchmarks/acoustics/evidence/r100b_pffdtd_rigid_modes_2026-09-18.json` に固定済み。MFEM rigid-room independent referenceはPR #114 / merge `245a3efc66144b81742d65c62ad99ba081fe7426` でmain反映済み。PR #116でpressure authority欠落を修正するR100A-2（density明示）とPFFDTD complex-pressure convergenceを実装し、専用workflowをPASS。R100A hash変更により旧R100B artifactはcurrent selectionにはstaleとなり、新authorityで再実行する。PR #151でPFFDTD native DEF boundary/reflection-functionによるexplicit impedance gateはPASSしたが、spatial FDTD incident/reflected pressure decompositionは未検証。PR #154でMFEM concave independent referenceを追加し、solver-qualified p2–p5 solveは得られたもののp-refinement非収束のためconcave referenceはFAIL、impedance complex-R extractionはR100A-2にincident/reflected decomposition authorityが無いためBLOCKED。PR #155でpyroomacoustics stochastic seed/convergence evidenceを追加し、same-seed raw histogram replayは再現したがfine estimatorのinsufficient supportによりfixtureはFAIL/non-converged。PR #160でMFEM Portal continuity cross-fixture gateを追加し、同一conforming mesh・内部共有面・境界条件なしの表現で281点のmagnitude/phase比較が全て一致してPortal fixtureはPASS。PR #267でcandidate-wide production-adoption readiness gateを実装し、current evidenceのmachine decisionは`NO_GO / additional evidence required`、ready candidateは0件。production solver selectionは未完了。一方、PR #243でbounded PFFDTD candidate wave execution、PR #250でactual CPU-baseline R140 executorをmainへ接続し、PR #259でpinned R130A PFFDTD Python/Numba CPU candidateのsolver-specific resource estimator・workload authority・actual R140 executor integrationを追加した。R150はPR #245でbounded deterministic direct/first-specular GA adapter foundation、PR #253でexact R120 semantic surfaceに基づくgeneral planar single-region first-orderを成立させ、PR #258でordered surface pairのbounded deterministic second-order specular reflectionまで拡張し、PR #265でexact 2-region + 1 open Portal + maximum 1 crossingのbounded deterministic direct propagationを追加した。R160はPR #254のtyped foundationに続き、PR #262でexact R130/R150 artifactをbindするbounded composition authorityまで実装した。さらにPR #260でR100Bのexact frequency-independent purely-resistive specific-impedance→PFFDTD DEF mappingを再利用し、actual non-rigid material executionまでR130B vertical sliceを接続した。PR #264でR130C positive-real normalized-admittance DEF authority、causal/passive/stable contract、valid-band fail-closed、deterministic compiled boundary、mixed-material PFFDTD execution、solver-specific resource-estimate provenance、independent normal-incidence magnitude/phase/passivity referenceまでを追加した。これらはproduction adoptionやfull R130/R150 numerical validationを意味しない。実室の独立validation evidenceもまだ無いため、`production_owned_room` recommendationとowned-room directional capabilityはIssue #83のreal-data gate成立までdisabledを維持する。**

N70はIssue #63 / PR #64、N80 workspaceはIssue #65 / PR #74、O60 software validationはIssue #75 / PR #76・#78で完了済み。N90はIssue #77 / PR #79でstable Windows releaseを実装し、A15を通過した。N80a最終製品コード変更は `c6cc15e76edbc1ac263911ee084803ca1e32b42c`、accepted gate/headは `ff4dc8078eb9ca0b3effaed66b523cff175fea1a`。

| 区分 | 現在の状態 |
|---|---|
| main | **N05〜N90 stable releaseとO10〜O80 software pathをmerge済み**。O90/O100 software authorityとworkflow UXは実装済み。PR #291でUX160の部分owned-Windows acceptanceと発見不具合修正を反映したが、full UX160は`BLOCKED / NOT ACCEPTED`、actual owned-room evidenceも残件。R-seriesはPR #289でR100B exact 4 GL2 substeps experiment、PR #292でR150 bounded one-Portal first-order reflection、PR #295でR130D target-window diagnosticまで反映し、PR #287のR160 explicit frequency-grid reconciliation / bounded complex stitch authorityも維持。R100B candidate-wide production adoptionは`NO_GO`、R130D general-3D physicsは`NOT_VALIDATED`、R160 production/broadband validationは未成立。R140 real GPU evidence、R170B、R180/owned-room validationも未完了 |
| N80 tracking | Issue #65（closed） / Issue #67（O20 closed） / PR #74 merged |
| O60 tracking | Issue #75 / PR #76（implementation history） / PR #78 merged。final head `ce92d6d04e3ca7463fdf8cfc002e271ffdc00bc3`、CI #427 PASS |
| O90 tracking | Issue #140 / #146。O90A/B authorityとprobability/cancel/cache/stale safeguardsを実装し、O90CをPR #228/#236/#238、O90D workflow-first robustness software UIをPR #249、O90E owned-room robustness validation authorityをPR #266でmain反映済み。actual owned-room evidence未登録のためproduction gateはclosed。残件はreal evidence acquisition/validationとUX160 owned-Windows visual acceptance |
| O100 tracking | Issue #142。O100A〜O100Fは実装済み。O100GはPR #235でdescendant-aware proposal lineage + explicit As-built、PR #239でexact Measurement/Quality/Acquisition binding + measured lifecycle、PR #244でSystemVariant-specific MeasurementPlan/Campaign、PR #255でRoom/Optimize workflow-first software UX・badge/ghost・comparison・O100F proposal robustness read-only integration・measured/validation separationを実装。残件はUX160 owned-Windows visual acceptance |
| StandardsProfile tracking | Issue #170 / PR #192 + PR #263。immutable/versioned profile、criterion source/version/reference、PASS/FAIL/UNKNOWN/NOT_APPLICABLE、predicted/measured distinction、exact SceneRevision/SystemVariant/entity binding、append-only historical re-evaluation、user-defined profile、explicit hard-constraint opt-inを実装し、PR #263でRoom/Optimize workspaceへcriterion provenance/status/evidence basisと明示hard-constraint opt-inを統合。built-inは公開sourceで境界を明示できるRP22 spatial/layout subset、Dolby 5.1.2 azimuth range、AURO-3D Rev.12 elevation/opening-angle criteria。DTS:X推定criteria、Auro source上の不整合を補正したazimuth criteria、追加O100D SPL/headroom objectiveは未実装 |
| O60 validation state | software gate実装済み。owned-room calibration/holdout/repeatability evidence未登録のため、実model validatedとは扱わない |
| N80a last product-code head | `c6cc15e76edbc1ac263911ee084803ca1e32b42c` |
| N80a accepted gate head | `ff4dc8078eb9ca0b3effaed66b523cff175fea1a` |
| N80a product CI | #321 / run `35280237062` PASS |
| N80a acceptance docs CI | #324 / run `35286052855` PASS |
| N70 tracking | Issue #63 / PR #64（完了） |
| N70 last product-code head | `76c21eed7d7efcff23905e8af977854669df2752` |
| N70 accepted gate head | `2a6eaae351beb8b2cbbef23a07e3054bb4fb1c93` |
| acceptance docs head | `bff12d4a379a5458f7dcf6cfa7e2b55b570ea4ec` |
| product CI | #309 / run `35270706491` PASS |
| final harness CI | #312 / run `35272332747` PASS |
| acceptance docs CI | #313 / run `35276546531` PASS |
| A13 | stale、UI responsiveness、明示cancel、document change、clean close/no worker PASS |
| A14 | 8頂点L-room、rectangular-only model=`unsupported`、無silent approximation、overlayなし、scalar control gated PASS |
| F5 | 50 editable objects＋10,000 markers、1 non-pickable actor、初回11.406 ms、orbit p95 27.963 ms PASS |
| native entry | `htdt-native` / `run-native.ps1` / `python -m htdt.native_cad` はworkflow-first shellをdefaultで起動。`--legacy-ui`で旧`OptimizationWorkspaceWindow` compositionへrollback可。起動時にactive compositionがdiagnosticsへ記録される（UX160 launch-path task） |
| browser UI | 新CAD機能は凍結。native release CIからfrontend buildを除外済み。二重実装しない |
| N90 stable product head | `968a9461435ac37138ddd15526140c06613fccb8` / CI #458 PASS / Windows Release Artifact #23 PASS |
| N90 accepted gate head | `3ee2fb91b4976d7b0cac7b13718222cd6e359b76` / A15 owned-Windows PASS |
| stable version | `0.1.0` |
| R100A tracking | PR #110 merged `1714c078d4063f59da93f0d733171547f7eb486d`。CI #582 / Windows Release Artifact #109 PASS。solver-neutral authority + 10 canonical fixturesをmain反映済み |
| R100B tracking | PR #289でPR #285と同一p2/h1・40 elements/525 DOFs・同一M/K/source/receiver・同一R100A finite-record contractのまま、各output intervalをexact 4 GL2 substepsへ分割。6000→9000 / 9000→12000 complex-RMSは`0.00364295 -> 0.00186558`でnumerical convergence PASS、all-rate modal referenceもPASS。final 9000→12000は`0.444193 dB / 0.0498540 relative / 2.41554 deg`でunchanged `0.75 dB / 0.05 / 8 deg` toleranceを全項PASS。candidate solve total約`71.32 s`、PR #285比約`2.03x`だがresource ceiling内でoverall experiment PASS。candidate-wide production adoptionは`NO_GO`のままで、この結果だけをproduction qualificationへ昇格しない |
| R120B general-3D geometry | PR #272でsolver-neutral explicit polyhedral acoustic geometry authorityを実装。arbitrary planar polygon surface、closed air volume、sloped ceiling、step geometry、deterministic canonicalization/triangulation、surface material identity、topology diagnostics、bounded curved-surface tessellation metadata、independent wave/GA representation readinessを追加。wave numerical statusは`NOT_VALIDATED`のままで、対応R130/R150 numerical acceptanceは別gate |
| R130A execution | PR #243。exact snapshot/request/READY dispatch→pinned PFFDTD candidate numerical execution→immutable complex-pressure artifact→result envelope/save-reopenのbounded vertical sliceを実装。candidate-onlyでありR130A numerical acceptance/production adoptionは未完了 |
| R130B impedance execution | PR #260 / merge `0933db0c177f9cbbce44f6ae0434700b7a446062`。exact snapshot surface refs→existing R100B explicit specific-impedance authority→exact `Zn/DEF`→actual pinned PFFDTD non-rigid material execution→immutable complex-pressure artifactまで実装。supported subsetはpositive / frequency-independent / purely-resistive specific impedance。reactive/frequency-dependent/scalar absorption conversionはfail-closed。accepted dedicated runs: R130B `35490749861`、R130A rigid regression `35490749862`、R100B impedance regression `35490749864` PASS。`production_solver_selected=false` / `r130b_numerical_acceptance_completed=false` / `owned_room_evidence=false` |
| R130C causal frequency-dependent boundary | positive-real parallel series-RLC normalized-admittance `DEF` をversioned exact authorityとして追加。`D>=0,E>0,F>=0`、CAUSAL/PASSIVE/STABLE、analytic rational evaluation、valid-band外 extrapolation禁止をfail-closedで強制し、source authority hashとcompiled PFFDTD boundary hashを分離。actual pinned PFFDTD `write_freq_dep_mat` execution、independent normal-incidence complex reflection/passivity reference、resource-estimate provenance、save/reopen/tamper/stale identity evidenceをdedicated workflowで検証する。production adoption/GPU/R170/owned-room/scalar absorption conversionはnon-claim |
| R130D polyhedral execution | PR #278でexact R120B single-region closed polyhedron→actual pinned PFFDTD CPU execution/no-box gateを成立。PR #286で0.25 s rectangular record、MFEM 1/2/3、PFFDTD 8/10/12 PPWをpredeclareして再実行し双方SELF_CONVERGENCE_FAILED。PR #295では同一series/40・80 Hz/threshold/maskを保持し、PR #286 canonical transferをexact reproductionした上でPPW依存`N*dt`差だけをhash-bound diagnostic operatorで切り分けた。PFFDTD canonical adjacent complex-RMS `0.876122 -> 3.171427`に対しalignedは`0.871173 -> 3.187879`、`ALIGNED_NON_MONOTONICITY_REMAINS`でworsening-excessは約1.51%悪化。finite-window mismatchをprincipal causeとする根拠は得られずcanonical contractは変更しない。cross-solverはBLOCKED、`general_3d_validation_state=NOT_VALIDATED`を維持 |
| R140 execution | PR #236/#250のgeneric bounded executorに加え、PR #259でpinned R130A PFFDTD Python/Numba CPU candidateのsolver-specific estimator/workload authority/real executor adapterを追加。PR #273でGPU capability/resource/task/provenance authorityとCPU/GPU equivalence spec/evaluation harnessを追加し、UNKNOWN resourceはfail-closed、mock/synthetic GPU evidenceのPASS/FAIL昇格を禁止。GitHub runnerではGPU device/backendが無いため`gpu_validation_state=NOT_VALIDATED`、`production_gpu_support=false`のまま。real GPU worker/backend・numerical equivalence evidence・production adoption・owned-room evidenceは残件 |
| R150 execution | PR #245 deterministic direct/first-specular foundation、PR #253 general planar first-order、PR #258 bounded second-order、PR #265 2-region/1-Portal direct、PR #270 explicit directed region graph + bounded multi-Portal direct、PR #276 frequency-dependent complex path responseに続き、PR #292でexact 2 regions / 1 directed Portal / 1 ordinary finite surface / 1 first-order specular reflectionを実装。source-region reflection→PortalとPortal→receiver-region reflectionの両event orderをfinite surface/aperture/region membership/occlusion/material/directivity/persistence/stale authorityで検証し、Portal surface reflectionやmulti-Portal reflected pathはfail-closed。path responseは既存`Pa/(m3/s)` complex authorityを再利用。arbitrary multi-Portal reflected path / higher cross-region reflection / stochastic rays / late decay / scattering / diffraction / production validationは残件 |
| R160 hybrid foundation / bounded composition | PR #254/#262のtyped/bounded composition、PR #283のexact common-bin actual complex compositionに続き、PR #287でversioned/hash-bound frequency-grid reconciliationとfixed bounded crossover authorityを追加。default exact-binを保持し、unequal-gridは明示`cartesian_linear_v1`（linear Hz real/imag interpolation）、extrapolation禁止、validity band/output grid/rule/tolerance/overlap/blendをidentityへbinding。regular/irregular unequal-grid synthetic stitch fixture、no-double-count、out-of-band fail-closed、save/reopenをPASS。optimal crossover、late/diffraction、full broadband/production/owned-room validationは未成立 |
| R170A low-band integration | PR #271でexact R130 complex-pressure artifactからsolver-neutral `LowBandPredictionProvider`を構築し、N70 typed read、O30/O40 objective/Pareto、O50 Measurement Plan binding、O60 residual validation、O70 validation bindingへ接続。candidate/validated/production evidence state、valid band、source/receiver/environment/result identity、unsupported observable、save/reopen/staleをfail-closedで保持し、R130 resultをRoomSim attemptへ偽装しない。current R100B decisionがNO_GOのためproduction adoptionは未成立。R180 owned-room validationも未完了 |
| 次工程 | **R100BはPR #289で4-substep numerical gate自体はPASSしたがcandidate-wide adoptionは`NO_GO`なので、内部dtを無制限に細分化せず、未成立のcandidate-wide hard gate / geometry・external benchmark evidenceを事前固定して解消する。R130DはPR #295でfinite-record sampling差がPFFDTD non-monotonicityのprincipal causeではないことを確認したため、次はspatial/voxel-staircase representation、mode/bin sensitivity、source/receiver discretization等を分離した事前固定diagnosticへ進み、self-convergence成立前にcross-solver acceptanceを進めない。R150はPR #292のbounded reflected Portal pathを基礎に能力拡張できるがproduction validationとは分離する。R160はPR #287のexplicit grid authorityを基礎に、production claimを広げずR170B integration policy/validated overlap evidenceへ進む。production solver adoption、R140 real GPU evidence、R180/owned-room、O90E actual evidence、full UX160、Issue #83は引き続き独立gate** |

## O100G — workflow-first software UX / PR #255

Issue #142のsoftware UX slice。Issue #118の四つのglobal destinationを増やさず、O100 authorityをRoom / Optimizeへ統合した。

- Room / 「スピーカー・座席」: current physical systemとpersisted SystemVariant proposalをhuman-readable nameで選択し、追加speaker/channel、role、equipment/source、install zone、lifecycle、reasonを表示する。
- proposed entityはcurrent SceneDocumentへ混入させず、`proposed` semantic stateからselectable wireframe ghostをoverlayする。ghostはmeasurement/collision truthとして扱わない。
- lifecycle UIは「現在 / 提案 / 設置済み / 実測済み」。persistent `current/proposed/as_built/measured`は変更しない。`measured != validated`を固定し、実測済みでもO60/R180 validationが無ければ「実測済み・未検証」とする。
- Optimize / 「比較」: existing TopologyComparisonEvaluation / VariantEvaluationBundleを表示し、coverage、SPL/headroom、Standards、objective value+direction、eligibility/blocked reason、Pareto stateを独立表示する。overall score / automatic winnerは作らない。
- missing/unsupported/stale authorityは0やblank numericとして出さず、日本語reasonまたは「データなし / 要再評価」を表示する。
- proposal robustnessはO100専用画面を増やさず既存O90D「ばらつき耐性」内へread-only proposal panelを統合し、exact O100F spec/evaluation/sampleを通常O90 CadCandidateへ偽装せず表示する。
- applyはexisting SystemVariant application authorityのみを使用し、baseline in-place mutationを行わずnew SceneRevisionを作成する。applyだけでAs-builtへ昇格しない。
- validation areaは未計画 / 測定計画あり / campaign preregistered / evidence incomplete / 実測済み・未検証を区別する。generic N60 measurementはSystemVariant measured lifecycleを成立させない。
- UUID / SHA-256 / schema / repository/provenance keyはAdvancedへ隔離し、provenance自体は保持する。
- focused software semantic testsとRoom/Optimize/navigation regressionをGitHub Actions対象にした。
- RDC usage: **0**。
- UX160: PR #291で200% DPIのowned-Windows部分acceptanceを実施し、CAD navigation discoverability、O100G panel overflow、日本語copy、unsupported prediction readiness、disabled-action理由等を修正。full DPI matrix、Ctrl+K、mouse/focus/VTK gesture、O90D/O100G apply等は未完了のため **BLOCKED / NOT ACCEPTED**。CIや部分確認をfull Windows visual acceptanceの代用にしない。
- O90E software authorityはPR #266で実装済みだが、actual owned-room evidence / R180 / Issue #83 owned-room evidence gateは別途未完了であり、O100全体をproduction-validとしてcloseしない。

## Issue #447 / #452 / #567 / #568 / #615 / #619 — design lifecycle authorities — 2026-09-23

プロジェクト設計ライフサイクル系6 Issueのbackend software authorityを一括実装した。いずれも既存のimmutable authority + sha256 pin + append-only repository conventionに従う。

- **#447 DesignComparisonSet** (`cad_design_comparison.py` + repository): named alternatives pin exact SceneRevision (id + content hash) と任意の SystemVariant / as-built / checkpoint / prediction・measurement・standards・robustness evidence refs。supersedes chainでrevision管理。`diff_alternatives`は`diff_scene_documents`によるsemantic A/B diff + evidence added/removed/changed、`evaluate_comparison_set`はavailable / unresolvable / incompatible_baselineをitem別に報告し、non-comparableを落とさない。overall scoreやwinner判定は実装しない。
- **#452 Calibration workflow UX backend** (`cad_calibration_workflow.py` + `cad_applied_settings` table): 既存CalibrationPlan lifecycleをworkflow facade化。`review_plan`は計画自身のper-channel gain/delay/polarity/crossover/PEQをstructural projectionとして返し (generic 80 Hz defaultの注入なし)、`export_settings`はdeterministic JSON/CSV + `exported` lifecycle、`mark_user_applied`は明示user-applied transitionとして`CadAppliedSettingsRecord` (export ⊕ deviations でeffective settings再構築可能) を記録する。`plan_verification`/`record_remeasurement`/`mark_validated`は既存preregistration/completion authorityを束ね、evidenceが揃わない状態遷移はfail closed。
- **#567 TheaterOperatingPreset** (`cad_operating_preset.py` + repository): named preset (movie/music/game/night/custom) はSceneRevision cloneを作らずexact refsで構成全componentをpin。desired/applied/measuredを`TheaterOperatingPreset` / `AppliedPresetState` / `PresetMeasurementBinding`で分離し、`evaluate_preset_freshness`はcomponent別 current/stale/missingを報告してpreset自体は書き換えない。decoder internalsはdeclared input/device modeのみでUNKNOWNを維持。`OPERATING_PRESET_NOT_CONFIGURED`はexplicit default。best-preset scoreは実装しない。
- **#568 SystemHealthBaseline + HealthCheckPlan/Run** (`cad_system_health.py` + repository): post-commissioning baselineはas-built/preset/measurement/instrument/metric pin + tolerance policyをexact refsで固定 ("latest"なし)。bounded check planとrunはplan↔baseline hash bindingを必須とし、observationは`within_baseline` / `changed` / `indeterminate` (repeatability不足・比較不能) / `not_comparable` (acquisition context不一致) / `not_run`に分類。cause inferenceはせず`cause_hypothesis`は明示note扱い。run historyはappend-only。
- **#615 ProjectActivityEvent projection** (`cad_project_activity.py` + notes repository): Scene revision/label、SystemVariant lifecycle、capture inbox、measurement、calibration lifecycle、design checkpoint、operating preset、health run、AV sync、user noteからrebuildable deterministic event streamを射影。event_idはsource authorityのdeterministic digestで再構築も同一。`WorkspaceDeepLink`でexact authorityへdeep-linkし、Overview `OverviewReadinessViewModel.recent_activity`へ"Recent activity"として接続。routine UI opsは発火しない。
- **#619 ProjectDesignCheckpoint + ConstraintWorkspaceSnapshot** (`cad_design_checkpoint.py` + repository): scene_revision_id + content hash必須のimmutable manifest + mutable CadConstraintSetのsemantic snapshot。read時にcurrent/latestを解決しない。`diff_checkpoints`はcomponent別 unchanged/changed/added/removed、`restore_design_checkpoint`は新head SceneRevisionとして復元 (restore ≠ undo、履歴・as-built・measuredを書き換えない) + constraint workspaceの新generationを書き込み、partial restoreはapplied componentを列挙。
- 検証: 新規53 backend test +既存test_overview_readiness/test_workflow系をscopedでPASS (`tests/test_cad_{design_comparison,design_checkpoint,operating_preset,system_health,project_activity,calibration_workflow}.py`)。GUI/hardware-gated項目 (native Measurements/Optimize page上の表示確認、実機apply) はuntested。

## Issue #453 / #507 / #512 / #520 / #555 / #594 / #600 / #602 — installation/authority batch — 2026-09-24

インストール・エクスポート・証拠管理系8 Issueのbackend software authorityを一括実装した。いずれも既存のimmutable authority + sha256 pin + append-only repository conventionに従う。

- **#453 Installation handoff** (`installation_handoff.py` + `installation.export_handoff` command): 明示SceneRevision/SystemVariant選択→`InstallationReportService`のfail-closed resolve→`HandoffReview` (非AVAILABLE sectionを列挙) →preview→byte-deterministic export (dimension sheets CSV / settings CSV / coordinates CSV / report HTML) をdirectoryへ書き出す。exportはread-onlyでproject stateを変更しない。
- **#507 Field/as-built evidence** (`cad_field_evidence.py` + repository): installation photo/note/verification fileをcontent-addressed blob (sha256) + ≥1 exact target (scene revision+entity / variant / section) にbind。evidence追加はentity/verification stateを一切promoteしない。append-only + per-target index照会。
- **#512 Analysis export** (`analysis_export.py`): raw/derived/predicted/display_transformedのvalue classを明示したseriesをcanonical順で束ね、deterministic CSV/JSONとself-contained HTML (inline SVG plot + embedded machine-readable payload) を描画。stale/historical metadataはseries単位で保持しsource numericsはコピーしない。
- **#520 Commissioning verification** (`cad_commissioning.py` + repository): versioned `ToleranceProfile` (abs_error/min/max/range/equals) →`CommissioningPlan` (pinned profile hash + exact SceneRevision/Variant) →append-only `CommissioningRun`。判定はuncertainty-aware: interval全体がtolerance内でPASS、全体が外でFAIL、straddleまたはuncertainty未定量ならUNKNOWN、N/Aは明示。`AcceptedDeviation`はFAILのままeffective outcomeのみ`accepted_deviation`とし、決してPASSに昇格しない。
- **#555 ProjectDesignBrief** (`cad_design_brief.py` + repository): project goals/constraintsをrequired/preferred/informational付きの`BriefGoalRef`としてfirst-class化 (非free_textはref_id必須)。append-only revision chain (`supersedes_brief_id`)、NOT_CONFIGUREDは`latest_brief→None`として明示、`evaluate_brief_coverage`はgoal別current/stale/missing/unevaluable。
- **#594 AnalysisStudy** (`cad_analysis_study.py` + repository): bound authority refs (source numericsを複写しない) + presentation_spec + versioned analysis operations + typed notes (observation/decision/follow_up/rejected_alternative/assumption)。updateは`duplicate_analysis_study`のみでoriginalはbyte-identicalのまま。`evaluate_study_state`はreproducible/not_current/broken_referenceを報告。
- **#600 External dependency resolver** (`external_dependency_resolver.py` + repository): local→embedded→importedのdeterministic順でexact (kind, ref) matchのみresolve。expected sha256不一致は`identity_conflict`、未対応kindは`unsupported_dependency_kind`、未解決はrequired/optionalで分離。fuzzy substitutionなし。resolution eventはappend-onlyで永続化。
- **#602 Evidence reconciliation** (`cad_evidence_reconciliation.py` + repository): subject/observation/decisionを分離しsource provenance (capture/manual/floor_plan/imported/measurement) を保持。alignment_key未解決または不一致は`not_comparable`、uncertainty未定量は`unknown`のまま、tolerance+combined uncertaintyでconsistent/conflictを判定。decisionはversioned hash付きでappend-only。
- 検証: 新規54 backend testをscopedでPASS (`tests/test_{design_brief,analysis_study,field_evidence,external_dependency_resolver,commissioning,analysis_export,evidence_reconciliation,installation_handoff,command_registry}.py`) + `python -m compileall src/htdt` PASS。GUI項目 (export dialog/選択pickerの動作) はuntested。

## R100B — solver bakeoff authority / implementation in progress

- `backend/src/htdt/acoustic_bakeoff.py`: candidate/run/observable/hard-gate/decision authority、R100A + candidate semantic hash binding、candidate capability coverage、selection fail-closed validation、`preflight` / `validate-run` CLIを実装。
- `benchmarks/acoustics/r100b_candidates.json`: PFFDTD `main@aa319f6...`、MFEM `v4.10@d964264...`、pyroomacoustics `v0.10.1@f02b01d...` をversion pin。probe capabilityはverified capabilityではない。
- `.github/workflows/ci.yml`: Windows CIでR100B authority preflightを実行。
- `backend/tests/test_acoustic_bakeoff.py`: source pin、coverage gap、semantic hash、unknown fixture、capability mismatch、hard-gate selection block、reference-only selection blockを検証。
- `wave-portal-split-room-v1` はPR #160でMFEM candidateへ `portal_continuity` probe capabilityを明示し、fixture PASSまで確認済み。`hybrid-overlap-continuity-v1` は引き続き意図的にcandidate未割当で、未実装capabilityを黙ってclaimしない。
- PR #111でcandidate/run/selection authorityはmain反映済み（CI #588 PASS）。
- `backend/src/htdt/acoustic_bakeoff_observation.py`: backend raw sample→R100A expected sample/tolerance比較を中央化。scalar/complex/vectorのabsolute/relative/phase errorを共通評価し、unsampled observableはspecialized evaluator必須。
- PR #112 / merge `5725f8f2eecb150773202bf324132a34a40ba492`: raw observation evaluator + pyroomacoustics v0.10.1 Windows reference probe。direct/direct-delay/y-min first-reflection point/pathの4 observableはR100A tolerance PASS。
- PR #113 / merge `ea5f5b8631e5097d37788210b2652b3089a28807`: pinned PFFDTD Python/Numba CPU source-checkout pathをWindows Server 2025 / Python 3.12で実行。3つのexact-source-checked runtime compatibility shim後、geometry→voxel→HDF5→FDTD→receiver interpolationまでplatform smoke PASS。physics correctness / CPU baseline / product packagingは未判定。
- `backend/src/htdt/acoustic_pffdtd_adapter.py`: 上記compatibility shim、R100A rigid geometry compiler、upstream trilinear receiver recombinationを共通化。
- R100B fixture evidenceに `disk_mb` を追加し、R100A `disk_budget_mb` をfail-closed enforcement対象へ追加。
- `scripts/run_r100b_pffdtd_modes.py`: h=0.5/0.25/0.125 mの3段階Cartesian gridでrigid rectangular impulse responseを実行。run `35349358027` でm010/m100/m110/m001の4 observableがabs error 0.0108/0.0276/0.0314/0.0404 Hzで全PASS。delta ratio 3.93–4.12、compile 6.014 s、solve 0.586 s、peak RSS 202.66 MiB、disk 1.238 MiB。durable evidence summaryとraw signal archive hashを記録済み。
- PR #114 / merge `245a3efc66144b81742d65c62ad99ba081fe7426`: MFEM v4.10 serial H1 FEM Neumann referenceをWindows Actionsで実行。order 5 / 216 DOFで4 rigid-mode observableを最大9.06e-6 Hz errorでPASS。latest run `35350707920` はdisk/RAM/runtime evidence込みでPASS。
- PR #116: R100Aをschema `r100a-2` / revision 2へ更新し、`density_kg_m3=1.2` を全fixtureの環境authorityへ追加。complex pressure convergenceはabsolute/relative complex RMSで評価し、重複したexact-zero phase gateを削除。旧R100B artifactは新semantic hashのcurrent selection evidenceには流用しない。
- PR #116: PFFDTD velocity potential→pressure変換と3-level/full-2s complex-pressure convergence probeを実装。workflow自体はPASSし、candidate convergence結果はevidenceとしてFAILを保持してtoleranceを緩和しない。
- PR #151: canonical `wave-normal-incidence-impedance-v1` をPFFDTD native DEF boundary/reflection関数へexact mappingし、100/200/300 Hzで `R=1/3+0j`、|R|=1/3、phase=0°としてfixture PASS。これはnative boundary representation gateであり、spatial FDTD propagation/reflection validationではない。
- PR #154: exact five-hex L-prism MFEM concave reference probe。p2–p5の線形残差は固定qualification内だが、complex RMS p-refinementは `0.2966 → 3.7665 → 1.0520` で非収束のためconcave resultはFAIL。finest traceをreference truthへ昇格しない。explicit impedance Robinは表現可能だが、R100A-2 point-source finite-room fixtureにincident/reflected decompositionが未定義のためindependent complex-R extractionはBLOCKED。
- PR #155: pyroomacoustics v0.10.1 stochastic seed/convergence probe。same-seed raw histogramは再現する一方、fine budgetでrequired decay sampleがinsufficient supportとなりrepeatability curve/convergenceはFAIL。independent seeds、resource scaling、raw histogramを保持しzero responseは生成しない。
- PR #160: MFEM Portal/peerを同一2-hexahedron conforming meshへcompileし、x=3 m共有面を内部面としてboundary conditionを置かずにH1 continuityで表現。281 frequency sampleのmagnitude/phase/complex差は全て0でPortal fixtureはPASS。candidate manifest hash更新後のR100B workflow replayも実施。
- PR #162: R100A-3へ進め、explicit radiation terminationを `p/u_n=rho*c` / outward normal / `k=omega/c` / `dp/dn-i*k*p=0` としてsolver-neutralに固定。absolute transferは `dB re 1 Pa/(m3/s)` を明示し、281点のsemi-analytical referenceとfail-closed checkerを追加。
- PR #177 / merge `6e085576a3bfe3b51daabedcdfaa929d0518e3d3`: MFEM v4.10でR100A-3 radiation candidate gateを実装。workflow/compileはPASSしたが、fixtureはfrozen assembly+solve 360 s resource budgetを360.0043634 sで超過したためFAIL。timeout後のpressure sampleは生成せず、run `35422266641` / artifact `10578835428` / digest `sha256:88c3883a5f8c1214303cb5b9fa2adee775e924be370edb23d673ce219682c374` をnegative evidenceとして保持。
- PR #181でPFFDTD exact-concave geometry/evidenceを追加し、h=0.5/0.25/0.125 m self-refinementはFAIL/non-convergedとして保持。
- R100A-4 finite-record authority: rectangular convergence / concave L-roomだけにsolver-native dt、`[0,2s)`、direct scored-frequency DTFT、actual source recordとの `P/Q` normalizationを明示するIssue #180 sliceを実装中。tolerance変更なし。solver-neutral spatial reflection decomposition、qualified independent finite-record concave reference、geometric obstacle、external benchmark/candidate-wide hard gates、production stack ADRは未完了。R100B完了とは扱わない。
- RDCは使用しない。

## R100A — solver-neutral benchmark authority / merged

Issue #101の最初の実装slice。solver選定やkernel実装より先に、R100Bで全候補を同一条件比較するauthorityを固定する。

- `backend/src/htdt/acoustic_benchmark.py`: immutable Pydantic authority。AcousticRegion / AcousticObstacle / Portal / BoundaryTermination、wave/geometric material capability、source/receiver/environment、numerical comparison、observable/tolerance、resource budget、hard gate、canonical JSON/SHA-256 identityを実装。
- `benchmarks/acoustics/r100a_manifest.json`: 10 fixture。rigid analytical modes、convergence、complex impedance reflection、concave L-room、Portal split、explicit radiation termination、direct/first reflection、reflecting counter、stochastic seed repeatability、hybrid overlapをsolver-neutralに固定。
- `backend/tests/test_acoustic_benchmark.py`: manifestのcanonical round-trip、必須fixture網羅、Portal/termination明示、wave impedanceのfail-closed、obstacle participation、未知peer fixture拒否を検証。
- `docs/R100A_BENCHMARK_AUTHORITY.md`: authority境界、hard gate、resource contract、R100Bへの引継ぎを記録。
- scalar absorption/scatteringからphase-bearing impedanceを無言で生成しない。未知openingをanecoic扱いしない。performance budgetとphysics toleranceを分離する。
- この段階は数値solver精度やowned-room validityの証拠ではない。R100B/R130/R180のgateを迂回しない。
- RDCは使用しない。Windows実機操作はR100Aに不要。

## N90 — stable Windows release / A15 PASS

詳細: [N90 Windows acceptance](N90_ACCEPTANCE_2026-09-18.md)

- stable product version: `0.1.0`
- product/artifact head: `968a9461435ac37138ddd15526140c06613fccb8`
- final A15 gate head: `3ee2fb91b4976d7b0cac7b13718222cd6e359b76`
- CI #458 / run `35299355927`: PASS
- Windows Release Artifact #23 / run `35299355977`: PASS
- gate-harness fix CI #459 / run `35300374434`: PASS
- SQLite backup API + version/hash manifest + measurement asset verificationを実装。
- packaged `--backup` / `--restore` / `--version` はQApplication生成前に実行する。
- committed Windows dependency lockからPyInstaller onedir packageを再現する。
- stable AppIdのper-user Inno Setup installerを採用し、program rootとuser-data rootを分離する。
- uninstallはuser dataを削除しない。
- native release CIを正本とし、frontend buildはnative releaseの必須gateから外した。
- owned Windowsで `0.1.0.dev0 -> 0.1.0` update、backup/restore、reopen、uninstall/reinstall、data retentionを一連でPASS。
- gate後はowned-PC repoを元のdetached SHA `5ede848e8e0b0967a50c04c83ff679a649ca439b`へ戻し、clean statusを確認した。

## N70 — 完了内容

### Prediction authority / persistence

- prediction resultをexact native `SceneRevision`、scene content hash、model ID/version、canonical parameters、canonical input snapshotへimmutable bindingする。
- 完了結果のrequest identityをsubmission時identityと照合し、入力取り違えを保存しない。
- measured evidence、prediction、仮説を同じ証拠種別として扱わない。
- legacy `Context`をnative prediction authorityへ昇格させない。

### Geometry compatibility

- axis-aligned rectangular roomは既存のroom-mode / first-order image-source geometryをnative Sceneへadapter接続する。
- 非矩形polygon roomへ矩形専用modelを無言で適用しない。
- F2の8頂点L-roomでは`unsupported`を明示保存し、mode/reflection payloadやoverlayを生成しない。
- 将来rectangular approximationを使う場合は、exact polygonとは別にapproximation rule/error/useを明示保存する。

### Native prediction workspace

- 日本語`予測` dockを既存right-side CAD tab stackへ統合した。
- saved prediction history、model/assumption/compatibility/input revisionを表示する。
- direct path、first-order reflection path/pointはnon-pickable analysis overlayとして表示する。
- room-mode frequency候補はpredicted geometryとして表示し、空間SPL fieldと偽装しない。
- validated scalar fieldが存在する場合だけheatmap/slice/volume controlを有効化する。

### Async stale/cancel boundary

- prediction計算はGUI thread外で実行する。
- job tokenはsubmission時のdocument/revision/content hash/model/inputを固定する。
- edit、cancel、document switch後に遅延完了した結果をcurrent sceneへ自動適用しない。
- close時にworkerを残さない。

### F5 bulk marker rendering

- 10,000 analysis markersを1 marker=1 actorにしない。
- arbitrary `N×3` domain point cloudを1つの`PyVista.PolyData`へ変換し、1 mesh actorとして描画する。
- marker actorはnon-pickableで、editable scene entityと分離する。
- owned PCで50 editable objects＋10,000 markersを計測し、first render `11.406 ms`、orbit p50 `22.993 ms`、p95 `27.963 ms`、max `31.205 ms`を記録した。
- F5の64^3 scalar gridは「後続」。validated scalar-field modelがない現段階ではsynthetic fieldを生成しない。

## N70 Windows受入

詳細: [N70 Windows acceptance](N70_ACCEPTANCE_2026-09-18.md)

最終受入環境:

- Windows 11 Pro build 26200
- Ryzen 7 8845HS / Radeon 780M / 31.31 GiB
- Radeon driver 32.0.13032.11
- 2880×1800 / AppliedDPI 192 (200%)
- Python 3.12.10
- PySide6 6.11.2
- PyVista 0.49.0
- VTK 9.7.0
- PyQtGraph 0.14.0

Final gate:

```text
A13_N70_RESULT PASS
A14_RESULT PASS
N70_A13_A14_RESULT PASS
F5_RESULT PASS
N70_GATE_A13_A14_EXIT=0
N70_GATE_F5_EXIT=0
N70_RESTORED_SHA=5ede848e8e0b0967a50c04c83ff679a649ca439b
N70_POST_STATUS_COUNT=0
N70_RESTORE_OK=True
N70_HARDWARE_GATE_RESULT=PASS
```

A14はN70–N80にまたがるgate。今回の`unsupported` branchでは候補を生成しないためapply/Undoは発生しない。候補preview/適用と1-command UndoはN80でSearchSpec/O10/O20接続後に検証する。

## 継承済みCAD基盤

- N20b: multi-select、common pivot、object/grid/angle snap、hide/lock、entity Undo/Redo。
- N30a: 凹polygon room、stable RoomVertex、頂点挿入/移動/削除、edge寸法、ceiling height、self-intersection拒否。
- N30b: stable wall ID、opening、wall clearance binding、wall move/split/merge/delete、参照migration、曖昧操作拒否、room/topology atomic transaction。
- N40: speaker / seat / screen / furniture / AV equipment / measurement point、3.0.2 template、duplicate、寸法、acoustic reference、explicit aim。
- N50: G10 adapter、allowed/exclusion、walkway/wall clearance、constraint reason overlay、invalid commit rejection。
- N60: immutable measurement/revision binding、REW import/read、FR dock、historical ghost、A/B comparison、stale/cancel guards。
- N70: immutable prediction authority、geometry compatibility、prediction overlays、async guards、bulk marker rendering。

実機受入記録:

- [N05](N05_ACCEPTANCE_2026-09-16.md)
- [N10](N10_ACCEPTANCE_2026-09-16.md)
- [N20a](N20A_ACCEPTANCE_2026-09-16.md)
- [N20b](N20B_ACCEPTANCE_2026-09-17.md)
- [N30a](N30A_ACCEPTANCE_2026-09-17.md)
- [N30b](N30B_ACCEPTANCE_2026-09-17.md)
- [N40](N40_ACCEPTANCE_2026-09-17.md)
- [N50](N50_ACCEPTANCE_2026-09-17.md)
- [N60](N60_ACCEPTANCE_2026-09-17.md)
- [N70](N70_ACCEPTANCE_2026-09-18.md)

## N80 — workspace完了 / O60独立継続

N80a（native SearchSpec + candidate workspace）はWindows実機受入を完了した。詳細: [N80a Windows acceptance](N80A_ACCEPTANCE_2026-09-18.md)。

- last product-code head: `c6cc15e76edbc1ac263911ee084803ca1e32b42c`
- accepted gate head: `ff4dc8078eb9ca0b3effaed66b523cff175fea1a`
- product CI #321 / run `35280237062` PASS
- final gate CI #323 / run `35280664154` PASS
- A13 stale/cancel/document/clean close PASS
- A14 SearchSpec→candidate preview→1-command apply→1 Undo exact restore PASS

N80 workspaceはPR #74で完了しmain反映済み。O20〜O50のprediction/objective/Pareto/measurement-loop authorityをnative CADへ接続し、N80c/O50 owned-Windows acceptanceもPASSした。O60 full validationはIssue #75 / PR #76へ分離して継続する。

- PR #70: objective-vector / Pareto algorithms + immutable native objective/Pareto persistenceをmerge済み。
- PR #71: position-only REW Room Simulator transaction + native Scene/SearchSpec/Candidate adapterをmerge済み。CI #345 PASS。
- O20 exact batch spec / candidate attempt / resume-cancel persistenceとowned-Windows writable gateはIssue #67で完了・close済み。
- N50/N60/N70とO20〜O40の該当gateを前提にする。
- SearchSpec編集とO10候補集合をnative Sceneへadapter接続する。
- 候補preview/適用は1 commandでUndo可能にする。
- hard constraintとobjectiveを混同しない。
- objective vectorを保持し、Pareto比較を基本表示にする。
- 「音質総合点」へ縮約しない。
- 実測loopへ接続する場合も、独立検証前に自動推薦へ昇格しない。

N70で外部solverを暗黙採用しなかった方針を維持する。REW Room SimulatorはS01相当、polygon predictorはS03相当のWindows/座標/精度/性能/再現性証拠を通過した場合だけprediction authorityとして追加する。
### PR #74 — N80c / O50 / O60 現在地

- native Pareto比較はSearchSpec/Scene/constraintのstale状態をfail-closedし、候補間でobjective集合またはunitが不一致なら比較を保存しない。
- 同一semantic Pareto snapshotはSHAで再利用し、ボタン再実行で同一内容を重複保存しない。
- provenance列は `evidence_class:source_kind:source_id` を表示し、measured/predicted等を潰さない。
- O50 Measurement PlanはSearchSpecからcandidateを再生成し、candidate-set SHAと、候補を適用したexact Scene content hash、直接parent revisionまで検証して保存する。
- native最適化dockに実測キューを追加し、同じapplied SceneRevision/content hashのN60 `measured` evidenceのみを明示選択して `planned -> measured` のappend-only履歴へ関連付ける。
- O60 validationはcalibration/holdoutを候補単位で分離し、exact SearchSpec/candidate-set/model version/prediction attempt/Measurement Planへcross-evidence bindingする。
- holdout residualは `pass/fail/insufficient` として保存するが、これだけでrecommendationを有効化しない。trend/rank、sensitivity、repeatabilityの独立検証が未成立ならrecommendation gateはdisabledのまま。
- O70 adaptive plannerはO60の実データgate未通過のため自動推薦としては未実装・無効化を維持する。


## N80c / O50 acceptance

PR #74 product head `2a891dbc1796d3cfdaebbe762d0d6e0d2636563f` はCI #397 / run `35294094501`をPASSし、gate head `44628a1e51c199c10b883ed8accba452578bb1eb`でowned-Windows受入もPASSした。

- Pareto比較・evidence provenance・semantic snapshot de-dup: PASS
- candidate apply/save → exact SceneRevision Measurement Plan: PASS
- exact revisionのN60 measured evidence関連付け: PASS
- planned→measured append-only history: PASS
- stale SearchSpecでのPareto再計算拒否: PASS
- gate後のローカルcheckout復元/clean status: PASS

詳細は [N80c/O50 Windows acceptance](N80C_ACCEPTANCE_2026-09-18.md)。

N80 workspaceのIssue #65完了条件はこの受入で満たす。残るO60 full validationはIssue #75で独立継続し、trend/rank・sensitivity・repeatabilityと実データgateが成立するまでO70 automatic recommendationはdisabledを維持する。


## O60 — full model-validation implementation

PR #76 final head `ce92d6d04e3ca7463fdf8cfc002e271ffdc00bc3` はCI #427 / run `35295833407` PASS。connector上のdraft状態を解除できなかったため、同一headをnon-draft merge-only PR #78でmainへmergeし、merge commitは `b0b56497255425b5b343c6f5f52763d11dbf5ee6`。

- calibration / holdout candidateを分離し、両方が無ければrecommendation gateを開かない。
- objectiveごとのholdout pairwise-ordering agreementを保存し、tie/insufficient/failを独立表示する。
- placement perturbationのobserved sensitivityとprediction error / mをobjectiveごとに保存する。
- 同一SceneRevisionの再測定からrepeatability floorを算出し、candidate差がnoise floor以下ならgateを停止する。
- geometry/band/routing等のapplicability checkとstop reasonをimmutable recordへ保存する。
- O20 prediction attempt、O30 objective evaluation、O50 Measurement Plan、N60 measured evidence、SearchSpec/candidate-set SHAをrepository save時に再照合する。
- `synthetic_fixture` はrecommendation eligibleにならない。`owned_room` は参照measurement provenanceの `validation_scope=owned_room` も必須。
- native最適化dockでresidual / trend / sensitivity / repeatability / applicability / stop reasonを別々に表示する。
- O70向けには永続化済み `eligible` recordだけを取得するAPIを設けたが、Adaptive Planner自体は実室O60 gate通過まで実装・有効化しない。

このCI PASSは算法・authority実装の検証であり、REW Room Simulator等の特定modelが実室で妥当と証明されたことを意味しない。現時点ではowned-roomのcalibration/holdout/repeatability evidenceが無いため、O70 recommendation gateはdisabled。


## O60E — owned-room validation campaign / software authority完了

Issue #81 / PR #82。product head `e3bdd111cfb2ed0487cdf98d93adfa58e759532b` は
CI #512 / run `35305119035` PASS。O60 real-data gateを実測後の恣意的splitから保護する
preregistration authorityとnative workflowを実装した。実室campaignそのものはまだ実施していない。

- calibration / holdout candidateを測定前にimmutable固定する。
- exact SearchSpec / candidate-set SHA / model version / target response / evaluation band / thresholdをcampaign hashへ含める。
- stale SearchSpec/current constraint mismatch、SearchSpec SHA mismatch、candidate-set SHA mismatchをfail-closedする。
- sensitivity / repeatability / candidate separation / applicability requirementsを事前固定する。
- 対象candidateにmeasured Measurement Planが存在した後のcampaign新規登録を拒否する。
- campaign作成時刻より前にcapturedされたmeasurementをowned-room validation evidenceへ使わない。
- REW APIのtimezoneなしlegacy日時はhost local timezone ruleでoffset-aware ISOへ正規化し、raw値/解釈元をprovenanceへ残す。
- generic N60 importはvalidation evidenceへ自動昇格しない。明示的なCampaign REW読込だけが
  `measured + validation_scope=owned_room + validation_campaign_id` を保存する。
- O20 prediction / O50-N60 measured evidenceから、campaignと完全一致するO30 objective vectorを同じtarget responseでmaterializeする。
- readinessはmissing/ambiguous evidenceをcandidate単位で表示し、自動測定や自動推薦を行わない。
- applicabilityはgeometry/band/routingを未確認/PASS/FAILで明示し、PASSには確認根拠を必須とする。
- owned-room ValidationRecordはcampaign ID/SHAへbindingし、repository saveとO70 entry取得時に再検証する。
- synthetic fixtureはeligibleにならず、O70/O80はgenuine owned-room campaignがO60全gateを通過するまでdisabledを維持する。

N90 stable 0.1.0のacceptanceは完了済みで、O60E software authorityのためにRDC/N90実機gateは再実行していない。

## O60R — real-data audit software gate / main反映済み

Issue #83 / PR #85で、実室campaign完了後に使うread-only監査harnessをmainへ追加した。

- source `cad-scenes.sqlite3`はSQLite `mode=ro` / `query_only=ON`で開く。
- committed WALを含む一貫snapshotをtemp DBへ作り、full save-time authority replayはsnapshot上だけで実行する。
- current O70-entry ValidationRecord、campaign ID/SHA、readiness、residual/trend/sensitivity/repeatability/separation/applicabilityを再検証する。
- clean worktree / exact product-head ancestor / checkout restoreをWindows runnerが保証する。
- CI preflightはPASS済み。実室PASSはまだ主張しない。
- `inventory_o60_owned_room.py`でcampaign/readiness/REW read-only状態を1コマンド確認できる。
- owned-Windows inventory preflightで検出したtemp SQLite `WinError 32`に対し、Search/RoomSim/Objective/ModelValidation repositoryの接続を明示closeへ統一し、DB存在時のinventory cleanup回帰testを追加した。
- 残る作業は人手のspeaker/setup移動とREW実測を伴うowned-room campaign実行のみ。O70/O80のsoftware実装は完了済みだが、これを満たすまで`production_owned_room` recommendationとowned-room directional capabilityはdisabled。
## O70 — Adaptive Planner software実装完了 / synthetic acceptance PASS

Issue #90 / PR #92・#93で、実測待ちをソフトウェア完成のblockerにしないdevelopment laneを実装した。

- `development_synthetic`: O60のresidual/trend/sensitivity/repeatability/separation/applicabilityが全PASSし、唯一のstop reasonがowned-room evidence不足であるsynthetic ValidationRecordを許可する。
- `production_owned_room`: current campaign-backed `eligible` ValidationRecordだけを許可する。
- calibration objectiveの measured−predicted 残差をobjective別RBF Gaussian Processで補正し、候補ごとのcorrected meanとresidual uncertaintyを算出する。
- 次測定候補はnormalized residual uncertainty acquisitionで決め、単一の「音質総合点」は作らない。
- SearchSpec SHA、candidate-set SHA、ValidationRecord SHA、algorithm/version、length scale、training/measured candidate、全proposalをimmutable保存する。
- synthetic planはproduction recommendationを開かず、実室妥当性の主張に使わない。
- native最適化dockにAdaptive Planner UIを統合し、ValidationRecord選択→scope/length scale/proposal上限→immutable plan保存→proposalの補正値/不確実性表示→candidate選択同期まで接続した。
## O80 — Extended Search software実装完了 / synthetic acceptance PASS

Issue #90で、既存O10を壊さずmodel-dependent変数を追加するextended-search layerを実装した。

- 既存O10のmultiple entity XYZ探索（高さを含む）を再実装しない。
- base SearchSpec / candidate-setをimmutable authorityとして再生成し、そのfeasible候補へ追加parameterを直積展開する。
- 最初のparameterはspeaker `aim_yaw_deg`（toe-in）。exact XYZとaimをextended candidate IDへbindingする。
- model capabilityをimmutable保存し、parameterを明示サポートしないmodelではExtended SearchSpecを保存できない。
- REW Room Simulatorはspeaker指向性/toe-inを扱わないため、`aim_yaw_deg` capability宣言をhard rejectする。
- synthetic directional fixtureはsoftware acceptance専用で、owned-room model evidenceへ昇格しない。
- native最適化dockへcapability作成/選択、toe-in axis、非同期candidate生成、3D aim preview、1-command apply/1 Undoを統合した。
- `--seed-synthetic-demo` は通常repositoryを通してScene→O10→O20-style prediction→O50/N60 synthetic measurement→O30→O60→O70→O80→O80A Adaptive Extendedを永続化する。measurement provenanceは `synthetic_fixture` / `physical_measurement=false` のまま。
- production O70/O80は引き続きowned-room eligible ValidationRecordを必須とする。

詳細: [O70/O80 Synthetic Software Completion](O70_O80_SYNTHETIC_COMPLETION.md)



## O80P / O80A software extension

- O80P: `body_yaw_deg`をphysical cabinet toe-inとして実装。body orientationとexplicit aimを同一yaw deltaで回転し、新規SearchSpecではsource-orientation cabinet footprintを保存、回転後のroom/allowed/exclusion/wall/envelope-pair hard constraintを再評価する。
- O80A: base O10 XYZ + O80 parameterをaxis span正規化したfeature vectorでobjective別residual GP/uncertainty acquisitionを行う別schemaのAdaptive Extended Planを追加。既存O70 plan schema/hashは変更しない。
- extended objective observationはimmutableで、予測→実測は直前observation SHAを明示したsupersession chainとして追加する。plannerはcurrent headだけを使用し、実測済みextended candidateをproposalから除外する。
- Adaptive Extended Planはexact base SearchSpec SHA/base candidate-set SHA/Extended SearchSpec SHA/extended candidate-set SHA/capability SHA/O60 ValidationRecord SHAへbindingする。
- `development_synthetic`はsynthetic directional fixtureでend-to-end確認できるが、`production_owned_room`はcurrent campaign-backed eligible O60とowned-room directional capabilityを要求する。

## O70/O80 final software acceptance

- O70 core: PR #92。synthetic/production authority分離、objective別residual GP、uncertainty acquisition、immutable persistence。
- O70 native UI: PR #93。ValidationRecord→Adaptive Plan→proposal表示/候補同期をnative最適化workspaceへ統合。
- O80 + synthetic completion: PR #94 merge `6faf554bcf3670f64ff13c530fa4fc79ab1881b8`。
- final product CI: #548 / run `35313405578` **PASS**。backend tests、CLI、N60/N70/N80/N80c/N90/O60R preflightを含む。
- Windows Release Artifact: #93 / run `35313405629` **PASS**。locked native package、packaged synthetic demo seed、Inno Setup installer、install/uninstall data-retention smoke、artifact uploadを含む。
- synthetic fixtureは通常repositoryを通るが、常に `synthetic_fixture` / `physical_measurement=false`。owned-room recommendationへ昇格しない。
- O80 owned-room capabilityはexact document/SearchSpec SHA/candidate-set SHA/O60 eligible ValidationRecord/model versionへ再照合する。
- このsoftware completionではRDCを使用していない。既存native stackのowned-Windows N90/A15受入は維持されるが、O70/O80の実室音響妥当性はIssue #83が未完了のため未主張。


## UX110 parallel integration — 2026-09-18

- PR #123 dark-first design system: main反映済み。
- PR #125 central command registry / Ctrl+K: main反映済み。
- PR #126 read-only Overview readiness: main反映済み。
- PR #124 Issue #102 data-management controller: main反映済み。
- workflow shell初版 #122 はcomponent統合前のbridge設計として再レビューし、独立legacy workspaceがstale WorkingDocumentを保持できる問題を検出した。
- integration branchでは `workflow_navigation.py` をsingle workspace/deep-link contractとし、shell/command/OverviewのID重複を解消した。
- legacy bridgeはdirty/preview/recovery/running-worker中のworkspace移動をfail-closedし、clean再activate時にlatest SceneRevisionへ同期する。
- shell独自QSSを廃止し `apply_dark_theme(app)` をcomposition rootへ接続、Ctrl+Kとreadiness-driven Overviewもshellへ接続した。
- Room bridgeはN20-N70 capability維持のため `PredictionWorkspaceWindow` を使用する。
- UX110 shellは `--workflow-shell` の明示previewとし、UX150/UX160 Windows visual acceptance前は既定launcherへ昇格しない。
- 詳細: [UX110 integration record](UX110_INTEGRATION_2026-09-18.md)
- RDC未使用。


## UX120–UX140 parallel workspace integration — 2026-09-19

- #128 Settings / Data Management UI、#129 CAD input controller、#130 Room workspace、#132 Measurements workspaceをmainへmerge済み。
- #131 Optimization workspaceは#129とのshared command/navigation競合をintegration branchで解消して取り込む。
- `workflow_application.py` をcomposition rootとして追加し、`--workflow-shell` でOverview / Room / Measurements / Optimizationの新workspaceをlazy mountする。
- Roomはdark viewport、object palette、Inspector、overlay、MMB pan、Shift+MMB orbit、wheel zoom、RMB command menu、M/R direct transform、X/Y/Z constraint、F/Home、Esc/Enter、Ctrl+D、polygon room作図・vertex dragを既存WorkingDocumentへ接続した。
- Room transform previewは `working.document` を描画し、commitだけがrecovery/Undo履歴へ入る。
- Measurementsは `読み込み → 割り当て → 品質 → 比較` の4 page compositionへ切替。REW background job中はdeactivation/restoreをblockする。
- Optimization canonical contextは `setup / candidates / comparison / validation`。旧 `objectives / measurement-plan` deep-linkはshared navigation boundaryで互換normalizeする。
- rail下部の「設定」から#128 Data Management UIへ入り、restore時は全mounted workspace guard→handle dispose→native restore→fresh SceneRepository→lazy rebuildを行う。
- follow-upで新Roomへ既存N70 rectangular geometry predictionを接続。request identity / JobGuard / repository / constraint hashを再利用し、dirty/stale/cancelled resultはfail closed、current resultだけ3D overlayへ表示する。
- 既知残件: 旧wall/opening・高度geometry editingの完全移植、UX140のlegacy QMainWindow adapter除去、UX150/UX160 visual acceptance。
- workflow shellは引き続き明示 `--workflow-shell` preview。default launcherはUX160 acceptanceまで変更しない。
- 詳細: [UX120–UX140 integration record](UX120_140_INTEGRATION_2026-09-19.md)
- RDC未使用。


### UX120 Room prediction integration — 2026-09-19

- 新Roomの「音響」contextへN70矩形幾何予測を接続。
- `rectangular_geometry_request_identity`、`PredictionJobGuard`、`CadPredictionRepository`、`constraint_workspace_snapshot` を既存authorityとして再利用。
- prediction開始にはcleanな保存済みSceneRevisionとacoustic-reference receiverを要求。
- Scene/constraint変更後の遅延result、cancelled token、identity mismatchを保存しない。
- cancel後もworker thread終了まではworkspace移動/restore/new runをblock。
- 保存済みrunは現在/要再計算を表示し、current runだけdirect/first-reflectionを3D overlayへ出す。
- central `prediction.run` はRoom/acoustics deep-link後に新controllerへbind。
- 詳細: [UX120 Room prediction integration](UX120_ROOM_PREDICTION_2026-09-19.md)
- RDC未使用。


### UX120 Room advanced geometry — 2026-09-19

- 新Roomへvertex/edge selection、midpoint handle、wall drag、numeric vertex/edge/ceiling editを追加。
- Room > 形状の右contextを専用Geometry Inspectorへ分離。
- wall topology / opening editは既存 `RoomWorkingDocument.replace_room_topology()` と `cad_walls` authorityを再利用。
- wall thickness、opening update/deleteをreusable `cad_walls` helperへ抽出し、全操作をexisting topology validationへ通す。
- split/merge/delete/moveでwall/opening referenceを維持し、orphan/overflowはfail closed。
- selected wall / opening extentをviewport overlayへ表示。
- 詳細: [UX120 Room advanced geometry](UX120_ROOM_ADVANCED_GEOMETRY_2026-09-19.md)
- Roomの主要機能残件はUX150/UX160 polish/acceptance。UX140 legacy adapter cleanupは別途。
- RDC未使用。


### UX140 controller/window separation — 2026-09-19

- workflow-first 最適化workspaceをlegacy `OptimizationWorkspaceWindow/QMainWindow` 継承から分離。
- plain `QWidget` + `OptimizationWorkflowController(QObject)` compositionへ変更。
- existing O10〜O80 mixins/repositories/servicesをauthorityとして再利用。
- Scene/WorkingDocument lifecycleは既存 `RoomWorkspaceController` を再利用。
- candidate viewportはshared dark `RoomViewport3D`、O-seriesはoverlay portだけ利用。
- Validation pageのREW / measurement point / channel roleを明示化し、hidden legacy measurement controls依存を除去。
- workflow shell mountから `workflow_legacy_bridge` を除去。
- 詳細: [UX140 controller/window separation](ISSUE_118_UX140_CONTROLLER_SEPARATION.md)
- RDC未使用。


### UX150 software visual / interaction / language polish — 2026-09-19

- 共通dark themeのsurface border/radius、control height、rail/context、splitter、status、interaction stateを調整。
- workflow shellへlogical widthベースのcompact rail/context modeを追加。
- Roomで高DPI時にobject paletteをprogressive disclosureし、right contextual panelを縮退。
- Room 3Dへneutral floor、major/minor grid、soft lightingを追加。presentation onlyでScene/solver authorityは不変。
- Measurement plotへaxis spacing、clip-to-view、peak-preserving downsampling等のscientific readability設定を追加。
- Optimize Candidates side panelを縮退可能にし、日本語first copyを整理。
- 1280×800 / 1440×900 × 100/150/200%をlogical client sizeへ変換したoffscreen Qt layout gateを追加。
- 詳細: [UX150 software polish](ISSUE_118_UX150_SOFTWARE_POLISH.md)
- UX160 owned-Windows visual/first-use acceptanceは別gateとして残す。
- RDC未使用。

- UX150追加監査: Optimize/REWの標準表示から内部ID/英語内部語を退避し、Qt UserRole/repository authorityは維持。

### Issue #172 Measurement quality authority — 2026-09-19

- 既存N60 Measurement/REW importを変更せず、immutable `MeasurementQualityReport` を追加。
- exact Measurement/Dataset/raw asset SHA、SceneRevision/content hash、entity/measurement point、optional AcquisitionContext ID/hash、algorithm/profile hashへ固定。
- clipping、noise/SNR、usable band、timing reference、polarity、IR window/truncation、calibration provenance、repeatabilityを `PASS/FAIL/UNKNOWN/NOT_EVALUATED` で独立保持。
- FR-onlyやphase-only evidenceからclipping/SNR/common timing等を推定しない。
- downstreamを `magnitude_response / phase_response / common_timing / arrival_time / decay / calibrated_response / repeatability / polarity` のclaim別 `ALLOWED/BLOCKED/UNKNOWN` でgate。要求bandもfail closedで評価可能。
- profile変更は新report、retakeは別Measurement + append-only supersedes/selected lineage。旧measurement/reportとO50/O60 calibration/holdout authorityは変更しない。
- persistenceは既存native DB内のadditive tableで行い、native schema compatibility gateを再利用。
- GUI、#173 CalibrationPlan、#174 joint DSP optimization、owned-room physical acceptanceはscope外。
- 詳細: [Measurement quality authority](MEASUREMENT_QUALITY.md)
- RDC未使用。


### Issue #173 CalibrationPlan foundation — 2026-09-19

- immutable/versioned device-neutral `CalibrationPlan` を exact SceneRevision / SystemVariant / Measurement / Dataset / MeasurementQualityReport hash へ固定。
- MeasurementQualityReport の magnitude / phase / common timing / arrival / decay / calibrated response / repeatability / polarity / required-band gateを再利用し、absolute delayはcommon timing、polarity inversionはpolarity authorityなしではunsupported。
- normalized generic biquad authority、deterministic transfer evaluation、device filter-count / boost-cut / gain-delay / sample-rate / crossover / output制約を実装。silent clip/omissionは禁止。
- `htdt-generic-biquad@1` export snapshotでrequested planとquantized actual settingsを分離し、canonical JSON / deterministic CSV / JSON readbackを提供。
- export / user-applied / remeasured / validatedをappend-only lifecycleで分離。exportだけでinstalled/as-built/validatedへ進めない。
- exact exported settingsを参照するVerificationMeasurementPlan foundationとbefore/after Measurement lineageを実装。
- all-pass correction、coherent inter-channel phase correction、proprietary adapters、advanced PEQ generation、#174 joint optimization、owned-room production recommendation enablementはdeferred。
- 詳細: [CalibrationPlan authority](CALIBRATION_PLAN.md)

### Issue #142 / O100G MeasurementPlan / Campaign — 2026-09-20

- exact SystemVariant/Application/applied-revision binding: implemented
- exact AsBuilt record and actual revision binding: implemented
- preregistered target/channel/source/measurement-point plan: implemented
- campaign immutable exact plan-set binding: implemented
- capture-time, AcquisitionContext and quality-capability matching: implemented
- optional required-band gate via existing MeasurementQualityReport authority: implemented
- plan/campaign append-only completion and save/reopen validation: implemented
- measured lifecycle transition: reuses existing SystemVariantMeasuredRecord
- generic N60 measurement auto-promotion: prohibited
- O60 validation/recommendation implication: none
- O100G overall status: partial; workflow UX/ghost-badge/measured comparison/UX160 remain
- RDC: not used

Details: [ISSUE_142_O100G_VARIANT_MEASUREMENT_CAMPAIGN_2026-09-20.md](ISSUE_142_O100G_VARIANT_MEASUREMENT_CAMPAIGN_2026-09-20.md).

### Issue #140 / O90D robustness UI — 2026-09-20

- Optimize > ばらつき耐性 canonical context: implemented
- Nominal / sensitivity / sampled adverse / feasibility / completeness presentation: implemented
- axis-by-axis local sensitivity chart: implemented
- finite PerturbationSample performance-distribution histogram: implemented; unweighted frequency is not probability, explicit weights are normalized only over scored feasible samples and labeled conditional probability mass
- dedicated 3D position / aim / body-yaw tolerance overlay: implemented
- persisted infeasible position-sample markers: implemented
- bounded vs explicit probability semantics: implemented
- p95/probability unsupported reason near affected UI: implemented
- minimize/maximize direction: backend authority driven
- sampled worst wording: finite-sample wording, not global worst-case
- exact Scene/Search/constraint/model/fidelity/objective comparison eligibility: implemented
- stale evaluation warning/block: implemented
- hidden robustness score / automatic winner: absent by design
- internal IDs/SHA/provider/fidelity: Advanced only
- solver/sampling on UI thread: none
- UX160 owned-Windows visual acceptance: pending
- RDC: not used

Details: [ISSUE_140_O90D_ROBUSTNESS_UI_2026-09-20.md](ISSUE_140_O90D_ROBUSTNESS_UI_2026-09-20.md).

## Issue #101 / R130B explicit impedance candidate execution — 2026-09-20

- PR #260 / merge `0933db0c177f9cbbce44f6ae0434700b7a446062`。
- R100B exact mapping reuse: explicit frequency-independent purely-resistive specific impedanceのみ。scalar/statistical absorption conversion、missing phase synthesis、reactive/frequency-dependent fittingは許可しない。
- exact execution identity: semantic surface、material/boundary id/version/hash、physical Z、unit/capability/valid band/provenance、density/sound-speed authority、PFFDTD mapping id/version/hash、DEFを保持。
- actual backend: pinned PFFDTD `write_freq_ind_mat_from_Zn()` → material HDF5 → `mat_files_dict` → `sim_setup()`。packaged DEF一致とactive non-rigid boundary nodeを再読込検証。
- accepted evidence: R130B `35490749861`、R130A rigid regression `35490749862`、R100B canonical impedance regression `35490749864` PASS。canonical fixtureは `Z=823.2+j0 Pa*s/m`、`rho*c=411.6 Pa*s/m`、`DEF=[[0,2,0]]`、active impedance nodes 64、normal-incidence closed-formとのcomplex/magnitude/phase max error 0。
- non-claims: `production_solver_selected=false`、`r130b_numerical_acceptance_completed=false`、`owned_room_evidence=false`。R130C reactive/causal frequency-dependent boundary、spatial incident/reflected decomposition、production adoption、R180は未完了。
- Implementation record: [R130B_CANDIDATE_IMPEDANCE_EXECUTION_2026-09-20.md](R130B_CANDIDATE_IMPEDANCE_EXECUTION_2026-09-20.md)。
- RDC: 0。

## Issue #101 / R150 bounded second-order specular — 2026-09-20

- PR #258 / merge `7e058bf450539c393e89fc75dcf09591ae0dfbbf`。
- general-planar single-region laneをdirect/first-orderからbounded deterministic second-order specularまで拡張。legacy shoebox/pinned pyroomacoustics laneはfirst-orderのまま維持。
- ordered plane pairごとにmirror/reverse reconstructionし、各reflection pointをexact R120 triangle unionへ拘束。source→p1→p2→receiverの3 segmentをexact triangle visibilityで検証。
- same-surface repeat、coincident/degenerate plane、zero-length、grazing、shared-edge ambiguity、finite-surface miss、occlusion、unsupported directivity/materialはfail-closed。
- material transportは各interactionの `(1-absorption)*(1-scattering)` を積算し、scalar quantityからcoherent phaseを生成しない。
- synchronized final R150 workflow `35491381005` PASS。analytical reflection points/path length/delay、finite-surface rejection、occlusion、material product、deterministic identity、save/reopen、legacy regressionsを確認。
- remaining: third+ arbitrary order、stochastic rays、late/diffuse tail、directional scattering、diffraction、coherent reflection phase、Portal/multi-region、GPU ray tracing、production/owned-room validation。
- Implementation record: [R150_DETERMINISTIC_GA_ADAPTER.md](R150_DETERMINISTIC_GA_ADAPTER.md)。
- RDC: 0。

## Issue #101 / R140 actual executor — 2026-09-20

- actual CPU-baseline bounded worker pool: implemented
- outer worker / inner solver thread authority separation: implemented
- UNKNOWN/UNAVAILABLE resource estimate semantics: implemented
- queued + cooperative running cancellation: implemented
- immutable success/failure/cancel telemetry: implemented
- exact cache reuse + resume: implemented
- deterministic synthetic CI lane: implemented, explicitly non-production evidence
- portable observed peak-RSS metric: unsupported rather than fabricated
- pinned R130A PFFDTD Python/Numba CPU solver-specific resource estimator: implemented in PR #259
- source-bound grid / Nt / cell-time workload authority: implemented
- task-incremental RAM/scratch derivation with explicit component semantics: implemented
- multiprocess setup RAM without a portable bound: UNKNOWN -> fail-closed defer
- real bounded PFFDTD task through R140 + exact second-run cache/resume integration lane: implemented; PR #259 final workflow run #4 (`35489817678`) PASS
- GPU executor / GPU resource estimator / CPU-GPU numerical-equivalence evidence: pending
- production solver adoption / owned-room evidence: pending
- R140 overall status: partial completion

Implementation records: [ISSUE_101_R140_ACTUAL_EXECUTOR_2026-09-20.md](ISSUE_101_R140_ACTUAL_EXECUTOR_2026-09-20.md) and [ISSUE_101_R140_PFFDTD_RESOURCE_ESTIMATOR_2026-09-20.md](ISSUE_101_R140_PFFDTD_RESOURCE_ESTIMATOR_2026-09-20.md). RDC was not used.

### Issue #170 / S130 StandardsProfile workflow integration — 2026-09-20

- workflow placement: Room > スピーカー・座席 / Optimize > 比較へfirst-class integration。新global destinationなし。
- profile selection: immutable built-in profileとpersisted user-defined profileをversion付きで選択可能。
- criterion presentation: 日本語status、observed value、required range/rule、predicted/measured evidence、missing input/capability reasonを表示。
- provenance: exact SceneRevision / optional SystemVariant / profile / evaluation / criterion source/reference / evidence identityはAdvancedへ退避。
- explicit hard constraint: existing explicit_hard_constraint_gate() を再利用。selected FAIL/UNKNOWNのみblock、unselected FAILはadvisory、NOT_APPLICABLEはnon-blocking。
- SystemVariant comparison: criterion matrixのみ。compliance score / winner / automatic ranking / hidden Pareto objectiveなし。
- historical re-evaluation: newer profile versionはappend-only evaluation + reevaluation_of_id。旧evaluationを保存。
- software acceptance: dedicated offscreen Qt S130 tests + existing workflow testsで検証する。
- UX160 owned-Windows DPI/font/mouse/3D screenshot / first-use visual acceptance: **pending**。
- RDC: **0**。

Details: [Issue #170 S130 workspace integration](ISSUE_170_S130_STANDARDS_WORKSPACE_2026-09-20.md).

## Issue #101 / R130C causal frequency-dependent boundary — 2026-09-20

- exact source authority: specific acoustic admittance `m/(Pa*s)`、`Yn=rho*c*Y_specific`、parallel series-RLC normalized `DEF`、exact SceneRevision/content/surface/material identity、provenance/evidence state/optional uncertainty、explicit valid band。
- causal/passive/stable contract: `D>=0,E>0,F>=0` のpositive-real structural gate。sampled-response IFFTは使わず、PFFDTD native recursive boundary stateへ同一DEFを渡す。valid-band外、wrong quantity、missing phase-equivalent complex authority、malformed/stale/non-passive inputはfallbackせずfail-closed。
- compilation/execution: source authorityとは別のcompiled-boundary SHAを生成し、mixed rigid/non-rigid modelでpinned PFFDTD `write_freq_dep_mat`→packaged exact DEF→active boundary nodes→actual complex-pressure executionへ接続。R130A rigid / R130B frequency-independent pathは維持。
- independent reference: positive-real branch式から直接計算したnormal-incidence complex reflectionをPFFDTD `compute_Rf_from_DEF`と比較し、magnitude/phaseとdense valid-band `|R|<=1` を検証。
- persistence: exact resource-estimate ref、boundary authority/compiled boundary、solver/configuration、raw solver asset、immutable complex-pressure artifact/result envelopeを保存。save/reopen再解決とtamper fail-closedを維持し、boundary changeでcompiled/input identityが変わるため旧resultは新boundary identityへ再利用しない。
- canonical implementation record: [R130C_CAUSAL_FREQUENCY_DEPENDENT_BOUNDARY_2026-09-20.md](R130C_CAUSAL_FREQUENCY_DEPENDENT_BOUNDARY_2026-09-20.md)。
- non-claims: production solver adoption、GPU、R170 integration、general fitting/material identification、spatial reflection decomposition、R180/owned-room validationは未完了。RDC 0。

## Capture integration (HTDT-Capture → native HTDT) — contract authority

Issues #333–#338, #343, #345, #369. Production `.htdtcapture` import path
plus the enforced transaction-layer contract on top of the phase-6
vendored fixture bundle (HTDT-Capture pinned commit
`daa00b122f399050c29ba3af988b1d6438686746`).

- **Pipeline** (`htdt/capture_import.py`, `htdt-capture-import` CLI):
  untrusted `.htdtcapture` ZIP/directory → bounded `FrozenBundle` validation
  → exact `manifest.json` canonical/digest checks → reference-ingestor plan
  → payload hash/length verification → single SQLite transaction commit →
  staged result (bundle digest, revision/series ids, lineage digest,
  evidence/handoff/authority counts, quality state, app identity).
  Structured failures carry stage + reason; re-import is a verified no-op.
  Nothing auto-promotes to the semantic scene.
- **Contract layer** (`capture_bundle.py` + vendored
  `capture_contract/*.schema.json` + `capture_schema_eval.py` +
  `capture_binary_formats.py`): Capture Bundle v1 manifest grammar
  (canonical bytes, no floats, sorted keys), foundation payload set,
  per-payload length/SHA, schema-vs-path ownership (JSON only where a
  published schema owns the path), canonical-byte enforcement for
  Capture-owned JSON, bounded sizes/depth, `source_ref` grammar with
  path/sha256/capture_session refs and acyclicity, reserved-path metadata.
- **Rederivation** (`capture_reference.py`): every handoff semantic is
  rederived from the exact payload bytes — anchors↔meshbin header
  counts/hash, session/space/transform identity, annotation/measurement
  kind/provenance/coordinate space, rigid-pose matrices (finite,
  homogeneous, orthonormal within 1e-3), camera intrinsics, internal
  lineage refs (evidence_refs, placement source refs, RoomPlan binding,
  endpoint refs, acoustic-center authority_ref).
- **Transaction** (`capture_ingestion_transaction.py`): `ingest()`
  unconditionally runs the full contract (schema layer → semantic
  rederivation → quality gate) inside the commit transaction; nothing
  partial survives. Immutable `capture_revisions` registry enforces
  series/parent/digest/schema identity — reuse-with-different-values,
  self-parent, cross-series parent and cycles reject; out-of-order
  children land `pending_parent` and reconcile `linked` on arrival;
  pre-existing conflicts are recorded in `capture_revision_conflicts`.
  `capture_bundles` retains the exact canonical manifest bytes
  (sha256 == bundle digest) with typed app name/version/build +
  created/finalized stamps; `capture_bundle` accessor distinguishes
  "manifest retained" from plan-only runs.
- **Quality gate**: `capture-quality.json` is validated at plan build AND
  re-validated at ingest — ready_for_htdt_ingestion, integrity pass,
  supported ruleset (1.0.0/1.1.0/1.2.0), no error diagnostics. Persisted
  per-run quality SHA + ruleset is queryable; legacy rows backfill via
  revalidation or `unresolved`; `require_quality_state` and semantic
  promotion fail closed on unresolved.
- **Authoring handoff** (`capture_authoring.py`): typed annotation /
  measurement / RoomPlan inputs grouped by ingestion lineage, locators
  resolved to evidence, coordinate-space alignment checked before scene
  entry, `equipment_ref` verified against the versioned equipment
  authority (mismatch/unresolved = surfaced conflict, never auto-link),
  measurements reconcile (consistent/conflict/new — never overwrite),
  RoomPlan is suggestion-only, apply is an explicit operator action that
  stamps capture provenance (handoff id, payload hash, space, revision).
  Display/projection_screen entity types stay typed-unsupported (#564).
- **Cross-repo compatibility CI** (#334): `docs/CAPTURE_COMPATIBILITY.json`
  is the machine-readable registry — pinned HTDT-Capture commit, fixture
  bundle digest, canonical plan sha256
  `fa249e30…`, lineage digest `92e81abe…`, ingestor identity and expected
  counts. `backend/tests/test_capture_compat_pinned.py` freezes all
  identities end-to-end; `capture-ingestion.yml` reports the pinned
  revision in CI output and runs the compat gate plus the capture suite.
- **Verified on Linux**: 107 capture tests pass
  (`pytest tests/ -k capture` + `test_raw_mesh.py`). GUI/hardware/Swift
  authoring paths are macOS/Windows-gated and untested here; the fixture
  is byte-identical to HTDT-Capture's phase-6 generator output (a
  production-Swift-authored bundle is preferred when available, per the
  registry note).
