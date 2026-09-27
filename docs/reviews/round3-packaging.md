# Round 3 review — Packaging / scripts / docs correctness

Branch: `devin/rev3-packaging`. Scope: `scripts/*.ps1`, `scripts/*.py`,
`installer/HTDT.iss`, `docs/` (README, RELEASING, IMPLEMENTATION_STATUS and
design docs), `.gitignore` / repo hygiene. Prior-round findings in
`docs/reviews/round1-*/round2-*` were read first and are not re-reported.

Context that shapes this round: GitHub Actions was removed on this mirror in
`b47f052` ("Remove GitHub Actions workflows (CI not used on this mirror)").
Anything still *requiring* CI — tests that only inspect workflow YAML, docs
that describe CI as the enforcing layer — drifted stale the moment that commit
landed. Most of this round's findings are that drift.

## Fixed in this branch

| # | Severity | Location | Finding | Fix |
|---|----------|----------|---------|-----|
| 1 | medium | `scripts/golden_path_preflight.py` | **Failure path destroyed its own evidence.** With the default temporary work dir, `main()` wrote the Golden Path trace JSON + audit artifacts *into* `work_dir`, then called `shutil.rmtree(work_dir)` when `code != 0` — deleting the trace that exists precisely to debug the failure. Success preserved the dir, failure deleted it: inverted. | Removed the conditional `rmtree` (and the now-unused `import shutil`). Temporary work dirs are never auto-deleted; they hold the failure evidence. On success nothing changed (the dir was already kept). Bounded by OS temp cleanup; `--work-dir` callers unaffected. |
| 2 | medium | `backend/tests/test_dependency_lock.py`, `scripts/build-native.ps1` | **Dependency-lock release invariants lost all enforcement.** `test_release_workflow_tests_the_locked_closure` and `test_ci_workflow_checks_lock_consistency` asserted the *workflow* wired `--require-hashes` / `--verify-installed` / interpreter pinning; with `.github/workflows` deleted they became permanent skips — so nothing anywhere asserted the lock was still enforced, and `build-native.ps1` itself installed the lock without running `check_dependency_lock.py` at all. | `build-native.ps1` now runs `check_dependency_lock.py` (consistency, pre-venv) and `check_dependency_lock.py --verify-installed` (post-lock-install). The two tests were repointed at `build-native.ps1`: one asserts the interpreter pin (`(3, 12)`/`AMD64`) plus `--verify-installed` wiring, the other asserts the consistency check is ordered before `--require-hashes`. `test_workflow_actions_are_sha_pinned` left as-is — it self-skips while workflows are absent and reactivates unchanged if they return. |
| 3 | medium | `scripts/run-n90-hardware-gate.ps1` | **`py -3.12` launcher dependency, inconsistent with every sibling gate.** All sibling hardware gates (n60/n70/n80/n80-o20/n80c/o60) resolve `$RepoRoot\.venv\Scripts\python.exe` with a `Test-Path` guard; n90 alone shelled out to the `py` launcher — absent on machines with a store/installer python, and semantically different (whatever py resolves, not the repo venv). | Now uses `.venv\Scripts\python.exe` with the same Test-Path + throw as the siblings. The venv is untracked, so it survives the script's detached-HEAD gate checkout. |
| 4 | low | `scripts/build-native.ps1` | Stale comments referencing `actions/setup-python in windows-release.yml` and "CI restores it between runs" (the workflow no longer exists). | Comments reworded to describe the actual contract (caller-supplied interpreter; PyInstaller's content-keyed cache). No behavior change. |
| 5 | low | `scripts/export_equipment_catalog_snapshot.py` | Only script in `scripts/` that imports `htdt` without the repo-local `sys.path` bootstrap — fails outright unless `backend` is installed, while siblings self-anchor to `backend/src`. | Added `ROOT`/`sys.path.insert(0, backend/src)` matching the established convention (`# noqa: E402` as elsewhere). |
| 6 | low | `.gitignore` | Build output dirs were unignored: `build-native.ps1` writes `.tmp/n05-package` + `dist-native/`, `build-installer.ps1` writes `dist-installer/` — all under the repo root, all `git add`-able accidents (multi-GB PyInstaller trees, Inno output). | Added `dist-native/`, `dist-installer/`, `.tmp/` to `.gitignore`. |
| 7 | low | `docs/RELEASING.md` | Substantially stale: described `windows-release.yml` running the suite + `--verify-installed`, `ci.yml` lock checks, SHA-pinned Actions, "the `windows-release` workflow uploads" the manifest, "fails CI" test claims. | Rewritten: the manifest is now documented as a local release artifact; the dependency-closure section describes `build-native.ps1` as the packaging gate (consistency check → `--require-hashes` → `--verify-installed`); "Intentional dev-vs-shipped differences" recast as editable-install vs locked closure; "CI verification" section renamed "Test verification" with the assertions that still exist. |
| 8 | low | `README.md` | Two stale CI references: "native release CIの必須gateからも外しています" and "GitHub Actionsで検証できる事項はActionsを優先し". | Dropped the CI qualifier (→ "native releaseの必須gate"); the ops-policy line now points at the local test suite + `scripts/` packaging/validation gates instead of Actions. |
| 9 | low | `docs/IMPLEMENTATION_STATUS.md` | Live-state doc carrying `.github/workflows/ci.yml` as a current-fact bullet, plus ~40 historical "CI #nnn PASS" ledger lines with no marker that CI is gone. | Added a header note: `.github/workflows` CI was deleted in `b47f052`; CI references below are execution records, current verification is local suite + `scripts/` gates. The ci.yml bullet was annotated to say it is historical. |

## Findings — flagged, not implemented

| # | Severity | Location | Finding | Remediation sketch |
|---|----------|----------|---------|--------------------|
| A | medium | `scripts/pytest_shard.py` (365 lines), `scripts/select_affected_tests.py`, `scripts/test_durations.json` (~220 KB) | **Orphaned CI tooling.** Nothing calls them post-`b47f052`: the only invokers were the deleted workflows (`pytest_shard` drove `ci.yml`'s matrix, `select_affected_tests` picked per-PR subsets, `test_durations.json` is the sharder's balancing data). They still work standalone, so this is a keep-or-delete product decision, not a bug. | Either delete all three (they're CI machinery; regenerating test durations needs a suite run anyway), or keep and adjust docstrings so the "CI shard" / "pull request" framing reads as optional local tooling. |
| B | low | `scripts/build-native.ps1`, `scripts/build-installer.ps1` | `GITHUB_SHA` / `GITHUB_RUN_ID` / `GITHUB_*` env fallbacks feed `build_info.json` / the manifest — inert on a CI-less mirror. Harmless (they reactivate if CI returns), but they imply a pipeline that doesn't exist. | Either leave (self-healing) or strip the `GITHUB_*` reads and let `build_id`/`commit_sha` come only from git metadata. Cheap either way; flagged because it touches release-metadata provenance. |
| C | low | `scripts/select_affected_tests.py` `_CORE_PREFIXES` | Still lists `.github/` — technically stale. Harmless: the prefix list only classifies changed paths; if `.github` files ever reappear, treating them as core is the intended semantics. | Leave; it is forward-compatible rather than wrong. |

## Verified clean (no action)

- **Hardware gates** (`run-n60/n70/n80/n80-o20/n80c-hardware-gate.ps1`, `run-o60-owned-room-gate.ps1`): `Invoke-Git` wrappers check `$LASTEXITCODE`, `try/finally` restores the original branch/SHA, dirty-tree refusal, `allowedAfterProduct` allowlists. Their `.github/workflows/ci.yml` allowlist entries are **load-bearing, not stale** — the ci.yml *deletion* itself appears in `ExpectedProductHead..remoteRef` diffs; removing those entries would make future gates fail on the historical merge.
- **`installer/HTDT.iss`**: per-user install to `{localappdata}`, `[InstallDelete]` ordering clears `{app}\HTDT` before recopy, `.htdtproject`/`.htdtcapture`/`.htdt-backup` HKCU associations pass quoted `"%1"`, fallback `AppVersion` equals `htdt.__version__` (pinned by `test_release_identity.py`).
- **`check_dependency_lock.py`**: stdlib-only (tomllib → 3.11+; build interpreter pinned 3.12), `check_lock` + `verify_installed` + `refresh_hashes` are consistent with the lock header's freeze procedure.
- **`validate_update_windows.py` / `validate_backup_pc_migration_windows.py`**: bounded `subprocess.run` timeouts, Inno silent-mode flags correct, identifiers quoted via `_quote_identifier`, per-run temp dirs.
- **`run-local.ps1` / `run-native.ps1` / `Get-HtdtVersion.ps1` / `native_entry.py`**: `py -3.12` is used only to *create* the venv (documented convention — a venv cannot resolve itself); everything after uses `.venv` python.
- **CWD assumptions**: every Python script anchors via `Path(__file__).resolve().parents[...]` — none assume the working directory.
- **No stale module references**: a repo-wide sweep for `native_command_adapter` and round-1/2 deleted symbols finds zero references in `scripts/`, `docs/` (matches are external OSS-project filenames), or packaging config.
- **Version consistency**: `htdt.__version__` ↔ `pyproject.toml` `attr` ↔ `HTDT.iss` fallback ↔ `test_release_identity.py` all agree at `0.2.0.dev0`.
- **Hygiene**: no committed `__pycache__`/`*.pyc`/tmp files; largest tracked blob is the intentional `docs/imported-issues/issue-bodies.json` (~834 KB). `scripts/test_durations.json` and `update_acceptance_old_commit.txt` are intentional data.
- **No `shell=True`/`os.system`** anywhere in `scripts/`; all subprocess calls use arg lists.

## Check results (static — see caveat)

- `powershell.exe` `[Parser]::ParseFile` syntax check: `build-native.ps1` OK, `run-n90-hardware-gate.ps1` OK.
- The three tests repointed in `test_dependency_lock.py` were re-read post-edit; assertions match the actual new wiring in `build-native.ps1` (`check_dependency_lock.py` at line ~47 precedes `--require-hashes` at line ~62; `(3, 12)`, `AMD64`, `--verify-installed` all present).
- **Caveat**: this review VM has no Python interpreter at all (`py`/`python`/`python3` absent; the snapshot's claimed `C:/devin/python/python.exe` does not exist), so `pytest` could not be run. The Python edits are confined to import/structure changes verified by re-reading; running `backend/tests/test_dependency_lock.py` + `golden_path_preflight.py` on a machine with the pinned 3.12 toolchain is the remaining verification step.
