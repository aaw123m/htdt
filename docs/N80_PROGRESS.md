# N80 implementation progress

Tracking: Issue #65  
Branch: `feat/n80-optimization-workspace`  
Base main: `286f3b86a742ef6e3454d91c609ce1f84d840c57`

## 2026-09-18 — start

- N70 merged as `2ca8755b66af5521c2ed4fc98d39ce1aeb732c64`.
- N70 post-merge status sync main head is `286f3b86a742ef6e3454d91c609ce1f84d840c57`.
- CI #315 / run `35277173472` passed completely on that main head.
- Created Issue #65 and branch `feat/n80-optimization-workspace` from the green main head.

## Existing implementation findings

### O10 is already implemented

`backend/src/htdt/search_space.py` already provides:

- `search-space-grid-1` deterministic decimal grids;
- pre-generation raw candidate limits;
- hard-constraint filtering through G10;
- deterministic candidate IDs;
- raw / feasible / rejected / duplicate counts;
- deterministic `candidate_set_sha256`;
- paged candidate coordinates;
- no prediction, ranking, objective or Pareto semantics.

N80 will reuse this engine rather than reimplement it.

### Native N50 adapter is reusable

`backend/src/htdt/cad_constraints.py` already projects an exact native SceneDocument into the transient G10 Context-shaped algorithm input and builds the G10 constraint request. This adapter boundary lets N80 preserve native SceneRevision authority while reusing the existing O10 engine.

### Candidate apply infrastructure already exists

`backend/src/htdt/cad_document.py` already has `TransformEntitiesCommand`, which replaces multiple exact SceneEntity objects as one history command. N80a can use the same command semantics for candidate apply and guarantee one Undo restores all candidate-touched positions.

### Pareto/objective implementation is not present

Repository inspection found no existing O30 objective-vector / O40 Pareto implementation. N80 will not build a cosmetic Pareto UI before those algorithms exist. N80a is therefore restricted to SearchSpec authority, deterministic candidate generation, preview and explicit apply.

## N80a plan

1. Add immutable native SearchSpec model/repository beside the native scene database.
2. Bind SearchSpec to exact source SceneRevision/content hash and canonical native constraint-workspace hash.
3. Add native-to-O10 adapter using existing `scene_to_g10_context()` / G10 request generation.
4. Keep candidates derived/paged instead of persisting thousands of rows.
5. Add WorkingDocument candidate-position apply as one `TransformEntitiesCommand`.
6. Add Japanese native `最適化` workspace over N70.
7. Reuse N70 bulk marker cloud for candidate visualization.
8. Add focused tests only for immutable binding, deterministic regeneration, stale rejection and one-command apply/Undo.
9. Reserve RDC for final A13/A14 real-Windows acceptance after CI is green.

Detailed contract: [N80 design](N80_DESIGN.md).

## Authority decisions

- Native `SceneRevision` is the source authority.
- Native `CadConstraintSet` is the constraint authority.
- Legacy Context-shaped payloads are transient algorithm adapters only.
- Legacy `Store.search_specs` is not reused as native persistence authority.
- Candidate feasibility is not a quality score.
- Search candidates are not labeled recommended/optimal.
- External or scalar-field prediction remains model-gated exactly as in N70.

## 2026-09-18 — N80a owned-Windows acceptance

- Last product-code head: `c6cc15e76edbc1ac263911ee084803ca1e32b42c`.
- Product CI #321 / run `35280237062`: PASS.
- Gate-only head: `ff4dc8078eb9ca0b3effaed66b523cff175fea1a`.
- Final gate CI #323 / run `35280664154`: PASS.
- A13: stale revision, UI responsiveness, explicit cancel, document switch, and clean close/no-worker all PASS on owned Windows at 200% DPI.
- A14: SearchSpec creation, deterministic candidate generation, real-mouse candidate selection, non-authoritative preview, one-command apply, and one-Undo exact restoration all PASS.
- Hardware gate result: `N80_HARDWARE_GATE_RESULT=PASS`.
- Local checkout restored to `5ede848e8e0b0967a50c04c83ff679a649ca439b` with post-status count `0`.
- Detailed record: [N80a Windows acceptance](N80A_ACCEPTANCE_2026-09-18.md).

