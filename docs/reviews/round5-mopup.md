# Round 5 — final mop-up of LOW leftovers

Scope: the eight items enumerated in `round4-regression.md` → "Convergence
assessment". Branch `devin/rev5-mopup`. All verification is local (no CI).

## Verdict table

| # | Item | Verdict | Change |
|---|------|---------|--------|
| 1 | Raw `sqlite3.connect` writers → `connect_sqlite` | RESOLVED | 34 files routed; 6 URI `mode=ro` readers stay raw (unexpressible through `connect_sqlite`) |
| 2 | 8 unbounded `read_bytes()` on operator paths | RESOLVED | All 8 routed through `ingress.read_file_bounded` with domain-matched caps; error paths audited |
| 3 | `native_cad.py` context-free `QTimer.singleShot` | RESOLVED | `QApplication` instance passed as receiver context, matching the `singleShot(0, self, …)` convention |
| 4 | `xml_guard` UTF-16/32+BOM bypass | RESOLVED (documentation) | Module docstring documents the byte-scan blind spot and why it is defense-in-depth only (pyexpat rejects DOCTYPE post-decode) |
| 5 | `golden_path_preflight` cleanup-on-success | RESOLVED | `--clean-work-dir-on-success` flag; default unchanged (evidence preserved) |
| 6 | `json.dumps` canonical/identity audit | RESOLVED | 118 call sites audited: 10 converted to `canonical_json`/`canonical_sha256`, 45 gained `allow_nan=False`, 63 left as display/wire/best-effort paths |
| 7 | `acoustics.py` `speaker['speaker_id']` KeyError | RESOLVED | `.get(..., 'unknown')` matching the existing skip-path convention |
| 8 | `cad_benchmark._parse_point` validation | RESOLVED | Shared `_parse_triplet` helper: `isinstance(list)` + `len == 3` + `math.isfinite` for both `position_m` and `orientation_deg` |

## Item 1 — per-site verdicts (`sqlite3.connect`)

**Converted to `connect_sqlite`** (writers on FK-free tables — the pragma is a
no-op there, verified against `cad_schema_ddl.py`; the few writers touching
FK-bearing tables already had the pragma enabled):

- `cad_acoustic_material:233`, `cad_acoustic_source_pose:601`,
  `cad_action_item_repository:83`, `cad_ambient_noise:1256`,
  `cad_assumption_decision_repository:122`,
  `cad_calibration_workflow_repository:30`, `cad_compute_envelope:466`,
  `cad_design_checkpoint:603` (writes `scene_revisions` — FK table, pragma
  already on, now via helper), `cad_field_session:419`,
  `cad_intervention_study_repository:222`, `cad_ir_analysis:775` (local import
  kept — module convention), `cad_listener_pose:447`,
  `cad_measurement_pose:544`, `cad_measurement_runner_repository:68`,
  `cad_measurement_target_pattern:457`, `cad_project_activity_repository:28`,
  `cad_project_template_repository:44`, `cad_review_note:187`,
  `cad_robustness_repository:136`, `cad_screen_transfer:337`, `cad_site:332`,
  `cad_source_response:441`, `cad_validation_dashboard:390`,
  `cad_visual_qa:245`, `capture_retention:100`, `project_lifecycle:234`,
  `database.py:70` (`connect()`) + `:599` (`integrity_problems`),
  `cad_schema.py:1131` (`ensure_native_schema` migration boundary).

**Converted for uniformity** (readers / `Connection.backup` page-copy pairs —
backup copies pages verbatim so the pragma is inert):
`application_pages:104,565`, `system_expansion_workflow:487`,
`default_document:121`, `data_relocation:426,837`, `migration_guard:29,48,92,
93,228,229`, `native_authority_audit:3698`, `native_backup:427`.

**Stay raw** — `file:…?mode=ro` URI opens that `connect_sqlite` cannot express:
`cad_schema:544,590`, `legacy_data:144,179`, `native_backup:224,299` (wait —
the two `_backup`-pair plain connects at 427 were converted; the `mode=ro`
readers at 225/300 were not), `native_upgrade:511`, `support_diagnostics:230`.

**Out of scope**: `scripts/` dev harnesses and `backend/tests/` fixture
construction keep raw connections deliberately.

**Regression caught and fixed**: `cad_schema._validate_legacy_database` and
`data_relocation` compared `PRAGMA integrity_check` result rows to the tuple
literal `[('ok',)]`; `sqlite3.Row` does not equal a tuple. Both now compare
`len(...) != 1 or row[0] != 'ok'` — valid for `Row` and tuple alike.

