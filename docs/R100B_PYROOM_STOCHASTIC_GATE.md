# R100B pyroomacoustics stochastic seed / convergence gate

Issue #153 extends the merged pyroomacoustics direct/first-reflection reference with stochastic evidence. It does not select a production solver and does not finalize the R100B ADR.

## Frozen authorities

- R100A-2 fixture: `geometric-seed-repeatability-v1`.
- Candidate: `pyroomacoustics-v0.10.1-f02b01d`.
- Upstream source: tag `v0.10.1`, commit `f02b01dd6609709e2089aefa5d1e59c91d3a0601`.
- R100B stochastic controls: `benchmarks/acoustics/r100b_pyroom_stochastic_authority.json`.
- Exact R100A manifest hash, exact fixture hash, candidate manifest hash and stochastic-authority hash are embedded in every raw-evidence envelope. A stale hash is rejected rather than promoted to current evidence.

The R100A-2 fixture itself is unchanged. Its source, receiver, room, material bands, sound speed/density, frequency grid, timing convention, normalization, random seed, resource budget and observable tolerance remain the authority.

## Pre-registered stochastic controls

The same-seed replay uses the R100A seed `20260918` twice at the finest refinement level. Both the selected raw histogram SHA-256 and the extracted decay curve must be exactly identical. This is **repeatability only**, not statistical convergence.

Independent-seed evidence uses `20260919`, `20260920`, `20260921`, and `20260922` at every refinement level:

| Level | Rays | Receiver radius | Histogram bin |
|---|---:|---:|---:|
| coarse | 8,192 | 0.60 m | 8 ms |
| medium | 32,768 | 0.45 m | 4 ms |
| fine | 131,072 | 0.30 m | 2 ms |

The estimator is normalized Schroeder cumulative ray-energy decay. The common observable grid is 500/1000/2000 Hz at 40, 80, 120, 160, 200, 240, 280 and 320 ms. All times align exactly to every histogram-bin level. Air absorption is disabled because the frozen geometric fixture does not authorize an additional atmospheric-loss model; room sound speed is explicitly overridden to the R100A value.

A required sample with no positive cumulative ray energy is `insufficient_support`. It is retained as non-converged evidence and is never converted to a zero response.

## Convergence criterion

For each refinement level, the four independent-seed decay curves are retained individually. The evaluator then computes the per-sample independent-seed mean and sample standard deviation (`ddof=1`).

Statistical convergence requires all of the following, fixed before the numerical run:

1. coarse→medium and medium→fine RMS changes of the independent-seed mean curve decrease strictly;
2. the final medium→fine absolute RMS change satisfies the frozen R100A absolute tolerance;
3. the final relative RMS change satisfies the frozen R100A relative tolerance;
4. the maximum pointwise seed-to-seed standard deviation at the fine level satisfies the frozen R100A statistical standard-deviation tolerance.

The central R100B observable reports the maximum of replay and convergence error metrics, and the existing central R100B validator applies the R100A tolerance without relaxation. Same-seed identity cannot make a non-converged budget sequence pass.

## Raw and resource evidence

The dedicated workflow stores:

- complete JSON authority/provenance and evaluator result;
- selected-band raw ray histograms in a compressed NPZ archive;
- per-observation seed, ray budget, receiver radius, histogram bin, histogram digest and support state;
- per-observation setup, solve, postprocess and peak-RSS measurements;
- aggregate setup/solve/postprocess/RAM/output evidence passed into `BakeoffFixtureEvidence`;
- official wheel filename/SHA-256 and exact Python package versions;
- candidate source ref/commit;
- standard `BakeoffRun` output validated by the central R100B evaluator.

The workflow outcome and candidate outcome are printed separately. A workflow PASS therefore does not imply stochastic candidate PASS.

## Numerical result

Authoritative dedicated workflow: Actions run `35414892047` (`R100B Pyroomacoustics Stochastic Gate`), conclusion **success**. Artifact `r100b-pyroomacoustics-stochastic` has artifact id `10575133493` and digest `sha256:249f4802be6534c4f1fea707271a2aaad28f3ea8df35694655f523b841a97c0c`.

