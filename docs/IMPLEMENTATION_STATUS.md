# 実装ステータス

> 更新: 2026-09-20 / N05〜N90 + O10〜O80 software path実装済み / O90A〜O90C実装済み・O90D/E未実装 / O100A〜O100F実装済み・O100G backend lifecycle部分実装済み / Issue #170 StandardsProfile実装 / Issue #101 R-series authority + bakeoff継続 / synthetic acceptance対象 / 実室model gate未通過
> 実装順は[ロードマップ](IMPLEMENTATION_ROADMAP.md)。旧browser/backendの詳細履歴は[2026-09-16 archive](IMPLEMENTATION_STATUS_ARCHIVE_2026-09-16.md)へ保存する。

## Native CAD — 現在地

**N05〜N90のnative CAD release pathとO10〜O80のsoftware pathは実装済み。O90 robust/tolerance-aware optimizationはIssue #140で正式計画化し、O90A robustness authorityをPR #145、bounded multidimensional O90B foundationをPR #149で実装。Issue #146 completionでcanonical O90B authorityを完了し、O90CはPR #228/#236/#238でauditable multi-fidelity screening、R140 exact execution/cache、common-fidelity robust-Pareto finalizationまで実装済み。O90D UXとO90E owned-room robust validationは未実装。O100 system expansion / virtual channel topology optimizationはIssue #142で正式計画化し、O100AをPR #144、O100BをPR #150、O100CをIssue #168、O100DをIssue #169、O100EをPR #228、O100FをIssue #229 / PR #231/#232/#233で実装。O100GはPR #235/#239でproposal lineage / explicit As-built / exact measured evidence backend lifecycleまで部分実装済みで、SystemVariant-specific measurement plan/campaignとUX/visual acceptanceは未実装。O70はPR #92/#93、O80はPR #94でmain反映済み。PR #94 merge `6faf554bcf3670f64ff13c530fa4fc79ab1881b8` はCI #548 / run `35313405578` とWindows Release Artifact #93 / run `35313405629`をPASSし、real-repository synthetic O10→O80 laneとpackaged seed/installerまで検証した。Issue #101のpost-0.1 arbitrary-room R-seriesはR100AをPR #110 / merge `1714c078d4063f59da93f0d733171547f7eb486d` でmain反映済み。R100B authority基盤はPR #111 / merge `7be0127fb352c7073d4a686f2e77cc22bc06eac3` でmain反映済み。raw observation evaluator / pyroomacoustics reference probeはPR #112 / merge `5725f8f2eecb150773202bf324132a34a40ba492`、PFFDTD Windows Python/Numba platform smokeはPR #113 / merge `ea5f5b8631e5097d37788210b2652b3089a28807` でmain反映済み。PR #115 / run `35349358027` でPFFDTD R100A rigid rectangular eigenfrequency fixtureは3段階grid convergence + p=2 Richardson extrapolationにより4 observableすべてPASS。accepted evidence summaryを `benchmarks/acoustics/evidence/r100b_pffdtd_rigid_modes_2026-09-18.json` に固定済み。MFEM rigid-room independent referenceはPR #114 / merge `245a3efc66144b81742d65c62ad99ba081fe7426` でmain反映済み。PR #116でpressure authority欠落を修正するR100A-2（density明示）とPFFDTD complex-pressure convergenceを実装し、専用workflowをPASS。R100A hash変更により旧R100B artifactはcurrent selectionにはstaleとなり、新authorityで再実行する。PR #151でPFFDTD native DEF boundary/reflection-functionによるexplicit impedance gateはPASSしたが、spatial FDTD incident/reflected pressure decompositionは未検証。PR #154でMFEM concave independent referenceを追加し、solver-qualified p2–p5 solveは得られたもののp-refinement非収束のためconcave referenceはFAIL、impedance complex-R extractionはR100A-2にincident/reflected decomposition authorityが無いためBLOCKED。PR #155でpyroomacoustics stochastic seed/convergence evidenceを追加し、same-seed raw histogram replayは再現したがfine estimatorのinsufficient supportによりfixtureはFAIL/non-converged。PR #160でMFEM Portal continuity cross-fixture gateを追加し、同一conforming mesh・内部共有面・境界条件なしの表現で281点のmagnitude/phase比較が全て一致してPortal fixtureはPASS。candidate-wide physics / explicit termination / obstacle / Windows product packaging / production solver selectionは未完了。実室の独立validation evidenceもまだ無いため、`production_owned_room` recommendationとowned-room directional capabilityはIssue #83のreal-data gate成立までdisabledを維持する。**

N70はIssue #63 / PR #64、N80 workspaceはIssue #65 / PR #74、O60 software validationはIssue #75 / PR #76・#78で完了済み。N90はIssue #77 / PR #79でstable Windows releaseを実装し、A15を通過した。N80a最終製品コード変更は `c6cc15e76edbc1ac263911ee084803ca1e32b42c`、accepted gate/headは `ff4dc8078eb9ca0b3effaed66b523cff175fea1a`。