N80a is accepted and merged through PR #66 as `7473bb3efdbc511369c9a023b0b210eb5cde3553`. Issue #65 remains open: O20 requires a verified model contract, and O30/O40 objective/Pareto algorithms are not yet implemented.


## 2026-09-18 — O30/O40 core started (PR #70)

Branch `feat/n80-objectives-pareto` / draft PR #70 starts the algorithm-only part of N80b/c without bypassing the O20 model gate.

Commit `88f8a348a211789a2d0f5d50278853eddcbf42e6` added:

- frozen `ObjectiveMetric` / `ObjectiveVector` models with explicit minimize direction;
- explicit response evaluation band/reference band/excluded bands;
- target-response level RMS, peak excess, dip deficit and optional level-aligned shape RMS;
- pair/left-right response difference metrics;
- multi-seat pairwise difference metrics;
- total/max physical movement objectives;
- deterministic `pareto-front-1` dominance and non-dominated extraction;
- focused synthetic tests for metric separation, movement math, known Pareto fronts, equal vectors, missing objectives and duplicate candidate rejection.

Commit `0e01bd9b75c4f7886d1cc1730bafa1cd5db973fa` refined multi-seat evaluation so level-sensitive difference and level-aligned shape difference remain separate objectives rather than selecting one or the other.

O20 batch prediction is still intentionally not connected. N70 currently exposes validated geometry candidates, not a validated FR/SPL predictor suitable for batch optimization. O30/O40 pure algorithms can be verified independently with synthetic data while preserving that boundary.

No RDC is required for this algorithm-only slice. Windows real-interaction testing remains reserved for later native persistence/UI integration.


## 2026-09-18 — O30/O40 native persistence

Commit `0bbec7295936b5cf7154a14f839e1004df4039dc` added immutable native persistence for objective vectors and Pareto sets:

- each `CadObjectiveEvaluation` binds to exact document / SceneRevision / scene content hash / SearchSpec ID+SHA / candidate ID;
- evaluation spec is canonical JSON + SHA-256;
- evidence references preserve explicit `measured / derived / predicted / hypothesis` classification and source identity;
- each stored vector keeps its independent objective IDs/units/direction;
- each `CadParetoSet` stores exact objective IDs plus referenced evaluation IDs/hashes/candidate IDs;
- Pareto save re-loads all referenced immutable evaluations and recomputes the front; a mismatched/tampered Pareto result is rejected;
- native SceneRevision/SearchSpec binding mismatch is rejected before persistence.

Commit `02965d0136a330a2ef0bad2733698a22a8665e91` canonicalized evidence-ref ordering so equivalent input-reference sets produce the same evaluation identity regardless of caller ordering.

The first persistence CI exposed only a test-fixture defect: the new physical speaker fixture omitted mandatory `size_m`. Commit `911b44c647c65c53c94febf6033432dc1b0927ae` fixed the fixture using the same physical speaker dimensions as the accepted N80a tests.

CI #335 / run `35288664125` passed completely on `911b44c647c65c53c94febf6033432dc1b0927ae`, including backend tests, native launcher checks, Windows acceptance-harness compile, N60/N70/N80 gate preflights, frontend build and smoke test.

This slice is algorithm/persistence only. It does not require a new real-Windows interaction gate and does not close Issue #65. O20 batch prediction and later native Pareto UI/measurement loop remain separate work.


## 2026-09-18 — O20 transactional REW Room Simulator batch (PR #71)

PR #70 merged to main as `b4381f4b01683bba65b3857514ea583160f0eb7b`. Issue #65 remains open.

Branch `feat/n80-o20-rew-roomsim-batch` / draft PR #71 starts O20 using the already validated REW Room Simulator contract rather than inventing a new predictor.

Implemented so far:

