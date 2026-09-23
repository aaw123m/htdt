# R130D general-3D spatial representation diagnostic evidence — 2026-09-21

## Scope

This slice follows PR #295 and diagnoses three bounded PFFDTD variables without changing the canonical solver contract:

1. exact R120B sloped polyhedron -> actual PFFDTD node/boundary-adjacency representation,
2. actual source/receiver trilinear interpolation stencil identity over 8/10/12 PPW,
3. fixed 39/40/41/79/80/81 Hz direct-DTFT neighborhood sensitivity from the same raw records.

It does **not** change HTDT-Capture, R100B, R140, R150, R160, R170, UX, `docs/IMPLEMENTATION_ROADMAP.md`, or `docs/IMPLEMENTATION_STATUS.md`. RDC calls: 0.

## Frozen authority

- task-start main: `9e6066259ec58c093e9a7587550ccf907f28402f`
- frozen spatial diagnostic plan commit: `f02a118cdfd2ed5e0a5bab6db7b5e90ff9058257`
- frozen spatial diagnostic semantic SHA-256: `7703ca0d2b083e6b732c04d3b1ef206dc67fe5448bbd9d05dfa25d3b7f637ad4`
- parent general-3D semantic SHA-256: `5c753073a88d6705ee2962aa387006d4c563f90f0249b722dcf309a8155f62ec`
- parent PR #295 target-window semantic SHA-256: `ff42a7e0c44ed4726ea34edfa2549d018d66a37181786df18bbd4cf59c61d0db`
- PFFDTD source commit: `aa319f6c86517cb95aabfae8656277da62c3ead5`
- PPW: 8 / 10 / 12 only
- duration: 0.25 s
- canonical scored frequencies: 40 / 80 Hz only
- diagnostic frequencies: 39 / 40 / 41 / 79 / 80 / 81 Hz
- canonical rectangular finite-record observation, thresholds, and -50 dB magnitude mask unchanged.

The actual pinned PFFDTD representation is treated as a node-centered Cartesian grid with boundary-node indices `bn_ixyz` and per-boundary-node blocked-neighbor adjacency `adj_bn`. This slice does not reinterpret it as voxel-cell-center occupancy.

## Authoritative run

- workflow: **R130D Independent General-3D Validation**
- run id: `35545706871`
- run number: `76`
- evidence-producing commit: `609acd6da9ab722685f565db18e2c9ec00592c92`
- artifact id: `10616239750`
- artifact digest SHA-256: `568e7620f623b99395cc76a5ba1e0cd6b7edb6a5e1a5e95f60547101c4224c0f`
- evidence semantic SHA-256: `8c7bef610281aad9eac18ff35e2419fe98b44069c38d0f46f3ca8a691a7ef44f`
- evidence file SHA-256: `49fb2ab861112d315a5013e4830b1756485f0f02b81b2b9499f7fecc9875fa39`
- focused tests: **46 passed, 1 warning**
- runner compile check: PASS
- repository CI: PASS
- Windows Release Artifact: PASS

Committed compact evidence:
`benchmarks/acoustics/r130d_spatial_representation_diagnostic_run76_summary.json`

The uploaded artifact additionally contains the full signed sloped-boundary distance arrays, frozen plans, PR #286/PR #295 baselines, exact evidence envelope, artifact manifest, and workflow summary.

## Canonical reproduction

PR #295 canonical 40/80 Hz transfers reproduced exactly at all six levels:

- MFEM refinements 1 / 2 / 3: max complex-component error 0.0
- PFFDTD 8 / 10 / 12 PPW: max complex-component error 0.0
- all-six-level maximum: **0.0**, tolerance `1e-9`

Therefore the diagnostic instrumentation did not change the canonical result.

Canonical PFFDTD adjacent complex-RMS remains:

- 8 -> 10 PPW: `0.8761219168261092`
- 10 -> 12 PPW: `3.171427115345244`

The canonical self-convergence state remains failed.

## Spatial representation evidence

The exact polyhedron volume is 56.0 m^3. The discrete air-domain estimate is defined by the frozen plan as the source-connected node component times `h^3`, respecting actual blocked-neighbor adjacency.

| PPW | h [m] | grid | air nodes | boundary nodes | discrete volume [m^3] | relative volume error | sloped RMS [m] | sloped max [m] | RMS/h |
| ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 8 | 0.4290 | 18 x 18 x 18 | 657 | 356 | 51.872507973 | -0.0737052148 | 0.1064327321 | 0.1868736991 | 0.2480949467 |
| 10 | 0.3432 | 20 x 20 x 20 | 1476 | 656 | 59.6661746504 | +0.0654674045 | 0.0890440737 | 0.1564839853 | 0.2594524292 |
| 12 | 0.2860 | 22 x 22 x 22 | 2408 | 932 | 56.3319236480 | +0.0059272080 | 0.0689402033 | 0.1079283531 | 0.2410496620 |

