# Round 14 review — HTTP API surface truth

Branch: `devin/rev14-api`. Scope: every route in
`backend/src/htdt/main.py` (41 registered routes + the SPA fallback; no
`APIRouter`), the request/response models in `models.py`,
`placement_constraints.py`, `search_space.py`, the REW upstream client
`rew_api.py`, the store error taxonomy in `database.py`, the request
boundary in `security.py`/`server.py`, and the SPA call contract in
`frontend/src/api.ts`. Method: every route was exercised through
`TestClient` with valid, missing-field, wrong-type, out-of-range,
unknown-id, wrong-method, wrong-content-type and traversal inputs, and the
status code plus body shape asserted against the one error contract the
SPA parses (`{"detail": ...}` — `api<T>` reads only `payload.detail`).

Context unchanged since round 4: the legacy API is dev-only —
`create_app(data_dir=None)` requires `HTDT_LEGACY_API=1`, docs are gated
behind `HTDT_LEGACY_API_DOCS=1`, and `server.py` serves it loopback-only
under `install_local_request_boundary` (Host check → 400, Origin allowlist
on unsafe methods → 403, per-route body ceilings → 413). Round 4 fixed the
unknown-`/api/*` fallback, non-finite floats, and the missing `KeyError`
clause; this pass covers what it did not: body-shape consistency at 500,
upstream-not-found semantics, repr leakage in 404 detail strings, and
whitespace validation on names.

## Fixed in this branch

