# Round 14 — solver / prediction-lane consistency

Scope: every surface where a user (or a dispatch record) selects a solver and
the app runs one — `RoomPredictionModelOption` model keys
(`rectangular`, `low-band-wave:<provider_id>`, `hybrid:<provider_id>`, and the
bare placeholders), `RoomPredictionController.prepare_run` → `_analyze`
dispatch, the `PredictionJobToken`/`PredictionJobGuard` apply gate,
`CadPredictionRepository` canonical replay verification, solver-execution
lanes (`bind_prediction_request_to_solver_adapter` → GA executor /
`PffdtdCandidateWaveExecutor` / R130D polyhedral executor), the R170A/R170B
provider lane (persisted sealed authority consumed as the run), REW Room
Simulator, and the topology-comparison execution lanes. For each lane:
*does the implementation that runs match the selection, and does the stamped
authority name the producer — or does it silently substitute?* Verification
is Qt-offscreen (`QT_QPA_PLATFORM=offscreen`) under Python 3.12 + pytest.
Branch `devin/rev14-wave`.

## The lane-truth contract (as verified)

Three enforcement layers make a wrong-lane result structurally impossible to
persist, verified end-to-end:

1. **Selection → identity.** `resolve_room_prediction_options` builds one
   `RoomPredictionModelOption` per real capability; provider ids are
   namespaced and content-addressed (`r170a-provider:<sha256>` vs
   `r170b-hybrid-provider:<sha256>`), so a `low-band-wave:` key can never
   resolve to a hybrid authority or vice versa — `_provider_by_id` looks the
   id up across both lists but the key prefix fixes the lane. `prepare_run`
   re-resolves options against the *current* revision and refuses any key
   that isn't READY+runnable with a JP error (`選択したprediction
   laneはこのSceneRevisionでは実行できません`).
2. **Run → stamped authority.** `_analyze` is a two-way dispatch:
   `spec.provider is not None` → `analyze_provider_frequency_response`,
   else `analyze_native_rectangular_geometry`. The result's
   `model_id`/`model_version` come from the *provider class instance*
   (`PROVIDER_RESPONSE_MODEL_ID` vs `HYBRID_RESPONSE_MODEL_ID` by
   `isinstance`), not the requested key; the provider response payload
   records `provider_id`/`provider_semantic_sha256`/`provider_adapter_id` of
   the authority that was actually consumed.
3. **Apply → fail closed.** `PredictionJobGuard` is latest-per-operation;
   `_result_matches_token` demands result.model_id/model_version/input_hash/
   revision/constraint equal the token's before apply; `save_run` replays
   the canonical request (`verify_prediction_input`) *and* re-runs the
   output (`verify_prediction_output`) demanding byte-exact equality —
   `(model_id, model_version)` pairs with no registered replayer are
   non-authoritative and rejected.

Instrumented checks this round recorded the analyzer actually invoked per
READY `model_key` on one scene exposing all three lanes — each key ran its
own implementation, stamped the producing authority, and cross-lane keys /
cross-lane results were refused (`test_review_round14_wave.py`).

## Lane-by-lane verdict

| Lane | Selection surface | Dispatch truth | Authority stamped | Fallback honesty |
|---|---|---|---|---|
| Rectangular geometry | `model_key='rectangular'`, READY only on exact-axis-aligned room | `analyze_native_rectangular_geometry` runs the pinned rect solver; requires `geometry_kind` exact else raises | `htdt.rectangular_geometry` + `rect-room-geometry-1`; assumptions warn `rectangular_geometry_model_requires_axis_aligned_rectangular_room` | Non-rect → UNSUPPORTED option ('矩形近似を適用しません') + `compatibility:unsupported` finding, never approximated. `rectangular_approximation` literal exists but has no producer — reserved label, not a lie. |
| Low-band wave (R170A) | `low-band-wave:<provider_id>` | `_prepare_provider_run` binds the sealed `LowBandPredictionProvider`; analyzer projects stored complex-pressure response for the band | `htdt.r170a_provider_response` + adapter `htdt.r170a.r130_complex_pressure`; provider payload names the consumed authority | Stale revision/stale doc/missing receiver/non-READY capability → BLOCKED with JP reasons on the option + refused `prepare_run`. No recompute substitution — the lane reads the sealed artifact, never re-solves. |
| Hybrid (R170B) | `hybrid:<provider_id>` | Same mechanism against `HybridPredictionProvider` | `htdt.r170b_hybrid_response` + adapter `htdt.r170b.r160_numerical_hybrid`; evidence state/scope projected verbatim | Same gating; option detail carries `hybrid (<blend_law>)` + evidence label (開発用candidate・未検証 etc.) — user sees exactly what the result is. |
| Bare `low-band-wave` / `hybrid` placeholders | Present only with `include_*_placeholder` | No provider → UNSUPPORTED, `runnable=False` | n/a | Explicitly UNSUPPORTED ('providerなし') — placeholder rows tell the user the capability doesn't exist. |
| GA deterministic paths (R150) | `bind_prediction_request_to_solver_adapter` + `AcousticSolverAdapterDescriptor` | `_validate_dispatch_chain` requires READY + `adapter_id==htdt.r150.deterministic-path` + version `1` + domain `geometric` + `dispatch.solver_implementation_ref == descriptor.solver_implementation_ref` + config ref match | `AcousticSolverResultEnvelope` copies `solver_implementation_ref`/`solver_configuration_ref` verbatim from dispatch; repository `_validate` re-resolves every ref + re-derives the envelope | Wrong adapter/wrong implementation/wrong domain → executor refuses; `test_engine_must_match_ready_dispatch_solver_implementation` covers wrong-implementation refusal. |
| PFFDTD candidate wave (R130A) | READY dispatch for `htdt.r130a.pffdtd_candidate_wave` | `compile_input` enforces adapter id/version-set/domain `'wave'` + identity chain + `expected_pffdtd_commit_sha == dispatch.solver_implementation_ref.authority_version` + real git HEAD check at run time | Provenance stamped `candidate_only: True`, `production_solver_selected: False`; adapter id + version variant on the envelope | A READY GA dispatch fed to this executor fails at the adapter check (`does not target the bounded PFFDTD candidate adapter`) — verified instrumented this round. |
| REW RoomSim | Batch position batch | Transactional: re-reads full RoomSim state hash after every write; `_safe_restore` only owned fields; final hash must equal pre-state | `model_id='rew-room-simulator'`, `model_version=<live REW version>` from `before.rew_version` | Stamps the *actual* REW build that produced the row; immutable batch spec enforced ('stored Room Simulator batch differs'). |
| Topology comparison | `_LanePlan` per candidate | Lanes missing repositories/data → `'unsupported'` with explicit reasons | Rows cite the bound evaluations per lane | No placeholder metrics — a lane that can't run is marked, never zero-filled. |
| Legacy REST `/analyze` | No solver choice | `analyze_rectangular_context` only | `algorithm_version='rect-room-geometry-1'` honest | Feature matching disabled for non-measured/invalid datasets — honest '非対応'. |

## Findings

| # | Severity | Issue | Disposition |
|---|---|---|---|
| F1 | LOW | **Provider lanes gated only the upper band edge.** `_provider_option`, `_hybrid_option` and `provider_response_request_identity` rejected `max_mode_hz > domain.maximum_hz` but nothing below `domain.minimum_hz`: a `max_mode_hz=30` request on a 40–80 Hz provider resolved READY + runnable, `prepare_run` minted a token, and only `_provider_response` failed at analyze time (`provider band selection yields fewer than two samples`). Explicit error, not a wrong-lane result — but a READY lane that can never succeed is a capability-matrix inconsistency. | **Fixed**: lower-edge check added in all three places; option now resolves BLOCKED with JP reason `要求帯域 (~… Hz) がprovider有効帯域 (…–… Hz) の下限を下回ります` and `provider_response_request_identity` raises `requested band is below the provider valid frequency domain` so replay/persistence also fails closed. |
| F2 | — (verified non-issue) | `_decode_provider_request` doesn't compare `parameters['provider_kind']` against the snapshot's `provider_kind`. | Dismissed — the embedded provider is re-validated by `provider_id` + `semantic_sha256`, and canonical replay regenerates `parameters_json` from the embedded provider, so any tamper byte-diverges and persistence fails closed. Cannot mislabel. |
| F3 | — (verified non-issue) | `spec.sound_speed_m_s = 0.0` on provider-lane run specs. | Dismissed — the field is only consumed by the rectangular analyzer; provider runs never read it and it never reaches the result. |

## Determinism

`result_sha256` hashes `result_identity_payload()` which excludes
`prediction_id`/`run_id`/timestamps → same selection on the same revision
yields identical canonical request (`parameters_json`, `input_snapshot_json`,
`input_hash`) and identical `result_sha256` — verified for all three lanes.

## Provenance

`CadPredictedProviderResponse` stamps `provider_id`, `provider_semantic_sha256`,
`provider_adapter_id`, `provider_adapter_version`, `provider_evidence_state`,
`provider_evidence_scope` from the *consumed* authority; `embedded_run_provider`
decodes the sealed provider back out of a persisted result for staleness
projection (`base_current_authority` for hybrid). Interpretation/comparison
rows project these verbatim — the recorded solver name is the producing run's,
not the UI label's.

## Performance claims

None found. Option rows carry evidence labels and band detail only; no UI or
doc claims a lane is faster/more accurate. (The honest evidence labels —
'開発用(candidate・未検証)', '実部屋検証済み', 'production採用済み' — are
themselves part of the honesty surface.)

## Fixes (small diffs)

| File | Change |
|---|---|
| `room_prediction_options.py` | `_provider_option` + `_hybrid_option`: BLOCKED reason when `max_mode_hz < domain.minimum_hz` (JP string). |
| `cad_provider_response.py` | `provider_response_request_identity`: raise when `max_mode_hz < domain.minimum_hz` so request-identity minting and replay both fail closed. |

## Deferred / known gaps

- **`max_mode_hz` between `domain.minimum_hz` and the second grid point** can
  still select < 2 samples at analyze time — data-dependent, stays
  runtime-fail-closed with an explicit error (honest; a resolve-time check
  would need the provider's grid).
- **Job guard is single-flight per operation** (`operation_key='prediction'`
  shared across lanes): preparing lane B supersedes lane A's outstanding
  token. Correct UX (one run at a time) and verified in tests; noted because
  it means lane A's late result applies to no token even though it's honest.
- **`parameters['provider_kind']`** isn't compared to the snapshot's kind at
  decode (F2) — safe today because replay regenerates it; if a third provider
  kind ever shares a model_id, add the check.

## Test evidence

- New: `backend/tests/test_review_round14_wave.py` — 6 tests:
  instrumented per-lane dispatch recording (which analyzer ran vs which
  `model_key` selected, on one scene with all three READY); token == identity
  == result authority; cross-lane key confusion refused (5 bad keys);
  wrong-lane result rejected by the apply gate; determinism of request +
  result keys; GA dispatch refused by the PFFDTD candidate executor; band
  lower-bound BLOCKED at resolve (regression coverage for F1).
- Regression: full `backend/tests -n 4` — all pass except one documented
  preexisting xdist concurrency flake
  (`test_cad_hybrid_prediction_provider.py::test_evidence_lifecycle_rejects_illegal_promotions`,
  `FileNotFoundError` on a fixture `.tmp` authority file under `-n≥2`;
  same failure recorded on clean `main` in round14-errmsg, unrelated to
  this diff — it passes serially and under `-n 4` file-scoped).
