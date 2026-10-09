# R130D original PFFDTD rigid-wall Neumann graph audit — no symmetry defect found

Date: 2026-10-09 JST; Issue #938, PR #1055 Draft. This is an original PFFDTD **boundary solver investigation**, not a changed-source physical model or a product qualification.

## Pre-observation contract

[The prospective immutable plan](../benchmarks/acoustics/r130d_original_pffdtd_neumann_graph_plan_2026-10-09.json) was committed as `613cd38461523a7742045d690b675aaee5d10f8a` **before any observed graph metrics**. The study reads the five original sloped rigid-room PFFDTD `vox_out.h5` assets at PPW28,32,36,40,44, using each exact original full-file SHA-256 already preserved in the earlier actual PFFDTD 5-grid 27-node physical point experiment. Upstream source remains pinned `aa319f6c86517cb95aabfae8656277da62c3ead5`, with no modified numerical update kernels. The original 8-node point impulse and its self-convergence FAIL are not recalculated, requalified or suppressed.

The original PFFDTD source code `python/fdtd/sim_fdtd.py` uses `nb_stencil_air_cart` for non-boundary nodes, a 7-point interior stencil with diagonal −6 and six nearest Cartesian neighbors +1. It uses `nb_stencil_bn_cart` on rigid boundary nodes, with diagonal negative the sum of six `adj_bn` neighbor flags and +1 per allowed direction. The independent graph analysis implements the exact 6-neighbor flags, following the original PFFDTD C-ordered `Nx,Ny,Nz` strides. It identifies the source-connected room graph without connecting outside ghost/halo nodes to separate domains and records both directed-edge reciprocity and constant-field row sums.

It separately invokes the **genuine upstream pinned Numba** `nb_flip_halos`, `nb_stencil_air_cart` and `nb_stencil_bn_cart` on the unmodified original boundary masks and independently evaluates up to 10,000 original Cartesian rows. For fixed-seed random fields `u` and `v` it computes the bilinear skew `abs(uᵀLv−vᵀLu) / max(abs(uᵀLv),abs(vᵀLu),1)`, with all source-connected room nodes included. This distinguishes an observed operator defect from mere graph topology assumptions.

## Observations: all native PPW28–44 room graphs

| Original native PPW | Source-connected room nodes | Reachable rigid boundary nodes | Directed interior connections | Missing reverse connections | Pinned Numba vs independent row max | Relative bilinear skew |
|---:|---:|---:|---:|---:|---:|---:|
| 28 | 31,185 | 5,610 | 180,864 | **0** | 7.11e−15 | 2.37e−15 |
| 32 | 44,659 | 7,139 | 260,064 | **0** | 7.11e−15 | 1.66e−16 |
| 36 | 64,848 | 9,248 | 378,944 | **0** | 7.11e−15 | 1.84e−16 |
| 40 | 89,723 | 11,513 | 525,778 | **0** | 7.11e−15 | 5.97e−16 |
| 44 | 116,739 | 13,790 | 685,452 | **0** | 7.11e−15 | 1.33e−16 |

All **constant-field native row sums are exactly zero**, and no point in the connected original rigid room graph violates reciprocal directed graph adjacency. No original source support overlaps ABC or ghost nodes in these five cases, according to actual graph data. The native stencil agrees with independent evaluation to floating-point roundoff, and both tested random fields show no meaningful relative bilinear skew.

[The entire full-grid audit evidence](../benchmarks/acoustics/r130d_original_pffdtd_neumann_graph_evidence_2026-10-09.json) preserves all original geometry file SHA-256, sample mask/room graph counts, exact source nearest grid node, boundary node population, all zero or nonzero symmetry exceptions, constant row sums, actual native Numba probe errors, bilinear skew, and product `NO_GO`. A synthetic intentionally one-sided boundary adjacency is tested to verify the diagnostic detects asymmetry, rather than trivially declaring all graphs symmetric.

## Interpretation and remaining work

**No algebraic boundary-graph asymmetry or Numba-vs-independent boundary-kernel defect was found** in the exact original five PPW28–44 room boundary data. This is a negative finding: do not make a boundary self-adjointness fix not indicated by evidence. It does **not** verify that the staircase room geometry approximates the analytic rigid sloped wall accurately, nor whether original broad-band point source excites underresolved high-order numerical eigenmodes, or whether original finite 0.25s pressure record amplifies native-mode shifts. Those are separate remaining issues.

This graph audit cannot qualify original PPW8/10/12, nonconvex CAD, BRAS/owned-room physical measurements or production. **Original original-source fullband SELF_CONVERGENCE_FAILED; point candidate NOT_QUALIFIED; physical NOT_VALIDATED; product NO_GO. Issue #938 OPEN and PR #1055 Draft.**
