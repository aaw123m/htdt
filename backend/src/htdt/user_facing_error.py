"""User-facing error contract (#903).

One structured presentation model for operation failures: user surfaces
(notice banners, status lines, warning dialogs) show an actionable,
localized operator message — never raw backend exception text — while the
exception's technical detail stays preserved for diagnostics.

Two mappings live here:

* a stable presentation *code* per failure class so the localization layer
  (#624) can cover messages without parsing English exception strings;
* a typed exception → (code, message, recovery) map built on exception
  *types/class names*, never on message content.

Exceptions whose ``str()`` is already a user-facing localized message keep
their message: the documented contract types (``LayoutError``,
``SeatingLayoutError``) plus any exception whose text is a single, short
line containing Japanese kana/kanji (``_looks_localized``) — domain code
frequently raises bare ``ValueError('日本語…')`` as operator-facing
rejections. Every other type maps to a safe generic message and keeps the
raw text only in ``technical_detail``.
"""

from __future__ import annotations

import errno as _errno
import logging
import re
import sqlite3 as _sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError

from .ui_theme import SemanticState

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

_LOG = logging.getLogger('htdt.errors')

#: Hiragana/katakana/CJK — presence means a message was authored as operator
#: text for the Japanese UI rather than as a technical exception detail.
_LOCALIZED_RE = re.compile(r'[ぁ-んァ-ヶ一-龥ー]')


def _looks_localized(text: str) -> bool:
    """Heuristic companion to ``_PRESERVE_MESSAGE_TYPES``.

    Much of the domain code raises bare ``ValueError('日本語…')`` for
    operator-facing rejections, so the type list cannot cover them. A
    single-line message containing Japanese kana/kanji is treated as
    already-localized operator text; multi-line dumps and long payloads
    (pydantic errors embedding JP field names) still map to safe generics.
    """
    return (
        bool(_LOCALIZED_RE.search(text))
        and '\n' not in text
        and len(text) <= 160
    )


#: Exceptions whose str() is already a localized operator-facing message by
#: contract (their raises are written as Japanese UI text). Mapping by class
#: *name* keeps this module decoupled from every UI/domain module that could
#: import it back; the message *content* is never inspected.
_PRESERVE_MESSAGE_TYPES: frozenset[str] = frozenset({
    'LayoutError',
    'SeatingLayoutError',
})