| # | Severity | Location | Finding | Fix |
|---|----------|----------|---------|-----|
| 1 | medium | `main.py` | **An uncaught exception answered `text/plain` "Internal Server Error".** Starlette's default `ServerErrorMiddleware` body is plain text, so a store fault (sqlite error, missing asset file, any bug) produced the *only* non-JSON body on the surface — the SPA's `api<T>` reads `payload.detail` off the JSON it parsed, and on `text/plain` it gets a parse failure instead of an error message. The one body the client cannot degrade gracefully was the one with the least shape. | `@app.exception_handler(Exception)` → logged `logger.exception` + `JSONResponse({"detail": "Internal Server Error"}, 500)`. FastAPI installs class-`Exception` handlers as the `ServerErrorMiddleware` handler, so the response covers every unhandled raise and the traceback still reaches the log (the middleware re-raises for that purpose after sending). |
| 2 | medium | `rew_api.py`, `main.py` | **An upstream *not found* reported as a misleading 502 — or a flat-out wrong message.** `get_frequency_response_snapshot` checked `len(matches) != 1` and always raised `"UUID is not unique"` — 0 matches (the common case: stale/typo'd UUID) produced a 502 claiming a duplicate that does not exist. `get_roomsim_frequency_response` likewise raised `RewApiError` → 502 for an unknown mic position or source, blaming the upstream connection for what is really a bad id. Both made a client-fixable 404 condition look like a REW outage. | New `RewApiNotFound(RewApiError)`: 0-match UUID, unknown mic position, and unknown source now raise it; the two affected routes map it to 404 *before* the `RewApiError`→502 clause. `len(matches) > 1` keeps the (real) 502 since the ambiguity is upstream's. Inheritance keeps every other caller (`except RewApiError`, `user_facing_error`'s MRO match) working; a dedicated `rew.not_found` JP entry precedes the generic one for the native path. |
| 3 | low | `main.py` (all 11 `except KeyError` sites) | **404 detail leaked the Python repr.** `detail=str(KeyError('context_not_found'))` produces `"'context_not_found'"` — the detail string arrives wrapped in single quotes, so the SPA displays `'context_not_found'` instead of the sentence sibling routes already use (`'Project not found'`). | `_key_error_detail` maps each store code to the same sentence the literal raises use (`project_not_found` → `'Project not found'`, `parent_context_not_found` → `'Parent context not found'`, etc.), falling back to the unquoted arg for unmapped codes. |
| 4 | low | `models.py` `ProjectCreate` | **`name: "   "` passed `min_length=1`.** A whitespace-only project name stored as `''` (the store strips) — an invisible row in the project list that satisfies validation but is unusable in every name-keyed UI. | `model_validator` rejects blank-after-strip names with `ValueError('name must not be blank')` → 422. |

`user_facing_error.py` also gained the `('RewApiNotFound', 'rew.not_found',
'REWで指定した項目が見つかりません', 'REWの計測一覧を再読み込みしてください')`
entry for the same fault surfaced in the native UI.

## Verified honest (checked, no change needed)

- **Route inventory:** 41 concrete routes, single module, no routers. Every
  SPA `api<T>` call in `frontend/src/api.ts` resolves to a registered route
  with the shape it reads — no dead SPA-facing routes. SPA-unused but
  intentional surface: `/api/integrity` (ops report, `status: error` body
  is the *content* not the transport status — by design), `/api/rew/roomsim/*`,
  `GET /api/projects/{id}`, context geometry, and the retired backup/restore
  pair (explicit 410 pointing at the native authority — honest, keep).
- **Status-code convention** (uniform post-round-4, re-verified per route):
  `KeyError`→404, `ValueError`/`RewParseError`/`FeatureDetectionError`/
  `ComparisonError`/`IngressTooLargeError`→422, `RewApiUnavailable`→503,
  `RewApiError`→502, integrity-flag conflicts→409, store faults→500 —
  no route returned 200-with-error or a 500-on-bad-input.
- **Error shape:** every non-2xx body — 400/403/404/405/409/410/413/422/500/
  502/503 — is `{"detail": str | list}`, parseable by the SPA's single
  `payload.detail` reader. 422 detail is the standard pydantic error list
  with non-finite floats sanitized (`_finite_safe_error_detail`).
- **Method/content-type:** wrong method → 405 JSON; form-encoded bodies on
  JSON endpoints → 422; `OPTIONS`/unclaimed methods → 405 (the `/api/*`
  404-fallback deliberately doesn't claim them); no CORS middleware — the
  boundary's Origin allowlist is the mechanism.
- **Traversal/injection:** `'..%2F'` path segments, 4KB unicode ids, ids
  with `/` — all plain 404s (ids are matched verbatim against scoped keys,
  never joined to paths); oversized bodies → 413 at the boundary; id
  lookups are `id AND project_id` SQL parameters.
- **Response truth:** declared response fields present; enums serialize as
  their `Literal` strings not `Repr`s; no absolute paths, stack frames, or
  store internals in any body (`/api/integrity` reports *relative* asset
  paths — dev-only report, acceptable). `list_*` payloads expose only
  stored metadata.
- **Idempotency:** all creates are honest creates (each POST appends a row);
  measurement/attachment POSTs dedupe the *asset bytes* by sha256
  (`duplicate_asset` flag in the response) while still creating the
  measurement/link row — the documented semantics. `import_measurement`
  cleans up the freshly-stored asset on validation failure. No PUT/DELETE
  surface exists (so no delete-of-nonexistent question). A retried
  comparison POST just creates another comparison — safe.
- **`HTDT_LEGACY_API` gating:** `create_app(data_dir=None)` refuses without
  it; docs gated separately. Both still hold.

## Deferred (consistent with round 4's list)

- **`response_model` coverage:** only `/api/health` declares one — the rest
  return dicts checked here by contract assertions instead of schema.
- **No pagination** on any `list_*` endpoint (round 4 deferred; unchanged).
- **Free-form timestamp strings** (`SessionCreate.started_at`,
  `MeasurementImportRequest.captured_at`) accept arbitrary text —
  display/sort-only fields, validation would only aid cosmetics.
- **`len(matches) > 1`** on REW measurement UUID stays 502 — upstream
  ambiguity, not a client error.

## Verification

- New `backend/tests/test_review_round14_api.py`: 10 tests covering the
  four fixes (uniform JSON 500, sentence 404s incl. `parent_context_not_found`,
  blank-name 422, upstream-not-found→404 ×3) plus contract assertions
  (201 shapes, 405-with-detail, form-post→422, traversal→404).
- `pytest backend/tests/test_review_round14_api.py` — 10 passed.
- Full suite `pytest backend/tests -q -n 4` — see PR body.
