"""Opt-in ``.htdtcapture`` watch folder (REV42).

A paired Capture device — or the user dragging export files into a drop
folder — currently stages a bundle only if the app is restarted with the
file as a launch argument or the bundle is opened by hand. That is the
same gap ``integrations.rew_watch_dir`` filled for REW text drops, so the
contract is mirrored deliberately:

- Opt-in: the preference path being empty disables everything; editing it
  re-baselines the new folder — a changed path starts a new watch epoch,
  never a cumulative one.
- Baseline: files that pre-date watching are marked seen, never staged —
  the folder is for *new drops*, not a bulk import of whatever was there.
  A scan that cannot list the directory at all claims no baseline, so a
  transient read failure can never convert pre-existing files into
  "new drops" on the following poll.
- Settle: a file is only routed once its ``(mtime_ns, size)`` signature
  survives two consecutive scans, so a mid-copy bundle is never read
  truncated.
- Re-drop: ``seen`` markers for files no longer in the directory are
  evicted — the map stays bounded by the watched file set, and a file
  dropped again after deletion is delivered again instead of matching
  its stale marker (the REW lane's contract, mirrored).
- Stage, never promote: delivery goes through ``route_capture_intent``
  which ingests the bundle and stages it into the Capture Inbox for
  review — exactly what document-open does — and stamps the honest
  ``watch_folder`` arrival source instead of ``document_open``.
- Bounded retry: a failed route re-queues the drop (the scanner's seen
  marker is removed) up to ``_ROUTE_MAX_ATTEMPTS`` times, then keeps the
  marker so a wedged file stops cycling — a rewrite re-delivers it.
- Exhausted drops are not invisible: a route that hits the cap also
  emits a :class:`~htdt.capture_watch_failures.WatchRouteExhaustion`
  record on ``routes_exhausted`` so the app can queue it for manual
  reprocessing (#1022).
- Link/reparse defense (#1019): entries are classified by ``lstat``,
  never by following them — symlinks, junctions and other reparse
  points, non-regular dirents and unreadable entries are SKIPPED with
  an explicit reason on ``entries_skipped`` instead of being delivered.
  The watch identity is the *canonical* root (a spelled-through-link
  root watches the directory it resolves to; a re-pointed root link is
  a new epoch). Delivered drops are routed through
  ``staged_capture_drop``: a verified, bounded copy into a private
  temp dir whose descriptor is bound to the lstat'd dirent, so the
  router never reads the watched path and the settle→route window
  cannot redirect the import.

The runner itself only surfaces outcomes; the inbox page refresh, the
Activity Center entry and the statusbar line are the app's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import time
from pathlib import Path
from typing import MutableMapping

from PySide6.QtCore import QObject, QTimer, Signal

from ...cad_repository import SceneRepository
from ..domain.capture_watch_failures import (
    WatchRouteExhaustion,
    WatchSkippedEntry,
    classify_route_failure,
    classify_stage_failure,
    describe_watch_skip,
)
from ..domain.capture_watch_guard import (
    SKIP_REPORT_LIMIT,
    WatchStageError,
    _StageCancelled,
    classify_watch_entry,
    resolve_watch_root,
    staged_capture_drop,
    sweep_stale_staging,
)
from ...error_boundary import (
    EXPECTED_OPERATION_ERRORS,
    is_authority_failure,
    report_boundary_failure,
)
from ...launch_intents import build_launch_intent
from ...launch_router import route_capture_intent
from ...native_worker import NativeWorkerPool, WORKER_CANCELLED


_LOGGER = logging.getLogger(__name__)

_TASK_KEY = 'capture-watch.scan'

#: Poll cadence for the opt-in watch folder (mirrors the REW lane's tick).
DEFAULT_INTERVAL_MS = 10_000

_CAPTURE_SUFFIX = '.htdtcapture'

#: Arrival source stamped on inbox items the watch folder staged — the
#: same routing as document-open but an honest provenance label.
WATCH_ARRIVAL_SOURCE = 'watch_folder'

#: Routing attempts one dropped file gets before the lane gives up.
#: ``scan_capture_watch_dir`` marks a file seen on delivery, so a failed
#: route would otherwise be invisible to every later scan — one transient
#: failure (a locked database while another writer commits, an ingest
#: hiccup, a descriptor arriving a beat before its companion bundle) used
#: to drop the bundle permanently, and even a restart could not recover
#: it because the baseline pass marks every pre-existing file seen.
#: Below the cap the seen marker is dropped — the scanner's documented
#: requeue path — so the file re-settles and routes again on a following
#: scan. At the cap the marker stays and the file stops cycling; a later
#: rewrite still re-delivers it because the signature change reads as a
#: new drop. Mirrors the REW lane's ``_REW_AUTO_MAX_ATTEMPTS``.
_ROUTE_MAX_ATTEMPTS = 3

#: Outcomes that prove the drop reached the inbox. Everything else —
#: ``failed``, ``invalid_or_unsupported``, ``user_action_required`` or a
#: routing exception — counts as one failed attempt under the cap.
_STAGE_SUCCESS_OUTCOMES = frozenset(
    {'staged_for_review', 'already_staged'}
)

#: One scan must never hold the lane longer than this. ``_in_flight`` is
#: the only re-entry gate and clears via the worker's ``on_finished``, so
#: a route call that blocks forever (e.g. a contended ``BEGIN IMMEDIATE``)
#: would mute every later tick with zero diagnostics. Past the budget the
#: runner cooperatively cancels the wedged job and releases the latch —
#: the same-key ``pool.start`` then detaches the lingering thread under
#: module ownership instead of blocking the GUI on a bounded stop.
_STALL_BUDGET_S = 300.0


@dataclass(frozen=True)
class _WatchScanResult:
    """One scan job's computed next-state plus its routing outcomes.

    The worker computes against *job-local* copies of the epoch dicts —
    it never touches the runner's ``_seen``/``_pending``/
    ``_route_failures`` — and carries the result back inside the
    ``completed`` payload. The runner publishes the next-state on its own
    (Qt owner) thread only while ``(root, generation)`` still names the
    live epoch, so an abandoned worker that wakes up after a stall
    release can never write into the epoch that replaced it (#1014,
    #1021).
    """

    root: Path
    generation: int
    seen: dict[str, tuple[int, int]]
    pending: dict[str, tuple[int, int]]
    route_failures: dict[str, int]
    results: list
    exhausted: list = field(default_factory=list)
    skipped: list = field(default_factory=list)


def scan_capture_watch_dir(
    directory: str | Path,
    seen: MutableMapping[str, tuple[int, int]],
    pending: MutableMapping[str, tuple[int, int]],
    *,
    skipped_out: list[tuple[str, str, tuple[int, int] | None]] | None = None,
) -> list[Path]:
    """Scan an opted-in folder for settled ``.htdtcapture`` drops.

    ``seen`` maps ``str(path) -> (mtime_ns, size)`` for baseline and
    already-delivered files; ``pending`` does the same for candidates
    sighted exactly once. Both persist across calls and are updated in
    place. The per-directory sentinel marks which roots already had their
    baseline pass — files that pre-date watching are marked seen but NOT
    staged (the opt-in contract is *new drops*, not a bulk import).

    A new or changed signature is only *delivered* once it survives
    unchanged into the following scan: a file still being copied defers
    instead of routing a truncated bundle. A file delivered but whose
    routing failed can be re-queued by removing its ``seen`` marker.
    ``seen`` markers for files no longer in the directory are evicted:
    the map stays bounded by the watched file set, and a file dropped
    again after deletion is delivered again instead of matching its
    stale marker. An unreadable listing is no scan at all — nothing is
    claimed and the next call still owes a baseline. Returns the settled
    paths ready for verified staging + ``route_capture_intent``.

    **Link/reparse defense (#1019).** ``directory`` is canonicalized
    (strict resolve) before listing, and every ``.htdtcapture`` entry is
    classified by ``lstat`` — never by following links — via
    :func:`capture_watch_guard.classify_watch_entry`. Anything that is
    not a plain regular file is a *skip*, not a candidate: symlinks
    (external or internal), junctions and other reparse points,
    non-regular dirents, broken links, and unreadable entries. A skipped
    entry still takes a ``seen`` marker — its own lstat signature — so
    an unchanged skip stays quiet, and the first scan's skipped entries
    are baselined silently like every other pre-existing file. New or
    changed skips are appended to ``skipped_out`` (when given) as
    ``(path, reason, signature)``, capped at ``SKIP_REPORT_LIMIT`` so a
    mass-link drop cannot flood the reporting path.
    """

    root = Path(directory)
    canonical = resolve_watch_root(root)
    if canonical is None:
        # Missing or unresolvable — no listing is possible, so no scan
        # happened and nothing is claimed (same contract as an
        # unreadable listing below).
        return []
    sentinel = f'\x00scanned:{canonical}'
    try:
        entries = sorted(canonical.iterdir())
    except OSError:
        # Nothing was listed, so nothing was scanned — claiming the
        # baseline here would let a transient failure stage every
        # pre-existing file as a "new drop" on the next poll.
        return []
    first_scan = sentinel not in seen
    seen[sentinel] = (0, 0)
    files: list[Path] = []
    observed: set[str] = set()
    for entry in entries:
        if entry.suffix.lower() != _CAPTURE_SUFFIX:
            continue
        signature, skip_reason = classify_watch_entry(entry, canonical)
        key = str(entry)
        if skip_reason is not None:
            observed.add(key)
            # The marker is the dirent's own lstat signature — a link
            # reports its link metadata, never its target's — so an
            # unchanged skip is silent and a re-pointed one re-reports.
            marker = signature if signature is not None else (0, 0)
            if seen.get(key) == marker:
                pending.pop(key, None)
                continue
            seen[key] = marker
            pending.pop(key, None)
            if (
                not first_scan
                and skipped_out is not None
                and len(skipped_out) < SKIP_REPORT_LIMIT
            ):
                skipped_out.append((key, skip_reason, signature))
            continue
        observed.add(key)
        if seen.get(key) == signature:
            pending.pop(key, None)
            continue
        if first_scan:
            # Baseline: pre-existing files are marked, never staged.
            seen[key] = signature
            pending.pop(key, None)
            continue
        if pending.get(key) != signature:
            # First sighting — or the signature is still changing because
            # the copy is in flight. Hold one scan so a mid-write file is
            # never routed truncated.
            pending[key] = signature
            continue
        # Same signature on two consecutive scans: the write has settled.
        pending.pop(key, None)
        seen[key] = signature
        files.append(entry)
    for stale_key in [key for key in pending if key not in observed]:
        del pending[stale_key]
    for stale_key in [
        key
        for key in seen
        if not key.startswith('\x00scanned:') and key not in observed
    ]:
        del seen[stale_key]
    return files


class CaptureWatchRunner(QObject):
    """Drives the opt-in ``.htdtcapture`` drop folder on a worker thread.

    The watch path is re-read from preferences every tick (same convention
    as the REW lane): editing the setting takes effect on the next poll
    without a restart, and clearing it disables everything. Routing runs
    on a ``NativeWorkerPool`` job — ingest + stage can take a while for a
    large bundle and the repository is already used from worker threads
    (the project-bundle import lane does the same).
    """

    #: (files) — each element ``(path, result, error)`` for every settled
    #: drop routed this scan: ``result`` is the ``LaunchIntentResult`` on
    #: success or ``None`` when routing itself raised (``error`` then
    #: carries the exception text). Emitted only when at least one file
    #: was routed; empty scans stay silent.
    scan_completed = Signal(object)

    #: (records) — one ``WatchRouteExhaustion`` per drop whose route
    #: attempt count reached ``_ROUTE_MAX_ATTEMPTS`` this scan (#1022).
    #: An exhausted drop also appears in ``scan_completed``'s batch as a
    #: failed route — this signal is the dedicated hand-off into the
    #: persistent failure queue, emitted after ``scan_completed``.
    routes_exhausted = Signal(object)

    #: (records) — one ``WatchSkippedEntry`` per dirent the scan refused
    #: this poll (#1019): links, reparse points, non-regular files,
    #: broken links. Reported once per signature — an unchanged skip is
    #: silent — and capped per scan, so a folder of links cannot spam
    #: the surface. Emitted after ``scan_completed``.
    entries_skipped = Signal(object)

    def __init__(
        self,
        repository: SceneRepository,
        preferences: object,
        parent: QObject | None = None,
        *,
        interval_ms: int = DEFAULT_INTERVAL_MS,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self._preferences = preferences
        self._pool = NativeWorkerPool(parent=self)
        self._timer = QTimer(self)
        self._timer.setInterval(max(1, int(interval_ms)))
        self._timer.timeout.connect(self._tick)
        self._seen: dict[str, tuple[int, int]] = {}
        self._pending: dict[str, tuple[int, int]] = {}
        # Per-path routing-attempt counts. ``_seen``/``_pending``/
        # ``_route_failures`` are mutated ONLY on this (owner) thread: a
        # scan job receives snapshots and hands back a
        # ``_WatchScanResult`` whose next-state is published here, so a
        # detached stale worker can never write into the live epoch.
        self._route_failures: dict[str, int] = {}
        self._watched_root: Path | None = None
        self._in_flight = False
        self._in_flight_since: float | None = None
        # Bumped when a wedged job is abandoned and when a new watch
        # epoch begins: a job's result is published only while its
        # ``(root, generation)`` still names the live epoch, and a stale
        # ``_job_finished`` callback can never unlatch a newer job.
        self._job_generation = 0
        self._closed = False

    def start(self) -> bool:
        """Arm the poll timer and run the first scan; False once closed."""

        if self._closed:
            return False
        self._timer.start()
        self._tick()
        return True

    def _watch_dir(self) -> str:
        try:
            return str(
                self._preferences.get('integrations.capture_watch_dir')
                or ''
            ).strip()
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: preference probe — expected store failures fail closed (no watch dir) and report; sealed-store failures and bugs propagate to diagnostics
            # A store that cannot answer must fail closed — watching a
            # guessed path would stage the wrong files.
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='キャプチャ監視ディレクトリ設定の読み取り')
            return ''

    def _tick(self) -> None:
        if self._closed:
            return
        if self._in_flight:
            if (
                self._in_flight_since is not None
                and time.monotonic() - self._in_flight_since > _STALL_BUDGET_S
            ):
                _LOGGER.critical(
                    'capture watch scan wedged for %.0fs — abandoning job',
                    time.monotonic() - self._in_flight_since,
                )
                self._pool.cancel(_TASK_KEY)
                self._job_generation += 1
                self._in_flight = False
                self._in_flight_since = None
            else:
                return
        watch_dir = self._watch_dir()
        if not watch_dir:
            # Watching is off — the next non-empty path arms a new watch
            # epoch, not a continuation of the last one.
            self._watched_root = None
            return
        spelled = Path(watch_dir)
        root = resolve_watch_root(spelled)
        if root is None:
            # A configured-but-missing or unresolvable directory (a
            # dangling root link, a share that dropped) is a transient
            # gap, not a re-arm: keep the epoch so drops made during
            # the outage still read as new on recovery. (#1019: the
            # watch identity is the *canonical* root — a root spelled
            # through a link watches the directory it resolves to.)
            return
        if root != self._watched_root:
            # A changed watch path starts a new watch epoch: everything
            # in the directory pre-dates it, so re-baseline instead of
            # letting markers from another directory stage or dedupe
            # files (the opt-in contract applies to *this* watch).
            self._seen.clear()
            self._pending.clear()
            self._route_failures.clear()
            self._watched_root = root
            # The epoch boundary also retires every earlier job's
            # snapshot: a result computed under another root or an
            # abandoned job resuming late must never publish here.
            self._job_generation += 1
        self._in_flight = True
        self._in_flight_since = time.monotonic()
        generation = self._job_generation

        # Job-local state. The worker computes the next epoch dicts on
        # its own snapshots and returns them for the owner thread to
        # publish; it never reads or writes the runner's dicts, so an
        # abandoned worker resuming after a stall release cannot corrupt
        # the epoch that replaced it (#1014, #1021). Re-routing the same
        # drop is safe even when a stale job still attempts it: the
        # canonical ingestion/inbox authorities deduplicate on the
        # bundle's content lineage digest, not on (mtime_ns, size)
        # settled-signature hints — a re-delivered identical capture
        # reports ``already_staged`` instead of a second inbox row.
        job_seen = dict(self._seen)
        job_pending = dict(self._pending)
        job_route_failures = dict(self._route_failures)

        def job(cancel) -> object:
            skipped_out: list = []
            delivered = scan_capture_watch_dir(
                root, job_seen, job_pending, skipped_out=skipped_out
            )
            skipped = [
                WatchSkippedEntry(
                    path=path_key,
                    error_kind=reason,
                    detail=describe_watch_skip(reason),
                    mtime_ns=sig[0] if sig else None,
                    size=sig[1] if sig else None,
                    watch_root=watch_dir,
                )
                for path_key, reason, sig in skipped_out
            ]
            results = []
            exhausted = []
            if delivered:
                # A crashed run can leave its temp stage behind — collect
                # it lazily here (own prefix only, staleness-bounded).
                sweep_stale_staging()
            for path in delivered:
                if cancel.is_set():
                    break
                key = str(path)
                routed_result = None
                stage_reason: str | None = None
                try:
                    # TOCTOU discipline: the router never sees the
                    # watched path — only a verified staged copy whose
                    # descriptor identity was bound to the lstat'd
                    # dirent before a byte moved (#1019).
                    with staged_capture_drop(
                        path, canonical_root=root, cancel=cancel
                    ) as staged:
                        intent = build_launch_intent(staged)
                        result = route_capture_intent(
                            intent,
                            repository=self.repository,
                            arrival_source=WATCH_ARRIVAL_SOURCE,
                        )
                except _StageCancelled:
                    break
                except WatchStageError as exc:
                    _LOGGER.info(
                        'capture watch staging refused %s: %s',
                        path,
                        exc.reason,
                    )
                    results.append((path, None, exc.reason))
                    succeeded = False
                    stage_reason = exc.reason
                except Exception as exc:  # error-boundary: per-drop routing — one poison drop must not kill the scan lane; the failure identity is logged and the drop is recorded with its reason (noqa: BLE001)
                    _LOGGER.exception(
                        'capture watch routing raised for %s', path
                    )
                    results.append((path, None, str(exc)))
                    succeeded = False
                else:
                    routed_result = result
                    succeeded = result.outcome in _STAGE_SUCCESS_OUTCOMES
                    results.append((path, result, None))
                if cancel.is_set():
                    break
                if succeeded:
                    job_route_failures.pop(key, None)
                    continue
                attempts = job_route_failures.get(key, 0) + 1
                job_route_failures[key] = attempts
                if attempts < _ROUTE_MAX_ATTEMPTS:
                    # Re-queue: dropping the seen marker re-enters the file
                    # as a candidate, so the next scans re-settle and
                    # re-route it instead of leaving the drop silently lost.
                    job_seen.pop(key, None)
                else:
                    # At the cap the marker stays and the file stops
                    # cycling — the drop is still owed a recovery surface,
                    # so it is reported for the persistent failure queue
                    # (#1022). The kept ``seen`` entry is the fingerprint
                    # of exactly the bytes that failed.
                    signature = job_seen.get(key)
                    if stage_reason is not None:
                        kind, failure_class, detail = classify_stage_failure(
                            stage_reason
                        )
                    else:
                        kind, failure_class, detail = (
                            classify_route_failure(routed_result)
                        )
                    exhausted.append(
                        WatchRouteExhaustion(
                            path=key,
                            error_kind=kind,
                            failure_class=failure_class,
                            detail=detail,
                            attempts=attempts,
                            mtime_ns=(
                                signature[0] if signature else None
                            ),
                            size=signature[1] if signature else None,
                            watch_root=watch_dir,
                        )
                    )
            # A failure count for a vanished file is dead weight — a
            # re-drop lands under a fresh count either way.
            for stale_key in [
                k for k in job_route_failures if not Path(k).exists()
            ]:
                del job_route_failures[stale_key]
            return _WatchScanResult(
                root=root,
                generation=generation,
                seen=job_seen,
                pending=job_pending,
                route_failures=job_route_failures,
                results=results,
                exhausted=exhausted,
                skipped=skipped,
            )

        try:
            self._pool.start(
                _TASK_KEY,
                job,
                self._on_completed,
                on_finished=(
                    lambda key, gen=generation: self._job_finished(key, gen)
                ),
            )
        except Exception:  # error-boundary: in-flight flag reset — a raise must not wedge ``_in_flight`` (the lane would silently stop); any failure type resets it before re-raising (noqa: BLE001)
            self._in_flight = False
            self._in_flight_since = None
            raise

    def _on_completed(
        self, _key: object, result: object, error: object
    ) -> None:
        if self._closed:
            return
        if error == WORKER_CANCELLED:
            return
        if error is not None:
            _LOGGER.warning('capture watch scan failed: %s', error)
            return
        if not isinstance(result, _WatchScanResult):
            return
        if (
            result.generation == self._job_generation
            and result.root == self._watched_root
        ):
            # Atomic publish on the owner thread: the live epoch adopts
            # the job's next-state wholesale. A stale job's state — one
            # that outlived a stall release or a watch-root change — is
            # dropped here even though the pool already drops detached
            # workers' completions; its external routes were nonetheless
            # safe because staging deduplicates on content lineage.
            self._seen.clear()
            self._seen.update(result.seen)
            self._pending.clear()
            self._pending.update(result.pending)
            self._route_failures.clear()
            self._route_failures.update(result.route_failures)
        if result.results:
            self.scan_completed.emit(result.results)
        if result.skipped:
            self.entries_skipped.emit(result.skipped)
        if result.exhausted:
            self.routes_exhausted.emit(result.exhausted)

    def _job_finished(self, _key: object, generation: int) -> None:
        if generation != self._job_generation:
            # A finished callback from an abandoned generation must not
            # unlatch the job that replaced it — pool-detached threads
            # already lose their task record, so this guard covers the
            # queued-callback ordering the pool cannot retract.
            return
        self._in_flight = False
        self._in_flight_since = None

    def shutdown(self) -> None:
        self._closed = True
        self._timer.stop()
        self._pool.shutdown()