## Item 2 — per-site verdicts (`read_bytes` → `read_file_bounded`)

| Site | Cap | Error path |
|------|-----|-----------|
| `managed_assets.read_file` (retrying reader used by every asset lookup) | `MAX_ATTACHMENT_BYTES` (256 MiB) | `IngressTooLargeError` (ValueError) propagates to callers; retry loop still catches `PermissionError` |
| `managed_assets.install` staged-verify read | `len(raw_bytes)` (exact) | ValueError propagates alongside existing verification failure |
| `project_bundle` export asset read | recorded `size_bytes` | wrapped into `ProjectBundleError` (verification-failure message) |
| `support_diagnostics` log-zip member read | `8 * MAX_LOG_BYTES` (8 MiB vs 1 MiB rotation) | marked `skipped`/`too large` in member manifest, same as `OSError`→`unreadable` |
| `capture_receiver` TLS cert + key reads | `TLS_CREDENTIAL_MAX_BYTES` (1 MiB) | wrapped into `CaptureReceiverError` — the class's error contract |
| `installation_handoff` staged re-read | `len(encoded content)` (exact) | `IngressTooLargeError` (ValueError) — a corrupt staged write was already an IOError path |
| `cad_acoustic_treatment_repository` compare read | `len(data)` (exact) | oversize ⇒ `identical = False` ⇒ existing hash-collision `ValueError` |

## Item 6 — `json.dumps` audit (118 sites)

- **Converted to canonical helpers** (exact canonical profile, digests/rows
  byte-identical for finite payloads; NaN now raises instead of emitting the
  non-JSON `NaN` token): `cad_repository._constraint_revision_sha256`,
  `cad_measurement_loop._hash`, `database.canonical_json_sha256` + REW-API
  asset store, `search_space._canonical_sha`,
  `system_expansion_workflow._short_semantic_id`,
  `cad_constraint_repository` workspace payload, `cad_equipment_repository`
  field-groups column, `cad_field_explorer_repository` session payload,
  `cad_video_workspace` payload, `project_bundle` per-row JSONL + the
  `_canonical_sha256` helper now delegates to `canonical_sha256`.
- **`allow_nan=False` added** (persisted/identity payloads whose profile is
  intentionally not canonical — `indent=2` manifests, sorted-but-spaced
  journal rows, wire compatibility): 45 sites across `database.py`,
  `cad_repository.py`, `project_bundle.py` (manifest), `authority_graph.py`,
  `localization.py`, `project_lifecycle.py`, `help_registry.py`,
  `cad_readiness_planner.py`, the repository persist paths
  (`cad_action_item`/`cad_assumption_decision`/`cad_design_decision`/
  `cad_project_template`/`cad_measurement`), `cad_camilladsp` persisted
  config, `activity_center`, `automatic_backup`, `commissioning_plan`,
  `data_relocation`, `launch_intents`, `legacy_data`, `migration_guard`,
  `reference_libraries`, `runtime_instance`, `__main__`,
  `cad_device_adapter_file`, `cad_candidate_wave_execution`,
  `cad_pffdtd_resource_estimator`, `project_performance` journal writers,
  `acoustic_bakeoff_readiness`.
- **Left alone** (63 sites): display/CLI-print paths, device wire payloads
  (`cad_hue_lighting`, `cad_pjlink`, `cad_yamaha_rxa4a`, `cad_camilladsp`
  config getter), and best-effort diagnostics where a `ValueError` would break
  the error path (`support_diagnostics` package build,
  `project_performance:247` inside `except OSError`).

Non-canonical-profile hashes (`authority_graph`, `localization`,
`project_lifecycle`, `help_registry`, `cad_readiness_planner`) were NOT
switched to `canonical_sha256`: the different separators would change every
digest and invalidate persisted identities. `allow_nan=False` is the only
byte-compatible hardening there.

## Tests

`backend/tests/test_round5_mopup.py` (12 tests): importer triplet validation
(malformed/non-finite `position_m`/`orientation_deg`, valid case),
speaker-id fallback, managed-asset bound (monkeypatched cap), TLS credential
bound → `CaptureReceiverError`, and all three preflight cleanup behaviors
(success+clean, failure preserve, default preserve).

Full suite: `cd backend && TMPDIR=/c/t C:/devin/python/python.exe -m pytest -q -n 4`
— result reported in the PR/summary.
