# Round 16 — network client resilience

Scope: every surface where HTDT acts on, or answers, a live socket —
`rew_api.RewApiClient` + `rew_roomsim_batch.RewRoomSimControlClient` (the
loopback REW HTTP API), `__main__.probe_htdt` (launcher health probe),
`capture_receiver`'s `ReceiverHandler` (paired HTTPS capture endpoint),
`ingress.read_response_bounded` (the shared bounded read both sides call),
and the cancel/staleness guards around in-flight REW work
(`native_worker`, `measurement_editor`, `measurement_page_workspace`).
Method: code-verified plus real stub servers and raw-socket peers in
`backend/tests/test_round16_network.py` — a byte-drip responder, chunked
transfer-encoding senders, and a client that disconnects mid-body —
Python 3.12, Windows, `QT_QPA_PLATFORM=offscreen`, `pytest -n 4`.
Branch `devin/rev16-net`.

## Conventions found (the good parts)

- **Every live request already had a per-operation timeout** —
  `RewApiClient.timeout_s=1.5`, the receiver's `timeout` socket window
  (30 s, documented slow-loris guard), the probe's `timeout_s`. No
  infinite connect/read hangs were found.
- **Byte ceilings already existed end-to-end**: `MAX_REW_API_RESPONSE_BYTES`
  with Content-Length preflight + bounded `read()`, receipt/archive ceilings
  on the receiver, per-array sample limits at decode.
- **Malformed payloads never become state**: every REW JSON body goes
  through shape normalization that raises `RewApiError` before any
  mutation, and the UI side (`measurement_page_workspace._job_completed`)
  routes errors into `operation_error_message` → retryable Japanese
  dialogs rather than partial application.
- **Stale-response and cancel races were already closed**:
  `MeasurementJobGuard` tokens gate application of every REW job result
  (`can_apply` re-checks project + selection context), latest-wins job
  keys drop superseded completions, and `NativeWorker` reports discarded
  late results as `WORKER_CANCELLED`. Verified by reading the guards and
  the prior rounds' coverage.
- **The receiver already verified archive integrity** — declared SHA-256
  and byte count are re-hashed in `handle_delivery`, so a truncated
  transfer never staged silently.

## Findings fixed

| # | Severity | Where | Defect → Fix |
|---|----------|-------|--------------|
| N1 | **high** | `rew_api._get_json`, `rew_roomsim_batch._post_json` | **Every HTTP error was reported as transport failure.** `except (HTTPError, URLError, …) → RewApiUnavailable` meant a real 404 (measurement deleted upstream, endpoint missing on an older REW) surfaced to the user as 「REWに接続できませんでした」 — "cannot connect" — with the wrong recovery hint, and the FastAPI surface answered 503 where the docstringed contract calls for 404. `RewApiNotFound` existed but was unreachable from HTTP statuses. Now `map_http_error`: 404 → `RewApiNotFound`, other 4xx → `RewApiError` (the endpoint answered; the request was rejected), 5xx → `RewApiUnavailable`. Regressions: `test_404_maps_to_not_found[_end_to_end]`, `test_500_maps_to_unavailable`, `test_400_maps_to_api_error_not_unavailable`, `test_roomsim_post_*`. |
| N2 | **medium** | `ingress.read_response_bounded` | **No wall-clock bound on a transfer.** The socket timeout bounds each recv; a peer dribbling one byte under that window held the read open forever — on a worker thread whose cancellation is only logical, that is a permanently wedged job (the UI recovers, the thread does not). New `deadline_s=` parameter drives a `read1` chunk loop re-checking `time.monotonic()` between recvs; both REW clients pass `transfer_timeout_s` (default 30 s, injectable). Regressions: `test_bounded_read_deadline_stops_drip_feed`, `test_slow_drip_get_raises_unavailable_via_stub_server`. |
| N3 | **medium** | `capture_receiver.do_POST` | **A truncated request body was silently accepted.** `self.rfile.read(length)` returns whatever the peer sent before going away — `len(body) != length` was never checked, so a disconnect mid-transfer fell through to `handle_delivery` with short bytes (rejected later by the SHA-256 check, but logged as a hash mismatch rather than the real cause: an incomplete request). Now: `400 'incomplete request body'` + connection close. Regression: `test_truncated_post_body_rejected` (raw socket). |
| N4 | **high** | `capture_receiver.do_POST`/`do_GET` | **Keep-alive desync on unread declared bodies.** Every early rejection — unknown route, invalid/negative `Content-Length`, over-ceiling length, `Transfer-Encoding` bodies (never decoded) — answered without consuming the announced body. On a kept-alive connection the leftover bytes would be parsed as the *next* request: `handle_one_request` would read e.g. `GARBAGE` or chunk frames as a request line and answer 400 to a request the client never sent, silently shifting every subsequent request/response pairing. Now: `Transfer-Encoding` → 400, invalid CL → 400, oversize → 413, unknown route → 404 — each followed by `close_connection` + an honest `Connection: close` header; GET with a declared body answers then closes. Regression: `test_chunked_post_rejected_and_connection_closed`, `test_oversized_post_closes_connection`, `test_unknown_route_post_with_body_closes_connection`, `test_get_with_declared_body_closes_instead_of_desyncing`, `test_keep_alive_ordering_after_fully_drained_post`. |
| N5 | **low** | `__main__.probe_htdt` | **Unbounded read in the launcher probe.** `response.read()` buffers whatever an endpoint streams back; a mismatched service answering `/api/health` with a huge body would stall the instance-discovery loop. Body now capped at 64 KiB + 1 — oversize → `False`. Regressions: `test_probe_rejects_oversized_body`, `test_probe_rejects_stalled_body`. |
| N6 | **medium** | `main.py` `rew_frequency_response` | **Missing 404 surface.** The only REW endpoint lacking `except RewApiNotFound → 404` — with N1 a deleted measurement's frequency response would have answered 502 blaming the upstream instead of 404 naming it. Regressions: mapping covered by N1 tests + existing endpoint suite. |
| N7 | **low** | `capture_receiver._respond` | **Broken-pipe responses raised through `handle_one_request`.** `wfile.write` on a departed peer raised `OSError` mid-response. Not harmful (the socket dies anyway) but noisy; writes are now wrapped and drop the connection cleanly. |

