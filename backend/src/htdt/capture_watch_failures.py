"""Bounded manual-reprocess queue for exhausted watch drops (#1022).

The opt-in ``.htdtcapture`` watch lane re-routes a failed drop up to
``_ROUTE_MAX_ATTEMPTS`` times and then keeps its ``_seen`` marker — the
file stops cycling but never reached the Capture Inbox, so the inbox's
own search/list surfaces can never show it and the 15-second statusbar
line is the only trace it ever got. This module is the durable surface
for exactly that case:

* :class:`CaptureWatchFailure` — one queued drop: path, basename,
  sanitized error kind, operator-facing reason, attempt count, first and
  last seen, the ``(mtime_ns, size)`` fingerprint recorded when the last
  route attempt failed, and the watch root (watch epoch) it arrived
  under. The full path stays in this app-local store only — diagnostics
  and logs carry the basename and a correlation id, never the raw path.
* :class:`CaptureWatchFailureQueue` — the bounded, atomically persisted
  store under ``data_dir``. Recording is upsert-by-path so a rewritten
  drop refreshes its fingerprint and attempt count instead of piling up
  duplicates; resolving removes the entry once a route finally stages.
* :func:`verify_watch_retry` — the 安全に再試行 gate: the exact recorded
  path must still exist, still carry the recorded fingerprint (a
  replaced or mid-write file is a different drop, not a retry), and the
  watch preference must still point at the recorded watch root (a
  reconfigured folder is a different watch epoch).
* :func:`classify_route_failure` — maps a ``LaunchIntentResult``/routing
  exception onto the fixed error-kind vocabulary and the
  retryable/unsupported/user_action_required class.

Deliberate boundaries: the queue records *delivery* failures only — it
never promotes, never repairs schemas, and an invalid drop keeps showing
its original rejection reason. このファイルを選んで取込 re-routes the
current file contents explicitly at the operator's choice; it is the
operator's decision, not a disguised retry, so it skips the fingerprint
gate and lets the route itself re-validate.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .capture_watch_guard import STAGE_PERMANENT_REASONS
from .clock import utc_now_iso as _utc_now
from .native_diagnostics import diagnostics_dir


_LOGGER = logging.getLogger(__name__)

WATCH_FAILURE_SCHEMA_VERSION = 1
WATCH_FAILURE_QUEUE_FILENAME = 'capture-watch-failures.json'

#: Bounded by contract: the queue is a recovery surface for the operator,
#: not a log. Past the cap the oldest entries fall off.
WATCH_FAILURE_QUEUE_LIMIT = 50

#: Entries expire a bounded period after their last observed failure —
#: the queue persists long enough to be a real recovery path, never
#: forever.
WATCH_FAILURE_TTL_DAYS = 14

#: ``arrival_source`` stamped when a queued file is staged by an explicit
#: operator action from this queue — honest provenance: neither a fresh
#: watch drop (``watch_folder``) nor a document open.
WATCH_QUEUE_ARRIVAL_SOURCE = 'watch_queue_retry'


class WatchFailureClass(StrEnum):
    """What the operator can do about one failed drop."""

    RETRYABLE = 'retryable'
    UNSUPPORTED = 'unsupported'
    USER_ACTION_REQUIRED = 'user_action_required'


#: Localized operator-facing detail for each scan-time skip reason
#: (#1019). The reason vocabulary lives in ``capture_watch_guard``;
#: this map is deliberately keyed by string so the queue surface never
#: hard-fails on a reason it has not seen — unknown kinds fall back to
#: a generic honest line.
_SKIP_DETAILS: dict[str, str] = {
    'link_external': (
        '監視フォルダーの外を指すリンクのためスキップされました — '
        '実体のファイルをフォルダー内に直接置いてください'
    ),
    'link_internal': (
        'リンク経由の取り込みは行いません — '
        '実体のファイルをフォルダー内に直接置いてください'
    ),
    'reparse_point': (
        'ジャンクション/リパースポイント経由の取り込みは行いません — '
        '実体のファイルを直接置いてください'
    ),
    'not_regular': (
        'フォルダー/特殊ファイルは取り込めません — '
        '実体の .htdtcapture ファイルを置いてください'
    ),
    'broken_link': (
        '壊れたリンクのためスキップされました — '
        'リンク先を確認してください'
    ),
    'unreadable': (
        'ファイル情報を読み取れないためスキップされました — '
        'アクセス権を確認してください'
    ),
    'descriptor_external': (
        '記述子が監視フォルダーの外のバンドルを参照しています — '
        'バンドルごとフォルダー内に置いてください'
    ),
    'bundle_member_blocked': (
        'バンドル内にリンク/特殊ファイルが含まれているため '
        '取り込めません'
    ),
    'oversized': 'ファイルが取り込み上限を超えています',
}


def describe_watch_skip(reason: str) -> str:
    """Localized operator-facing detail for one skip/stage reason."""

    return _SKIP_DETAILS.get(reason, f'取り込めません（{reason}）')


def classify_stage_failure(
    reason: str,
) -> tuple[str, WatchFailureClass, str]:
    """Map one staging refusal onto the failure-queue vocabulary.

    Permanent conditions (link/reparse/descriptor escapes, oversize,
    blocked bundle members) classify ``unsupported`` — retry cannot
    heal them and the retry gate refuses them up front, so the operator
    gets an honest "fix the drop" instruction. Everything else is
    ``retryable``: a mid-write file settles, a lock lifts, a share
    returns.
    """

    if reason in STAGE_PERMANENT_REASONS:
        return (
            reason,
            WatchFailureClass.UNSUPPORTED,
            describe_watch_skip(reason),
        )
    detail = {
        'changed': 'ルーティング中にファイルが変化しました — 書き込み完了を待って再試行されます',
        'vanished': 'ファイルが見つかりません（削除または移動されました）',
        'unreadable': 'ファイルを読み取れません — アクセス権を確認してください',
    }.get(reason, '取り込みの準備中にエラーが発生しました')
    return (reason, WatchFailureClass.RETRYABLE, detail)


class WatchRetryVerdict(StrEnum):
    """Outcome of the safe-retry gate."""

    OK = 'ok'
    NOT_RETRYABLE = 'not_retryable'
    DELETED = 'deleted'
    REPLACED = 'replaced'
    EPOCH_MISMATCH = 'epoch_mismatch'


class CaptureWatchFailure(BaseModel):
    """One permanently-failed watch drop awaiting operator reprocessing."""

    model_config = ConfigDict(frozen=True)

    path: str = Field(min_length=1)
    basename: str = Field(min_length=1)
    error_kind: str = Field(min_length=1)
    failure_class: WatchFailureClass
    detail: str = ''
    attempts: int = Field(ge=1)
    first_seen_utc: str = Field(min_length=1)
    last_seen_utc: str = Field(min_length=1)
    # (mtime_ns, size) recorded when the last route attempt failed — the
    # same signature the scanner settles on, so a mid-copy file can never
    # verify as retryable.
    mtime_ns: int | None = None
    size: int | None = None
    watch_root: str = ''
    # Short local correlation id matching the log/diagnostics convention
    # ([diag: XXXX]); stable per path so every record of the same drop
    # carries one id.
    diagnostic_id: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class WatchSkippedEntry:
    """Runner -> app record for one entry the scan refused (#1019).

    ``error_kind`` is the skip vocabulary (``link_external``,
    ``reparse_point``, …), ``detail`` its localized line, and
    ``mtime_ns``/``size`` the dirent's own lstat signature — a link
    carries its *link* fingerprint, never its target's. ``watch_root``
    is the configured (spelled) watch path for queue identity.
    """

    path: str
    error_kind: str
    detail: str
    mtime_ns: int | None
    size: int | None
    watch_root: str


@dataclass(frozen=True, slots=True)
class WatchRouteExhaustion:
    """Runner -> app record for one drop that hit the route cap.

    ``mtime_ns``/``size`` are the signature the scanner kept in ``_seen``
    when it gave up — i.e. the fingerprint of exactly the bytes that
    failed — and ``watch_root`` is the root armed for that scan epoch.
    """

    path: str
    error_kind: str
    failure_class: WatchFailureClass
    detail: str
    attempts: int
    mtime_ns: int | None
    size: int | None
    watch_root: str


def classify_route_failure(
    result: object,
) -> tuple[str, WatchFailureClass, str]:
    """Map one routed outcome onto the sanitized failure vocabulary.

    ``result`` is the ``LaunchIntentResult`` from the route, or ``None``
    when routing itself raised — the raw exception text never enters the
    queue; the log already carries it. ``result.detail`` is the router's
    own localized operator-facing line, safe to keep verbatim.
    """

    if result is None:
        return (
            'routing_error',
            WatchFailureClass.RETRYABLE,
            '取り込み処理で予期しないエラーが発生しました',
        )
    outcome = getattr(result, 'outcome', None)
    detail = str(getattr(result, 'detail', '') or '')
    if outcome == 'invalid_or_unsupported':
        return (
            'invalid_or_unsupported',
            WatchFailureClass.UNSUPPORTED,
            detail or '未対応または不正なファイルです',
        )
    if outcome == 'user_action_required':
        return (
            'user_action_required',
            WatchFailureClass.USER_ACTION_REQUIRED,
            detail or 'このファイルには操作者の対応が必要です',
        )
    return (
        'import_failed',
        WatchFailureClass.RETRYABLE,
        detail or 'キャプチャの取り込みに失敗しました',
    )


def _norm_root(value: str) -> str:
    """Case/segment-insensitive watch-root key for epoch comparison."""

    return os.path.normcase(os.path.normpath(value)) if value else ''


def verify_watch_retry(
    entry: CaptureWatchFailure,
    *,
    watch_root_now: str | None,
) -> tuple[WatchRetryVerdict, str]:
    """The 安全に再試行 gate — same file, same fingerprint, same epoch.

    Order matters for honesty: an unsupported kind is never retryable no
    matter the file state; a missing file is ``deleted``; a changed
    ``(mtime_ns, size)`` means replaced or still being written — both are
    a different drop than the one that failed, so the safe path refuses
    it (このファイルを選んで取込 remains the explicit operator route);
    and a watch preference pointing anywhere but the recorded root means
    the failure belongs to a watch epoch that no longer exists.
    """

    if entry.failure_class in (
        WatchFailureClass.UNSUPPORTED,
        WatchFailureClass.USER_ACTION_REQUIRED,
    ):
        return (
            WatchRetryVerdict.NOT_RETRYABLE,
            'この種類の失敗は再試行では解決しません — '
            'ファイルを修正するか「このファイルを選んで取込」を使ってください',
        )
    path = Path(entry.path)
    try:
        stat = path.stat()
    except OSError:
        return (
            WatchRetryVerdict.DELETED,
            'ファイルが見つかりません（削除または移動されました）',
        )
    if not path.is_file():
        return (
            WatchRetryVerdict.DELETED,
            'ファイルが見つかりません（削除または移動されました）',
        )
    if entry.mtime_ns is not None and entry.size is not None and (
        stat.st_mtime_ns,
        stat.st_size,
    ) != (entry.mtime_ns, entry.size):
        return (
            WatchRetryVerdict.REPLACED,
            '記録時と内容が変わっています（書き込み中か別ファイルへ'
            '置き換えられました）',
        )
    if not watch_root_now or _norm_root(watch_root_now) != _norm_root(
        entry.watch_root
    ):
        return (
            WatchRetryVerdict.EPOCH_MISMATCH,
            '監視フォルダーの設定が記録時と異なります — '
            '設定を戻すか「このファイルを選んで取込」を使ってください',
        )
    return (WatchRetryVerdict.OK, '')


def _correlation_id(path: str) -> str:
    """Stable [diag: XXXX]-format id for one queued drop.

    Same digest convention as ``support_diagnostics.failure_correlation_id``
    without pulling the diagnostics package into the runner's import
    graph.
    """

    return sha256(path.encode('utf-8')).hexdigest()[:4].upper()


def _parse_seen(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class CaptureWatchFailureQueue:
    """Bounded, atomically persisted store of exhausted watch drops.

    JSON under ``data_dir`` — one ``record`` upserts by exact path (a
    rewritten drop refreshes fingerprint/attempts under the same entry),
    ``resolve`` removes it once a route finally stages, and stale rows
    fall off by TTL and by the size cap. A corrupt or unreadable store
    loads empty and is rewritten on the next save — the queue is a
    recovery surface, never an authority, so a damaged file must not
    fail the launch.
    """

    def __init__(
        self,
        path: Path,
        *,
        limit: int = WATCH_FAILURE_QUEUE_LIMIT,
        ttl_days: int = WATCH_FAILURE_TTL_DAYS,
    ) -> None:
        self._path = Path(path)
        self._limit = max(1, int(limit))
        self._ttl = timedelta(days=max(0, int(ttl_days)))
        self._entries: dict[str, CaptureWatchFailure] = {}
        self._load()

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> 'CaptureWatchFailureQueue':
        return cls(Path(data_dir) / WATCH_FAILURE_QUEUE_FILENAME)

    @property
    def store_path(self) -> Path:
        return self._path

    def entries(self) -> tuple[CaptureWatchFailure, ...]:
        """Newest failure first."""

        return tuple(
            sorted(
                self._entries.values(),
                key=lambda entry: entry.last_seen_utc,
                reverse=True,
            )
        )

    def get(self, path: str | Path) -> CaptureWatchFailure | None:
        return self._entries.get(str(path))

    def record(
        self,
        *,
        path: str | Path,
        error_kind: str,
        failure_class: WatchFailureClass | str,
        detail: str,
        attempts: int,
        mtime_ns: int | None,
        size: int | None,
        watch_root: str,
        seen_utc: str | None = None,
    ) -> CaptureWatchFailure:
        """Upsert one exhausted drop; preserves first_seen + the stable
        diagnostic id of any existing entry for the same path."""

        now = seen_utc or _utc_now()
        key = str(path)
        existing = self._entries.get(key)
        entry = CaptureWatchFailure(
            path=key,
            basename=Path(key).name,
            error_kind=error_kind,
            failure_class=WatchFailureClass(failure_class),
            detail=detail,
            attempts=max(int(attempts), existing.attempts if existing else 0),
            first_seen_utc=existing.first_seen_utc if existing else now,
            last_seen_utc=now,
            mtime_ns=mtime_ns,
            size=size,
            watch_root=str(watch_root),
            diagnostic_id=(
                existing.diagnostic_id if existing else _correlation_id(key)
            ),
        )
        self._entries[key] = entry
        self._prune()
        self._save()
        return entry

    def record_retry_attempt(
        self, path: str | Path, *, seen_utc: str | None = None
    ) -> CaptureWatchFailure | None:
        """A manual reprocess attempt failed again — bump the count."""

        entry = self._entries.get(str(path))
        if entry is None:
            return None
        entry = entry.model_copy(
            update={
                'attempts': entry.attempts + 1,
                'last_seen_utc': seen_utc or _utc_now(),
            }
        )
        self._entries[entry.path] = entry
        self._save()
        return entry

    def resolve(self, path: str | Path) -> bool:
        """Drop the entry once the file finally stages (any lane)."""

        if self._entries.pop(str(path), None) is None:
            return False
        self._save()
        return True

    # -- persistence -----------------------------------------------------

    def _load(self) -> None:
        try:
            raw = self._path.read_text(encoding='utf-8')
        except OSError:
            return
        try:
            payload = json.loads(raw)
        except ValueError:
            _LOGGER.warning(
                'capture watch failure queue is corrupt — starting empty'
            )
            return
        entries = payload.get('entries') if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return
        for item in entries:
            try:
                entry = CaptureWatchFailure.model_validate(item)
            except ValueError:
                continue
            self._entries[entry.path] = entry
        self._prune()

    def _save(self) -> None:
        payload = {
            'schema_version': WATCH_FAILURE_SCHEMA_VERSION,
            'entries': [
                entry.model_dump(mode='json')
                for entry in self.entries()
            ],
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f'.{self._path.name}.',
                suffix='.tmp',
                dir=self._path.parent,
            )
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
                    json.dump(payload, file, ensure_ascii=False, indent=1)
                os.replace(temp_name, self._path)
            except BaseException:  # error-boundary: temp-file cleanup — the staging temp must be unlinked on ANY failure (including KeyboardInterrupt) before the original error re-raises (noqa: BLE001)
                Path(temp_name).unlink(missing_ok=True)
                raise
        except OSError:
            # The queue is app-local recovery state — a disk hiccup must
            # not crash the lane; the in-memory entries still serve this
            # session and the next mutation retries the write.
            _LOGGER.warning(
                'capture watch failure queue could not be persisted',
                exc_info=True,
            )

    def _prune(self, *, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        cutoff = now - self._ttl
        for key in [
            key
            for key, entry in self._entries.items()
            if (_parse_seen(entry.last_seen_utc) or now) < cutoff
        ]:
            del self._entries[key]
        if len(self._entries) > self._limit:
            ordered = sorted(
                self._entries.values(),
                key=lambda entry: entry.last_seen_utc,
            )
            for entry in ordered[: len(self._entries) - self._limit]:
                del self._entries[entry.path]


def write_watch_failure_diagnostic(
    data_dir: Path,
    entry: CaptureWatchFailure,
) -> Path:
    """「サポート診断」— a bounded, path-free note next to the app log.

    The note deliberately carries the basename, the watch root's basename
    and the fingerprint only: the full path stays in the app-local queue
    store and the raw bundle never enters diagnostics at all.
    """

    root = diagnostics_dir(Path(data_dir))
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        'schema_version': WATCH_FAILURE_SCHEMA_VERSION,
        'kind': 'capture-watch-failure',
        'diagnostic_id': entry.diagnostic_id,
        'basename': entry.basename,
        'error_kind': entry.error_kind,
        'failure_class': str(entry.failure_class),
        'detail': entry.detail,
        'attempts': entry.attempts,
        'first_seen_utc': entry.first_seen_utc,
        'last_seen_utc': entry.last_seen_utc,
        'file_signature': {
            'mtime_ns': entry.mtime_ns,
            'size': entry.size,
        },
        'watch_root_basename': (
            Path(entry.watch_root).name if entry.watch_root else None
        ),
    }
    destination = root / f'capture-watch-failure-{entry.diagnostic_id}.json'
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f'.{destination.name}.', suffix='.tmp', dir=root
    )
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
            json.dump(payload, file, ensure_ascii=False, indent=1)
        os.replace(temp_name, destination)
    except BaseException:  # error-boundary: temp-file cleanup — the diagnostic temp must be unlinked on ANY failure (including KeyboardInterrupt) before the original error re-raises (noqa: BLE001)
        Path(temp_name).unlink(missing_ok=True)
        raise
    return destination


__all__ = [
    'CaptureWatchFailure',
    'CaptureWatchFailureQueue',
    'WATCH_FAILURE_QUEUE_FILENAME',
    'WATCH_FAILURE_QUEUE_LIMIT',
    'WATCH_QUEUE_ARRIVAL_SOURCE',
    'WatchFailureClass',
    'WatchRetryVerdict',
    'WatchRouteExhaustion',
    'WatchSkippedEntry',
    'classify_route_failure',
    'classify_stage_failure',
    'describe_watch_skip',
    'verify_watch_retry',
    'write_watch_failure_diagnostic',
]