| 区分 | 現在の状態 |
|---|---|
| main | **N05〜N90 stable releaseとO10〜O80 software pathをmerge済み**。O90AはPR #145、O90B bounded foundationはPR #149、Issue #146 completionでcanonical O90B authorityを完了。O90CまでPR #228/#236/#238で実装済み、O90D/Eは未実装。O100A〜O100Fは実装済み、O100G backend lifecycleはPR #235/#239で部分実装済み。O80 PR #94 merge `6faf554bcf3670f64ff13c530fa4fc79ab1881b8` / CI #548 PASS / Windows Release Artifact #93 PASS |
| N80 tracking | Issue #65（closed） / Issue #67（O20 closed） / PR #74 merged |
| O60 tracking | Issue #75 / PR #76（implementation history） / PR #78 merged。final head `ce92d6d04e3ca7463fdf8cfc002e271ffdc00bc3`、CI #427 PASS |
| O90 tracking | Issue #140 / #146。O90AをPR #145、bounded multidimensional sampling / linked axes / sampled envelope / feasible fraction / O40 robust ParetoをPR #149でmain反映済み。Issue #146 completionでexplicit distribution/empirical/discrete、明示weight/seed semantics、mean/percentile/probability gate、constraint-violation probability、cancel/cache/resume/stale protectionを実装。O90C以降は未実装 |
| O100 tracking | Issue #142。O100A〜O100Fは実装済み。O100GはPR #235でdescendant-aware proposal lineage + explicit As-built、PR #239でexact Measurement/Quality/Acquisition binding + measured lifecycleを部分実装。SystemVariant-specific MeasurementPlan/campaignとUX/Windows visual acceptanceは残件 |
| StandardsProfile tracking | Issue #170 / PR #192。immutable/versioned profile、criterion source/version/reference、PASS/FAIL/UNKNOWN/NOT_APPLICABLE、predicted/measured distinction、exact SceneRevision/SystemVariant/entity binding、append-only historical re-evaluation、user-defined profile、explicit hard-constraint opt-inを実装。built-inは公開sourceで境界を明示できるRP22 spatial/layout subset、Dolby 5.1.2 azimuth range、AURO-3D Rev.12 elevation/opening-angle criteria。DTS:X推定criteria、Auro source上の不整合を補正したazimuth criteria、GUI、O100D SPL/headroom objectiveは未実装 |
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
| native entry | `htdt-native` / `run-native.ps1` / `python -m htdt.native_cad` はN80a `OptimizationWorkspaceWindow` compositionを起動 |
| browser UI | 新CAD機能は凍結。native release CIからfrontend buildを除外済み。二重実装しない |
| N90 stable product head | `968a9461435ac37138ddd15526140c06613fccb8` / CI #458 PASS / Windows Release Artifact #23 PASS |
| N90 accepted gate head | `3ee2fb91b4976d7b0cac7b13718222cd6e359b76` / A15 owned-Windows PASS |
| stable version | `0.1.0` |
| R100A tracking | PR #110 merged `1714c078d4063f59da93f0d733171547f7eb486d`。CI #582 / Windows Release Artifact #109 PASS。solver-neutral authority + 10 canonical fixturesをmain反映済み |
| R100B tracking | PR #111 authority / #112 pyroom direct+first-reflection / #113 PFFDTD platform / #114 MFEM rigid reference / #115 PFFDTD rigid modes / #116 R100A-2 complex-pressure convergence / #151 PFFDTD impedance / #154 MFEM concave independent reference / #155 pyroom stochastic evidence / #158 fail-closed low-band wave adoption profile / #160 MFEM Portal continuity / #162 R100A-3 radiation authority / #177 MFEM radiation candidate gateまでmain反映済み。Portal PASS、MFEM concave FAIL、impedance extraction BLOCKED、radiation fixtureはfrozen 360 s resource budget超過でFAIL。negative evidenceを保持 |
| 次工程 | **PFFDTD exact-concave candidate evidenceはPR #181でFAIL/non-convergedまで完了。Issue #179のspatial incident/reflected decompositionはsolver-neutral authority未定義のためBLOCKEDを維持。Issue #180でR100A-4 solver-neutral finite-record `P/Q` authorityを実装し、その後MFEM transient finite-record independent referenceを別sliceで実装する。candidate traceをreference truthへ昇格しない。その後geometric obstacle、candidate-wide hard gateとADR readinessへ進む。production solverは未選定、R110は引き続きblocked。O90はO90C以降を別sliceとして進め、O100はR110 interface確定までO100C source authorityを先走らない。Issue #83 owned-room gateも未完了** |

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
