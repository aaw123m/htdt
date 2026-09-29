# Round 14 — CLI / command-line surface truth

Scope: every way a human, script, or packaging smoke step can invoke this
codebase. Each surface was executed end-to-end on Windows with real
arguments (not just inspected): `--help`, valid input with checked side
effects, and invalid input for exit-code/stderr honesty. Environment:
Python 3.12.10 (NuGet), `backend` installed `pip install -e .[dev]`,
MSYS bash, `QT_QPA_PLATFORM=offscreen` for GUI paths. Branch
`devin/rev14-cli`.

## Surface inventory

| Surface | Entry | --help | Valid input | Invalid input | Verdict |
|---------|-------|--------|-------------|---------------|---------|
| `HTDT.exe` / `python -m htdt.native_cad` / `scripts/native_entry.py` / `htdt-native` | `htdt.native_cad:main` | OK (JP usage, exit 0) | seed/backup/restore/migrate all verified against real data dirs | mutual exclusion, unknown flag, missing value → usage error, exit 2; nonexistent/corrupt archive → honest stderr, exit 1 | BROKEN on non-UTF-8 consoles → FIXED (F1) |
| `python -m htdt` (dev launcher) | `htdt.__main__` | OK | server starts, `/api/health` 200; second instance forwards and exits 0 | no `--data-dir` → guidance + exit 2 | VERIFIED |
| `htdt-capture-import` | `htdt.capture_import:main` | OK | real bundle → exit 0, pure-JSON stdout, idempotent reimport (`stage:'verified'`) | missing `--db` → exit 2; nonexistent artifact → JSON `{"stage","error"}` on stderr + exit 1 | VERIFIED |
| `python -m htdt.acoustic_bakeoff` | `acoustic_bakeoff:main` (`preflight`, `validate-run`) | OK | `preflight` on shipped `benchmarks/acoustics/*.json` → exit 0, valid JSON | missing/malformed `--manifest|--candidates|--run` → **raw traceback, exit 1** → FIXED (F3) | FIXED |
| `python -m htdt.acoustic_bakeoff_readiness` | `acoustic_bakeoff_readiness:main` (`audit`) | OK | `--expect-decision NO_GO` → exit 0, `--expect-decision READY` → exit 2 (decision semantics match docs) | missing files → **raw traceback** → FIXED (F4) | FIXED |
| `python -m htdt.project_performance` | `project_performance:main` | OK | `--project-class p-small --no-measure` → exit 0, JSON report | bad enum / missing `--data-dir` → exit 2 | VERIFIED |
| `python -m htdt.native_editor` / `room_editor` / `wall_editor` (dev shells) | per-module `main()` | **ImportError before argparse even runs** | — | — | FIXED (F2) |
| `scripts/*.py` (50 files with argparse) | per-script `main` | 33 OK | — | — | mixed — see F5–F7 |

## Findings

### F1 — Maintenance CLI crashes *after* succeeding on non-UTF-8 consoles (FIXED)

`python -m htdt.native_cad --data-dir <dir> --seed-synthetic-demo` on a
cp1252 console (any default Windows console without `PYTHONIOENCODING`):
the seed commits the database and assets, then the Japanese success
`print()` raises `UnicodeEncodeError` → exit 1. Same for `--backup`,
`--restore`, `--automatic-backup`, `--migrate-legacy-data`, and even
`--help` (the help text itself is Japanese). Automation therefore sees a
failure *after* the requested work already succeeded — the worst kind of
exit-code lie: a smoke step that retries a "failed" seed now runs against
a populated store.

The asymmetry: `write_stderr` (native_diagnostics.py) is tolerant
(`backslashreplace`-style fallback, swallowing stream errors) while the
raw `print()` calls were not. Fix: `sys.stdout.reconfigure(
errors='backslashreplace')` at the top of `main()`, before argparse —
stdout now degrades to escapes exactly like stderr. Verified: cp1252
seed → exit 0, real `cad-scenes.sqlite3` + `measurement-assets/` written,
stdout carries `\uXXXX`-escaped Japanese; cp1252 `--help` → exit 0.

