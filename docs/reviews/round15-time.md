# Round 15 — time / clock / locale-temporal truth

Scope: every timestamp write/read path — row `created_at_utc`/`captured_at_utc`/
`updated_at_utc` writers and readers, evidence staleness, inbox ordering keys,
`runtime.json`, report headers, backup generation naming/rotation, launch-intent
expiry, capture pairing expiry, and every user-facing timestamp display. Verified
locally (Windows, `QT_QPA_PLATFORM=offscreen`, `pytest backend/tests`, Python
3.12.10) by injecting shifted clocks — not just reading code. Branch
`devin/rev15-time`.

## Conventions found (the good parts)

- **Single UTC writer**: `htdt.clock.utc_now_iso()` (`datetime.now(timezone.utc)`
  `.isoformat()` → `+00:00`, µs) is imported by ~84 modules; only **two** naive
  `datetime.now()` call sites exist tree-wide (see findings). A 'Z'-suffixed
  seconds variant exists in 5 modules for in-memory/history records.
- **Ordering tie-breakers**: virtually every `ORDER BY created_at_utc` appends an
  id (`ORDER BY created_at_utc, decision_id` etc.) — same-tick ties resolve
  honestly.
- **Durations/timeouts use `time.monotonic()`**: `native_worker` budgets,
  `cad_r140_executor` wall_time, `cad_prediction_execution` in-flight age,
  `__main__` waits — all monotonic. No wall-clock duration lies.
- **Aware-enforcing validators**: `_require_iso8601` (commissioning),
  `_instant()` in repositories normalizes to UTC before comparing
  supersede/create order; `cad_project_activity._event_sort_key` parses mixed
  'Z'/`+00:00` shapes to real instants and sorts same-tick by emission order.
- **External-file honesty**: `normalize_rew_capture_timestamp` labels naive REW
  dates `host_local_timezone` vs `source_timezone`; `capture_bundle` requires
  strict RFC3339 `Z` and compares parsed instants; backup ordering reads the
  filename stamp, never file mtime.

## Findings fixed

| # | Severity | Where | Defect → Fix |
|---|----------|-------|--------------|
| T1 | **high** | `automatic_backup.evaluate` | `last_automatic_at_utc` in the future (clock set backward, state file from another machine, restored bundle) produced `elapsed < 0` → "within interval (-2 days < 24.0h)" → **all automatic backups silently suppressed until real time catches up**. Now a future stamp is *due* with an explicit `(clock skew)` reason — a spurious backup is cheap, a stalled safety net is silent. Regression: `test_future_last_run_stays_due_with_skew_reason`. |
| T2 | **medium** | `automatic_backup.evaluate` | Offset-**naive** `last_automatic_at_utc` (hand-edited/foreign state) parsed fine then raised `TypeError` on the aware subtraction, escaping `except ValueError` — a crash instead of the 'unreadable' guard. `except (ValueError, TypeError)` now; unreadable ⇒ due (fail-safe). Regression: `test_naive_last_run_is_due_not_a_crash`. |
| T3 | **high** | `automatic_backup.prune_generations` + `run_due` | Under a skewed clock the just-written generation embeds a stamp *older* than existing ones → sorts outside `keep_generations` → **the run deleted the archive it had just created** (reproduced: 3 files, keep 2, newest-real file removed). `prune_generations(protected=…)` never removes protected paths; `run_due` protects `destination`. Regression: `test_run_due_never_prunes_the_generation_it_just_wrote`, `test_prune_generations_respects_explicit_protection`. |
| T4 | **medium** | `data_management_ui._default_backup_name` | Suggested backup filename stamped `datetime.now()` (naive local, minute precision): `HTDT-backup-2026-09-29-1305` — unlabeled local wall time beside an all-UTC naming convention, and invisible to the `htdt-backup-<class>-YYYYMMDDTHHMMSSZ` generation format. Now UTC + `Z` marker (`HTDT-backup-2026-09-29-1305Z.htdt-backup`). Regression: `test_default_backup_name_uses_utc_stamp_with_marker`. |
| T5 | **medium** | `data_management_ui._format_created_at` | Backup info panel `作成日時` did `astimezone()` → **host-local** time with no zone label — the only UTC→local conversion in the app, while the restore-confirmation sibling shows raw UTC. Now `astimezone(timezone.utc)` + ` UTC` label, consistent with the app's convention. Regression: `test_format_created_at_labels_utc_not_local`. |
| T6 | **medium** | `room_history_panel` 時刻 column | `created_at_utc.replace('T',' ')[:19]` truncated away the `Z`/`+00:00` suffix — displayed the UTC instant as a bare local-looking `YYYY-MM-DD HH:MM:SS`. Now strips only sub-second precision, keeping the zone marker (`2026-09-26 10:00:00+00:00`). Regression: `test_history_timestamp_cell_keeps_the_zone_marker`. |

## Findings deferred / documented

- **`saved_label` unlabeled UTC digits** (`cad_display_labels`): generated labels
  like `2026-09-24 18:42 の保存` show the stored UTC instant with no zone label —
  used pervasively, asserted verbatim in tests. Consistent everywhere = honest
  convention, but relabeling (`UTC の保存` or host-local) is a product decision.
- **`localization.format_datetime`**: converts aware values
  `astimezone(timezone.utc)` into a variable named `local` and renders unlabeled
  — exported but **not called anywhere** in app code (tests only). Flagged for
  whoever adopts it: as written it renders UTC while reading as local.
- **Lexical order across ISO shapes**: `ORDER BY created_at_utc` on TEXT columns
  orders lexically — `'…:00Z'` sorts after `'…:00.9+00:00'` and after any
  `+HH:MM` offset digit-wise. No in-tree writer mixes shapes in one column today
  (all `+00:00` µs), but a bundle/restored row carrying 'Z' or a foreign offset
  into an ordered column misorders silently. `cad_project_activity` already does
  it right (parse → instant → sort); the SQL-ordered stores keep relying on the
  single-shape write invariant.
- **Launch-intent expiry** (`launch_intents.drain`): `time.time()` vs
  `st_mtime` — a forward clock jump expires fresh intents into `dead/`; a
  backward jump keeps zombies alive. Cross-process so monotonic cannot fix it;
  mtime-in-future already handled (kept). Accepted limitation, logged here.
- **Capture pairing expiry** (`capture_receiver`): forward clock skew expires a
  pairing early (fail-safe); backward skew keeps it usable past TTL — inherent
  wall-clock expiry, noted.
- **No-tiebreak `ORDER BY created_at_utc`**: `capture_receiver_pairings` and
  `cad_ambient_profiles` — µs-precision single-writer columns, so ties are
  near-impossible; adding a PK tiebreak is opportunistic cleanup.
- **`acoustic_treatment_service` naive `datetime.now().timestamp()`** — used
  only as an id-suffix entropy token; cosmetic.
- **`_instant()` naive edge**: repository supersede comparisons call
  `parsed.astimezone(timezone.utc)` — a naive stored value would be reinterpreted
  as host-local (silent skew), but writers enforce aware ISO at the model layer;
  only foreign/restored rows could hit it. Documented residual.
- **`runtime.json` `acquired_at`** is informational; stale-instance detection
  keys on pid liveness, not the clock — unaffected by skew.

## Suite result

Scoped run (new + touched suites): `test_round15_time.py` 7 passed;
`test_automatic_backup`, `test_automatic_backup_runner`,
`test_data_management_ui`, `test_room_history_panel`, `test_round14_dialogs`,
`test_round11_design`, `test_cad_display_labels`, `test_round10_ux`,
`test_native_launch` — 106 passed. Full suite result below once completed.
