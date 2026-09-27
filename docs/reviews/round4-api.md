# Round 4 review — FastAPI surface

Branch: `devin/rev4-api`. Scope: `backend/src/htdt/main.py` (the entire legacy
API surface — ~40 routes) plus route-adjacent modules `security.py`
(`install_local_request_boundary`), `server.py` (boundary wiring), `models.py`,
`placement_constraints.py`, `search_space.py` (request models), and
`database.py` `Store` error paths. Prior rounds covered
`round1-*.md`/`round2-*.md`/`round3-*.md`; endpoints were only
contract-spot-checked in round 3 — this is the first dedicated API-surface pass.

Context: the legacy API is development-only — `create_app(data_dir=None)`
requires `HTDT_LEGACY_API=1`, docs/OpenAPI are gated behind
`HTDT_LEGACY_API_DOCS=1` (round 3), and `server.py` serves it loopback-only
under `install_local_request_boundary`. Findings below are real but their
exposure is local/dev-tooling, not production.

## Fixed in this branch

| # | Severity | Location | Finding | Fix |
|---|----------|----------|---------|-----|
| 1 | medium | `main.py` `/{path:path}` SPA fallback | **Unknown `/api/*` paths returned 200 `text/html`.** With `frontend/dist` built, a typo'd API call (`/api/contextss`) — and the deliberately gated-off `/api/docs`, `/api/openapi.json` — fell through the router into the SPA catchall and served `index.html` (200). A 200-with-HTML masks client errors far worse than a 404 (fetch clients read `.ok`, then get HTML where JSON was expected). | Two-layer guard. Inside the `frontend_dist.is_dir()` branch, after every concrete `/api/*` route: `api_route` fallbacks on `/api` and `/api/{path:path}` for `GET/HEAD/POST/PUT/PATCH/DELETE` return JSON 404. A belt-and-suspenders `_is_api_path` check in `frontend()` catches case variants (`/API/x`) the case-sensitive route table misses. `OPTIONS` is deliberately not claimed — preflights keep the router's 405 instead of a fake 404. |
| 2 | medium | `models.py` request models | **Non-finite floats passed validation.** `json.loads` accepts `Infinity`/`NaN` literals, and pydantic's default `allow_inf_nan=True` admits them. `RoomSnapshot.width_m: float = Field(gt=0)` rejects `NaN` (comparisons false) but **accepts `inf`** — so `{"width_m": Infinity}` stored a poisoned context payload. Same for unbounded fields (`SpeakerPlacement.aim_xyz`, `AVRConfiguration.volume_db`, `extra` dict floats, `MeasurementPoint.position_precision_m`) and `ComparisonCreate`/`ExcludedBand` bands (`low_hz=Infinity` passes `gt=0`). Downstream serialization of the stored payload then emits invalid JSON (`ValueError` on response encode). | `model_config = ConfigDict(allow_inf_nan=False)` on all 9 models.py classes carrying floats. `placement_constraints.ConstraintSetCreate.check_finite` and `search_space.GridAxis.validate_range` already rejected non-finite — models.py was the only uncovered tree. |
| 3 | medium | `main.py` (validation error path) | **422 response itself crashed on non-finite input.** FastAPI's default `RequestValidationError` handler echoes the offending `input` back in the error detail; `inf`/`nan` cannot be encoded by the strict JSON encoder (`allow_nan=False`), so rejecting the request raised `ValueError` → unhandled 500 — the #2 fix would otherwise trade silent poisoning for a loud crash. | Registered `RequestValidationError` handler that runs `exc.errors()` through `_finite_safe_error_detail` (non-finite floats → their string form, everything else untouched), preserving the standard `{"detail": [...]}` 422 shape. |
| 4 | low | `main.py` `create_constraint_set` | **Missing `except KeyError`.** Every sibling create handler maps store-level `KeyError`→404; this one didn't, so a `KeyError` from `store.create_constraint_set` (e.g. entity row deleted between the `get_context` check and the insert) escaped as an unhandled 500. | `except KeyError → 404` clause matching the convention. |
| 5 | low | `main.py` all 7 project-scoped list endpoints | **`GET /api/projects/{id}/X` returned `[]` for a nonexistent project** — indistinguishable from an empty project, so a mistyped id silently reads as "no data" instead of a client error. (Get-one endpoints already 404'd.) | `store.get_project(project_id) is None → 404 'Project not found'` on `sessions`, `contexts`, `constraint-sets`, `search-specs`, `measurements`, `attachments`, `comparisons`; the two endpoints with a `context_id` filter additionally 404 when the filter context isn't in the project. |
| 6 | low | `database.py` `_store_asset` | **Server-side corruption mapped to 422.** A DB-referenced raw asset missing on disk raised `ValueError` — which every handler maps to "client sent bad request". The client did nothing wrong; the store is corrupt. | New `AssetIntegrityError(RuntimeError)` — deliberately not a `ValueError` — surfaces as a real 500. |

### Error-mapping convention (now uniform)

`KeyError`→404 · `ValueError` (+ `RewParseError`, `FeatureDetectionError`,
`ComparisonError`, `IngressTooLargeError`)→422 · `RewApiUnavailable`→503 ·
`RewApiError`→502 · integrity-failed flags→409 · `sqlite3`/store faults→500.
Findings #4–#6 closed the three places that diverged from it.

## Verified clean (checked, no change needed)

- **Boundary coverage:** `install_local_request_boundary` is installed once on
  the served app in `server.py`, wrapping *every* route — there is no way to
  register a mutating endpoint outside it. Loopback `Host` check (400),
  `Origin` allowlist on unsafe methods (403), per-route
  `Content-Length`/read-body ceilings (413) with bounded chunked reads.
- **Upload-size limits:** universal ≥2 MiB ceiling for unsafe methods;
  `/api/import/preview` and `*/measurements` get `MAX_REW_REQUEST_BODY_BYTES`
  (32 MiB b64 + slack), `*/attachments` get 256 MiB — additionally
  `raw_base64` model fields carry `max_length`. No upload path is unbounded.
- **Path params → filesystem:** none of the 16 `{param}` route params are
  interpolated into file paths. Assets are keyed `{sha256}{sanitized-suffix}`
  (filename contributes only a ≤12-char lowercased suffix); comparison report
  filenames are synthesized from the validated uuid4 prefix; the SPA fallback
  resolves through `_resolve_frontend_path` (round-2 traversal hardening,
  still verified by `test_frontend_containment`).
- **REW upstream proxy:** `quote(x, safe='')` per segment, urlencoded query,
  1.5 s timeout, loopback-only base URL — no SSRF/param-injection surface.
- **CORS:** no CORS middleware exists — same-origin is enforced by the
  boundary's `Origin` check rather than response headers, so there are no
  `Access-Control-Allow-*` assumptions to get wrong.
- **Idempotency:** no PUT/PATCH/DELETE routes exist at all; POSTs append rows
  (standard create semantics). Asset ingest dedupes on `sha256` — re-importing
  identical bytes returns the existing row rather than duplicating.
- **SQL:** all `list_*`/lookups parameterized; integrity checks hit flagged
  rows consistently (409, not stale-data reads).
- **`SearchGenerationCancelled`** is only raised when a `cancelled` callback
  is passed — the endpoint never passes one, so it can't escape as a 500 via
  HTTP.
- **`generate` pagination:** `/search-specs/{id}/generate` already takes
  `offset`/`limit` with `le=MAX_SEARCH_PAGE_SIZE` — the only
  expensive-per-request route is bounded.

## Deferred with sketch

| Item | Why deferred | Sketch |
|------|--------------|--------|
| **Pagination on `list_*` endpoints** — all 7 are unbounded `fetchall()` | Real but low-severity: dev-only local API, per-project entity counts are small by domain (measurements, comparisons — dozens, not millions). The fix is a response-shape change (`{items, total}` envelope or `Link` headers) which ripples into `frontend/src/api.ts` types — a contract change, not a patch. | Add `offset: int = Query(0, ge=0)`, `limit: int = Query(500, le=1000)` per list route; `SELECT … LIMIT ? OFFSET ?` plus `SELECT COUNT(*)` for `total`; return `{'items': …, 'total': …}`; update the TS wrapper to unwrap `.items`. |
| **`response_model` coverage** — only `/api/health` declares one | Same contract-commitment problem: response models would pin shapes currently free-floating as `dict`. The frontend already pins them via TS interfaces, so drift is caught at the wrong layer. | Cheapest durable version is a contract test (assert response JSON keys per route against a checked-in schema snapshot); full `response_model=` per route is the stricter end-state. |
| **`KeyError` inside `create_comparison` after `get_dataset_descriptor`** | Already handled — noted only to record the check: `except KeyError → 404` covers deleted-between-lookup-and-read there too. | — |

## Behavior note (accepted, documented)

`DELETE /api/projects` (a real route with an unclaimed method) now answers
404 JSON via the fallback instead of the router's 405+`Allow`. Unknown-path
consistency was judged worth more than 405 precision on a dev-only API; the
Allow-header hint loss is cosmetic here.

## Files changed

- `backend/src/htdt/main.py` — `/api` 404 fallback + `_is_api_path` guard +
  `_API_FALLBACK_METHODS`, `RequestValidationError` handler +
  `_finite_safe_error_detail`, project/context 404s on 7 list endpoints,
  `KeyError`→404 in `create_constraint_set`
- `backend/src/htdt/models.py` — `allow_inf_nan=False` on the 9
  float-carrying request models
- `backend/src/htdt/database.py` — `AssetIntegrityError` (was `ValueError`)
- `backend/tests/test_review_round4_api.py` — new; 31 regression tests

## Tests

`cd backend && TMPDIR=/c/t C:/devin/python/python.exe -m pytest -q -n 4`
— see branch notes for the run result; new file alone: 31 passed.
