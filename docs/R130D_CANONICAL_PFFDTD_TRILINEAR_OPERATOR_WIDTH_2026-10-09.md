# Canonical PFFDTD PPW8–12 native source/receiver effective widths

Date: 2026-10-09 JST | Issue #938 | Draft PR #1055 | No solver changes.

## Purpose and preregistration

This diagnostic analyzes the **ORIGINAL PFFDTD eight-node trilinear point-source and point-receiver operators**, rather than the experimental spatial Gaussian FV/MFEM variants. It reads only the frozen [run76 canonical spatial-representation summary](../benchmarks/acoustics/r130d_spatial_representation_diagnostic_run76_summary.json) (raw SHA-256 `c95ad31aeedae3138a3141763c26cb2a03a44a9069aeabd2793e61cdb9de26d4`), without executing, postprocessing, tapering, changing the source, or modifying the original PFFDTD adapter. The analysis definition was committed **before calculation** in [the prospective plan](../benchmarks/acoustics/r130d_pffdtd_trilinear_effective_width_plan_2026-10-09.json), commit `51fc8c37d9e8c4dc4f89c6ee13bf3a54d06925cc`.

For a trilinearly interpolated native eight-node point functional with grid spacing h and fractional cell coordinates \(f_x,f_y,f_z\), the normalized weights form independent Bernoulli distributions on each grid axis. The operator's **physical second central moment** around the exact source/receiver location is \(V_i=h^2f_i(1-f_i)\) on each axis, and its 3D RMS radius is \(R_{RMS}=\sqrt{V_x+V_y+V_z}\). This is an *effective second-moment radius of a discrete grid operator*, not an acoustic transducer physical width, acoustic pressure smoothing, or continuum Gaussian shape. Each original 8-element weight array was checked as a permutation of the exact trilinear tensor product; original coordinate reconstruction and sum(weights)=1 were verified. Both source and receiver, all PPW8/10/12, and nonmonotonic results are retained.

## Results from the original PFFDTD run76 records

| Original PPW | Physical grid spacing (m) | Source RMS radius (m) | Receiver RMS radius (m) | Discrete air volume relative error |
|---|---:|---:|---:|---:|
| 8 | 0.4290 | 0.224971 | 0.300840 | −7.3705% |
| 10 | 0.3432 | 0.255246 | 0.267964 | +6.5467% |
| 12 | 0.2860 | 0.237569 | 0.236354 | +0.5927% |

The **source's effective spatial RMS radius increases from PPW8→10** despite smaller grid spacing, then decreases PPW10→12. The receiver radius decreases at both steps. The geometry air volume error changes sign from PPW8 to 10 and improves greatly by PPW12; this means changing the PFFDTD spatial grid alters several coupled factors, **not just the source/receiver effective width**.

Full measured coordinate fractions, eight-node shape provenance SHA-256, per-axis second moments, Gaussian reference widths (for dimensional context **only**), and trend labels are retained in [this immutable evidence](../benchmarks/acoustics/r130d_pffdtd_trilinear_effective_width_evidence_2026-10-09.json). The previous one-sided regularization FV/P2 experiment independently found material frequency-transfer sensitivity to spatial source/receiver widths, consistent with prioritizing this diagnostic.

## Scientific interpretation and limits

The original PFFDTD point-source/receiver interpolation has an **effective grid-dependent and nonmonotone physical width**, even though both positions are reconstructed exactly. This is a tangible and testable potential source of grid-sensitive high spatial modal excitation and observation. However, the observed PPW8→12 radius behavior **cannot by itself establish** which part caused the original 40/80 Hz finite-record error or the entire broadband PFFDTD run25/run76 failure. The grid geometry representation and source/receiver spectral coupling also change; a controlled PFFDTD redesign would be needed to isolate them.

The unchanged canonical point/point impulse is **SELF_CONVERGENCE_FAILED**, reference point candidate **NOT_QUALIFIED**, BRAS/owned-room physical validation **NOT_VALIDATED**, product **NO_GO**. Issue #938 remains OPEN, PR #1055 Draft; no numerical acceptance thresholds have changed, and the Gaussian source is **not** an approved replacement for the original point source.
