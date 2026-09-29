# Round 14 — scene revision / document-graph truth

Scope: `scene_revisions` + `scene_document_heads` + `scene_revision_labels` +
`scene_recovery_snapshots` (`cad_repository.py`, the immutable-lineage write authority),
`WorkingDocument` / `CommandHistory` (`cad_document.py`), `RoomWorkspaceController`
save/restore (`room_workspace.py`), `RoomHistoryPanel` lineage display
(`room_history_panel.py`), project deletion of revision data (`project_lifecycle.py`),
and the R120 semantic-geometry binding gate inside `_save_in_transaction`. Method:
real revision chains written to a scratch `cad-scenes.sqlite3`, raw-row diffing of the
persisted graph vs. the returned objects, threaded two-writer CAS races, byte-level
payload tampering, a racing head moved mid-restore, an actual
`SemanticAcousticGeometry` compiled from a tetrahedral mesh so the binding check runs
against real self-verifying geometry, and offscreen Qt probes of the panel and
workspace slots. Python 3.12, `QT_QPA_PLATFORM=offscreen`, pytest.
Branch `devin/rev14-scene`.

## Document-graph contract as verified

| Mechanism | Probe | Observed behavior | Verdict |
|---|---|---|---|
| `SceneRepository.save` chain of N revisions | 5- and 55-revision chains, raw rows diffed | Every `parent_revision_id` equals the true predecessor; `content_hash` equals sha256 of `canonical_scene_json(document)`; stored `payload_json` byte-identical to the canonical serialization; `seq` dense and monotone; `list_revisions` returns seq order | Honest |
| Root save | `parent=None` twice | Second root rejected with `duplicate root SceneRevision` | Honest |
| Mid-history "branch" | `save(doc, parent=<old revision>)` | Rejected with `SceneRevisionConflictError('stale parent …')` — mainline history cannot silently fork | Honest |
| Detached branch | `save_detached_revision(parent=<old rev>)` | Row flagged `detached` with reason; `scene_document_heads` untouched; head stays the true editing head | Honest |
| No-op save | Re-save identical content on the head | `created=False`, returns the existing row, no new row, recovery snapshot cleared | Honest (real dedupe) |
| Content-addressed dedupe | Same content under a *different* parent | A NEW row with the SAME `content_hash` — the hash is content-addressed but rows are per-lineage-node; dedupe applies at parent level only | Honest (documented scope) |
| Detached dedupe edge | `save_detached_revision` whose content equals the parent's | Returns the non-detached parent row with `created=False` — the "detached" intent silently collapses into the mainline row | Honest-but-noted (the returned `SaveResult` reports truth; callers see `detached=False`) |
| Two-writer CAS | Two threads `save(parent=same head)` off one `BEGIN IMMEDIATE` | Exactly one commits; the loser gets `SceneRevisionConflictError` — no lost update | Honest at the repository layer |
| `RoomWorkspaceController.save` on a moved head | Advance head behind the controller, then save | **Before fix**: raw `SceneRevisionConflictError` (a `ValueError`, not `EditStateError`) escaped — `RoomWorkspace.save()` caught nothing, so the slot crashed instead of surfacing a conflict | **Fixed**: controller translates to `EditStateError`「別の変更で先頭版が更新されました。最新の状態を読み込んでから保存してください」 |
| `RoomWorkspaceController.restore_revision` on a moved head | Head moved between `current_head` read and commit (racing save) | **Before fix**: same raw conflict escaped `_history_restore`'s `except EditStateError` | **Fixed**: same translation, restore-specific JP message |
| `RoomWorkspace.save` slot vs `EditStateError` | Save during pending recovery candidate | **Before fix**: `EditStateError` from `controller.save()` propagated uncaught out of the Qt slot | **Fixed**: caught, surfaced via `_set_operation_error`, returns `False`; the dirty working document is preserved |
| Restore byte-for-byte | Restore rev K, diff stored vs. working | New head's payload byte-identical to stored K (`canonical_scene_json` equal, `content_hash` equal); parent = previous head; working doc rebound clean — restore is append-only, never a rewind | Honest |
| Restore guards | Restore head / missing / cross-document / mid-edit (dirty) / pending recovery / active preview | `EditStateError` with JP message in every case. **Before fix** a same-database cross-document restore reached the repository's raw `ValueError('parent revision belongs to a different document')` | **Fixed**: explicit document-scope guard raises `EditStateError`「対象のリビジョンは別のドキュメントに属しています」 |
| Restore of geometry-bearing revision | Revision carrying `SemanticAcousticGeometry` G1 restored over a head carrying G2 | **Before fix**: `repository.save` raised `ValueError('new R120 semantic geometry must bind to the exact parent SceneRevision')` — G1's `source_scene_revision_id` is hash-bound to ITS original parent and can never equal the new head, so any document whose geometry was ever re-derived could never restore an older geometry revision — and the error escaped as an uncaught `ValueError` | **Fixed**: `_save_in_transaction` now permits a changed geometry whose identical `geometry_id` already exists in the document's own lineage (a reintroduction, not a new derivation). `geometry_id` hashes the full self-verifying core including the source pointer, so the recorded binding stays the true derivation revision and the payload stays byte-exact. A genuinely new geometry with a wrong source is still rejected (regression test added) |
| Tamper detection | Byte-flip one stored `payload_json` | `get()` raises `scene revision hash mismatch` — fail-closed | Honest |
| Recovery snapshot | Save dirty recovery, re-read, clear | Persisted keyed to `source_revision_id`; cleared on commit; dirty draft never presents as a revision | Honest |
| Working document vs. revision | Panel populated only from `list_revisions` | Uncommitted edits never appear as a revision row | Honest |
| `RoomHistoryPanel` lineage display | 5-revision chain + detached row, offscreen | Flat seq-ordered list: every row is a real stored revision, order is honest, HEAD/detached marked — but the persisted `parent_revision_id` graph was invisible; a detached row's branch point could not be seen | **Improved**: detached rows now name their true parent (`◇detached ←<parent label>`). Mainline parents are by construction the previous seq row, so only the invisible fork is annotated |
| Label editor truthfulness | Select a revision that has a stored label | **Before fix**: label/note fields stayed blank — the form claimed no stored label while the row showed one; typing silently replaced it | **Fixed**: fields populate from stored labels on selection change |
| Label storage | `set_revision_label` on real + unknown revision | Real revision labelled without touching payload/hash/lineage; unknown id rejected | Honest |
| Project deletion | Archive → `delete_project` on a doc with mainline + detached + labels + recovery | All revision-family tables swept (`scene_revisions`, `scene_document_heads`, `scene_revision_labels`, `scene_recovery_snapshots`); tombstone row written; `PRAGMA foreign_key_check` clean; `adopt_existing_documents` does not resurrect it | Honest |
| Cross-revision evidence | Authority rows keyed to revision X after restore/head-advance | Unchanged — evidence stays keyed to the revision it measured; restores mint NEW revision ids so no pointer is ever rewritten | Honest by construction |
| Deep history | 55 revisions | Dense `seq`, ordered listing, all parents verified | Honest |

