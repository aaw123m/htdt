# Issue 815 — typed error boundaries in workflow UI

## Scope

Classified and narrowed the broad `except Exception` boundaries in
`measurement_page_workspace.py`, `workflow_application.py`, and
`room_workspace.py`. The classification is by product semantics — each site is
assigned to exactly one category below, and the boundary was either narrowed to
the expected-error vocabulary or kept broad with an explicit
`# error-boundary:` classification comment.

## Typed vocabulary

`backend/src/htdt/error_boundary.py` introduces the shared vocabulary. It
reuses the existing `user_facing_error` machinery
(`to_user_facing_error` / `log_operation_error` / `warn_user`) rather than
inventing a parallel hierarchy:

- `EXPECTED_OPERATION_ERRORS` — the tuple that replaces
  `except Exception` at narrowed boundaries: `ValueError`, `KeyError`,
  `OSError`, `RuntimeError`, `sqlite3.Error`, `ValidationError`. These are the
  types the product already treats as expected rejections (domain
  `ValueError`s, REW/IO adapters, store access). `TypeError`,
  `AttributeError`, `AssertionError`, and other programming bugs are
  deliberately outside the tuple so they escape to the central
  `native_diagnostics.push_uncaught_sink` boundary instead of being dressed up
  as recoverable UI states.
- `is_authority_failure(exc)` — detects persistence/integrity/authority
  failures by name suffix (`*IntegrityError`, `*StaleHeadError`,
  `*ConflictError`, `*RefError`, `*SealError`, `*TamperError`,
  `*SchemaError`), MRO (`NativeSchemaError`, `NativeUpgradeError`,
  `ManagedDataUnavailableError`), and sqlite error codes
  (`SQLITE_CORRUPT` / `NOTADB` / `CANTOPEN` / `READONLY`). Needed because the
  domain deliberately keeps all authority exceptions as `ValueError`
  subclasses, so `except ValueError` alone cannot separate a user-input
  rejection from a sealed-store corruption.
- `report_boundary_failure(exc, operation=...)` — logs through
  `htdt.errors` at `ERROR` level for authority failures and
  `WARNING` for ordinary operational ones, preserving the exception's original
  reason string (adapter provenance is never masked).
- `report_unexpected_error(exc, operation=...)` — the single reporting path
  for category-5 surprises: fixed `internal.unexpected` code, JA message
  「予期しない問題が発生しました」, `ERROR`-level log entry.
- `ERROR_BOUNDARY_MARKER` (`'# error-boundary:'`) — the comment convention
  that classifies a broad catch that must stay broad. The AST guard test
  fails if a new `except Exception` appears in the three modules without the
  marker.

## Taxonomy and per-category handling rule

| # | Category | Rule applied |
|---|----------|--------------|
| 1 | Expected user/input error (`ValueError`, `MeasurementWorkflowError`, `EditStateError`, `KeyError`, `ValidationError`) | Surface actionable JA feedback via the existing notice/warn path (`_operation_error_notice`, `_set_notice`, `warn_user`); degrade to the safe option only where the code already did so, and only for these types. |
| 2 | External dependency/adapter (`RewApiError`, `RewParseError`, `OSError`, `RuntimeError` adapters, `sqlite3.Error` non-integrity) | Preserve the exact reason/provenance: `log_operation_error` keeps `technical_detail` with `"{type}: {str}"`; no generic masking that loses the reason. |
| 3 | Cancellation / teardown / cleanup | Stays broad **only** where the handler must finish teardown and re-raise or absorb a secondary fault; each such site carries an `# error-boundary:` comment. |
| 4 | Integrity / persistence / authority / stale-state | Never downgraded to a default UI. `is_authority_failure(exc)` → re-raise so it reaches the uncaught diagnostics boundary; logged at `ERROR` as `authority failure during <op>` when the boundary handles it before raising. |
| 5 | Unexpected programming errors | Excluded from `EXPECTED_OPERATION_ERRORS`; propagate to `sys.excepthook` → `push_uncaught_sink`. `report_unexpected_error` gives them the non-recoverable `internal.unexpected` vocabulary at the central boundary. |

## Converted vs deliberately-broad counts

| Module | Converted to `EXPECTED_OPERATION_ERRORS` or narrower | Kept broad with `# error-boundary:` marker |
|--------|----------|---------|
| `measurement_page_workspace.py` | 75 | 1 — leaked `active_jobs` key cleanup must run no matter the underlying fault, then re-raises (`error-boundary: teardown`) |
| `workflow_application.py` | 15 | 2 — the diagnostics self-report path must never raise (`error-boundary: reporting`); shutdown accounting logs and returns (`error-boundary: teardown`) |
| `room_workspace.py` | 6 | 0 |

Total: 96 narrowed, 3 classified-broad of 99 sites.

## Notable semantic fixes inside the boundaries

- **Journey probes** (`_refresh_journey`, plan-state, pending-checks,
  REW-auto eligibility): narrowed to `MeasurementWorkflowError` where the
  probe's designed meaning is "scene not saved yet"; any other store failure
  propagates instead of masquerading as "unsaved".
- **Registration freshness**: the freshness column's fallback for an
  unverifiable revision check was `freshness.get(id, 'current')` — a stale
  registration read as "最新" (current). The narrowed boundary now maps the
  failure to `'unverified'`, renders 確認不可 instead of 最新, and
  `residual_compute` stays disabled because `can_compute` requires an
  explicit `'current'` freshness.
- **Silent collection degrades** (source/target/pattern/evidence/pin/summary
  refreshers): still degrade to the same UI, but only for expected types and
  with `report_boundary_failure` — previously the failure was invisible to
  logs entirely.
- **`except Exception: pass` sites**: either removed by narrowing, or kept
  with a comment justifying silence (e.g. preference probe where any failure
  legitimately means "no preference").

## Guard

`test_no_unmarked_broad_catches_in_scope_modules` parses the three modules'
ASTs and asserts every remaining broad catch carries
`# error-boundary:` — new silent broad catches fail CI.
