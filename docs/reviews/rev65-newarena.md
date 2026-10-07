# REV65 — NEWARENA review record

Scope: the 36 `cad_*` modules and their repositories landed after REV61
(`git log --diff-filter=A --name-only 408ec651..HEAD -- backend/src/htdt/*.py`)
— the sealed-authority arena: solvers, envelopes, deployments, campaigns,
providers, supportability, standards matrices, manifests, exporters.

Method: every file read end-to-end. Per module: sealed-identity pattern
(`identity_payload` → `canonical_sha256` → `_seal`), cross-field model
validators, evaluator fail-closed ladders (does the evaluator re-derive
from pinned authority, or trust stored/caller-passed fields?),
`AuthorityRef` sha-pin resolution, Literal vocabularies vs issue
§-numbering, repository `_SealedStore` + row-integrity registration, and
existing test coverage (which was happy-path only).

## Defects fixed (7)

1. `cad_equalizer_apo_export.py` — `apo_band_support_problems` rejected
   every *valid* `notch`/`band_pass` band ("takes no gain_db" fired when
   `gain_db is None`) and silently accepted gained ones, which then
   rendered a `Gain` APO line the bounded subset forbids. Inverted
   both checks.

2. `cad_camilladsp_deploy.py` — `compile_camilladsp_config`: two channel
   ids slugging to the same htdt filter prefix (`sub.out` vs `sub_out`)
   silently overwrote each other's `filters[]` entries while the
   mixer/pipeline blob claimed both channels. Collisions now land in
   `unsupported_items` (fail-closed) instead of producing a config that
   does not match the materialized snapshot.

3. `cad_camilladsp_deploy.py` — `CamillaDSPDeploymentSession`: a session
   could record `readback_matched=True` with no
   `readback_config_sha256` (an unverifiable "verified" claim) or pin a
   read-back sha with no verdict. Both orderings of the pairing are now
   validated together.

4. `cad_camilladsp_deploy.py` — `normalize_camilladsp_config`: htdt
   order-2 crossovers are only ever emitted as Butterworth
   (`q = 1/sqrt(2)`). A device-side drift to a non-Butterworth q on the
   `htdt_`-owned filter was still reconstructed as the sealed
   `CadCrossoverSetting`, making the read-back diff report no deviation.
   The reconstruction now requires the Butterworth q so drift surfaces
   as a diff.

5. `cad_solver_confidence_bound.py` — `evaluate_input_envelope`:
   `SolverInputEnvelope.solver_envelope_ref` /
   `capability_manifest_ref` are sha-pinned `AuthorityRef`s, but the
   evaluator ran the solver gate against whatever
   `solver_envelopes`/`manifest` the caller passed, unchecked. Pinned
   envelopes now resolve via `_resolve_envelope_refs` (id + sha must
   match a supplied record); unpinned envelopes keep the prior
   caller-passed semantics.

6. `cad_owned_room_campaign.py` — `evaluate_campaign_promotion`: the
   ladder compared `verdict.preregistration_sha256` against the prereg
   but never verified that `verdict.campaign_ref`,
   `holdout_measurement_refs`, or `calibration_measurement_refs`
   resolved to measurements actually bound to this campaign — a verdict
   pinning a phantom, tampered, or cross-campaign measurement could
   still reach `recommendation_eligible`. A structural-binding pass
   (refs resolve id+sha, holdout refs land on `role='holdout'`
   measurements) now floors the ladder at `owned_room_insufficient`.

7. `cad_owned_room_campaign.py` — `CampaignVerdict`: the evaluator
   keyed `claim_verdicts` into a dict, so a record declaring the same
   `claim_kind` twice silently last-won. The model validator now
   rejects duplicate claim kinds.

Tests: `backend/tests/test_rev65_newarena.py` — one failing-now-passing
test per defect (9 tests).

## Findings (report only)

- `cad_benchmark_qualification.evaluate_qualification` pins
  `evidence_ref` with `ref_sha256=None` — intentional:
  `BenchmarkValidationEvidence` is unsealed (nothing to pin).
- `cad_deployment_target.evaluate_export_target_fit` returning
  unsupported reasons into `MaterializedCalibrationSettings
  .unsupported_items` is the designed input to the #806 gate.
- `binding.created_at_utc` as the EquipmentDeviceAdapter timestamp
  source for probe/observe/plan/apply records is the contract
  (same pattern as `cad_camilladsp`).
- `evaluate_input_envelope` pin resolution still trusts the caller to
  supply the bound record set; the sealed refs bind *identity*, and an
  absent record now fails closed. Records with a matching id but wrong
  sha raise rather than substitute.
- `_slug` collisions are now rejected at compile time, but slugging is
  lossy by nature (`-`/`_`/`.` → `_`); a config authored outside htdt
  could still collide with an `htdt_`-prefixed name — fail-closed by
  prefix ownership already.
- `normalize_camilladsp_config` treats unknown `parameters.type` keys
  as foreign-owned and skips them — correct, but device firmware
  that renames a filter type could shadow an `htdt_` filter without a
  diff. Lower-severity observation, not fixed.
- All repositories reviewed register their tables with
  `native_row_integrity` and use the canonical `_assert_sealed` /
  append-only `_SealedStore` pattern; no missing audit-probe
  registrations found.
- `CamillaDSPDeploymentSession.readback_matched` semantics are
  asymmetric with `verify` outcome tiers by design (matched vs.
  verified-different); the pairing fix covers the structural hole.