- `rew_roomsim_batch.py`: explicit write-capable control subclass separated from the ordinary GET-only adapter;
- documented Room Simulator POST routes for head/source position only;
- full-state pre/apply/post-response/restore hashing;
- exact native room dimension and active-source coverage checks before prediction;
- fail-closed handling for concurrent REW/user state changes;
- restore of write-induced position side effects such as coupled source movement;
- no result return unless restore is exact;
- exact REW version and batch adapter version in the returned model provenance;
- `cad_roomsim.py`: native SceneRevision/SearchSpec/Candidate -> Room Simulator request adapter;
- exact shifted-rectangle local-coordinate conversion;
- explicit acoustic-reference semantics;
- rejection of non-rectangular rooms, unbound moved speakers and speakers with unknown acoustic reference.

The product does not modify Room Simulator room dimensions, absorptions, options or source configuration in this slice.

Focused tests use fake transport/state and cover success, dimension/source mismatch, documented POST routes, external change, restore failure, coupled-source side effects, shifted rectangles, acoustic-reference offsets and L-room rejection.

The existing 2026-09-16 owned-PC probe already established that beta135 can change the head by 1 cm, produce a changed FR, then restore state and raw FR exactly. PR #71 first converts that observation into a fail-closed production transaction contract. A new RDC write gate will be deferred until CI and the later native batch/persistence integration are complete, so real-machine calls stay minimal.


## 2026-09-18 — O20 transaction accepted; immutable result slice started

PR #71 was accepted after CI #345 / run `35289874654` passed completely and merged to main as `cdbd0f45f8c66b01522fcc3006b8019e2cd1ba88`.

The initial CI #344 failure was a representation bug in expected derived HTDT coordinates plus an overly broad restore-error classification. Commit `fb1ba7a90c15810a01284a68dab9245679fab347` fixed both without widening REW write authority. PR #68 was then closed unmerged as superseded by the narrower position-only contract.

Current branch `feat/n80-o20-result-persistence` / draft PR #72 continues Issue #67 with the next O20 slice:

- immutable batch specs bound to exact SceneRevision/SearchSpec/candidate-set hash;
- candidate requests frozen before any REW write;
- append-only completed/failed candidate attempts;
- exact REW/state/FR provenance for completed attempts;
- failure records never publish an FR;
- resume skips completed candidates and retries failed candidates using a new immutable attempt index;
- cancellation is checked between candidates so the active transaction always completes its restore path;
- any candidate failure halts that invocation instead of continuing writes blindly;
- completed attempt FR can be converted to the existing O30 `FrequencyResponse` input without changing its evidence class.

Focused tests cover cancel→resume, failed-attempt retry, authority binding, exact request freezing and exact Room Simulator state restoration. No RDC is used in this slice before CI is green; the writable owned-Windows gate remains consolidated with the later native integration.


## 2026-09-18 — O20 owned-Windows writable acceptance prepared

PR #72 merged to main as `df630d686f4e0c1687f05427585c0af1ae7bcf79` after final CI #350 / run `35290639089` passed completely.

Branch `feat/n80-o20-hardware-acceptance` is gate-only: no product code changes are allowed after that accepted product head.

The consolidated live gate now:

- fingerprints the installed REW version and current `/roomsim` OpenAPI subset;
- reads the exact live Room Simulator state and source-specific baseline FR;
- projects one active REW source + Main head into a temporary native SceneRevision/SearchSpec;
- generates exactly one candidate that moves Main/head X by 1 cm inside room bounds;
- executes the production `CadRoomSimBatchSpec -> CadRoomSimRepository -> run_cad_roomsim_batch()` path;
- requires a completed immutable attempt with exact live REW version and pre/restored state hash;
- requires the candidate state hash and candidate FR hash to differ from baseline;
- re-reads live Room Simulator state and FR after restore and requires both hashes to match the pre-transaction baseline exactly;
- uses only a temporary SQLite database and leaves no local acceptance data;
- the PowerShell runner refuses a dirty checkout, checks that no product code changed after `df630d68`, then restores the original checkout and verifies a clean status.

