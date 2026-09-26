# Round-1 Security & Robustness Review

Scope: `backend/src/htdt` plus `installer/` packaging scripts and the
`frontend/` trust boundary where it reaches the backend. Method: manual
trust-boundary tracing of every attacker-controlled input — `.htdtcapture`
bundle ingestion (directory + ZIP wrapper), the paired capture receiver
HTTPS endpoint, `.htdtproject` bundle import, migration rollback archives,
raw-mesh (OBJ/PLY/STL/GLB/HTDTMSH1) import, launch-intent descriptors,
CamillaDSP config sources, REW measurement ingestion, the localhost REW
API, native backup/restore, and the Inno Setup / PowerShell packaging.

Base: `main` @ 28758fa. Findings below were verified by reading the full
code path, not pattern matching.

## Findings

| # | Severity | Location | Description | Status |
|---|----------|----------|-------------|--------|
| F1 | **High** | `capture_receiver.py` `_default_bundle_reader` | The production bundle reader was broken three ways: it treated raw POST bytes as a filesystem path, returned the wrong shape for the `(plan, payloads)` contract, and — on one code path — committed an ingestion itself, which would double-commit inside `handle_delivery`. Any `.htdtcapture` delivered to a receiver running the default reader failed or misbehaved; the endpoint only worked with a test-injected reader. | **Fixed** (19eebc1) — rewritten to mirror `import_capture_artifact`'s read/validate/plan stages (frozen bundle, manifest digest check, bounded reads) without the commit the caller owns. |
| F2 | Medium | `capture_bundle.py` `ZipSource.read_bytes` | All archive preflight checks (entry count, member bytes, total bytes, compression ratio) consult *declared* central-directory headers, but `zf.read(info)` materialized the member's *actual* decompressed stream (~1032:1 deflate amplification ceiling). A forged header could turn a small upload into a very large allocation. | **Fixed** (d68de0c) — streamed `zf.open` read capped at `bound + 1` bytes; length/ratio checks retained. (Note: CPython's `ZipExtFile` already truncates deflate output at the declared size and then fails CRC, so the exploitable window was narrow; the bound no longer depends on decompressor behavior.) |
| F3 | Medium | `project_bundle.py` `import_project_bundle` | Same declared-vs-actual class: `archive.read('manifest.json')`, the doc-body member read, and asset reads were unbounded against the real deflate stream. | **Fixed** (fb3b205) — shared `_read_member_bounded` helper streams each member with a `bound + 1` cap (256 MiB member / 8 MiB-class manifest). |
| F4 | Low–Medium | `migration_guard.py` `_rollback` | Rollback archive manifest read unbounded and `extractall` over a locally-supplied ZIP had only a traversal-name check — no member-count or declared-size sanity bounds. | **Partially fixed** (af8b9dd) — bounded manifest read + member-count/declared-total checks before extraction. **Residual deferred:** `extractall` still expands to actual sizes; a local attacker who can write the data-dir rollback archive could amplify disk usage. Sketch: stream-extract members individually through a `remaining_budget` loop (the `native_backup.py` extractor already implements this pattern — reuse it) or drop the archive format for a manifest+sidecar layout. Requires local write access to the data dir, hence low. |
| F5 | Medium | `capture_receiver.py` `do_POST` | Non-integer or negative `Content-Length` raised `ValueError` inside the handler (connection dropped unanswered, exception in logs); mission-receipt bodies shared the 2 GiB archive ceiling; no per-connection timeout → slow-loris on the TLS port. | **Fixed** (19eebc1) — malformed/negative length → 400, per-route ceilings (`RECEIVER_MAX_RECEIPT_BYTES` = 1 MiB for `/receipt` routes vs the archive ceiling), `timeout = 30 s` on the request handler. |
| F6 | Low–Medium | `capture_receiver.py` `handle_delivery` | `int(declared_bytes)` `ValueError` propagated out of the request handler; a delivery id replayed under different bytes could raise `CaptureReceiverError` from `reject()`/the record write and drop the connection; a replayed id with a *different* archive returned the original receipt (integrity semantics). | **Fixed** (19eebc1) — integer header → 400 reject; record-conflict inside `reject()` → 409; prior-delivery lookup now compares `archive_sha256` → 409 `delivery id replayed with different bytes`. |
| F7 | Medium | `raw_mesh.py` `_parse_glb` / `_append_glb_node` | The GLB node graph is a DAG: cycle detection is path-local (`ancestry`), so a hostile acyclic graph with shared subtrees expands exponentially (2^N node evaluations for a ~60-node file). Deep chains raised `RecursionError` — a `RuntimeError` callers never catch (`import_raw_visual_mesh` documents ValueError/OSError) → interpreter-level crash of the import path. GLB also bypassed the post-parse vertex/triangle caps present on other formats. | **Fixed** (701b56b) — global expansion budget `max(1024, 8·len(nodes))` → `RawMeshImportError('instancing bound')`; `RecursionError` → `RawMeshImportError('too deep')`; cumulative vertex/triangle bounds inside `_append_glb_mesh`; `MAX_RAW_MESH_VERTICES`/`_TRIANGLES` post-parse caps apply to every format including GLB. |
| F8 | Low–Medium | `geometry_import_dialog.py`, `equipment_library.py` | `Path.read_bytes()` on user-picked geometry/directivity source files with no size bound — a multi-GB selection could stall/OOM the desktop process before the parser rejected it. | **Fixed** (e975fb9) — `read_file_bounded(path, MAX_ATTACHMENT_BYTES)` (256 MiB, the shared "generous but finite" ingress policy) at all three sites; `IngressTooLargeError` is a `ValueError`, matching existing UI error surfacing. |
| F9 | Low | `launch_intents.py` `drain_launch_intents` | Queued intent descriptors were read+parsed unbounded — the 256 KiB descriptor bound existed at *write* time but not at drain time, so anything dropped into `incoming/` bypassed it. | **Fixed** (e975fb9) — `stat()` bound before read; oversize goes to `dead/` like other unreadable descriptors. |
| F10 | Low | `cad_camilladsp.py` `load_camilladsp_config` | No input byte bound on CamillaDSP config sources; deep YAML/JSON nesting surfaced `RecursionError` (a `RuntimeError`) to callers that only handle `CamillaDSPError`. | **Fixed** (e975fb9) — 8 MiB `MAX_CAMILLADSP_CONFIG_BYTES`; `RecursionError` → `CamillaDSPError('malformed_config')` on both the JSON and YAML parse paths. |