#: Ordered type-name → (code, message, recovery) map for the domain
#: exception families that benefit from an actionable, stable message. Class
#: names are matched on the leaf suffix so every ``*ConflictError`` /
#: ``*StaleHeadError`` / ``*IntegrityError`` / ``*RefError`` repository
#: exception is covered without importing dozens of repository modules.
_NAME_PATTERNS: tuple[tuple[str, str, str, str | None], ...] = (
    ('RewParseError', 'import.rew', 'REWファイルを解析できませんでした',
     '対応するREWエクスポート形式か確認してください'),
    ('RewIrParseError', 'import.rew_ir', 'REWインパルス応答を解析できませんでした',
     '対応するREWエクスポート形式か確認してください'),
    ('RewApiNotFound', 'rew.not_found',
     'REWで指定した項目が見つかりません',
     'REWの計測一覧を再読み込みしてください'),
    ('UnderlayImportError', 'import.underlay', '下図ファイルを読み込めませんでした',
     'PNG/JPEG/PDF/DXF形式か確認してください'),
    ('RawMeshImportError', 'import.mesh', 'メッシュファイルを読み込めませんでした',
     'ファイル形式を確認してください'),
    ('CaptureImportError', 'import.capture', 'インポートファイルを解析できませんでした',
     'ファイル形式と内容を確認してください'),
    ('MeshAssetUnavailableError', 'import.asset',
     '必要なアセットを読み込めませんでした',
     'アセットの配置を確認してください'),
    ('ProjectNotFoundError', 'project.not_found', 'プロジェクトが見つかりません',
     'プロジェクト一覧で対象を確認してください'),
    ('ProjectArchivedError', 'project.archived',
     'このプロジェクトはアーカイブされています',
     'プロジェクトライブラリでアーカイブを解除してください'),
    ('ProjectLibraryError', 'project.library', 'プロジェクトを開けませんでした', None),
    ('LibraryDeleteBlocked', 'library.delete_blocked',
     'このライブラリ項目は使用中のため削除できません', None),
    ('LibraryError', 'library.operation', 'ライブラリ操作を完了できませんでした', None),
    ('SceneValidationError', 'scene.validation', 'シーンデータを検証できませんでした', None),
    ('EditStateError', 'edit.state', '現在の編集状態では完了できませんでした', None),
    ('ManagedDataUnavailableError', 'storage.managed_data',
     '管理データにアクセスできませんでした',
     'データ保存先の設定を確認してください'),
    ('BackupError', 'backup.invalid',
     'バックアップデータを処理できませんでした', None),
    ('PreferenceError', 'preferences.error', '環境設定を適用できませんでした', None),
    ('IngressTooLargeError', 'ingress.too_large', 'ファイルが大きすぎます',
     'より小さいファイルを選択してください'),
    # Launch/data-format family: subclasses of NativeUpgradeError must
    # precede it (pattern lookup is MRO-membership based).
    ('IncompatibleNewerSchemaError', 'schema.too_new',
     'このデータはより新しいHTDT形式で作成・更新されています',
     'データを作成した新しいビルドで開くか、互換性のあるバックアップを復元してください'),
    ('InsufficientUpgradeSpaceError', 'migration.no_space',
     '更新前の復旧コピーを作成する空き容量が不足しています',
     '空き容量を確保してから再起動してください'),
    ('NativeUpgradeQuarantineError', 'migration.quarantine',
     'データ形式更新後の検証が完了していないため、前のデータが保持されています',
     '再起動して検証を再試行してください'),
    ('NativeUpgradeVerificationError', 'migration.verify',
     'データ形式更新後の検証を完了できませんでした',
     '再起動して検証を再試行するか、復旧用コピーから復元してください'),
    ('NativeUpgradeError', 'migration.failed',
     'データ形式を更新できませんでした',
     '再起動して再試行するか、最新のバックアップを復元してください'),
    ('MigrationOpenError', 'migration.open',
     'データ移行を完了できませんでした',
     '最新のバックアップを復元するか、診断ログを確認してください'),
    ('NativeSchemaError', 'schema.error',
     'データベース形式を確認できませんでした', None),
    # RewApiUnavailable must precede its RewApiError base: pattern lookup is
    # MRO-membership based, so the more specific entry must come first.
    ('RewApiUnavailable', 'rew.unavailable', 'REWに接続できませんでした',
     'REWが起動していてAPIが有効か確認してください'),
    ('RewApiError', 'rew.api', 'REWデータを取得できませんでした', None),
)

#: Failure classes where asking again is a reasonable next step — transient
#: transports (REW API), file locks/permissions the user may have just fixed,
#: a store lock held by another process, and optimistic-concurrency
#: rejections whose recovery text already says "retry". Deterministic input
#: problems (parse, validation, not-found) stay non-retryable: re-running
#: them without changes would re-fail identically.
RETRYABLE_ERROR_CODES: frozenset[str] = frozenset(
    {
        'rew.unavailable',
        'rew.api',
        'io.error',
        'io.permission',
        'io.no_space',
        'storage.locked',
        'authority.conflict',
        'authority.stale_head',
    }
)

#: Leaf-suffix → (code, message, recovery): covers the repository exception
#: taxonomy (*ConflictError, *StaleHeadError, *IntegrityError, *RefError) —
#: typed names, not message parsing.
_SUFFIX_PATTERNS: tuple[tuple[str, str, str, str | None], ...] = (
    ('StaleHeadError', 'authority.stale_head',
     '対象のデータが最新ではありません',
     '再読み込みしてからやり直してください'),
    ('ConflictError', 'authority.conflict',
     '競合する変更があるため完了できません',
     '最新の状態を確認してから再試行してください'),
    ('IntegrityError', 'authority.integrity',
     'データの整合性を確認できませんでした', None),
    ('RefError', 'authority.ref', '参照している項目が見つかりません', None),
)


