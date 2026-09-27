# Round 7 review — doc↔code truth + REST API / CLI consistency

Branch: `devin/rev7-docapi`. Scope: `README.md`, `docs/IMPLEMENTATION_STATUS.md`,
`docs/REW_API.md`, `docs/RELEASING.md` and doc-table references vs. code;
`backend/src/htdt/main.py` (legacy FastAPI surface) and
`backend/src/htdt/capture_receiver.py` (the production HTTPS surface) for
response-shape / error-format / naming / filter consistency; `scripts/*.py`,
`scripts/*.ps1`, `htdt.native_cad`, `htdt.capture_import`, `python -m htdt` for
CLI surface. Prior rounds: `round4-api.md` established the error-mapping
convention and list-endpoint 404 semantics this round extends.

Context reminder: the legacy API is development-only (`HTDT_LEGACY_API=1` gate,
loopback boundary, docs gated); `capture_receiver.py` is the *production*
LAN-HTTPS surface for HTDT-Capture bundles, so drift there weighs more.

## Fixed in this branch

| # | Severity | Location | Finding | Fix |
|---|----------|----------|---------|-----|
| 1 | medium | `capture_receiver.py` `handle_mission_receipt` | **Receipts skipped the capture-instance binding.** `GET missions` and `GET missions/{id}` both require `X-HTDT-Capture-Instance-ID` and bind the pairing to that device; `POST missions/{id}/receipt` treated the header as *optional* — a receipt without it skipped `_bind_capture_instance` and the body-field match, so any caller holding the pairing token could mark packages received/failed without ever proving device identity. | Require the header → `400 'X-HTDT-Capture-Instance-ID required'`, then bind and compare unconditionally — identical contract to the two sibling mission routes. |
| 2 | low | `capture_receiver.py` `do_GET`/`do_POST` | **Path-suffix leniency on singleton routes.** `GET /htdt-capture/v1/{token}/capabilities/<anything>` served the capabilities document and `POST .../deliveries/<anything>` ingested the bundle — trailing junk was silently ignored while `missions` routes are strict. Silent-suffix tolerance hides client URL bugs (e.g. `deliveries/typo` succeeding). | `capabilities`/`deliveries` now require an empty suffix → `404 'unknown endpoint'`, matching `missions`. |
| 3 | low | `main.py` `GET .../measurements` + `database.py` `Store.list_measurements` | **Missing `context_id`/`session_id` filters.** `constraint-sets` and `search-specs` already take `context_id` (with 404 on foreign context); `measurements` took none, so the UI must fetch every measurement and filter client-side — `MeasurementSessions.tsx` literally does `measurements.filter(m => m.session_id === selectedSessionId)`, and `App.tsx` loads the unfiltered list beside contexts/comparisons. | `?context_id=` + `?session_id=` query params, existence-checked inside the store query (`KeyError('context_not_found'/'session_not_found')` → 404), same convention as the sibling filters. |
| 4 | low | `main.py` `GET .../attachments` + `Store.list_attachments` | **Same gap on attachments.** `AttachmentCreate` already carries `measurement_id`/`context_id`/`kind`, but the list endpoint couldn't filter on any of them — the UI fetches all links (`App.tsx`) to render context/measurement views. | `?context_id=` + `?measurement_id=` (both 404-checked) + `?kind=` validated by the `AttachmentKind` Literal (unknown values → 422, not a silent `[]`). |
| 5 | medium | `scripts/run-local.ps1` | **The documented legacy dev path was broken.** README tells developers to run `.\scripts\run-local.ps1` for the browser stack; the script invoked `python -m htdt` with no `--data-dir`, which since #598 exits 2 with the "development-only launcher" refusal — the script could never reach uvicorn. | `-DataDir` parameter defaulting to repo-local `.local\dev-data` (already covered by `.gitignore` via `.local/`), passed through as `--data-dir`; `-NoBrowser`/`-SkipFrontendBuild` unchanged. |

## Verified clean (checked, no change needed)