## Verified clean (no change needed)

- **SQL**: every executed identifier is a module-level constant assembled from fixed strings; all values parameterized. No f-string SQL on user input.
- **Deserialization**: no `pickle`/`marshal`/`eval`/`exec` anywhere in `backend/src`; YAML usage is `yaml.safe_load` only.
- **`security.py`**: loopback Host/Origin middleware with bounded bodies; correct defaults.
- **`native_backup.py`**: streaming verified-extraction with per-member budgets — the strongest extractor in the tree; the `migration_guard` residual should reuse this pattern.
- **`rew_api.py`**: loopback-only bind, bounded array ingestion.
- **`report.py`**: `html.escape` on all interpolated fields.
- **`native_diagnostics.py`**: bearer/secret redaction patterns + 64 KiB record cap.
- **Packaging** (`installer/HTDT.iss`, `build-*.ps1`): hash-pinned lockfile verification, per-user install scope, quoted `%1` file-association verbs — no DLL/load-path or command-injection vectors found.
- **Subprocess**: argv-list invocations with timeouts; no `shell=True`.

## Regression tests

`backend/tests/test_security_review_round1.py` (17 tests) covers: forged
ZIP central-directory size rejection + honest round-trip, the repaired
default bundle reader end-to-end (directory→ZIP→plan/payloads), malformed
archive-byte headers, delivery-id replay conflicts, malformed
`Content-Length` and the receipt ceiling over a real TLS socket, GLB
DAG-expansion/deep-chain bounds, the mesh vertex cap, oversized launch
intents, and CamillaDSP size/depth limits.

## Residual risks & suggestions

1. **`migration_guard` extractall** (F4): still expands to actual member
   sizes; adopt the `native_backup.py` streaming extractor or a
   manifest+sidecar layout.
2. **Receiver sockets**: the 30 s handler timeout bounds each socket op;
   HTTP keep-alive across requests is still bounded by it. Consider an
   explicit request-rate guard if the endpoint is ever exposed beyond
   loopback/LAN.
3. **Frontend**: `frontend/` is a thin React client of the loopback
   backend; no injection surface found worth a fix in this pass.
