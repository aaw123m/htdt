"""Opt-in ``.htdtcapture`` watch folder (REV42).

A paired Capture device — or the user dragging export files into a drop
folder — currently stages a bundle only if the app is restarted with the
file as a launch argument or the bundle is opened by hand. That is the
same gap ``integrations.rew_watch_dir`` filled for REW text drops, so the
contract is mirrored deliberately:

- Opt-in: the preference path being empty disables everything.
- Baseline: files that pre-date watching are marked seen, never staged —
  the folder is for *new drops*, not a bulk import of whatever was there.
- Settle: a file is only routed once its ``(mtime_ns, size)`` signature
  survives two consecutive scans, so a mid-copy bundle is never read
  truncated.
- Stage, never promote: delivery goes through ``route_capture_intent``
  which ingests the bundle and stages it into the Capture Inbox for
  review — exactly what document-open does — and stamps the honest
  ``watch_folder`` arrival source instead of ``document_open``.
- Bounded retry: a failed route re-queues the drop (the scanner's seen
  marker is removed) up to ``_ROUTE_MAX_ATTEMPTS`` times, then keeps the
  marker so a wedged file stops cycling — a rewrite re-delivers it.

The runner itself only surfaces outcomes; the inbox page refresh, the
Activity Center entry and the statusbar line are the app's job.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import MutableMapping

from PySide6.QtCore import QObject, QTimer, Signal

from .cad_repository import SceneRepository
from .launch_intents import build_launch_intent
from .launch_router import route_capture_intent
from .native_worker import NativeWorkerPool, WORKER_CANCELLED


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


def scan_capture_watch_dir(
    directory: str | Path,
    seen: MutableMapping[str, tuple[int, int]],
    pending: MutableMapping[str, tuple[int, int]],
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
    Returns the settled paths ready for ``route_capture_intent``.
    """

    root = Path(directory)
    sentinel = f'\x00scanned:{root}'
    first_scan = sentinel not in seen
    seen[sentinel] = (0, 0)
    files: list[Path] = []
    observed: set[str] = set()
    try:
        entries = sorted(root.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if (
            not entry.is_file()
            or entry.suffix.lower() != _CAPTURE_SUFFIX
        ):
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        key = str(entry)
        observed.add(key)
        signature = (stat.st_mtime_ns, stat.st_size)
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
        # Per-path routing-attempt counts — mutated on the worker only,
        # alongside ``_seen``/``_pending``, so no cross-thread access.
        self._route_failures: dict[str, int] = {}
        self._in_flight = False
        self._in_flight_since: float | None = None
        # Bumped when a wedged job is abandoned: the abandoned worker may
        # still be mid-loop, and ``_seen``/``_route_failures`` are runner
        # state shared with its replacement — a stale generation must not
        # write to them.
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
        except Exception:
            # A store that cannot answer must fail closed — watching a
            # guessed path would stage the wrong files.
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
            return
        root = Path(watch_dir)
        if not root.is_dir():
            return
        self._in_flight = True
        self._in_flight_since = time.monotonic()
        generation = self._job_generation

        def job(cancel) -> object:
            delivered = scan_capture_watch_dir(
                root, self._seen, self._pending
            )
            results = []
            for path in delivered:
                if cancel.is_set() or self._job_generation != generation:
                    break
                key = str(path)
                try:
                    intent = build_launch_intent(path)
                    result = route_capture_intent(
                        intent,
                        repository=self.repository,
                        arrival_source=WATCH_ARRIVAL_SOURCE,
                    )
                except Exception as exc:  # never let one drop kill the lane
                    _LOGGER.exception(
                        'capture watch routing raised for %s', path
                    )
                    results.append((path, None, str(exc)))
                    succeeded = False
                else:
                    succeeded = result.outcome in _STAGE_SUCCESS_OUTCOMES
                    results.append((path, result, None))
                if self._job_generation != generation:
                    break
                if succeeded:
                    self._route_failures.pop(key, None)
                    continue
                attempts = self._route_failures.get(key, 0) + 1
                self._route_failures[key] = attempts
                if attempts < _ROUTE_MAX_ATTEMPTS:
                    # Re-queue: dropping the seen marker re-enters the file
                    # as a candidate, so the next scans re-settle and
                    # re-route it instead of leaving the drop silently lost.
                    self._seen.pop(key, None)
            # A failure count for a vanished file is dead weight — a
            # re-drop lands under a fresh count either way.
            if self._job_generation == generation:
                for stale_key in [
                    k
                    for k in self._route_failures
                    if not Path(k).exists()
                ]:
                    del self._route_failures[stale_key]
            return results

        try:
            self._pool.start(
                _TASK_KEY,
                job,
                self._on_completed,
                on_finished=self._job_finished,
            )
        except Exception:
            self._in_flight = False
            self._in_flight_since = None
            raise

    def _on_completed(self, _key: object, result: object, error: object) -> None:
        if self._closed:
            return
        if error == WORKER_CANCELLED:
            return
        if error is not None:
            _LOGGER.warning('capture watch scan failed: %s', error)
            return
        if result:
            self.scan_completed.emit(result)

    def _job_finished(self, _key: object) -> None:
        self._in_flight = False
        self._in_flight_since = None

    def shutdown(self) -> None:
        self._closed = True
        self._timer.stop()
        self._pool.shutdown()
