# Independent pinned MFEM sparse systems for R130D

These are **deterministically gzipped, unmodified raw JSON export bytes**
from the independent pinned MFEM build of the R130D eight-vertex sloped
polyhedron. The gzip wrapper uses fixed mtime=0; decompressed bytes have
the exact SHA-256 hashes frozen in
`../r130d_fv_mfem_same_drive_plan_2026-10-09.json`.

| File | MFEM P2 refinement | Degrees of freedom | Decompressed SHA-256 |
|---|---:|---:|---|
| mfem-r1.json.gz | 1 | 125 | 758dff6aeafeaa5c902e905aa797c2e5f8250e4b81822723e968bb842187161b |
| mfem-r2.json.gz | 2 | 729 | e61410d4a69000c39975762b9986008974353953f7d8d9f100f9953a33c5ecb4 |
| mfem-r3.json.gz | 3 | 4913 | 6a43a624223fa512152059d103c4d841723fa1266ea8a22842322fb50db42511 |

**Provenance:** original `mfem/mfem` source commit
`d964264cdb9a13e94a201b6c236c7721e0c8765f`,
GitHub Actions full pinned execution
https://github.com/ka0923s-a11y/HTDT/actions/runs/37855143962,
artifact `r130d-mfem-reference-replay` id 11584137754,
artifact digest `sha256:553fcd49c3c1d1a9a2df2e277dadeda6b65533857c2406641bd2b76602d85ce1`.

The original artifact expires under GitHub Actions retention; these
compressed immutable matrices are kept in the repository for reproducible,
offline, **actual same-waveform source injection** and independent FEM/FV
numeric comparisons. Do not mutate and re-upload matrices under the same
source hashes. **This is not an approved or self-converged continuous
acoustics reference.** The finest MFEM r3 exhibits spatial error; retain
canonical `SELF_CONVERGENCE_FAILED`, `CROSS_SOLVER_BLOCKED` and `NO_GO`.
