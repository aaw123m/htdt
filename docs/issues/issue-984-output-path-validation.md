# Issue #984 — パッケージ出力先の型付き検証（Windows パスバグ修正）

## Scope

The presentation export page validated the post-chooser output directory
with `target.startswith(('/', 'C:', 'D:'))`. That check rejected every
valid Windows absolute path outside `C:`/`D:` (e.g. `E:/`, `F:/`), every
`\\server\share` UNC location, and string-passed `C:relative` — a
drive-relative spelling that is **not** absolute on Windows at all.

This issue replaces the two duplicated prefix branches in
`PresentationWorkspace._build_review` and `._build_proposal` with one
typed validation helper, `htdt.output_target.validate_output_target`,
that resolves the spelled path against the real filesystem. Both
builders now share exactly the same rule, show a named reason plus a
concrete alternative on every rejection, and report honest success only
after the written package re-verifies.

Lives in `backend/src/htdt/output_target.py` (the typed rule) and
`backend/src/htdt/presentation_workspace.py` (`_validated_export_dir`
glue, verified-success reporting, 「出力先を開く」 affordance). No new
export format — review/proposal packages are unchanged.

## Vocabulary

| Term | Meaning |
|---|---|
| `OutputTarget` | Frozen result: `root` (the chosen directory) + `package_dir` (`root / package_name`, the exact dir the build writes). |
| `OutputTargetError` | Fail-closed refusal (`ValueError`) carrying a stable machine `reason`, a localized operator message, and a `suggestion`; `operator_text()` renders both for the warning dialog. |
| `package_name` | `review-<session8>` / `proposal-<session8>` — validation covers the *package directory*, not just the chosen root. |
| `_PACKAGE_DIR_LIMIT` | 200 chars — the legacy `MAX_PATH` 260 budget minus ~50 chars of package member names (`renders/<slug>-yaw-120.png` …); long-path opt-in manifests cannot be assumed. |

## Named reasons

`validate_output_target(raw, *, package_name)` raises with one of:

| `reason` | Trigger |
|---|---|
| `empty` | Nothing chosen / label placeholder / blank text. |
| `not_absolute` | `C:relative`, `d:also-relative`, `/posix`, `relative/dir`, bare `\\server`. Grammar-level, via `PureWindowsPath.is_absolute()`. |
| `invalid_chars` | `<>\|?*:` in the tail after the drive prefix, or NUL — Windows can never store them. |
| `drive_unavailable` | Drive letter resolves to nothing (`Path('E:\\').exists()` false or raising) — removed/disconnected media named, not silently re-anchored. |
| `share_unavailable` | UNC anchor `\\server\share\` not reachable — includes non-filesystem shares (IPC$) that error rather than answer. |
| `redirected` | Any spelled component is a symlink or junction — resolved target shown so the operator sees where output would really land. |
| `missing` | Directory does not exist. |
| `not_directory` | Target is a file, or the package name collides with a file. |
| `link_escape` | The package directory itself is a symlink/junction — the build must never be redirected outside the chosen root. |
| `collision` | Package dir already holds artifacts — **no automatic overwrite**, matching the `export_io` atomic staged-then-promoted contract (members must not already exist). An empty pre-created dir is allowed. |
| `path_too_long` | Package dir exceeds the 200-char budget. |
| `not_writable` | Local drive fails the real create/delete probe. |
| `share_denied` | Same probe through a UNC spelling — named as a share permission problem. |
| `io_error` | Reachability probes raising `OSError` — honest transport failure, never mislabeled `missing`. |

Every reason carries a Japanese suggestion line (「フォルダを作成してから
再試行するか、既存の場所を選択してください。」 etc.). The dialog never
falls back outside the operator's chosen root.

## Fail-closed edges

- **Any drive letter, real check.** `A:`–`Z:` all reach the filesystem
  probes; the old C/D whitelist is gone. `PureWindowsPath` grammar runs
  first so `C:relative` is refused before any I/O.
- **Component-wise reparse walk.** `(path, *path.parents)` is checked
  with `is_symlink() or is_junction()` — checking only the leaf would
  miss `C:\link\sub` where `C:\link` is the junction. 8.3 short names
  (`ADMINI~1`) are not reparse points and correctly pass, so a resolved-
  vs-spelled comparison would produce false rejects here; the walk
  avoids that trap.
- **Honest writability probe.** `_assert_writable` does a real
  `os.open(O_WRONLY|O_CREAT|O_EXCL)` + delete inside the chosen root.
  `os.access` is not trusted — on Windows it answers from the access
  token, not the directory ACL. **Not** `tempfile.mkstemp`: its Windows
  `PermissionError` branch re-checks `os.access`, which still reports
  writable on ACL-denied dirs and retries forever — measured on this
  box, it busy-spins instead of failing.
- **Ordering.** Grammar → invalid chars → drive/share reachability →
  reparse components → exists/dir → package-dir link/collision/length →
  writable probe. A half-dead network path reports `share_unavailable`,
  never `missing`.
- **Verified success only.** After `build_*_package` returns, the
  workspace calls `verify_review_package` / `verify_proposal_package`,
  which re-hashes every manifest entry on disk. Only on a clean verify
  does the status line show `生成完了: <exact output_dir>` +
  `エントリ N 件` + `マニフェスト SHA-256: <hash>` and reveal
  「出力先を開く」. A verify failure leaves the button hidden and
  `_last_output_dir` cleared — the affordance can only ever open a
  *verified* build.
- **Scene invariance.** Validation runs before any write, and the
  helpers are pure checks — a rejected target leaves `SceneRevision`,
  `PresentationSession`, head, and the filesystem untouched.

## Tests

`backend/tests/test_issue_984_output_path.py` — 46 tests, one manual leg:

- `TestGrammar` (platform-free): empty / drive-relative / driveless /
  bare-UNC / invalid-char spellings; quoted absolute path acceptance.
- `TestFilesystemMatrix` (Windows-only): valid dir, trailing `\`,
  forward slashes, missing dir, file-as-root, disconnected drive letter
  (dynamically finds an unused letter), missing UNC share, collision,
  empty package dir allowed, package-dir-as-file, path-too-long,
  symlinked/junction root (`redirected`), package-dir link escape,
  writable-probe naming via monkeypatched `_assert_writable`.
- `TestWorkspaceExport`: mocked `QFileDialog` chooser → both builders
  invoke `validate_output_target` with the same rule signature; warning
  dialogs show the named text; invariant scene on every error; verified
  success shows exact path + entry count + manifest SHA-256 + the open
  affordance; a forced verify failure produces no success surface.
- `TestPhysicalTargets`: guarded real-topology legs — every present
  drive letter, loopback `\\localhost\C$` share accepted, missing
  loopback share named, ACL write-deny via `icacls` named `not_writable`
  (local) and `share_denied` (through the UNC spelling). Unreachable-
  network leg (`\\192.0.2.1\share`) stays behind
  `HTDT_TEST_UNREACHABLE_NET=1` — the TCP-445 timeout makes it
  slow by design, run manually.

Result: 45 passed, 1 skipped (the opt-in unreachable leg).

## Known gaps

- UNC coverage is loopback `\\localhost\C$` — a genuinely remote denied
  share and a dead-server timeout need the manual legs (or a lab share).
- `path_too_long` uses the conservative 200-char budget rather than
  probing whether the OS long-path opt-in is enabled — operators get a
  named refusal instead of a mid-build `FileNotFoundError`.