The workflow result and candidate result are intentionally different:

- workflow/harness: **PASS**;
- candidate fixture `geometric-seed-repeatability-v1`: **FAIL / non-converged**;
- central `BakeoffRun` id: `pyroom-stochastic-35414892047`;
- R100A semantic hash: `97d9ff8f4225569f0193d2f4d5e4a3ae011d4c0a67fb612d525eae2287101aae`;
- frozen fixture semantic hash: `2f542b9293812bdf5ca92d39bafb228c201a69a8e020754df313fc02888d9027`;
- stochastic-authority semantic hash: `fc77691e12cab20f6d06cae2ccc9eab8bff83faf232e09bfe8973576fe00c80d`;
- candidate manifest hash: `58314d89ad7a0e462ff49fde67f53d4b235afd920fe5e4b4f651c3cd2684b764`;
- HTDT PR head: `5df4242edb68e90a176428d1bb01e2253e71a0c1`;
- GitHub pull-request checkout commit: `bb263ac` synthetic merge of that PR head into main `cb6d15b0...`;
- the artifact binds SHA-256 identities for the probe script, stochastic evaluator source, and stochastic-authority file;
- raw NPZ SHA-256: `114c1d08ce54e3e291946701a3b10ed9471e4fa80698706d3effeb632d519172`.

### Same-seed replay

The two finest-budget runs using seed `20260918` produced the same selected-band raw histogram SHA-256, `460ab2c20e8c11fa6ddd8524048795bbac11ad7232bba151b93f597b99e3d065`. Therefore `exact_histogram_replay=true`. Both runs, however, reached `insufficient_support` at 500 Hz / 240 ms, so no decay-curve value was invented and `exact_curve_replay=false`; the evaluator consequently leaves the curve-level `repeatability_status=fail`. This raw replay identity is not called statistical convergence.

### Independent seeds and convergence

Seeds `20260919`, `20260920`, `20260921`, and `20260922` are preserved independently at every budget. At 8,192 rays all 4/4 observations were supported and the maximum pointwise seed standard deviation was `0.1272795361 dB`. At 32,768 rays all 4/4 observations were supported and the maximum pointwise seed standard deviation was `0.0721816652 dB`. At 131,072 rays all 4/4 independent-seed observations were `insufficient_support` at 500 Hz / 240 ms, so the pre-registered medium→fine convergence criterion could not be evaluated and no zero response was substituted.

Because the fine level is unsupported, the evaluator records `convergence_status=fail`, no final absolute/relative RMS delta, and no finest-budget standard deviation. The coarse and medium seed-to-seed variation remains in `budget_variation`; unavailable fine-level statistics are not fabricated. No tolerance or fixture setting was changed after observing the result.

### Resource and dependency evidence

Environment setup/install time was `17.095633 s`. Aggregate numerical evidence was setup/compile `0.0147098 s`, solve `17.0295824 s`, postprocess `0.0042547 s`, peak RAM `129.41796875 MB`, raw archive/output `0.0115881 MB`. Independent-seed mean solve time scaled from `0.155511175 s` at 8,192 rays to `0.602541125 s` at 32,768 and `2.3386152 s` at 131,072; the fine level had 0/4 supported decay estimates.

The executed wheel was `pyroomacoustics-0.10.1-cp312-cp312-win_amd64.whl` with SHA-256 `421fa320b6ad31465dc59e137a7b0e1033687cb821febcc8cb4d76f67a4c7b57`. Recorded dependencies were `pyroomacoustics==0.10.1`, `numpy==2.5.3`, `scipy==1.18.1`, and `psutil==7.2.2`; the candidate source commit remains `f02b01dd6609709e2089aefa5d1e59c91d3a0601`.

### Remaining boundary

This FAIL is valid R100B bakeoff evidence. It does not justify relaxing the frozen tolerance, changing the fixture to suit pyroomacoustics, or selecting/rejecting a production solver by itself. Candidate-wide physics correctness remains open, and R100B solver selection / ADR remains outside this slice. MFEM, PFFDTD, R110/R150, O90/O100 and GUI scope are unchanged.
