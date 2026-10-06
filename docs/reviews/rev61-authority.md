# REV61-AUTHORITY — domain-authority / sealed-record / repository deep review

Scope: the `cad_*` domain-authority layer — sealed pydantic model modules and
their `*_repository.py` persistence twins, prioritizing the REV57–REV60
authorities (ULF / noise-ingress / fire-safety / accessible-playback,
collaboration/approval, observer metamerism, HVAC/timebase, material aging).

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | `evaluate_ulf_capability`: the `covering` filter dropped observations whose `band_high_hz` fell below the claimed `request_high_hz` but never failed closed — a profile claiming ULF verification observed only down to e.g. 40 Hz on a 15 Hz claim still returned `ulf_capability_verified` | high | FIXED |
| 2 | `evaluate_ulf_capability`: `ulf_characterized_with_limitations` capability state was never consulted — identical verdict to `ulf_calibrated`, `limitation_notes` discarded | medium | FIXED → `ulf_capability_verified_limited` carrying the limitation notes |
| 3 | `evaluate_ingress_model_qualification`: `model.envelope_state == 'design_envelope'` alone gated `design_model_only` — an `installed_envelope` model with **zero** measurement_refs returned `ingress_model_qualified` | high | FIXED → `design_model_only` unless `field_observed_state`/`qualified_state` or measurement evidence exists |
| 4 | `evaluate_material_deployability`: `required_evidence_present(+_with_limitations)` requirement state was trusted without checking that the supplied evidence covered `required_evidence_kinds` — unresolved evidence kinds still reached `material_deployable*` | high | FIXED → fail-closed `fire_safety_evidence_required` (`required_evidence_kinds_unresolved`) |
| 5 | `evaluate_material_deployability`: `applicable_with_limitations` specimens were ignored by the downgrade predicate (only `assembly_differs`/`test_method_incompatible` propagated) — limited applicability silently yielded `material_deployable` | medium | FIXED → `deployable_with_limitations` |
| 6 | `evaluate_accessible_playback`: AD observations whose `output_state` was only `not_verified`/`unknown` fell through the lost/downmix loop to `verified_accessible_playback` | high | FIXED → `routing_failed` (`ad_output_unverified`) |
| 7 | `_SealedStore` in all 27 sealed repositories: `record.__dict__.get(path.split('.')[0]) is None → continue` skipped column-vs-payload verification whenever an optional bound field was unset, and the `('document_id', '__document_id__')` pseudo-path never matched a field — so the `document_id` column was **never verified anywhere**. Tampered columns read back silently and rescoped `list()` queries | high | FIXED — every bound column compared on `get` (NULL ↔ `None`, bools normalized); `list()` additionally verifies the `document_id` column its `WHERE` clause depends on |

Every FIXED finding ships a regression test in
`backend/tests/test_rev61_authority.py` proven to fail on the unfixed code
(`git stash` for the evaluator + `get` changes; `git checkout 86e735ed` for
the `list()` path).

## Verified-clean surfaces

| Surface | How verified |
|---------|--------------|
| `evaluate_collaboration_*` family (divergence staleness, state-transition legality, approval currency, role capability, merge-agent whitelist) | probe scripts + contract tests; all forged/dissent/stale inputs fail closed |
| `CadObserverMetamerismRepository` getters | dedicated getters verify id **and** sha + per-column values; does not share the `_SealedStore` skip pattern — tamper probe confirmed |
| `evaluate_clock_jitter_failure` boundary (`jitter < limit` passes at equality) | boundary values probed both sides; contract test keeps the `<` convention explicit |
| `evaluate_thermal_path` (HVAC), `evaluate_timebase_drift` | bounded-input guards inspected + probed; verdict tails fail closed |
| `evaluate_material_applicability` gate ordering | fire-safety gate precedes applicability/material gates — probed with conflicting states |
| Enum/label registries in slice (`ULF_LABELS`, `INGRESS_LABELS`, `FIRE_LABELS`, `ACCESSIBLE_LABELS`, collab/material-condition labels) | every enum member has a label key; no embedded domain prefixes; JA/EN both present |

## Residual notes

- `backend/tests/test_cad_profile_repositories.py` — 6 tests reference
  `save_profile`/`select_profile`, methods removed by REV59-DRAWPROF
  (#741/#742/#733 renamed them per-domain). Deterministic `AttributeError`
  rot on `origin/main`, unrelated to this diff. Report-only: the file is
  shared across slices and needs a rewrite against the current store API.
- `ULF_LABELS['stale_after_room_change']` (and similar keys) have no
  producing verdict string — dead label keys, harmless, reported.
- `evaluate_material_applicability` compares
  `evidence.material_family_ref.ref_id` to the *instance*
  `material_ref.ref_id` — family-vs-instance ref-id comparison. Plausibly
  intended (a family record legitimately covers instances) but needs a
  domain decision; reported, not fixed.
- Evaluators trust caller-resolved evidence binding: a dangling
  `satisfied_by_ref`/evidence ref silently drops coverage. Finding 4 closes
  the exploit at the deployability gate; sibling evaluators retain the
  pattern — flagged for a follow-up pass.
- `_SealedStore.get` verifies bound columns but does not re-verify
  `record.sha == canonical_sha256(identity_payload())` — consistent with
  the observer-metamerism store convention (seal verified at save, id
  embeds the payload digest). Noted for the parent.
- Bespoke non-`_SealedStore` repositories (`cad_acoustic_snapshot`,
  `cad_acoustic_solver_dispatch`, `cad_acoustic_treatment`,
  `cad_active_crossover`, `cad_acceptance`, replay/media stores) use a
  different integrity model and were not audited to this depth this pass.
- Not covered this pass: `native_authority_audit.py` rule modules
  (~8.7k lines), `authority_revalidation`, remaining HVAC/airflow
  evaluators, `cad_timebase`/`cad_calibration` domain models, full
  enum↔label drift sweep outside the REV60 edge authorities.

## Scoped pytest

```
backend/tests/test_rev61_authority.py
backend/tests/test_rev60_edge.py backend/tests/test_rev60_collabenv.py
backend/tests/test_rev59_audiomet.py backend/tests/test_rev59_apply.py
backend/tests/test_rev59_audio2.py backend/tests/test_rev59_buildenv.py
backend/tests/test_rev59_cadref.py backend/tests/test_rev59_codepolicy.py
backend/tests/test_rev59_display3.py backend/tests/test_rev58_displaymeas.py
backend/tests/test_rev58_audiomodel.py backend/tests/test_rev59_infra2.py
backend/tests/test_rev59_bench2.py backend/tests/test_rev59_audiometb.py
backend/tests/test_rev59_listenexp.py backend/tests/test_rev59_closeaux.py
backend/tests/test_rev59_acoust2.py backend/tests/test_rev59_vidmeta.py
backend/tests/test_rev59_qualnum.py backend/tests/test_rev59_powerev.py
backend/tests/test_rev59_drawprof.py backend/tests/test_rev59_roomq.py
backend/tests/test_rev59_signal.py backend/tests/test_rev59_digchain.py
backend/tests/test_rev59_acoust3.py backend/tests/test_rev59_verauto.py
backend/tests/test_rev59_loudspk.py
backend/tests/test_cad_profile_repositories.py
```

Result: 752 run — 746 pass; the only 6 failures are the pre-existing
`test_cad_profile_repositories.py` rot above (verified red on `origin/main`
before this diff).
