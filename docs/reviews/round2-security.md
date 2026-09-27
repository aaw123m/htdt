# Round 2 — Security & Robustness Review

Scope: deeper pass over `backend/src/htdt` and `frontend/src` after the round-1
fixes (`docs/reviews/round1-security.md`). Verified each candidate by reading
the reachable code path; no already-fixed round-1 finding is re-reported.

## Fixed this round

| ID | Severity | Area | Finding | Resolution |
|----|----------|------|---------|------------|
| R2-F1 | Low | `migration_guard.py` | **Round-1 F4 residual.** `_restore_pre_migration_backup` still used `ZipFile.extractall`, which expands members to their *actual* decoded sizes — the declared-size and member-count sanity checks could be bypassed by a swapped rollback archive (local-attacker disk amplification). | Replaced `extractall` with `_extract_member_bounded`, a streaming extractor mirroring `native_backup.py`: per-member actual-byte cap (`written > file_size` aborts before write), archive-wide `_MAX_ROLLBACK_EXPANDED_BYTES` budget shared across members, check-before-write. `_validate_archive_member` now also rejects symlink members (`S_IFLNK`) and non-regular/special file modes, and duplicate member names are rejected. |
| R2-F2 | Low–Medium | `room_workspace.py`, `cad_spatial_reproduction.py` | Unbounded `path.read_bytes()` on user-picked files — same class as round-1 F8: `import_underlay` read the whole file *before* checking `MAX_SOURCE_BYTES` (64 MiB), and the three mesh-import paths (`import_entity_mesh`, `set_entity_mesh_body`, `import_room_mesh_geometry`) plus `load_sofa_dataset_profile` had no byte bound at all. A large picked file would be fully materialized in memory before any rejection. | All five sites now read through `ingress.read_file_bounded` (stat-preflight + bounded read), keeping `IngressTooLargeError` (a `ValueError`) flowing into the existing dialog error paths. Underlay keeps its 64 MiB `MAX_SOURCE_BYTES` policy and user-facing message; mesh/SOFA reads use the shared `MAX_ATTACHMENT_BYTES` (256 MiB) ingress bound. |
| R2-F3 | Low | `cad_adm_bw64_validator.py`, `cad_spectral_lighting.py` | **XXE / entity expansion.** These are the codebase's only two XML parsers, both `xml.etree.ElementTree.fromstring`. Verified empirically on the shipped interpreter (Python 3.12 / expat 2.7.1): internal entities ARE expanded and pyexpat exposes no billion-laughs protection knobs, so a `<!DOCTYPE>`-bearing ADM `axml` chunk or TM-27/33 spectral file can amplify ~300 bytes into megabytes of tree text. Both parsers are currently reached only from tests, so the exposure is latent — hardening was cheap and fail-closed. | New `xml_guard.contains_xml_doctype` rejects any payload containing `<!DOCTYPE`/`<!ENTITY` (case-insensitive) before parsing; `summarize_adm` returns `None` and `parse_spectral_xml` returns `verdict='invalid'` on such input, consistent with existing malformed-input behavior. |

## Verified clean (checked this round, no findings)

- **FastAPI surface** (`main.py`, `security.py`, `server.py`): legacy app only
  runs when `HTDT_LEGACY_API` is set or a data dir is passed explicitly;
  loopback Host boundary + Origin check on unsafe methods; per-route body
  ceilings via `install_local_request_boundary`; all mutations go through
  pydantic models; SPA catch-all `_resolve_frontend_path` rejects absolute
  paths, drive letters and `..` after normalization; no CORS middleware is
  installed (same-origin only).
- **capture_receiver.py**: TLS with self-signed cert, pairing tokens from
  `secrets.token_hex`, integer-only Content-Length, per-route byte ceilings,
  delivery-id replay → 409, socket timeouts, parameterized SQL, argv-form
  openssl subprocess.
- **Frontend** (`frontend/src`): zero hits for `dangerouslySetInnerHTML`,
  `eval`, `innerHTML`, `document.write`, `javascript:` URLs, `window.open`,
  `postMessage`, `srcdoc`, or bare `target=_blank` links.
- **Deserialization**: no `pickle`, `marshal`, `yaml.load`, `eval`, or `exec`
  anywhere in backend source; JSON parsing is `json.loads`/`model_validate*`
  on bounded payloads.
- **All archive readers**: `capture_bundle.ZipSource` (bounded streaming,
  symlink/special-file rejection, compression-ratio cap, dup detection),
  `project_bundle` (whitelist member set + bounded member reads, no
  extraction to disk), `native_backup` (streaming verified extractor),
  `migration_guard` (fixed this round), `capture_reference`/`support_diagnostics`
  (write or hash-verify only, no untrusted extraction). No `tarfile` use.
- **subprocess** (`scripts/`, `launch_intents`, `acoustic_pffdtd_adapter`,
  `build_info`, `capture_receiver`): every call is argv-list form; no
  `shell=True`, `os.system`, or string commands anywhere.
- **Remaining `read_bytes()`/`read_text()` sites**: all read app-owned data-dir
  state, verified write-backs, or test fixtures — local-write-access-only,
  below the reporting bar.

## Deferred (needs product decisions)

- **Swagger UI on the loopback API** — `main.py` exposes
  `/api/docs` + `/api/openapi.json`. Only reachable on the legacy boundary,
  but it does hand the API surface to anything that reaches the port.
  Sketch: gate both behind `HTDT_LEGACY_API` + an explicit docs flag.
- **`load_sofa_dataset_profile` TOCTOU** — the file is hashed from
  `file_bytes`, then h5py re-opens the path; a local process could swap the
  file between reads so the recorded `source_file_sha256` describes bytes
  other than what was parsed. Now bounded, but a future hardening could feed
  the verified `file_bytes` to `h5py.File` via `fileobj=` so hash and parse
  cover the same bytes.
- **Local-boundary authentication** — the localhost HTTP boundary trusts any
  process running as the user. A per-launch token header would harden
  against rogue local processes and browser content reaching the port, at
  the cost of scriptability/UX changes. Round 1 already judged the loopback
  defaults correct; flagging only as the next step if the threat model
  ever extends to other local processes.

## Regression coverage

`backend/tests/test_security_review_round2.py`:

- Rollback member-type rejection: symlink, FIFO/special file, duplicate
  names, traversal (still rejected).
- Bounded extraction: forged central-directory `file_size` (declared 4,
  actual ~2 MiB) is rejected before any bytes reach the live tree, and an
  honest backup round-trips through the new extractor (assets + DB restored).
- XML guard: DOCTYPE/ENTITY rejected in both parsers (including a
  billion-laughs payload), clean documents still parse.
- Bounded user-file reads: `read_file_bounded` preflights size; the SOFA
  loader rejects an oversized file before touching the parser.
