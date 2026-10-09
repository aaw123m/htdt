# R130D original 8-node PFFDTD q0: finite-window partition of PPW28–44 signed spatial errors

Date 2026-10-09 JST. Issue #938 / Draft PR #1055. Original physical point source, receiver, wall and FDTD wave outputs are unchanged; this is a **post-hoc analysis ONLY with prospectively registered analysis windows**, not modified source physics or acceptance.

## Prospective scientific method

The complete analysis plan was committed as `d78734a76d084a015c545c1eaaebe22d642bb9da` [here](../benchmarks/acoustics/r130d_original_high_ppw_native_window_partition_plan_2026-10-09.json) **before inspecting any window score**. It uses exact original native eight-node PFFDTD raw `sim_outs.h5` (not the experimental 27-node Q2 source). All five PPW28/32/36/40/44 native `comms_out.h5` are pinned by preregistered SHA-256 and require undifferentiated original discrete `q[0]=1; q[n>0]=0` per eight-node source, normalized original receiver weights, original no-taper time trace, exact 0.25 s record, c=343.2 m/s, rho=1.2 kg/m³ and the original room/rigid wall boundary. Pinned upstream PFFDTD SHA `aa319f6c86517cb95aabfae8656277da62c3ead5`. Independent high-PPW original full data from [the frozen experiment](https://github.com/ka0923s-a11y/HTDT) raw SHA `9a0c7c4cf6080fdd75ce42a4e4a4a08256973b1d8c73724974073782dd9aeb34` must be present; original fully reconstituted signed complex 40/80 Hz pressures are also checked against the prior original-Q2 study's unchanged 8-node controls before any segmentation.

Physical pressure is obtained once by the original standard second-order centered `p=rho * d(phi)/dt` interior and one-sided second-order endpoints. The exact native sample timestamp is `n Ts`, with finite 0.25 s record [0,T). Predeclared **exhaustive** physical-time windows are [0,0.05), [0.05,0.15), [0.15,0.25) seconds. For both frequencies each no-taper signed complex contribution is `Σ(n in window) p[n] exp(+i 2π f n Ts)`; since the exact original q0 denominator is `Ts` and numerator `Ts * sum`, the Ts cancels. The sum of the **three signed components** must reproduce the original full-window original P_T/Q_T in every PPW/frequency, and the sum of each pair's three signed differences must reconstruct its original full signed adjacent delta. No taper, changed Q, phase alignment, arbitrary correction, new physical source, new run or silently selected frequency is allowed.

## Actual original native signed transfer error attribution

The table shows the **norm of the segment's complex two-bin spatial adjacent difference** divided by the norm of the **full original 0.25s complex two-bin spatial difference**; this ratio is diagnostic, not a pre-registered independent PASS gate and **is NOT an additive percentage**.

| Original PPW pair | Early 0–50 ms | Middle 50–150 ms | Late 150–250 ms |
|---|---:|---:|---:|
| 28→32 | 0.35059 | 1.14666 | **1.81573** |
| 32→36 | 0.45963 | 1.16024 | **1.71593** |
| 36→40 | 0.64099 | **3.25483** | **3.50534** |
| 40→44 | 0.16705 | 0.80525 | **1.42101** |

All four consecutive PPW comparisons have a late-segment difference whose complex two-bin norm exceeds that of the complete original record. The original finite-time transfer's spatial nonconvergence is **strongly coupled to continued mid/late-time modes**. On PPW36→40 the middle and late differences are over 3 times the full difference *individually*, because earlier/later signed complex contributions **cancel in the full record**. A ratio above 1 is entirely compatible with exact sum conservation; it is not "more than 100% of the error" in an additive attribution sense.

The exact native original pressure RMS values in the early/middle/late windows (the physical room is fully rigid and does not require monotonic ring-down) were:
- PPW28: 46.5251 / 51.0020 / 43.8297 Pa
- PPW32: 54.6257 / 46.8077 / 42.9500 Pa
- PPW36: 32.9039 / 31.3019 / 29.4281 Pa
- PPW40: 51.1512 / 47.9120 / 52.3554 Pa
- PPW44: 63.8895 / 54.4409 / 54.7794 Pa

[All original signed complex 40/80 Hz contributions, explicit segment sample counts, raw exact PFFDTD comms and `sim_outs.h5` SHA-256, per-grid RMS/last sample and original full-basis no-taper source/reference values, and signed coarse-to-fine complex contributions](../benchmarks/acoustics/r130d_original_high_ppw_native_window_partition_evidence_2026-10-09.json) are preserved. No original PPW or 40/80 frequency is excluded.

## Interpretation and limits

This result supports investigating finite 250ms modal interference and the high spatial modes from an exact point impulse. It **does not prove** temporal integration instability, incorrect boundary rigidity, or a unique numerical defect. A physically rigid undamped room naturally sustains reflections, so there is no expectation of simple monotone pressure decay, and the original finite-record P_T/Q_T remains the only authority for numerical qualification. Do not select only the early window to claim convergence. Previous complete native Neumann graph audit found zero adjacency asymmetry on all five original room meshes, while original pinned native quadratic physical-point interpolation **also failed** the original convergence criteria.

Original point-to-point PFFDTD with its exact original eight-node temporal impulse remains **SELF_CONVERGENCE_FAILED**. BRAS/owned-room physical validation = **NOT_VALIDATED**; product = **NO_GO**. Issue #938 OPEN, PR #1055 Draft.