## What "dishonest" looked like, concretely

- **Restore crash on geometry lineage**: the R120 binding gate required a changed
  geometry's `source_scene_revision_id` to equal the commit's parent — true for a new
  derivation, impossible for a restore, where the geometry's binding is frozen into
  its own content hash. Restoring any revision whose geometry had since been
  superseded failed with a raw `ValueError`, making part of the document's recorded
  history unrestorable. The fix distinguishes a *new* derivation (must bind to
  parent) from a *reintroduction* (must be byte-identical to a geometry already in
  this document's lineage).
- **Conflict text hidden from the user**: `SceneRevisionConflictError` — the
  repository's honest "someone else moved the head" signal — is a `ValueError`, but
  every UI-facing guard in this flow raises `EditStateError`. So a real lost-update
  race surfaced as an uncaught exception through `RoomWorkspace.save()` /
  `_history_restore` instead of an actionable message. The controller now translates
  the conflict at the boundary where the user-facing error belongs.
- **Save slot had no failure path at all**: `RoomWorkspace.save()` called
  `controller.save()` bare, so even the routine `EditStateError`s (pending recovery
  draft, active preview) escaped the slot. It now reports through the same
  `_set_operation_error` channel as every other failure and returns `False` without
  dropping the user's dirty state.
- **Lineage display hid the one edge worth seeing**: the persisted graph records
  `parent_revision_id` on every row, but the flat list showed none of it — a
  detached revision (the only row whose parent is NOT the previous seq entry)
  floated with no visible branch point. Detached rows now carry `← <parent label>`.
- **Label form contradicted stored state**: selecting a labelled revision left the
  label/note editors blank, so the UI presented stored truth as absent and made
  blind overwrites easy. Selection now loads the stored values.

## Deferred (documented, not fixed)

- `_rollback_template_creation` deletes *all* `scene_revisions` for the template's
  document_id. Reachable only if revisions exist for that id before the creation
  txn completes (a same-document race or a headless-history edge); template creation
  already guards on `latest(document_id) is None`, so the blast radius is
  theoretical. Left as a note — narrowing the delete to the created revision ids is
  a small follow-up with its own risk.
- Restore scope is the *scene payload* only: sidecar stores (materials, poses,
  video workspace, placements) are document-scoped and unversioned — restoring an
  old scene keeps today's sidecar state. That is the recorded design (#915), but the
  UI copy 「この版に復元」 could read as "everything from that era". Documented, not
  changed.
- `save_detached_revision` dedupe returns the non-detached parent row
  (`created=False`, `detached=False`) when content matches the parent — reported
  truthfully in `SaveResult`, but a caller that ignores the flags could believe a
  detached branch exists when none was written.
- The tree stays flat (insertion order) rather than rendering the full DAG —
  honest as a log now that detached rows name their fork point; a nested view is a
  display feature, not a truth fix.
- `most_recently_created_revision` can return a detached row — documented as
  chronology-only; `current_head` remains the authority.

## Files changed

- `backend/src/htdt/cad_repository.py` — `_save_in_transaction` geometry gate:
  a changed R120 geometry is accepted when the identical `geometry_id` already
  exists in the document's lineage (reintroduction keeps its true derivation
  binding); genuinely new geometry still must bind to the exact parent.
- `backend/src/htdt/room_workspace.py` — `RoomWorkspaceController.save` and
  `restore_revision` translate `SceneRevisionConflictError` to `EditStateError`
  (JP); `restore_revision` rejects revisions of another document before touching
  state; `RoomWorkspace.save()` catches `EditStateError` and reports via
  `_set_operation_error` instead of letting the slot die.
- `backend/src/htdt/room_history_panel.py` — label/note editors populate from the
  selected revision's stored label; detached rows annotate their true parent
  (`◇detached ←<parent display label>`).
- `backend/tests/test_cad_repository.py` — geometry reintroduction vs. forgery
  regressions (real compiled `SemanticAcousticGeometry` over a tetra mesh).
- `backend/tests/test_room_workspace.py` — controller save/restore conflict
  translation and workspace-slot conflict reporting regressions.
- `backend/tests/test_room_history_panel.py` — label-field sync and detached
  branch-point annotation regressions.
- `docs/reviews/round14-scene.md` — this review.