CI compiles the new Windows harness, validates all PowerShell syntax, and preflights the O20 runner before any RDC call. The actual live gate remains pending until this gate-only branch is green.


### First owned-Windows O20 gate attempt — harness cleanup failure, transaction evidence preserved

The first live invocation reached the production transaction and immutable-attempt persistence successfully, then failed in the acceptance harness while deleting its temporary SQLite database on Windows (`PermissionError [WinError 32]`). The repository checkout was restored cleanly, so this is not accepted as a gate pass.

Observed before the cleanup failure:

- owned worktree discovered at `C:\Users\ka092\Desktop\HTDT\repo`;
- original local SHA: `5ede848e8e0b0967a50c04c83ff679a649ca439b`, detached checkout; pre-status count 0;
- gate SHA: `de41e296b86f2b3e87afd8378fb81a45c8eaa89f`;
- REW: `5.40 Beta 135 API 0.9.8`;
- Room Simulator OpenAPI subset SHA256: `c75b9269233f25b9394e15fc9925e1b8d3f386be3e5edd4f19a4bfe52c8d897b`;
- 14 `/roomsim` paths observed; head/source positions expose GET/POST;
- source-specific probe source: `Left`;
- baseline full-state SHA256: `b428d4b26ba669c3c809ed297cfd11b1bcfa231bf8826242925ecfca4afeb5da`;
- baseline FR SHA256: `ca4e7159c135a16d628b5210917cae7aaca04b7dbe23a6e5246ca4881119e870`, 751 points;
- Main/head X: 2.00 m -> 2.01 m candidate;
- batch spec SHA256: `9e819b6126cf55fa260fdb519770f27a0746d51b0fa72f0e5230f560a93a93a3`;
- completed attempt SHA256: `0e6ede8b3e096707a99d00d020619f63e1b4cb4bca26d74a7f4e5700c6a1c5d1`;
- applied-state SHA256: `f89b64ef3841ccef04abefa28d56ea346ec131c33bd5e029fa9bc425288c34ff`;
- candidate FR SHA256: `12f194c45bf4a304714d0f7f983fc24364822fdf9c0e3a59085305aec8ebf0f4`;
- gate result: FAIL because temporary `acceptance.sqlite3` deletion raised WinError 32 before the independent post-restore re-read;
- local checkout restore: SHA `5ede848e8e0b0967a50c04c83ff679a649ca439b`, post-status count 0, repo restore OK.

Because a completed persisted attempt is only constructible after the production transaction has already verified exact pre/restored state equality, the transaction itself had completed its mandatory restore path. Nevertheless the live acceptance remains failed until the independent final state/FR re-read also completes.

Gate-only fix `e16688346144572d45061f33474332379184324e` moves the independent live state/FR restore verification before temporary database teardown, then explicitly drops repository/result references and runs GC before `TemporaryDirectory` cleanup. Product code remains unchanged after accepted head `df630d68`.


### Owned-Windows O20 writable acceptance — PASS

After gate-only cleanup fix and CI #354 / run `35291463956` passed completely, the consolidated live gate was rerun on the owned Windows machine and passed.

Accepted evidence:

