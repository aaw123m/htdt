# Round 22 — Internal contract drift

Scope: functions whose declared or implicit contract diverges from what
callers assume, hunted across seven categories — return-type drift
(None/empty/wrong-type in unguarded paths), exception drift (raised type
vs caught type), parameter-semantics drift (name vs behaviour),
docstring-vs-code lies after refactors, caller-only assumptions (two
callers passing semantically different values through one param),
default-value traps, and pure-looking accessors that mutate. Method:
AST scans over `backend/src/htdt` for mixed None/value returns, bare
`except` sites, and raise-type vs catch-type mismatches, then
line-by-line reads of every candidate callee against ≥1 caller, ending
in an executable reproduction for each surviving finding. Rounds 1–21
assumed; their findings are not re-reported. Branch
`devin/rev22-contract`.

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | `ManagedAssetStore.read_verified` / `ensure_installed` raised plain `ValueError` for content-address violations while the module contract — and every docstring on the seam — promises `ManagedAssetError`; callers filtering on the typed error miss corruption | MED correctness | FIXED |
| 2 | `evaluate_measurement_readiness` crashed with `AttributeError` on a stored attachment row whose `filename` is `''` — `isinstance(str)` passes, `_basename('')` returns `None`, `.casefold()` then raises inside the `any()` generator | MED correctness | FIXED |
| 3 | `_close_sketch`, `_commit_vertex_drag`, `_commit_wall_drag`, and the `RoomGeometryPanel._run` runner caught `(ValueError, WallTopologyError)` but not `EditStateError` — a stale `recovery_candidate` mid-gesture raised the seam's own error type straight through the event filter / panel | MED correctness | FIXED |
| 4 | `automatic_backup_runner`, `cad_repository._authoring_head_pointer_id`, `room_workspace.evaluate_constraints` mix `return None` with value returns under a `-> object` annotation | — | VERIFIED OK — `object` covers None; callers guard |
| 5 | Timestamp helpers diverge (`+00:00`+micros vs `Z`+seconds across `clock`, `activity_center`, `intervention_planner_panel`, `support_diagnostics`) | — | VERIFIED OK — each format is self-consistent per store; no shared field mixes them |
| 6 | `_same_file` docstring claims `os.path.samefile` "raises OSError when either side does not exist" while the body swallows OSError → False | — | VERIFIED OK — docstring documents the *library* behaviour to justify the try, not this function's own contract |
| 7 | `CommissioningPlanError(Exception)` sits outside the `ValueError` family its sibling plan errors use | — | VERIFIED OK — only `list_plans` catches it, and it does so by name |
| 8 | `report.py _svg_chart`, `cad_geometric_acoustics_portal`, `raw_mesh._parse_stl` flagged by the None-return scan | — | FALSE POSITIVES — `return None` lives in nested helpers, not the annotated function |

## 1 — Managed-asset errors escape the typed contract (fixed)

`backend/src/htdt/managed_assets.py` opens with a fail-closed contract:
"Any gap fails closed with ``ManagedAssetError``" (line 17) and
`read_verified`'s docstring repeats "fails closed instead of treating a
…". `ManagedAssetError` is defined at line 34 as a `ValueError`
subclass, and every other raise in the module uses it — path-safety
checks (lines 72/74), bounds (93/115/123/128/133).

But two raises on the content-address seam itself emitted plain
`ValueError`:

- `ensure_installed` — "content-addressed managed asset hash collision"
- `read_verified` — "managed asset content does not match its content
  address"

That is exception-type drift in both directions: code catching
`ManagedAssetError` specifically (the contract the module advertises)
let raw `ValueError` through, while the raised type contradicted the
docstring on the same function.

Fix: swap both raises to `ManagedAssetError` — two-word diff, and since
the error is a `ValueError` subclass every legacy `except ValueError`
caller keeps working. Regression tests corrupt the stored bytes and
assert the typed error surfaces.

## 2 — Empty attachment filename crashes readiness evaluation (fixed)

`backend/src/htdt/readiness.py`, the `attachment_matches` generator in
`evaluate_measurement_readiness`:

```python
item.get('kind') == 'microphone_calibration'
and item.get('context_id') == context_id
and isinstance(item.get('filename'), str)
and _basename(item['filename']).casefold() == expected_cal.casefold()
```

`isinstance(item.get('filename'), str)` is True for `''` — but
`_basename` (`def _basename(value: str | None) -> str | None`) early-
returns `None` on falsy input, so `_basename('').casefold()` raises
`AttributeError` inside the `any()`. The row shape is reachable: the
attachments input is a sequence of stored RawAsset dicts, and nothing
upstream validates that `filename` is non-empty. A single malformed row
took down the whole readiness evaluation with a raw traceback instead of
failing the one check.

Fix: `and bool(item['filename'])` ahead of the `_basename` call — a
falsy filename simply fails the match, which is exactly the intended
semantics. Regression test feeds an empty-filename row and asserts the
check fails closed (`calibration_raw_asset_attached` in
`failed_check_keys`, `machine_ready` False) instead of raising.

## 3 — `EditStateError` missing from geometry catch seams (fixed)

`RoomWorkspaceController.replace_room` / `replace_room_topology`
(room_workspace.py:1809/1817) raise `EditStateError("復旧データを処理して
から…")` when `recovery_candidate is not None`. `recovery_candidate` can
legitimately be set while a sketch or drag is in flight — it is loaded
by `_sync_recovery`, which early-returns only when *it* is asked to
save, not when one already exists from a prior dirty state.

Every sibling edit surface (`room_workspace.py` `open_seating_layout`,
the transform controllers) catches `(EditStateError, ValueError, …)` —
that is the seam's stated error contract. Four call sites in the
geometry layer caught only `(ValueError, WallTopologyError)`:

- `room_geometry_input.py` `_keypress_edit` vertex nudge (line ~446)
- `room_geometry_input.py` `_close_sketch` (line ~646)
- `room_geometry_input.py` `_commit_vertex_drag` (line ~677)
- `room_geometry_input.py` `_commit_wall_drag` — had *no* try at all
- `room_geometry_panel.py` `_run` (the shared operation runner)

An `EditStateError` therefore propagated raw through the Qt event filter
(user sees a crash-level traceback mid-gesture) or through the panel's
generic runner, bypassing `mark_pending_editor_rejected` and the error
notice.

Fix: add `EditStateError` to each catch tuple and wrap `_commit_wall_drag`
in the same try/refresh/render pattern its two sibling commits use — the
diff deliberately mirrors `_commit_vertex_drag` line-for-line. Tests
drive `_close_sketch` and `_commit_vertex_drag` with a truthy
`recovery_candidate` stub and assert fail-closed (status text / no
raise), plus a panel `_run` test asserting the notice is populated.

## Deferred / verified-clean summary

The systematic scans also produced the verified-OK and false-positive
rows above; each was read end-to-end rather than filtered out
mechanically. Notably the return-annotation scan collapsed to three
`-> object` functions (annotation admits None) and four nested-helper
false positives; the timestamp-format divergence across
`clock`/`activity_center`/`intervention_planner_panel`/
`support_diagnostics` is real but never mixes formats inside one stored
field, so no ordering bug is reachable today — flagged here only so a
future round can revisit if stores start sharing a timeline.