- **Response shapes:** every list endpoint returns a bare `list[dict]` — no
  `{items:[...]}` envelope drift exists to fix (envelopes don't appear anywhere).
  Detail objects on get-one endpoints are plain `dict`s; the shape convention
  is uniform even if it isn't `response_model`-pinned.
- **Error format:** both HTTP surfaces answer errors as `{'detail': ...}`
  (FastAPI `HTTPException`/`RequestValidationError` on the legacy API;
  `_json(status, {'detail': ...})` on the receiver). The frontend `api.ts`
  wrapper consumes exactly that field.
- **Error codes:** the round-4 mapping holds — `KeyError`→404,
  `ValueError`+domain errors→422, `RewApiUnavailable`→503, `RewApiError`→502,
  integrity→409, body ceilings→413, retired backup/restore→410. The new
  filters reuse the `KeyError`→404 route; receiver rejects all carry `detail`.
- **Naming:** query params and JSON fields are snake_case throughout
  (`context_id`, `session_id`, `capture_instance_id`, `max_hz`, …); receiver
  headers are `X-HTDT-*`.
- **Idempotent verbs:** no PUT/PATCH/DELETE on either surface; GETs are
  read-only (verified no GET mutates state — receipts/statuses only move under
  POST). Creates are `status_code=201`.
- **Pagination:** the only unbounded-per-request route already paginates —
  `POST search-specs/{id}/generate` takes `offset`/`limit` with
  `le=MAX_SEARCH_PAGE_SIZE`. The domain-bounded `list_*` routes stay deferred
  (below).
- **Doc truth sweep:** every *verifiable* claim checked out — README entry
  points (`htdt-native`, `python -m htdt.native_cad`, `--backup`/`--restore`/
  `--seed-synthetic-demo`/`--document-id`, `.instance.lock` semantics), the
  `410 Gone` backup/restore retirement claim, `docs/REW_API.md`'s 7-endpoint
  table (all exist in `main.py`), `RELEASING.md`'s version chain
  (`htdt.__version__` = `0.2.0.dev0` = pyproject dynamic version), every
  linked file in the README doc table, and the modules
  `IMPLEMENTATION_STATUS.md` names in current-state sections
  (`cad_fir_filter`, `cad_operating_preset`, `capture_receiver`, …) all exist.
  Dated milestone sections are historical records by the doc's own convention
  (「過去の記録は見出しか行に基準日・snapshotと明示」) and were not re-audited.
- **Frontend ↔ API coverage:** all 34 `fetch`/`api()` call sites in
  `frontend/src` map to real routes — no phantom endpoints, no fields the UI
  reads that responses lack (measurement rows already expose `session_id`/
  `context_id`; attachment rows expose `measurement_id`/`context_id`/`kind`).
- **CLI consistency:** `native_cad` argparse is consistent — mutually
  exclusive maintenance group (`--backup`/`--restore`/`--automatic-backup`/
  `--seed-synthetic-demo`/`--migrate-legacy-data`), `--version`, exit codes
  0 ok / 1 maintenance failure / 2 lock contention. `htdt-capture-import`
  (`capture_import.py`) uses positional `artifact` + required `--db`,
  exit 0/1 with `CaptureImportError`, JSON output. `python -m htdt` dev
  launcher: 0 ok / 2 usage-guard & lock-held (consistent with native_cad's
  lock=2). The ~40 `scripts/run_r*_*.py` experiment harnesses are
  intentionally arg-free entry points — no argparse needed.

## Deferred with sketch

| Item | Why deferred | Sketch |
|------|--------------|--------|
| **`list_*` pagination** — still unbounded `fetchall()` behind the new filters | Same verdict as round 4: dev-only API, project-scoped row counts are small; an `{items,total}` envelope is a contract change rippling into `api.ts`. The new server-side filters narrow what's fetched without breaking shape. | `offset`/`limit` query params + `SELECT COUNT(*)`; unwrap in the TS layer. |
| **`response_model` coverage** — only `/api/health` declares one | Unchanged from round 4: contract-pinning is a schema-commitment decision; the UI pins via TS interfaces instead. | Contract test asserting response keys per route, or `response_model=` per endpoint. |
| **`--workflow-shell` suppressed arg referenced by dated docs** | The argparse flag is `help=argparse.SUPPRESS` (compat no-op); only dated/historical docs mention it — by the doc convention those are records, not current claims. | If confusion recurs, point the flag's help (un-suppress it as deprecated) or sweep dated docs with a marker. |
| **Bulk/batch endpoints** (e.g. multi-import) | The UI issues several sequential POSTs only at create-time user actions; no evidence of a hot N-call loop needing a bulk route. The filter additions cover the read-side needs actually observed. | If batch import becomes real: `POST .../measurements:batch` or an `items[]` body variant, per-item result list. |

## Files changed

- `backend/src/htdt/main.py` — `context_id`/`session_id` filters on
  `GET .../measurements`; `context_id`/`measurement_id`/`kind` filters on
  `GET .../attachments`; `AttachmentKind` import.
- `backend/src/htdt/database.py` — `Store.list_measurements` and
  `Store.list_attachments` take the filters; referenced entities are
  existence-checked inside the same connection (`KeyError` → route 404).
- `backend/src/htdt/capture_receiver.py` — `handle_mission_receipt` requires
  `X-HTDT-Capture-Instance-ID` and binds like the sibling mission routes;
  `capabilities`/`deliveries` reject path suffixes (404).
- `scripts/run-local.ps1` — `-DataDir` param (default `.local\dev-data`),
  passed as `--data-dir` so the script actually launches.
- `backend/tests/test_review_round7_docapi.py` — 7 regression tests.
