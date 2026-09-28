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
    ('PreferenceError', 'preferences.error', '環境設定を適用できませんでした', None),
    ('IngressTooLargeError', 'ingress.too_large', 'ファイルが大きすぎます',
     'より小さいファイルを選択してください'),
    # RewApiUnavailable must precede its RewApiError base: pattern lookup is
    # MRO-membership based, so the more specific entry must come first.
    ('RewApiUnavailable', 'rew.unavailable', 'REWに接続できませんでした',
     'REWが起動していてAPIが有効か確認してください'),
    ('RewApiError', 'rew.api', 'REWデータを取得できませんでした', None),
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
) -> UserFacingError:
    """Present one operation failure as a warning dialog.

    Visible text is the mapped, localized message plus its recovery hint and
    optional effect line — never raw exception text. The exception's class
    and message are preserved under the dialog's Details expander and in the
    diagnostics log.
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
    box.exec()
    return error


__all__ = [
    'UserFacingError',
    'log_operation_error',
    'operation_error_message',
    'to_user_facing_error',
    'warn_user',
]
