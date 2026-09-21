# R130D general-3D spatial representation / stencil / neighborhood diagnostic result — 2026-09-21

Issue: #101  
Draft PR: #298  
Task-start `main`: `9e6066259ec58c093e9a7587550ccf907f28402f`  
Frozen-plan commit: `f02a118cdfd2ed5e0a5bab6db7b5e90ff9058257`  
Frozen spatial diagnostic semantic SHA-256: `7703ca0d2b083e6b732c04d3b1ef206dc67fe5448bbd9d05dfa25d3b7f637ad4`  
Evidence-producing head: `609acd6da9ab722685f565db18e2c9ec00592c92`  
GitHub Actions run: `35545706871` / run #76  
Artifact ID: `10616239750`  
Artifact digest: `sha256:568e7620f623b99395cc76a5ba1e0cd6b7edb6a5e1a5e95f60547101c4224c0f`  
Evidence semantic SHA-256: `8c7bef610281aad9eac18ff35e2419fe98b44069c38d0f46f3ca8a691a7ef44f`  
Evidence file SHA-256: `49fb2ab861112d315a5013e4830b1756485f0f02b81b2b9499f7fecc9875fa39`

## Result

This slice kept the PR #295 canonical solver contract, PFFDTD 8/10/12 PPW series, 0.25 s record, 40/80 Hz scored frequencies, rectangular/no-taper observation, thresholds, and magnitude mask unchanged. The canonical six transfers reproduced PR #295 exactly: maximum absolute complex-component error was `0.0`.

The diagnostic states are intentionally separate:

| item | result |
| --- | --- |
| solver execution | `PASS` |
| PR #295 canonical reproduction | `PASS` |
| spatial representation trend | `SPATIAL_REPRESENTATION_MONOTONIC` |
| interpolation stencil diagnostic | `RECORDED` |
| frequency-neighborhood sensitivity | `NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS` |
| canonical MFEM self-convergence | `SELF_CONVERGENCE_FAILED` |
| canonical PFFDTD self-convergence | `SELF_CONVERGENCE_FAILED` |
| cross-solver eligibility | `CROSS_SOLVER_BLOCKED` |
| general-3D validation | `NOT_VALIDATED` |

The diagnostic classifications do not alter canonical acceptance.

## PFFDTD representation authority

The pinned PFFDTD authority is not treated as a cell-center occupancy grid. The executed Cartesian representation is node-centered and uses:

- `bn_ixyz`: boundary-node linear indices;
- `adj_bn`: per-boundary-node Cartesian nearest-neighbor adjacency/blocked-edge mask;
- source/receiver interpolation from `SimComms.get_linear_interp_weights`;
- actual persisted `cart_grid.h5`, `vox_out.h5`, and `comms_out.h5`.

The discrete air-domain estimate is the source-connected node component under the persisted blocked-adjacency graph, multiplied by `h^3`. The sloped-boundary sample is the midpoint of a blocked Cartesian node-neighbor edge whose segment intersects the exact R120B ceiling triangulation.

The exact ceiling plane used for normal-distance metrics is:

`0*x + 0.242535625036333*y + 0.970142500145332*z - 3.880570000581328 = 0`.

## Spatial trend

| PPW | h (m) | grid | discrete air volume (m³) | signed rel. volume error | abs. rel. error | sloped samples | RMS plane error (m) | max plane error (m) | RMS/h | max/h |
| ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 8 | 0.4290 | 18×18×18 | 51.872507973 | -0.073705215 | 0.073705215 | 99 | 0.106432732 | 0.186873699 | 0.248094947 | 0.435603028 |
| 10 | 0.3432 | 20×20×20 | 59.666174650 | +0.065467404 | 0.065467404 | 180 | 0.089044074 | 0.156483985 | 0.259452429 | 0.455955668 |
| 12 | 0.2860 | 22×22×22 | 56.331923648 | +0.005927208 | 0.005927208 | 252 | 0.068940203 | 0.107928353 | 0.241049662 | 0.377371864 |

Both physical metrics used by the frozen classifier improve from 10 to 12 PPW:

- absolute volume error: `0.0654674 -> 0.00592721`;
- sloped-plane physical RMS error: `0.0890441 m -> 0.0689402 m`.

Therefore the frozen classification is `SPATIAL_REPRESENTATION_MONOTONIC`. This does not prove that staircase geometry has no effect; it only says that the two predeclared physical geometry-error metrics do not worsen where the canonical PFFDTD convergence worsens.

Representation hashes:

| PPW | representation SHA-256 | cart-grid logical SHA-256 | geometry-mask logical SHA-256 |
| ---: | --- | --- | --- |
| 8 | `87a5cbf2a518b01a5fa4fabbf20ff66894026226d73d2665a36df685f168e422` | `616c8232fe335c8ff6c024b773ef1ade0e8ecf8f9c4795886f69f242a5e5a6ec` | `f64b971faede55905860b6bec414b2c46eebc12b3fabbbf99ee41bf22bcf1aee` |
| 10 | `9151d2678a4b4f3d76abbde8ed5443c0f545ee78f0d57eee99e18d459110322d` | `a06cd614a880aaa2b26190b80f0ccc05e35eda0651b8e6131d86d9a913564637` | `f5e7609debd00609668c6952ed408b69b66b9287b70f90b235e632edfed919ac` |
| 12 | `2a3b07b1e561787ba3a520a922731bc91f71ef78868e7981018b96bd0de99c08` | `45339c995ac59acfc173b91412956f9c615efc470d21448e35d7d86a5e3a1083` | `eb785a6f24ab69f39c521411218f2ce98236d067efa241df87c9e9e63d4d79fa` |

