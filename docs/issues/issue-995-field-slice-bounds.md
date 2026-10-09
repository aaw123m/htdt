# Issue #995 — Field-Explorer Slice Rendering Bounded：高密度断面描画の有界化（#994 併合）

## Scope

The field explorer (`FieldExplorerPanel`) computed every slice render on
the GUI thread: `explorer_slice` (exact extraction over up to ~4M
samples), a per-pixel Python raster loop, and `QPixmap.scaled(w*8, h*8)`
— an ~800x800 slice asked for a ~164 MiB pixmap (~394 MiB at the
near-4M-sample edge) while the event loop froze for seconds. A
param-edit burst (plane / coordinate / quantity / session) re-ran the
whole pipeline per keystroke.

This change bounds the whole path (#995) and fixes the stale-paint bug
the sibling issue #994 described: an unsupported or failed selection
left the previous heatmap painted as if it were the current slice.

- **Worker-side compute.** Slice extraction, rasterization and
  `QImage` construction run on one `NativeWorkerPool` (the same
  pattern the measurement workspace uses); `QPixmap` conversion and
  painting stay on the GUI thread. Session build + repository save are
  also worker-side — a ~4M-sample `build_mode_field_explorer_session`
  can take seconds.
- **Debounce + epoch gate (request id).** Every control edit bumps a
  monotonic epoch, cancels the in-flight raster, and queues ONE request
  behind a 120 ms debounce. A worker result only paints when its stamped
  epoch is still live — 100 rapid edits collapse into a single bounded
  final render and stale payloads are dropped, never drawn.
- **Honest display decimation.** `FIELD_SLICE_MAX_RASTER_CELLS`
  (1_000_000) bounds the painted raster; `SliceRaster` subsamples by the
  smallest uniform integer stride that fits and the status line labels
  it (「表示 550x700 (間引き x2 · サンプル本体は不変)」). Canonical samples,
  `FieldSliceView`, and every sealed authority are never touched —
  decimation is display-only and stats (`lo`/`hi`/`hidden`) are measured
  on the FULL slice, so an extremum can never hide inside a dropped
  stride cell.
- **Bounded paint memory.** `FIELD_SLICE_MAX_PIXMAP_EDGE` (2048 px) and
  `FIELD_SLICE_MAX_PIXMAP_PIXELS` (4_194_304) cap the painted QPixmap at
  every DPI — worst case ≈16.8 MiB RGBA vs ~164–394 MiB before.
- **Honest failure + clearing (#994).** Unsupported quantity, missing
  plane/coordinate, deselected/missing session, worker failure, or a
  cancelled render all clear the pixmap + scale bar + probe readout and
  show the mapped failure reason — never the previous slice.
- **No layout feedback loop.** `field_image_label` uses
  `QSizePolicy.Ignored` and the busy row is a fixed-height strip, so
  pixmap/text/busy changes can never move the label's geometry — a
  Resize→re-render→repaint→Resize loop observed on the real GUI is
  structurally impossible. Display-driven repaints (`_request_repaint`)
  keep the current paint until the new raster lands and only fire when
  the device-pixel target diverges >12 px from the applied one
  (dead-band), plus on `ScreenChangeInternal` (DPI move).

## Vocabulary

| Term | Meaning |
|---|---|
| `_slice_epoch` | Monotonic render-request identity; bump on every selection edit, deselect, cancel, or stop. The only results allowed to paint. |
| `_SliceRequest` | Immutable render job — epoch, session id, plane, coordinate, quantity. |
| `_SliceJobResult` | Worker envelope — request + `QImage` + `SliceRaster` + unit/sample_state, or the mapped `error` reason. |
| `SliceRaster` | Orientation-applied `(h, w, 4)` uint8 BGRA buffer + full-resolution `lo`/`hi`/`hidden` + `display_stride` and source/display dims. |
| `_slice_display_stride` | Binary-searched minimal stride satisfying the raster cell budget; 1 inside budget, collapses degenerate grids. |
| `_SLICE_DEBOUNCE_MS` | 120 ms coalescing window; a burst of edits dispatches once. |
| `_BUILD_TASK_KEY` / `_SLICE_TASK_KEY` | Pool task names — `pool.cancel(key)` supersedes in-flight work by name. |
| `stop_workers` / `dispose` | Bounded hide-path stop (pool stays usable) vs physical teardown on dialog close. |

## Fail-closed edges

- **Request identity over parameter hashing.** Only the live epoch's
  result applies; a superseded payload — even a *valid* one — is dropped
  in `_slice_job_completed` and re-verified in `_apply_slice_result`
  against the CURRENT combo state before painting.
- **Paint cleared before compute, not after.** `_refresh_view` clears
  the image + legend synchronously, then queues. A failure therefore can
  only transition from "cleared + reason" to "cleared + reason" — never
  stale → new, stale → fail-stale.
- **Stats at full resolution.** `lo`/`hi`/`hidden` come from the
  unsampled slice; masked and non-finite (NaN/inf) cells count as hidden
  and paint the neutral alpha cell colour.
- **Qt-thread boundary respected.** Workers produce `QImage` (non-GUI
  object) only; `QPixmap` conversion, `scaled`, and label paint run in
  the completion slot. `QPixmap`/`QWidget` are never touched off-thread.
- **Cancellation is honest.** `_cancel_render` abandons pending +
  in-flight work, bumps the epoch so a late worker result drops, and
  reports 「キャンセルしました」 rather than pretending a state.
- **Deselect is a clear, not a freeze.** `(セッションを選択)` / a deleted
  session id unloads the model, disables the 3D overlay path, clears
  every painted artifact and shows the honest empty state.
- **Probe readout is context-bound.** The probe label belongs to
  (session, quantity); leaving that context clears it — a stale probe
  value can't masquerade as the current quantity's sample.
- **Armed 3D overlay can't outlive its request.** A clear that makes the
  overlay request impossible unchecks the toggle (emitting
  `field3DCleared`) so the viewport clears too.

## Determinism

Render requests are the only nondeterminism-bearing input (timing), and
the epoch gate makes them convergent: every finished render either
paints the current selection or nothing. Decimation stride is a pure
function of (rows, cols, budget); raster colour, orientation and mask
propagation are pure functions of the `FieldSliceView`. Identical
selections always produce identical painted bytes.

## Evidence (offscreen, this box)

Worker-side `_slice_raster` + `_slice_qimage` (the whole per-render
cost the GUI thread previously ate):

| Slice | Stride | p50 | p95 | Painted raster |
|---|---|---|---|---|
| 160x200 (typical) | 1 | 1.4 ms | 1.8 ms | 0.1 MiB |
| 1100x1400 (> budget) | 2 | 81.7 ms | 86.8 ms | 1.5 MiB |
| 500x630 | 1 | 34.9 ms | 38.4 ms | 1.3 MiB |

Process RSS growth across the run: ~0 MiB. Worst-case painted QPixmap
is `<= 2048x2048` ≈ 16.8 MiB regardless of slice size or DPI — the
previous `*8` upscale reached ≈394 MiB near the 4M-sample edge. The
real-GUI evidence (responsiveness during a dense slice + failure-state
honesty, Mesa GL launch) is attached to the PR.

## Residuals

- A worker inside one uninterruptible sealed computation (e.g.
  `build_rectangular_mode_field` mid-build) cannot be stopped between
  bytecodes — cancellation is cooperative at call boundaries. The
  dialog-hide path therefore detaches instantly (`stop_all(0)`); the
  lingering worker's late completion is dropped by the epoch gate. A
  close-during-build still waits out the current sealed call's GIL
  phase (observed ≈20 s on a ~1.9M-sample build) — finer checkpoints
  inside the sealed builder are a separate scope decision.