@dataclass(frozen=True)
class UserFacingError:
    """One operation failure as the operator should see it.

    ``technical_detail`` preserves backend diagnostics (exception class and
    text) for the details/diagnostics path; it never becomes the primary
    operator message.
    """

    code: str
    severity: SemanticState
    title: str
    message: str
    effect: str | None = None
    recovery: str | None = None
    technical_detail: str | None = None

    def notice_text(self) -> str:
        """Compact single-line presentation for notice/status surfaces."""
        parts = [self.title, self.message]
        if self.effect:
            parts.append(self.effect)
        if self.recovery:
            parts.append(self.recovery)
        return ' · '.join(part for part in parts if part)


def _map_exception(
    exc: BaseException,
) -> tuple[str, str, str | None]:
    """(code, message, recovery) for ``exc`` — typed/class-name based only."""
    name = type(exc).__name__
    if name in _PRESERVE_MESSAGE_TYPES:
        text = str(exc).strip()
        return (
            'operation.rejected',
            text if text else '操作が拒否されました',
            None,
        )
    mro_names = {c.__name__ for c in type(exc).__mro__}
    for type_name, code, message, recovery in _NAME_PATTERNS:
        if type_name in mro_names:
            return code, message, recovery
    for suffix, code, message, recovery in _SUFFIX_PATTERNS:
        if name.endswith(suffix):
            return code, message, recovery
    if isinstance(exc, ValidationError):
        return (
            'data.validation',
            '入力データを検証できませんでした',
            '必須項目と値の形式を確認してください',
        )
    if isinstance(exc, _sqlite3.Error):
        # sqlite errors are typed by ``sqlite_errorcode`` (an extended result
        # code — mask to the primary byte), never by parsing message text:
        # 'database is locked', 'malformed', and 'disk full' must each reach
        # the operator as their real cause, not a generic failure.
        primary = getattr(exc, 'sqlite_errorcode', 0) & 0xFF
        if primary in (_sqlite3.SQLITE_BUSY, _sqlite3.SQLITE_LOCKED):
            return (
                'storage.locked',
                'データベースが他の処理によって使用されています',
                '他のアプリや処理を終了してから再試行してください',
            )
        if primary == _sqlite3.SQLITE_FULL:
            return (
                'io.no_space',
                'ディスク容量が不足しています',
                '空き容量を確保してから再試行してください',
            )
        if primary in (_sqlite3.SQLITE_CORRUPT, _sqlite3.SQLITE_NOTADB):
            return (
                'storage.corrupt',
                'データベースが破損しています',
                '最新のバックアップを復元してください',
            )
        if primary == _sqlite3.SQLITE_READONLY:
            return (
                'io.permission',
                'データベースが読み取り専用です',
                'データ保存先の権限を確認してください',
            )
        if primary == _sqlite3.SQLITE_IOERR:
            return ('io.error', 'ファイルにアクセスできませんでした', None)
        if primary == _sqlite3.SQLITE_CANTOPEN:
            return (
                'storage.cantopen',
                'データベースを開けませんでした',
                'データ保存先のパスと権限を確認してください',
            )
        return ('storage.error', 'データベース処理に失敗しました', None)
    if isinstance(exc, FileNotFoundError):
        return 'io.not_found', 'ファイルが見つかりません', 'パスを確認してください'
    if isinstance(exc, PermissionError):
        return 'io.permission', 'ファイルへのアクセスが拒否されました', None
    if isinstance(exc, OSError):
        if getattr(exc, 'errno', None) == _errno.ENOSPC:
            return (
                'io.no_space',
                'ディスク容量が不足しています',
                '空き容量を確保してから再試行してください',
            )
        # EADDRINUSE (POSIX 98/48, WinSock 10048): the real culprit is a
        # port conflict, not a file — name it so the receiver settings
        # dialog tells the user to change the port or stop the occupant.
        if getattr(exc, 'errno', None) in (_errno.EADDRINUSE, 10048):
            return (
                'io.address_in_use',
                '指定のアドレス・ポートはすでに使用中です',
                'ポート番号を変更するか、使用中のアプリを終了してください',
            )
        return 'io.error', 'ファイルにアクセスできませんでした', None
    if isinstance(exc, KeyError):
        return 'data.missing', '対象の項目が見つかりません', None
    if isinstance(exc, (ValueError, TypeError)):
        text = str(exc).strip()
        if _looks_localized(text):
            return 'operation.rejected', text, None
        return 'data.invalid', 'データを処理できませんでした', None
    text = str(exc).strip()
    if _looks_localized(text):
        return 'operation.rejected', text, None
    return 'operation.failed', '操作を完了できませんでした', None