## Source / receiver trilinear stencil

All stencils reconstruct the exact requested physical coordinate with weight sum `1.0`. Maximum reconstruction error is `6.66e-16 m`, but the fractional-cell coordinates and exact stencil identities change materially with PPW.

| PPW | source fractional coordinate | source stencil SHA-256 | receiver fractional coordinate | receiver stencil SHA-256 |
| ---: | --- | --- | --- | --- |
| 8 | [0.996503497, 0.162004662, 0.162004662] | `fec54f949f37b2a3c28da1cd16cead28d74aec1b64d2eeaceb7c334f633b8393` | [0.327505828, 0.162004662, 0.162004662] | `81c9ad84677512d9c2f4f455e92277c589247e8ade8569ef2cfdc0c3c380acae` |
| 10 | [0.870629371, 0.327505828, 0.327505828] | `11829baa76b35508779facea868293dc94b4433aed0bdee6c7793ab809199bd0` | [0.784382284, 0.327505828, 0.327505828] | `60a196298d0c36afa8bdfe4ebce2dd6dacf0581a5f1009ba0f6adf34651adfa2` |
| 12 | [0.744755245, 0.493006993, 0.493006993] | `dd49f6327c1382ecb920670cecb0ef1b065e9367233bee62f6feceed57ef9f43` | [0.241258741, 0.493006993, 0.493006993] | `379272a1e9a0b5c65e42bddbcb9a0844ef399e9053f6066a15ee5278213e2290` |

This evidence records PPW-dependent interpolation phase; it does not establish it as the cause.

## Fixed frequency neighborhood

The predeclared symmetric metric was:

`d(a,b) = |H_b-H_a| / max(|H_b|, |H_a|, 1e-12)`.

| f (Hz) | d 8→10 | d 10→12 | worsening |
| ---: | ---: | ---: | --- |
| 39 | 0.423909904 | 0.914648650 | yes |
| 40 | 1.164154285 | 1.066984093 | no |
| 41 | 1.649713574 | 1.033596242 | no |
| 79 | 0.840924012 | 0.795634850 | no |
| 80 | 0.873918616 | 0.873284084 | no |
| 81 | 0.945786294 | 1.003569152 | yes |

Two of six frequencies worsen. Under the frozen classifier this is `NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS`.

That label is a predeclared classification name, not a claim that only 40/80 Hz worsen: in this metric the two worsening samples are 39 and 81 Hz. The evidence supports only the narrower statement that worsening does **not** persist over four or more of the six predeclared neighborhood frequencies.

The 39/41/79/81 Hz values remain diagnostic-only and do not enter canonical acceptance.

## Canonical state

PR #295 canonical transfers reproduced exactly at all MFEM 1/2/3 and PFFDTD 8/10/12 levels. The current canonical states therefore remain unchanged:

- MFEM self-convergence: `SELF_CONVERGENCE_FAILED`;
- PFFDTD self-convergence: `SELF_CONVERGENCE_FAILED`;
- cross solver: `CROSS_SOLVER_BLOCKED`;
- general-3D: `NOT_VALIDATED`.

No threshold, mask, PPW, scored-frequency set, observation contract, amplitude/phase fit, or geometry fit was changed.

## Next pre-fixed numerical experiment

The next experiment should isolate the PPW-dependent trilinear stencil phase while keeping the PFFDTD solver, geometry, PPW series, trilinear interpolation rule, duration, and canonical observation operator unchanged.

Predeclare a **matched half-cell source/receiver fixture** using world coordinates chosen from the frozen PFFDTD grid formula `h = 3.432 / PPW` and origin offset `-3.5h`. A coordinate equal to an integer multiple of `1.716 m` has fractional grid coordinate `0.5` for PPW 8, 10, and 12. One bounded fixture is:

- source: `(1.716, 1.716, 1.716) m`;
- receiver: `(3.432, 1.716, 1.716) m`.

Both points are inside the same exact sloped R120B polyhedron. At all three PPWs, every axis is predeclared to land at half-cell phase, so the trilinear source and receiver weights are exactly the same eight `1/8` weights while the physical world positions remain fixed **within that new experiment**.

The experiment should rerun only PPW 8/10/12, preserve the canonical 40/80 Hz finite-record scoring and current thresholds/mask, and record the same spatial metrics. It is a separate diagnostic fixture and must not reinterpret or replace the current canonical fixture. If its self-convergence trend materially changes while geometry metrics remain well behaved, that would motivate a narrower source/receiver stencil study; if it does not, interpolation phase becomes less plausible as the dominant contributor.

No execution of this proposed experiment is part of the present slice.

## Verification and provenance

Focused tests: `46 passed, 1 warning`. Runner compile-check: `PASS`.

The immutable run artifact contains the full evidence envelope, frozen parent and spatial plans, PR #286 and PR #295 baselines, artifact manifest, source/receiver node indices and weights, signed sloped-plane samples, and raw/provenance hashes.

Committed compact machine-readable evidence:

`benchmarks/acoustics/r130d_spatial_representation_diagnostic_run76_evidence.json`

HTDT-Capture diff: 0. Shared implementation/status docs diff: 0. RDC calls: 0.