## Verified honest (checked, no change needed)

- **No retry hammering**: no client retries automatically anywhere — and that
  is correct here. `set_roomsim_*` writes are not idempotent (a retry could
  double-apply a position move), and read retries are the user's call via
  the retryable-error dialog (`RETRYABLE_ERROR_CODES` includes `rew.*`).
  A transient failure surfaces fast (1.5 s) instead of multiplying load on
  a struggling REW.
- **Cancel semantics**: `NativeWorker.cancel` is cooperative; a late REW
  result is reported `WORKER_CANCELLED` and dropped by the job guard, so a
  response arriving after the user navigated away cannot mutate the new
  context. The dripped-transfer wedge (N2) was the only way to strand a
  thread permanently.
- **Concurrent same-endpoint requests**: each call opens a fresh
  connection (no shared pipelining), and the workspace's purpose-keyed
  latest-wins ordering makes cross-response ordering moot on the UI side.
- **Room Simulator batch transaction**: `run_roomsim_position_batch`
  snapshots pre-state, applies, and `_safe_restore`s on any failure —
  a mid-batch 5xx now reports as unavailability instead of aborting the
  transaction unhandled; restore already logged honestly.
- **State machine**: every error path lands in `operation_error_message`
  + `warn_user`; there is no "loading forever" state reachable from a
  network failure — all REW job completion paths (success, error, cancel)
  settle the indicator.

## Deferred / documented

- **`frontend/src/api.ts` `fetch()` without a timeout/AbortSignal** — the
  legacy browser transport it serves is a retired stack (#598 dev-only
  launcher); flagged for whoever revives it. The native app's REW calls
  are the live path and are bounded by this round's work.
- **`ThreadingHTTPServer` spawns unbounded threads** on the receiver —
  one per connection; a flood of slow connections is bounded by the 30 s
  socket timeout, not by a thread cap. Documented here rather than fixed:
  a thread-pool swap is a larger change than this round's safe-diff rule.
- **`get_audio_preflight` fans out ~16 sequential GETs** with no cross-call
  consistency check — REW can mutate between them (e.g. driver switched
  mid-preflight), yielding a stale mixed view. The roomsim snapshot's
  before/after SHA-256 gate (`get_roomsim_snapshot` raises
  `RewApiConcurrentChange`) is the pattern; applying it to preflight is
  deferred — a wrong mixed preflight surfaces as "retry", not bad state.
- **No resumable downloads**: bundle/mission-package transfers are all-or-
  nothing by design (digest-sealed). A mid-transfer disconnect produces a
  400/413 with the connection dropped — the sender retries the whole
  delivery; dedup keys make a redelivery safe (round-15 verified).
