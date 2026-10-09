# Issue #1019 — Capture watch folder: symlink/reparse defense

## Threat model

The opt-in `.htdtcapture` watch folder is a shared surface: another
machine on the LAN, a sync client, or another local account can place
entries there. Before this change `scan_capture_watch_dir` classified
entries with `entry.is_file()` / `entry.stat()`, which *follow* links —
an `evil.htdtcapture` pointing outside the watched root was
indistinguishable from a real bundle, and the settle→route window was
unprotected: whatever happened to be at the path when the router
finally opened it is what got imported.

Attacker capabilities assumed: can create files, symlinks, junctions
and directories in the watch folder; can modify or replace them
between scans and between settle and route; can point links at
arbitrary paths (including `C:\Windows\...`, `%TEMP%`, UNC shares)
and can time swaps inside the settle→route gap.

Not in scope: an attacker who can write inside the app-managed
`capture-store`/`incoming` directories (that is a bigger compromise
than the watch lane), or rootkits that defeat `lstat` itself.

## Fix shape

**`lstat` classification (`capture_watch_guard.py`).** Every
`.htdtcapture` dirent is classified by `entry.lstat()` before anything
follows it:

| reason | condition |
|---|---|
| `link_external` | symlink whose strict-resolved target escapes the canonical root |
| `link_internal` | symlink whose target resolves inside the root — never routed either |
| `reparse_point` | non-symlink reparse (NTFS junction, app-exec link, cloud placeholder) via `st_reparse_tag` |
| `broken_link` | symlink whose strict resolve fails (dangling) |
| `not_regular` | dirent that is not a regular file (directory, device, FIFO…) |
| `unreadable` | `lstat` itself failed (ACL race, vanished mid-scan) |

Skipped entries get a `seen` marker from the link's *own* `lstat`
signature — an unchanged skip is silent, a re-pointed link re-reports.
Reports are capped per scan (`SKIP_REPORT_LIMIT = 64`) so a folder of
links cannot spam the surface, and each one reaches the app through a
new `entries_skipped` signal → persistent failure queue with
`unsupported` class (retry never fixes a link — the fix is dropping a
real file). Nothing skipped ever produces an inbox row.

**Canonical root = watch identity.** The runner resolves the configured
spelling once per tick (`resolve_watch_root` = strict `resolve()` +
`is_dir()`). The resolved path drives the epoch identity
(`_watched_root`), the scan, and the generation-guarded publish.
Contract: a root spelled through a link/junction *watches the directory
it resolves to* (accept-by-canonicalization); re-pointing the root link
arms a fresh epoch; an unresolvable root is a transient gap — the epoch
is kept and the only filesystem access is the resolve attempt itself.
Containment checks use `os.path.normcase` + `os.path.commonpath`
component-wise (the `ADMINI~1` 8.3 lesson from `output_target.py` —
never a whole-string compare of resolved-vs-spelled).

**Verified staging (`staged_capture_drop`).** A delivered drop is never
read from the watched path by the router. The job copies it into a
private `tempfile.mkdtemp('htdt-watch-*')` stage: `lstat` →
`os.open(O_RDONLY | O_NOFOLLOW | O_BINARY)` → `fstat` identity check
(`st_dev`, `st_ino`, `st_mtime_ns`, `st_size` vs the dirent the scanner
classified) → chunked copy bounded by `MAX_TOTAL_BYTES`/`MAX_ENTRIES`,
cancel-aware → post-copy `lstat` identity re-check. A descriptor bundle
(`.htdtcapture` JSON) additionally stages its `bundle_path` companion —
required to be relative and resolve inside the root; absolute paths,
`..` traversal and link/reparse members are refused
(`descriptor_external`, `bundle_member_blocked`). `build_launch_intent`
only ever sees staged bytes, so a settle→route swap cannot smuggle
different content into import; the original drop is never written to.
Stage directories are always `rmtree`'d on exit and `sweep_stale_staging`
reaps crashed leftovers (>1 h, own prefix only) on the next delivery.

A stage failure is a routing failure with an honest reason
(`WatchStageError` → `classify_stage_failure`): permanent refusals
(`link_*`, `reparse_point`, `not_regular`, `broken_link`,
`descriptor_external`, `bundle_member_blocked`, `oversized`) classify
as `unsupported` and land in the queue immediately classified;
transient ones (`changed`, `vanished`, `unreadable`, `io_error`)
count toward the bounded retry like any other route failure.

**Preserved.** Opt-in disabled → zero filesystem access (unchanged).
Normal real-file drop → identical settle/auto-stage flow, now via the
verified copy. Epoch machinery untouched — job-local snapshots,
generation-guarded publish, re-baseline on root change all behave as
in #1014, now keyed on the canonical root.

## Windows contract (verified on this box)

- `Path.lstat().st_reparse_tag` distinguishes: symlink
  `0xA000000C` (`IO_REPARSE_TAG_SYMLINK`), junction
  `0xA0000003` (`IO_REPARSE_TAG_MOUNT_POINT`) — both skipped; other
  tags (cloud files, app-exec links) are also `reparse_point` — the
  lane never dereferences any of them.
- `mklink /J` junctions and `os.symlink` (with privilege) verified
  live; `Path.resolve(strict=True)` on a dangling link raises
  `FileNotFoundError` → `broken_link`.
- `\\server\share` UNC spellings never compare inside a drive-letter
  root (`normcase` keeps `\\` distinct from `X:\`) — fail-closed.
- A watch root configured *as* a junction/symlink is accepted and
  watches its resolved target (documented contract above).

## Tests — `backend/tests/test_issue_1019_capture_watch_links.py`

21 tests, all verified live on the Windows box (NTFS symlinks via
`os.symlink`, junctions via `mklink /J`):

- classification: external/inside/broken/dir-target symlinks,
  NTFS junction → `reparse_point`, plain dir → `not_regular`,
  `lstat` failure → `unreadable`, UNC-vs-drive containment,
  pre-existing link baselines silently, 300-link scan bounded.
- swaps: settled file → link between scans (skipped, then real file
  re-delivers), `lstat`→`open` inode swap caught by `(dev, ino)`,
  forged same-signature rewrite stages the *new* bytes (router
  decides honestly on what was copied).
- staging: descriptor external/`..` refused, descriptor + in-root
  companion stages and routes, link member inside a dir bundle
  blocks the stage, cancel mid-copy leaves zero temp state.
- runner E2E: external link → `entries_skipped` record, no route
  call, no inbox row; watch root spelled through a junction arms on
  the resolved target and still stages real drops.