### F2 — 59 GUI modules unimportable in any process where Qt loads first (FIXED)

`python -m htdt.native_editor --help` — and any `import htdt.X` for 59 of
the ~130 package modules — died with:

    ImportError: cannot import name 'import_string' from partially
    initialized module 'pydantic._internal._validators'

Mechanism: PySide6's `shibokensupport.signature` import hook calls
`inspect.unwrap` on each audited module (`pydantic`,
`pydantic.errors`, `pydantic.fields`, `pydantic.types` are all audited).
`hasattr(module, '__wrapped__')` then fires pydantic's lazy
`__getattr__` migration, which does
`from ._internal._validators import import_string` — while `_validators`
is mid-initialization inside the very import chain being audited (the
crash enters through `pydantic.errors` finishing inside `_validators`'s
own init). Any `from pydantic import Field` that lazily initializes the
fields→types→_validators→errors chain *after* QtCore is imported takes
this path and dies.

Survival was pure import-order luck: `import htdt` (→ `migration_guard`
→ `cad_schema` → pydantic via a different chain) or
`htdt.workflow_shell` happening to initialize pydantic's lazy pieces
before QtCore. The packaged `HTDT.exe`, `python -m htdt.native_cad`,
and pytest all work only because something else wins the race first.

Fix: eagerly finalize the lazy chain in `htdt/__init__.py` —
`import pydantic.errors` first (self-initializes safely even under the
hook: nothing else is mid-init when the hook unwraps it), then
`from pydantic import Field` (pulls fields→types→_validators with
`_validators`/`errors` already complete). After the fix, a fresh sweep
over all ~130 modules shows zero unexpected failures (only
`htdt.server`, which exits by design with `LegacyApiDisabledError`).

### F3 — `acoustic_bakeoff` dumped raw tracebacks for bad input (FIXED)

`python -m htdt.acoustic_bakeoff preflight --manifest <missing>` — the
invocation `docs/R100B_SOLVER_BAKEOFF.md` says "Windows CI executes" —
produced a `FileNotFoundError` traceback at exit 1 instead of a usage
error; malformed JSON likewise produced a `ValidationError`/`JSONDecodeError`
dump. `main()` now wraps the loaders in `try/except (OSError, ValueError)`
→ `parser.error(...)` (exit 2, clean `prog: error:` line) and a
semantically invalid `--run` → `parser.exit(1, ...)` with an honest
one-line message — usage error (2) now distinguishable from a run that
loads but fails validation (1).

### F4 — `acoustic_bakeoff_readiness` same unguarded-loader crash (FIXED)

Identical fix: all four loaders (`--manifest`, `--candidates`,
`--adoption-profile`, `--evidence-ledger`) wrapped → `parser.error`
(exit 2). Verified: valid audit → exit 0, `--expect-decision` semantics
unchanged.

### F5 — `OptimizationWorkspaceWindow` unconstructable → `--legacy-ui` rollback flag dead (FIXED)

`OptimizationWorkspaceWindow.__init__` calls
`_refresh_search_specs()` → `SearchControllerMixin.
_refresh_search_binding_state()` → `self.search_reauthor_button` — an
attribute only `OptimizationWorkflowWorkspace`'s dock creates. Plain
`AttributeError` on every construction; `TheaterEditorWindow` (the
`native_cad` lazy export that `--legacy-ui` launches) is that same class,
so the documented rollback flag (`README.md`, `--legacy-ui`) crashed on
every project open. `search_generate_reason_label` had the same problem
one refresh later. Both are now pre-declared `None` in `__init__`,
matching the file's existing convention (`search_save_button` etc. are
already `| None = None`); the shared refresh guards on `is not None` so
the workflow window's real button is unaffected. Verified: window
constructs and `show()`s offscreen.

### F6 — Two scripts ignore `--help` and execute their workload (FIXED)

