# R100B MFEM GL2 exact-four-substep experiment — 2026-09-20

## Scope and frozen authority

Issue #101 / R100B next predeclared numerical experiment after PR #285.

Task-start `main`: `c77b8b50d7e4f9706c08e64a1daa7a7a51e8ed96`.

The only numerical variable changed from PR #285 is:

`substeps_per_output_interval = 4`

The four-substep plan was frozen before result acquisition in commit `ba596ec0144b61874fbaaf1df9e1a5094778b44d`.

Unchanged authority:

- MFEM `d964264cdb9a13e94a201b6c236c7721e0c8765f`;
- exact H1 p2 / h1, one uniform refinement, 40 elements / 525 DOFs;
- semidiscrete M/K/source/receiver identity;
- source kick and half-open 2 s finite-record `P_T/Q_T`;
- output rates 6000 / 9000 / 12000 Hz;
- scored grid 20..300 Hz at 1 Hz;
- null masks -60 / -40 dB;
- tolerances 0.75 dB / 0.05 relative magnitude / 8 deg phase;
- full-basis modal reference, normalization, Fourier/phasor convention;
- resource ceilings and candidate-wide adoption semantics.

No tolerance or mask changes, result-driven exclusions, adaptive stepping, h2 retry, source/receiver change, record-duration change, spatial refinement change, normalization change, or production-claim promotion were performed.

Authority identities:

- plan SHA-256: `7eb0e56ff92fb4a755ef4feac99570699a11a2336f91e71708158eebc37f25ce`
- spatial configuration: `908005a1f9b7aa431d7b20e9ccdb84ae58cbc77e0ab6f0f36d20fc9d52f9de49`
- semidiscrete numerical identity: `f48eb9a7fc5881fd8d2f26b32fc1f20df0f71290ecc4ed0337087073555cc61d`
- integrator configuration: `57fb31f41af617db111c649ef3ba25ced2b09e3af15f2922479760f2f922ace2`
- output-grid configuration: `4437d61b18add95c918ac7a08bd1c02bb14770189023d664b4239a807064d8e4`
- modal plan SHA-256: `15c1faf1e24ef99910912666d476ba784b7724edcb58517e3f62c40415e624de`
- mass factorization identity: `7de6afb5a1ded6155a8912f80f1b28ec7ddead61ea9b97453ea85534da8b81f3`

## Exact step authority

| output rate | output dt | internal rate | internal dt | output samples | internal steps |
|---:|---:|---:|---:|---:|---:|
| 6000 Hz | 1/6000 s | 24000 Hz | 1/24000 s | 12000 | 47996 |
| 9000 Hz | 1/9000 s | 36000 Hz | 1/36000 s | 18000 | 71996 |
| 12000 Hz | 1/12000 s | 48000 Hz | 1/48000 s | 24000 | 95996 |

Internal step counts are derived as `4 * (N - 1)` from the unchanged half-open sampling contract.

## Authoritative execution

- implementation head: `f97831f5008776f685bfa224446c2e81a2d463e6`
- Actions checkout merge SHA: `84db5ccfaf9b6ebbd44f933afe2791c8703e9277`
- dedicated workflow: `R100B MFEM GL2 Exact Four Substeps`
- run: **35517017844**, attempt 1, conclusion **success**
- artifact id: **10607427093**
- artifact digest: `sha256:462e74730a2ff079e9f619edd909f42c22223d388de522c6d3e701a56c057d76`
- deterministic report identity: `babd0923af9f54140e702734ab552f00770acc7c063c4581d2f50eb552a30369`
- persistent evidence: `benchmarks/acoustics/evidence/r100b_mfem_gl2_four_substeps_2026-09-20.json`

Command:

```text
python scripts/run_r100b_mfem_transient_experiment.py --manifest benchmarks/acoustics/r100a_manifest.json --candidates benchmarks/acoustics/r100b_candidates.json --plan benchmarks/acoustics/r100b_mfem_transient_experiment_plan.json --modal-plan benchmarks/acoustics/r100b_mfem_modal_experiment_plan.json --modal-evidence benchmarks/acoustics/evidence/r100b_mfem_modal_2026-09-20.json --mfem-root $MFEM_ROOT --executable $MFEM_PROBE_EXE --work-dir artifacts/r100b/mfem_transient/raw --output artifacts/r100b/mfem_transient/report.json --native-build-s $MFEM_BUILD_S
```

The Actions artifact contains the report plus raw transient records, recomputed modal-reference records, and the semidiscrete-system export.

## Adjacent output-rate convergence

| pair | complex RMS relative | max magnitude dB | max relative magnitude | max phase |
|---|---:|---:|---:|---:|
| 6000 -> 9000 | 0.003642950229 | 0.9272249311 | 0.1012503103 | 4.713124682 deg |
| 9000 -> 12000 | 0.001865581019 | 0.4441933258 | 0.04985402331 | 2.415542375 deg |

The adjacent complex RMS error decreases from 6000→9000 to 9000→12000, so **numerical convergence = PASS**.

For the frozen final 9000→12000 gate:

- magnitude: `0.4441933258 dB <= 0.75 dB` — PASS
- relative magnitude: `0.04985402331 <= 0.05` — PASS
- phase: `2.415542375 deg <= 8 deg` — PASS

