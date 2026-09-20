# R130D independent general-3D validation — executed evidence

Date: 2026-09-20
Issue: #101
Draft PR: #282

## Authoritative run

- workflow: `R130D Independent General-3D Validation`
- run: `35508831838` / run #7
- numerical head: `cdd4977e2a3c03af9cc802b5bef32982f01585ac`
- workflow conclusion: `success`
- evidence artifact id: `10604704433`
- artifact ZIP SHA-256: `0ed919823a82e6f042fba51e98b76c292e8c07bc3ef6b37081761020003c00af`
- evidence-envelope semantic SHA-256: `be36811c713365846bbd1af978cc5e66676da7e994622953ee015a42bd8fcc04`

Workflow success is execution/integrity success only. The physics result below is independently evaluated and is **FAIL / NOT_VALIDATED**.

## Result

- reference build/execution: **PASS**
- MFEM reference self-convergence: **FAIL**
- PFFDTD self-convergence: **FAIL**
- fine/fine cross-solver agreement: **BLOCKED** by the two self-convergence failures
- bounded sloped fixture validation: **FAIL**
- R130D general-3D validation state: **NOT_VALIDATED**

No acceptance threshold or refinement schedule was changed after seeing this result.

## Independent reference

Reference implementation: `mfem/mfem@d964264cdb9a13e94a201b6c236c7721e0c8765f`.

The reference uses the independently authored audited six-tetrahedron base mesh, continuous H1 order 2, natural Neumann rigid boundary, MFEM mass/diffusion assembly, point source/receiver functionals, and a full generalized modal basis. PFFDTD voxelization, triangle/box intersection, and Cartesian-grid construction are not reused.

Reference refinement identities:

| refinement | tetrahedra | DOFs | mesh SHA-256 | transfer SHA-256 |
| ---: | ---: | ---: | --- | --- |
| 0 | 6 | 27 | `24dee00bb27b75d6547358119cb1b5bcd46e7404e8ba0dad1438a61e7fdc8037` | `e1c5319f15b0b62692395d4db58f1a690fa09eb8b77a0dfc8321f36a0c044190` |
| 1 | 48 | 125 | `6c7ce68a6e5bea482129b8aed7f148db6f655ee1b5d2bef9bad7a8bdbeba2c58` | `c1eaf55a486c0ba9bceb6361e1a3f54022e841c5573001f64b04082e3e3147d3` |
| 2 | 384 | 729 | `fd49cf4612207c2a55b40e3181bd722db259a02503277a24ab47d353fa096880` | `f30396e10e9b3426acbc844031605beb5d64229555b56c6c53e2c8479a203bc7` |

All three generalized eigen solves met the predeclared algebraic quality checks; the fine reference used 384 tetrahedra / 729 DOFs and remained well inside the memory ceiling. The failure is numerical self-convergence of the compared transfer quantity, not an MFEM build or linear-algebra failure.

MFEM medium -> fine metrics:

- complex RMS relative: `0.7958360401033702` (limit `0.05`)
- maximum magnitude relative: `1.205274869586647` (limit `0.08`)
- maximum magnitude difference: `6.869254569092059 dB`
- maximum phase error: `51.9111731102351 deg` (limit `5 deg`)

Therefore the reference is not converged under the frozen schedule.

## PFFDTD refinement

Candidate implementation: `bsxfun/pffdtd@aa319f6c86517cb95aabfae8656277da62c3ead5`.

| PPW | grid | spacing | executed-grid SHA-256 | transfer SHA-256 |
| ---: | --- | ---: | --- | --- |
| 6 | `15x15x15` | `0.572 m` | `c194f82ee8ac379cf003b2b751b551a3c26068c962c2c7c8a034ddfdfa297b33` | `b0e3a9f0d1348634539b6b4b445b4b2b735cff5cac7290bf78f3876866efdb46` |
| 8 | `18x18x18` | `0.429 m` | `f7cb3fb05469a507ccc7b207dc2a5fc7a393abcfa07f641fdf7e95c339b26a53` | `a7f6a7b0fb462b46864daf77cc229841762ae58fb957396f005fb950105197ed` |
| 10 | `20x20x20` | `0.3432 m` | `497178f35a9ed5b27100e9589532bfa8580019e5c3aefbcb0df0f8e5f93a2429` | `d7669ca04e02bee0101d8512829759a56ae562bf2310c26902d9f5c8a85528fe` |

PFFDTD medium -> fine metrics:

- complex RMS relative: `1.0100111028102499` (limit `0.20`)
- maximum magnitude relative: `1.0634318020028137` (limit `0.25`)
- maximum magnitude difference: `38.48482909941208 dB`
- maximum phase error: `97.79037481710755 deg` (limit `15 deg`)

Therefore PFFDTD is also not converged under the frozen schedule.

## Cross-solver comparison

Because both self-convergence gates failed, the cross-solver status is **BLOCKED**. Fine/fine metrics were still recorded as diagnostic evidence but are not eligible for validation PASS:

- complex RMS relative: `1.1716523113840118`
- maximum magnitude relative: `2.494314047650006`
- maximum magnitude difference: `10.867238682842968 dB`
- maximum phase error: `128.6396601799308 deg`

Per-frequency diagnostic results:

| frequency | magnitude difference | magnitude relative | phase error | status |
| ---: | ---: | ---: | ---: | --- |
| 40 Hz | `10.867238682842968 dB` | `2.494314047650006` | `128.6396601799308 deg` | rejected diagnostic only |
| 80 Hz | `1.4838831161441477 dB` | `0.15704217943493282` | `74.26695891257958 deg` | rejected diagnostic only |

## Scope and non-claims

This evidence does not validate arbitrary polyhedra. The state remains:

- `general_3d_validation_state=NOT_VALIDATED`
- `CONCAVE_NOT_VALIDATED`
- `MULTI_REGION_NOT_VALIDATED`
- `PORTAL_NOT_VALIDATED`

It does not select or adopt a production solver, does not establish GPU equivalence, and is not owned-room evidence.

The first numerical run before run #7 was fail-closed BLOCKED by an order-sensitive validator check after R120B canonicalized the vertex ordering. That validator was corrected to compare the exact vertex set rather than input ordering; no physics threshold, source normalization, mesh schedule, or solver result was altered.

Repository summary copy: `benchmarks/acoustics/r130d_general3d_validation_run7_summary.json`.

RDC calls: **0**. HTDT-Capture changes: **0**.