`scripts/generate_equipment_catalog_fixture.py --help` rewrote the
pinned `tests/fixtures/equipment_catalog_snapshot_v1.json` (harmlessly —
output was byte-identical — but still a side effect from a help flag).
`scripts/validate_n40_precision_windows.py --help` launched the full GUI
acceptance run. Both now have a bare `ArgumentParser`; `-h` prints usage
and exits 0 without touching anything.

### F7 — 17 research scripts crash on `--help` without the R100B toolchain (DOCUMENTED, not fixed)

`scripts/run_r100b_*` (16 files) and `run_r130d_general3d_validation.py`
die at module import with `ModuleNotFoundError: psutil` (modal scripts
also need `scipy`). These pins (`psutil==7.2.2`, `scipy==1.18.1`,
`pyroomacoustics==0.10.1`) are recorded in
`docs/R100B_PYROOM_STOCHASTIC_GATE.md` as the candidate-analysis
environment — deliberately separate from the shipped `[package]`/
`dev` closure, so no dependency change is proposed here. Flagged so the
missing-help crash isn't mistaken for a code bug.

## Verified-true behaviors (no change needed)

- **Exit-code grammar** on the maintenance CLI: 0 success, 1 operational
  failure (honest stderr via `write_stderr`), 2 argument/lock-contention
  usage error. Second-instance forward → 0. `python -m htdt` without
  `--data-dir` → 2 with guidance.
- **README option table**: every documented flag exists; the only
  implemented-but-undocumented flag is `--workflow-shell`, a deliberate
  `argparse.SUPPRESS` compatibility no-op.
- **`htdt-capture-import`**: stdout is pure JSON on success and pure
  JSON error objects on stderr — CI can `| jq` it.
- **Backup/restore honesty**: `--backup` to a missing parent dir creates
  it (exit 0); restore into a fresh dir works (exit 0); nonexistent and
  corrupt archives both fail with a real stderr message and exit 1.
- **`--migrate-legacy-data`** on an empty dir → exit 0 + valid JSON
  `{"state":"nothing_to_migrate"}`.
- **Packaging smoke invocations** (`validate_update_windows.py`,
  `validate_n90_a15_windows.py`, `validate_backup_pc_migration_windows.py`)
  — `--seed-synthetic-demo`, `--version` (`native_cad.py 0.2.0.dev0+g<sha>`),
  `--backup`, `--restore` — all pass on this checkout via
  `python -m htdt.native_cad`, including `--data-dir` paths containing
  spaces and non-ASCII characters.
- **Env vars**: `HTDT_DATA_DIR`, `HTDT_LEGACY_API`, `HTDT_BUILD_INFO`,
  `HTDT_REW_API_URL` are the only env reads; the documented two are
  honored; no undocumented required vars found.
- `python -m htdt` with a nonexistent `--data-dir` creates the store
  (`htdt.sqlite3` + `assets/`) and serves; `--port` honored; a second
  instance forwards and exits 0.
- `check_dependency_lock.py` → exit 0 ("consistent and hash-pinned").

## Files changed

- `backend/src/htdt/__init__.py` — eager pydantic lazy-chain init (F2)
- `backend/src/htdt/native_cad.py` — tolerant stdout reconfigure (F1)
- `backend/src/htdt/acoustic_bakeoff.py` — loader error handling (F3)
- `backend/src/htdt/acoustic_bakeoff_readiness.py` — loader error handling (F4)
- `backend/src/htdt/optimization_workspace.py` — two `| None` pre-declarations (F5)
- `scripts/generate_equipment_catalog_fixture.py` — argparse (F6)
- `scripts/validate_n40_precision_windows.py` — argparse (F6)

## Test evidence

- 130-module import sweep: 59 ImportErrors before → 0 after (only
  by-design `htdt.server` refusal).
- `pytest tests/test_native_maintenance_cli.py tests/test_acoustic_bakeoff.py
  tests/test_acoustic_bakeoff_readiness.py tests/test_native_launch.py`
  → all pass.
- Full `pytest backend/tests -q -n 4` → see suite result in PR body.
- Manual: every surface in the inventory table invoked on this checkout.