- stale temp directory from the failed harness run was removed first and confirmed absent;
- original local SHA: `5ede848e8e0b0967a50c04c83ff679a649ca439b`, detached checkout;
- pre-status count: 0;
- gate SHA: `8a9a8d2a8895629da58171282f22abc62cf24347`;
- accepted product head pinned by gate: `df630d686f4e0c1687f05427585c0af1ae7bcf79`;
- REW: `5.40 Beta 135 API 0.9.8`;
- Room Simulator OpenAPI subset SHA256: `c75b9269233f25b9394e15fc9925e1b8d3f386be3e5edd4f19a4bfe52c8d897b`;
- 14 `/roomsim` paths observed;
- active source: `Left`;
- baseline full-state SHA256: `b428d4b26ba669c3c809ed297cfd11b1bcfa231bf8826242925ecfca4afeb5da`;
- baseline source-specific FR SHA256: `ca4e7159c135a16d628b5210917cae7aaca04b7dbe23a6e5246ca4881119e870`, 751 points;
- generated native candidate moved Main/head X from 2.00 m to 2.01 m;
- immutable batch spec SHA256: `5ecdca3e74ba519f7bd155955cca84b6e178a8e1de028b5e2d88adb6121856c2`;
- completed attempt SHA256: `0b45635544ef613b4ad29069a585f780fdfc98adb34adda2ba8c8b2046463227`;
- applied-state SHA256: `f89b64ef3841ccef04abefa28d56ea346ec131c33bd5e029fa9bc425288c34ff` (different from baseline);
- candidate FR SHA256: `12f194c45bf4a304714d0f7f983fc24364822fdf9c0e3a59085305aec8ebf0f4` (different from baseline);
- independent post-transaction state SHA256 exactly returned to `b428d4b26ba669c3c809ed297cfd11b1bcfa231bf8826242925ecfca4afeb5da`;
- independent post-transaction FR SHA256 exactly returned to `ca4e7159c135a16d628b5210917cae7aaca04b7dbe23a6e5246ca4881119e870`;
- `N80_O20_STATE_RESTORE_MATCH=True`;
- `N80_O20_FR_RESTORE_MATCH=True`;
- `N80_O20_CANDIDATE_FR_CHANGED=True`;
- `N80_O20_LIVE_GATE_RESULT=PASS`;
- `N80_O20_HARDWARE_GATE_RESULT=PASS`;
- original checkout restored to `5ede848e8e0b0967a50c04c83ff679a649ca439b`;
- post-status count: 0;
- repo restore OK.

This accepts the current O20 position-only writable path for the observed REW version/API fingerprint: native SceneRevision/SearchSpec/candidate authority -> immutable batch persistence -> live REW position transaction -> source-specific Room Simulator FR capture -> exact Room Simulator state restore -> immutable completed-attempt provenance.

No broader write authority is implied: room size, absorptions, options, source configuration and arbitrary REW state remain outside the accepted O20 write surface.


## 2026-09-18 — N80c native Pareto comparison started (PR #74)

- O20 owned-Windows writable acceptance is complete and Issue #67 is closed.
- Added the N80c authority/UI plan in `docs/N80C_PLAN.md`.
- Native Optimization workspace now opens the existing immutable objective repository for the selected SearchSpec.
- The comparison surface exposes objective IDs as independent multi-select dimensions; it does not create a scalar score or recommendation.
- Pareto recomputation uses the existing `build_pareto_set()` / `pareto-front-1` authority and persists the verified immutable `CadParetoSet`.
- Candidate rows show non-dominated/dominated status, evidence classes, and exact objective values/units.
- Selecting a comparison row reuses the established candidate selection/preview/apply path when that candidate is on the current generated page.
- Repository helper returns the latest immutable evaluation per candidate without mutating historical evaluations.

No RDC is used for this slice yet. GitHub Actions is the first verification authority; owned-Windows interaction will be consolidated only after the remaining measurement-loop/native UI work is CI-green.


## 2026-09-18 — O50 measurement-loop authority started

Roadmap O50 is now implemented at the immutable authority boundary before adding more UI:

- `CadMeasurementPlan` binds one SearchSpec/candidate/candidate-set hash to the exact SceneRevision created after the human applies that candidate.
- completing a plan accepts only `measured` evidence whose document/revision/content hash exactly matches that applied revision;
- predicted evidence is never reclassified as measured;
- completed measurement IDs convert to explicit O30 `CadObjectiveInputRef(evidence_class='measured')`;
- measurement plans are append-only rows in the native CAD database, so planned and completed history is not overwritten;
- a focused invariant test covers candidate -> applied revision binding.

This follows the roadmap safety boundary: HTDT does not autonomously move speakers, change AVR settings, or trigger REW playback. Existing N60 ingestion remains the measurement authority. O60 validation will consume these immutable links; O70 adaptive planning remains gated on O60 evidence.


