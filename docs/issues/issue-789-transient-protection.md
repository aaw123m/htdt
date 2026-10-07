# Issue #789 — surge / lightning transient-protection evidence authority

Status: software layer landed (REV63). Field evidence population stays
manual/externally sourced.

## What landed

`backend/src/htdt/cad_transient_protection.py` +
`cad_transient_protection_repository.py`; schema v88; full audit wiring
(replay probes, row bindings, JA table labels, display helpers).

Six sealed, append-only records:

| Record | Prefix | Role |
|---|---|---|
| `ProtectedPath` | `ppath-` | One exact electrical or conductive path (ordered topology stages + pinned load ref). The identity protection evidence hangs off. |
| `TransientProtectionPlan` | `tpplan-` | Declared protection domains per path + pinned jurisdiction/code context + qualified-review requirement. Design intent, never evidence. |
| `SPDEvidence` | `spd-` | Product/listing/install evidence bound to (path, domain): type/class, standard profile + `standard@edition` pin, method-specific ratings (VPR/MCOV/In/Imax/SCCR), install record, coordination ref, provenance. |
| `TransientProtectionObservation` | `tpo-` | Health readback (`status_ok`/`replace_required`/`fault`/`remote_alarm`/`unknown`) with source + manufacturer semantics ref. |
| `TransientProtectionEvent` | `tpe-` | Lightning/severe-transient/utility-fault/SPD-alarm/topology-change record; stales affected paths until `inspection_record_ref` closes it. |
| `TransientProtectionAssessment` | `tpa-` | Sealed per-path verdict composed by `evaluate_path_protection`, binding the exact evidence the verdict rested on. |

## Verdict vocabulary (fail-closed)

`evaluate_path_protection(path, plan, evidence, observations, events)`
derives, in precedence order:

1. `stale_after_event` — an event affecting the path has no inspection
   record (equipment powering on is not proof of health);
2. `status_degraded` — latest health readback says
   replace/fault/remote-alarm (degraded protection, not total power
   failure);
3. `no_protection_evidence` — no satisfying SPD evidence bound, or no
   declared plan for the path (coverage is undefined; evidence cannot
   complete an unspecified requirement);
4. `review_required` — the plan requires qualified
   electrical/lightning-protection review not yet recorded (external
   LPS always requires it);
5. `designed_not_installed` — all evidence is `designed_only`
   (design intent is not installed protection);
6. `coordination_unverified` — ≥2 SPDs with no coordination reference
   (two devices never imply coordination);
7. `partial_point_of_use_only` — declared domains remain uncovered;
8. `protected_with_evidence` — every declared domain covered and at
   least one `installed_verified` record.

`CoordinationState` reports `single_device` /
`multiple_uncoordinated_unknown` / `coordinated_with_reference` /
`unknown`.

## Cross-domain guards (fail-closed by construction)

- **Vendor marketing is not evidence** — `evidence_basis='vendor_marketing'`
  is recordable but never satisfies a domain (no `surge protector`
  strip → protected theater).
- **UPS / conditioner features stay separate** —
  `spd_type_class='non_spd_transient_feature'` can satisfy only
  `ups_power_conditioner_transient_feature` /
  `device_internal_protection` domains, never a power-path SPD domain.
  Ride-through is not transient protection.
- **Power quality is not this authority** — steady-state observations
  live in #738; there is no field here that a power-quality result
  could satisfy.
- **External LPS stays external** — declaring the
  `external_lightning_protection_system` domain forces
  `requires_qualified_review` on the plan; the assessment can only
  reach `review_required`/partial while review evidence is absent.
- **Standards are pinned, not normalized** — `standard_reference` must
  carry `id@edition`; IEC 61643-11:2011-withdrawn vs 2025 remain
  distinct profiles (`iec_61643_11_2011_withdrawn`).
- **Installed > designed** — `install_state='installed_verified'`
  requires an installer report, photo/label ref or exact panel/circuit
  association.
- **Status is observed, never estimated** — no remaining-life
  derivation from elapsed time.

## Out of scope (by issue contract)

Lightning-risk design, conductor routing, air-terminal placement,
earth-electrode design, SPD sizing/coordination calculation, unsafe
protective-earth modifications — `review_required` is the terminal
state, not a design output.

## Integration points

- JA labels: `application_pages.py` table-label dict + `TRANSIENT_PROTECTION_LABELS`;
  `measurement_evidence_display.transient_protection_verdict_line/label`.
- Stale propagation to dependent claims (#729) and health-baseline
  monitoring (#595) consumes `TransientProtectionEvent` /
  observation records — wired when those consumers land.
- Fixture coverage in `backend/tests/test_issue_789_transient_protection.py`
  mirrors SPD10–SPD80 acceptance scenarios (documented SPD, consumer
  strip, UPS-only, replace-required, external-path stale, product swap,
  post-lightning inspection, jurisdiction mismatch).

## What stays manual

Actual SPD listing/install/photo evidence capture, AHJ/jurisdiction
records, qualified electrical/lightning-protection review records and
post-event inspections — these are field/external artifacts the
authority stores, not produces.