Frozen physical-error classification:

`SPATIAL_REPRESENTATION_MONOTONIC`

Both classification metrics improve from 10 -> 12 PPW:

- absolute relative volume error: 6.5467% -> 0.5927%
- sloped-plane physical RMS normal-distance error: 0.08904 m -> 0.06894 m

`error/h` is recorded only as diagnostic metadata. Its 8/10/12 RMS values are 0.2481 / 0.2595 / 0.2410 and are not substituted for the physical-metre classification metric.

Geometry identities are distinct at every PPW, as expected for a different grid spacing:

- 8 PPW representation: `87a5cbf2a518b01a5fa4fabbf20ff66894026226d73d2665a36df685f168e422`
- 10 PPW representation: `9151d2678a4b4f3d76abbde8ed5443c0f545ee78f0d57eee99e18d459110322d`
- 12 PPW representation: `2a3b07b1e561787ba3a520a922731bc91f71ef78868e7981018b96bd0de99c08`

This evidence does not support a claim that worsening 10 -> 12 physical staircase error is the immediate explanation for the canonical 10 -> 12 transfer worsening.

## Source / receiver stencil evidence

All stencils are the actual pinned PFFDTD eight-node trilinear authority. No position snapping or nearest-node substitution was performed.

| PPW | source fractional coordinate | source hash | receiver fractional coordinate | receiver hash |
| ---: | --- | --- | --- | --- |
| 8 | [0.99650350, 0.16200466, 0.16200466] | `fec54f949f37b2a3c28da1cd16cead28d74aec1b64d2eeaceb7c334f633b8393` | [0.32750583, 0.16200466, 0.16200466] | `81c9ad84677512d9c2f4f455e92277c589247e8ade8569ef2cfdc0c3c380acae` |
| 10 | [0.87062937, 0.32750583, 0.32750583] | `11829baa76b35508779facea868293dc94b4433aed0bdee6c7793ab809199bd0` | [0.78438228, 0.32750583, 0.32750583] | `60a196298d0c36afa8bdfe4ebce2dd6dacf0581a5f1009ba0f6adf34651adfa2` |
| 12 | [0.74475524, 0.49300699, 0.49300699] | `dd49f6327c1382ecb920670cecb0ef1b065e9367233bee62f6feceed57ef9f43` | [0.24125874, 0.49300699, 0.49300699] | `379272a1e9a0b5c65e42bddbcb9a0844ef399e9053f6066a15ee5278213e2290` |

For every source and receiver stencil:

- weight sum = 1.0
- reconstructed physical coordinate equals the requested coordinate to floating-point tolerance
- maximum recorded reconstruction error = `6.661338147750939e-16 m`

Thus stencil identity and fractional grid phase are PPW-dependent, while physical source/receiver coordinates remain unchanged. This is recorded as a sensitivity candidate, not a proved cause.

## Fixed frequency-neighborhood evidence

The frozen symmetric metric is:

`|H_b - H_a| / max(|H_b|, |H_a|, 1e-12)`

| Hz | d_8_10 | d_10_12 | worsening? |
| ---: | ---: | ---: | --- |
| 39 | 0.4239099040 | 0.9146486504 | yes |
| 40 | 1.1641542852 | 1.0669840926 | no |
| 41 | 1.6497135736 | 1.0335962422 | no |
| 79 | 0.8409240122 | 0.7956348502 | no |
| 80 | 0.8739186164 | 0.8732840836 | no |
| 81 | 0.9457862940 | 1.0035691523 | yes |

Worsening count: **2 / 6**.

Frozen classification:

`NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS`

Important interpretation constraint: this label is the predeclared 0-2-worsening classifier name. In the actual data, the two worsening frequencies are **39 Hz and 81 Hz**. The canonical 40 Hz and 80 Hz points do **not** worsen under this separate symmetric per-frequency diagnostic metric. This does not supersede or reinterpret the canonical two-frequency complex-RMS failure.

## Raw/provenance identities

### 8 PPW