Therefore **unchanged R100A tolerance = PASS**. The relative-magnitude gate is passed without changing the tolerance; margin is approximately `0.0001459767`.

## Modal-reference comparison

| output rate | complex RMS relative | max magnitude dB | max relative magnitude | max phase |
|---:|---:|---:|---:|---:|
| 6000 | 0.0001364589842 | 0.02093015027 | 0.002406771670 | 0.2096309360 deg |
| 9000 | 0.00002868231403 | 0.004512258499 | 0.0005193580447 | 0.03940598217 deg |
| 12000 | 0.000009337470653 | 0.001468369056 | 0.0001690379465 | 0.01207702170 deg |

All three rates satisfy the unchanged limits, so **modal-reference agreement = PASS**.

## Sparse execution and residual evidence

| rate | factorization id | LU nnz | factor storage | stepping | peak RSS | max checked residual |
|---:|---|---:|---:|---:|---:|---:|
| 6000 | `bf08622a…e98a5` | 318540 | 3.6614 MiB | 16.3269 s | 98.43 MiB | 5.051e-15 |
| 9000 | `c507b97d…84ae` | 317308 | 3.6473 MiB | 23.0427 s | 100.23 MiB | 3.808e-15 |
| 12000 | `463ce720…8e30` | 318552 | 3.6616 MiB | 31.9161 s | 100.15 MiB | 3.031e-15 |

The denominator remains 1050 x 1050 with 93,636 nnz per rate. One denominator factorization is reused for all internal substeps. Maximum checked residual remains far below the frozen `1e-10` limit.

Raw hashes:

| rate | pressure record SHA-256 | raw output SHA-256 |
|---:|---|---|
| 6000 | `39bb2a73937a13c630c3598a1eba39316477d59751c90751416c900072506606` | `d53e111e657fb9e1c682b642cb17b822b6276cffcd6c650d17038d32991fed91` |
| 9000 | `5f4e0a0d8ee1943ab52072df4f9aa4bb9ec2b55d4704052280ecdb23cc7be02a` | `2acd1a69e5efdd80cb89a9ad269e6d003d6434b7f01108ca9f7f29a0c44306a8` |
| 12000 | `bee2e8af3cf05fc478a77fb447de3cc37b0e1c9d3d1a9d1530ac8ddc681da1ef` | `217ac8044d4c5d518bece37b26584c1c3a14043b52ee6fcf40021dbb14ecd872` |

## PR #281 / #285 / four-substep comparison

Final 9000→12000 numerical accuracy:

| experiment | substeps/output | complex RMS relative | max magnitude dB | max relative magnitude | max phase | unchanged tolerance |
|---|---:|---:|---:|---:|---:|---|
| PR #281 | 1 | 0.006045915825 | 0.9721982392 | 0.1058917791 | 9.030551419 deg | FAIL |
| PR #285 | 2 | 0.002110937350 | 0.4890405283 | 0.05474719762 | 2.662964186 deg | FAIL |
| PR #289 | 4 | 0.001865581019 | 0.4441933258 | 0.04985402331 | 2.415542375 deg | PASS |

Cost:

| experiment | factorization | stepping | transient solve total | max attempt RSS | work disk |
|---|---:|---:|---:|---:|---:|
| PR #281, 1 substep | 0.03065 s | 17.98990 s | 18.02055 s | 103.125 MiB | 17.29572 MiB |
| PR #285, 2 substeps | 0.02976 s | 35.12919 s | 35.15895 s | 103.441 MiB | 17.29707 MiB |
| PR #289, 4 substeps | 0.03332 s | 71.28563 s | 71.31895 s | 100.234 MiB | 17.29702 MiB |

Four-substep stepping is **2.029x** PR #285 and **3.963x** PR #281. Disk use is effectively unchanged and observed RSS did not increase. Factorization cost remains negligible compared with stepping.

Relative to PR #285, the final-pair complex RMS improves by about 11.6%, max magnitude by 9.17%, relative magnitude by 8.94%, and phase by 9.29%. This is sufficient to cross the frozen relative-magnitude threshold, but the runtime cost roughly doubles again.

## Separated decision

- workflow execution: **PASS**
- numerical convergence: **PASS**
- modal-reference agreement: **PASS**
- unchanged R100A tolerance: **PASS**
- execution/resource suitability: **PASS**
- overall experiment: **PASS**
- candidate-wide production adoption: **NO_GO**

The four-substep experiment itself passes all of its predeclared numerical and execution gates. This does **not** select a production solver. Existing candidate-wide readiness/adoption authority remains unchanged and continues to return `NO_GO`; no manual promotion was performed.

## Validation and non-claims

For implementation head `f97831f5008776f685bfa224446c2e81a2d463e6`:

- focused R100B transient tests: **14 passed**
- dedicated numerical workflow `35517017844`: **success**
- ordinary CI `35517017855`: **success — 1091 passed, 2 skipped**
- Windows Release Artifact `35517017872`: **success**
- HTDT-Capture changes: **0**
- RDC usage: **0**

No R130D/R140/R150/R160/R170 changes and no shared `IMPLEMENTATION_ROADMAP.md` / `IMPLEMENTATION_STATUS.md` changes are included.