## 2026-09-18 — O50 native composition + O60 holdout gate

- Native Optimization workspace can now create an immutable Measurement Plan from the selected candidate and the current saved applied SceneRevision. Dirty/unsaved working state is rejected.
- The action explicitly records a plan only; physical movement and REW measurement remain human-controlled.
- Added `CadModelValidationRecord` with explicit calibration/holdout split, prediction/measurement IDs, per-pair residual RMS, aggregate calibration/holdout RMS, model ID/version, band, algorithm version and immutable hash.
- Recommendation eligibility cannot become enabled without holdout evidence and remains disabled when holdout RMS exceeds the explicit validation threshold.
- Focused O60 tests verify that calibration fit cannot substitute for independent holdout evidence.

O70 adaptive planning is intentionally not implemented as an automatic recommendation yet: the roadmap requires real O60 holdout evidence and stability evidence, not merely the existence of the validation code. Until such evidence exists, the product remains a Pareto comparison and measurement-planning tool.


## 2026-09-18 — N80c/O50 authority tightened; O60 recommendation remains gated

Current PR: #74 / branch `feat/n80-pareto-workspace`.

- Measurement Plan now resolves the selected candidate by regenerating the exact SearchSpec candidate set. It stores the actual candidate-set SHA and rejects unknown candidate IDs.
- A plan is accepted only when the saved applied SceneRevision directly descends from the SearchSpec source revision and its content hash exactly equals the selected candidate placement applied to that source scene.
- Measurement-plan persistence independently rechecks every completed measurement ID, exact document/revision/content hash and `evidence_type=measured`.
- Native Optimization workspace now contains an O50 measurement queue. It lists latest immutable state per plan and only offers N60 measured records bound to the exact applied revision/content hash. Completion appends a new `measured` plan state; the original `planned` row remains immutable.
- Pareto refresh now fails closed for stale SearchSpec/Scene/constraint binding, mismatched objective sets, or mismatched units. Evidence provenance includes class/kind/source identity. Semantically identical Pareto snapshots are reused by SHA instead of duplicated.
- O60 `CadModelValidationRecord` binds document, SearchSpec ID+SHA, candidate-set SHA, model ID/version and immutable prediction/measurement pairs. Calibration and holdout candidates cannot overlap.
- O60 persistence cross-checks completed Room Simulator attempts against the exact batch/SearchSpec/model and requires every measured ID to be linked through a completed O50 Measurement Plan for the same candidate.
- Residual validation is stored separately as `pass/fail/insufficient`. Automatic recommendation remains `disabled` even after a residual pass until independent trend/rank, sensitivity and repeatability evidence exists. Therefore O70 remains gated.
- Added focused tests for exact candidate/revision rejection, planned→measured append-only history, Pareto semantic lookup, residual gating, and O60 cross-evidence persistence.
- RDC use for PR #74 remains zero at this point. GitHub Actions remains the primary verification authority until the product branch is green.


## 2026-09-18 — N80c/O50 owned-Windows acceptance PASS

- Accepted product head: `2a891dbc1796d3cfdaebbe762d0d6e0d2636563f`.
- Gate head: `44628a1e51c199c10b883ed8accba452578bb1eb`.
- GitHub Actions CI #397 / run `35294094501`: PASS.
- Owned-Windows 200% DPI gate: `N80C_HARDWARE_GATE_RESULT=PASS`.
- Pareto rendering/provenance, semantic snapshot de-dup, candidate apply/save, exact-revision Measurement Plan creation, exact-revision N60 measured attachment, append-only planned→measured completion, and stale Pareto rejection all PASS.
- Local checkout restored to `5ede848e8e0b0967a50c04c83ff679a649ca439b` with status count 0.
- Detailed record: [N80c/O50 Windows acceptance](N80C_ACCEPTANCE_2026-09-18.md).
- O60 trend/rank, sensitivity and repeatability continuation is tracked by Issue #75. O70 automatic recommendation remains disabled until independent real evidence passes that gate.