- result: `059b3c18a9aaf456dcb309d5e772a775fec5d0e2a0f444effe20695e3b69e4cd`
- execution input: `b0507ee2382a5c25272a8198fe77b8624d6336975011a4c5925884f16433b7dd`
- solver geometry: `e3f9b6b993ab6e40dd75a8d3f4844630c3498c94c0218ee7ad556bc6d60774ac`
- executed grid: `7652cb221a48b0209360ea12b579759ff1f0c57dc5a382742b1fec38a8af4faf`
- `sim_outs.h5`: `aaad4bef1aca76f92397eba679bbea83055707ca20b550cf9af23638c66f07c8`
- `comms_out.h5`: `ad3b1595d9d37d25c18de874dabf7bae8883f252888127d1ea12966c6aa1cfdc`

### 10 PPW

- result: `70d4de1bbd85146ba21de5fd82360ac01c2cdd252d9e635d785d0446f2a8dc2b`
- execution input: `71fecfe76a4c7fbf2526889eca28fe6c72209c03f6138666ab384d9507cda7e7`
- solver geometry: `af2e73de7f8fe4da44e7edd599f2bca5b54c5cc4e0633fc48d8092dec5969d79`
- executed grid: `cd14dac76857f22fa9366d19280a137cc064142a0e75c6c7a14ee97a562a60c9`
- `sim_outs.h5`: `bdde68b2832fa36f06c187db86b252f2b55b7715f17457c19b1322b3527c24ea`
- `comms_out.h5`: `0a1d77944147e250a418e436269058ddc9a0254d02b1afdd8a3c7b1ec7094f68`

### 12 PPW

- result: `d99be02249f34be41c1e414bb60fdd9491f7b004dee9061a5d51272ddb1748f5`
- execution input: `7585565697dc6680c966cc9fb90a19dc25402f1b3bf6b12654f925014fdab446`
- solver geometry: `ba20a46c43a28b110047a382d221f66f9c639a5d8053cf8f972c18052e0fdb8a`
- executed grid: `ffd8914bae95373e5825a767725f03fdb9f7e09e4d043ad2a766e23d5fdbd23f`
- `sim_outs.h5`: `27abba41a02e51bc0871485f054d17e3bff508a3574badffb988431bf6b39d2c`
- `comms_out.h5`: `853229fa8b8e9c9d7f4c5403312804246d6f952265a211de0c47a30248046cb2`

The compact JSON records pressure/source trace hashes as well.

## Decision semantics

The diagnostic findings remain separate from canonical validation:

- canonical solver execution: PASS
- canonical PR #295 reproduction: PASS
- spatial representation trend: `SPATIAL_REPRESENTATION_MONOTONIC`
- interpolation-stencil diagnostic: RECORDED, PPW-dependent identity
- frequency-neighborhood sensitivity: `NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS`
- canonical MFEM self-convergence: `SELF_CONVERGENCE_FAILED`
- canonical PFFDTD self-convergence: `SELF_CONVERGENCE_FAILED`
- cross-solver eligibility: `CROSS_SOLVER_BLOCKED`
- general-3D validation: `NOT_VALIDATED`

No threshold, PPW series, canonical observable, or validation state is changed by this slice.

## Interpretation

This slice narrows the next investigation but does not prove cause.

The 10 -> 12 physical geometry metrics both improve, so the measured physical staircase/volume errors do not exhibit the same worsening direction as canonical PFFDTD self-convergence. Source and receiver interpolation stencils do change materially with PPW while reconstructing the exact same coordinates. Separately, the frozen six-frequency symmetric diagnostic shows worsening at only 39 and 81 Hz rather than broadly throughout both neighborhoods.

These facts leave local transfer-shape / mode-bin sensitivity and PPW-dependent stencil phase as candidates. They do not establish either as causal.

## One next predeclared numerical experiment

Predeclare **one fixed dense frequency-neighborhood direct-DTFT experiment** using the already persisted 8/10/12 raw PFFDTD records, with no new solver execution and no canonical change.

Freeze before reading the dense-sweep result:

- frequencies: **36.0 through 44.0 Hz inclusive at 0.5 Hz spacing**, and **76.0 through 84.0 Hz inclusive at 0.5 Hz spacing**
- same raw pressure/source records from run #76
- same native finite-record direct-DTFT operator
- same symmetric difference `|H_b-H_a| / max(|H_b|, |H_a|, 1e-12)`
- same 8->10 and 10->12 pairing
- evaluate the full predeclared grid; no post-result frequency selection
- 40/80 Hz remain the only canonical scored frequencies
- no threshold/mask/window/source/receiver/geometry/PPW change

Rationale: the physical geometry errors are monotone while the six-point symmetric diagnostic worsens at only two neighboring frequencies. A fixed sweep spanning one nominal `1/T = 4 Hz` record-resolution neighborhood on each side of 40/80 is the lowest-confound next experiment for distinguishing a narrow transfer-shape/mode-bin effect before changing source/receiver interpolation semantics.