def to_user_facing_error(
    exc: BaseException,
    *,
    title: str,
    effect: str | None = None,
    severity: SemanticState = SemanticState.ERROR,
) -> UserFacingError:
    """Translate any operation exception into its presentation model.

    ``title`` is the caller-localized action that failed (e.g.
    「読み込みに失敗しました」); ``effect`` states the mutation outcome when
    it materially helps (e.g. 「変更は保存されていません」 for an atomic
    rejected commit, or a partial-commit statement for multi-step ops).
    """
    code, message, recovery = _map_exception(exc)
    return UserFacingError(
        code=code,
        severity=severity,
        title=title,
        message=message,
        effect=effect,
        recovery=recovery,
        technical_detail=f'{type(exc).__name__}: {exc}',
    )


def log_operation_error(
    error: UserFacingError,
    exc: BaseException,
) -> None:
    """Preserve diagnostics: technical detail goes to the log, never lost."""
    _LOG.warning(
        'operation failed [%s] %s — %s',
        error.code,
        error.title,
        error.technical_detail,
        exc_info=exc if _LOG.isEnabledFor(logging.DEBUG) else None,
    )


def operation_error_message(exc: BaseException) -> str:
    """Mapped operator message alone, for embedding into a localized context."""
    return _map_exception(exc)[1]


def warn_user(
    parent: "QWidget | None",
    title: str,
    exc: BaseException,
    *,
    effect: str | None = None,
    on_retry: Callable[[], None] | None = None,
    retry_label: str | None = None,
) -> UserFacingError:
    """Present one operation failure as a warning dialog.

    Visible text is the mapped, localized message plus its recovery hint and
    optional effect line — never raw exception text. The exception's class
    and message are preserved under the dialog's Details expander and in the
    diagnostics log.

    ``on_retry`` adds a retry button only when the failure class is actually
    retryable (``RETRYABLE_ERROR_CODES``) — the dialog never offers a second
    attempt it knows cannot succeed differently. The callback runs after the
    dialog closes; its own failure surfaces through the same error channel.
    """
    from PySide6.QtWidgets import QMessageBox

    error = to_user_facing_error(exc, title=title, effect=effect)
    log_operation_error(error, exc)
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    text = error.message
    if error.recovery:
        text += f'\n{error.recovery}'
    if error.effect:
        text += f'\n{error.effect}'
    box.setText(text)
    if error.technical_detail:
        box.setDetailedText(error.technical_detail)
    retry_button = None
    if on_retry is not None and error.code in RETRYABLE_ERROR_CODES:
        box.addButton(QMessageBox.StandardButton.Ok)
        retry_button = box.addButton(
            retry_label or '再試行', QMessageBox.ButtonRole.ApplyRole
        )
        box.setDefaultButton(QMessageBox.StandardButton.Ok)
    box.exec()
    if retry_button is not None and box.clickedButton() is retry_button:
        on_retry()
    return error


__all__ = [
    'RETRYABLE_ERROR_CODES',
    'UserFacingError',
    'log_operation_error',
    'operation_error_message',
    'to_user_facing_error',
    'warn_user',
]
